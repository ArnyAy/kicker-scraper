import os
import re
import json
import time
import sqlite3
import logging
import hashlib
import html
import requests
from datetime import date, datetime
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from clubs import CLUBS, schedule_url, END_DATE
from normalize import expand_club, is_first_team_friendly, norm_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
DB_PATH = "matches.db"
CACHE_PATH = "llm_cache.json"
S = requests.Session()
S.headers.update(HEADERS)

BERLIN = ZoneInfo("Europe/Berlin")
OUR_KEYS = {norm_key(c["name"]) for c in CLUBS}
KICKER_ENABLED = True

GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
AF_BASE = "https://v3.football.api-sports.io"

# ---------- DB ----------
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS matches (
        hash TEXT PRIMARY KEY, date TEXT, time TEXT, home TEXT, away TEXT,
        venue TEXT, source TEXT, url TEXT, sent INTEGER DEFAULT 0)""")
    con.commit()
    return con

def match_hash(m: dict) -> str:
    return hashlib.sha1(f"{m['date']}|{m['home']}|{m['away']}".lower().encode()).hexdigest()

def involves_our_club(home: str, away: str) -> bool:
    return norm_key(home) in OUR_KEYS or norm_key(away) in OUR_KEYS

def is_valid_team_name(name: str) -> bool:
    if not name or len(name) > 60:
        return False
    if any(tag in name for tag in ["<", ">", "div", "class=", "href=", "data-", '="']):
        return False
    if name.count(" ") > 6:
        return False
    if re.search(r"\d{2}\.\d{2}\.\d{4}", name):  # хвост "am 29.09.2026"
        return False
    return True

def purge_invalid(con):
    """Удаляет из БД мусорные записи, созданные до введения фильтра."""
    rows = con.execute("SELECT hash, home, away FROM matches").fetchall()
    bad = [h for h, home, away in rows
           if not (is_valid_team_name(home) and is_valid_team_name(away))]
    for h in bad:
        con.execute("DELETE FROM matches WHERE hash=?", (h,))
    if bad:
        con.commit()
        logging.info("PURGE: удалено мусорных записей: %d", len(bad))

def url_relevant(url: str, home: str, away: str) -> bool:
    if not url.startswith("http"):
        return False
    try:
        r = requests.get(url, timeout=12, headers={
            "User-Agent": "Mozilla/5.0 (compatible; FriendlyTracker/1.0)",
            "Accept-Language": "de-DE,de;q=0.9",
        }, allow_redirects=True)
        if r.status_code >= 400:
            return False
        body = r.text.lower()
        return norm_key(home) in norm_key(body) or norm_key(away) in norm_key(body)
    except Exception as e:
        logging.info("URL check fail %s: %s", url, e)
        return False

# ---------- Кэш ----------
def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_cache(cache):
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def cache_key_for_day(today: date, name: str) -> str:
    return f"{name}:{today.isoformat()}"

# ---------- Retry ----------
def api_get(url, params, headers, timeout=25, retries=3):
    delay = 2
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(delay); delay *= 2
            continue
        if r.status_code == 200:
            return r
        if r.status_code == 429:
            wait = int(r.headers.get("x-ratelimit-reset", "0"))
            if wait <= 0:
                try:
                    msg = r.json().get("response", "")
                    m = re.search(r"after\s+(\d+)", str(msg))
                    if m: wait = int(m[1])
                except Exception:
                    pass
            if wait <= 0:
                wait = 60
            logging.warning("API 429: ждём %dс (attempt %d/%d)", wait, attempt + 1, retries)
            if attempt == retries - 1:
                return r
            time.sleep(wait)
            continue
        if r.status_code >= 500 and attempt < retries - 1:
            time.sleep(delay); delay *= 2
            continue
        return r
    return r

# ---------- Веб-поиск ----------
def web_search(query: str) -> list[dict]:
    key = os.environ.get("BRAVE_API_KEY")
    if key:
        try:
            r = requests.get(BRAVE_URL,
                             headers={"Accept": "application/json",
                                      "X-Subscription-Token": key},
                             params={"q": query, "count": 6, "search_lang": "de"},
                             timeout=20)
            if r.status_code == 200:
                data = r.json()
                hits = [{"url": h.get("url", ""), "title": h.get("title", ""),
                         "snippet": h.get("description", "")}
                        for h in data.get("web", {}).get("results", [])]
                if hits:
                    logging.info("SEARCH: Brave, %d hits", len(hits))
                    return hits
        except Exception as e:
            logging.warning("SEARCH: Brave error %s", e)
    try:
        from ddgs import DDGS
        with DDGS() as d:
            res = d.text(query, max_results=6)
        hits = [{"url": r.get("href", ""), "title": r.get("title", ""),
                 "snippet": r.get("body", "")} for r in (res or [])]
        logging.info("SEARCH: DuckDuckGo, %d hits", len(hits))
        return hits
    except Exception as e:
        logging.error("SEARCH: DDG error %s", e)
        return []

# ---------- Источник 1: weltfussball.de ----------
LABEL_RE = re.compile(
    r"Fu[ßs]ball\s+Freundschaft\s+Vereine\s+Kalenderwoche\s+([^<\"\n]+?)\s+-\s+([^<\"\n]+?)\s+am\s+"
    r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}:\d{2})"
)

def scrape_club(club: dict, today: date) -> list[dict]:
    url = schedule_url(club)
    r = S.get(url, timeout=25)
    r.raise_for_status()
    out = []
    for m in LABEL_RE.finditer(r.text):
        home = expand_club(m.group(1))
        away = expand_club(m.group(2))
        if not is_valid_team_name(home) or not is_valid_team_name(away):
            logging.info("WF skip (мусор): %s | %s", home[:40], away[:40])
            continue
        d = date(int(m.group(5)), int(m.group(4)), int(m.group(3)))
        if d < today or d > END_DATE:
            continue
        if not is_first_team_friendly(home, away):
            continue
        if not involves_our_club(home, away):
            continue
        out.append({
            "date": d.isoformat(),
            "time": m.group(6),
            "home": home, "away": away,
            "venue": "", "source": "weltfussball", "url": url,
        })
    return out

# ---------- Источник 2: kicker.de ----------
KICKER_SLUGS = {
    "Hertha BSC": "hertha-bsc", "Hannover 96": "hannover-96",
    "1. FC Kaiserslautern": "1-fc-kaiserslautern", "1. FC Magdeburg": "1-fc-magdeburg",
    "1. FC Nürnberg": "1-fc-nuernberg", "Karlsruher SC": "karlsruher-sc",
    "SV Darmstadt 98": "sv-darmstadt-98", "Dynamo Dresden": "dynamo-dresden",
    "SpVgg Greuther Fürth": "greuther-fuerth", "FC Schalke 04": "fc-schalke-04",
    "SC Paderborn 07": "sc-paderborn-07", "VfL Bochum": "vfl-bochum",
    "Fortuna Düsseldorf": "fortuna-duesseldorf", "Holstein Kiel": "holstein-kiel",
    "Eintracht Braunschweig": "eintracht-braunschweig", "SV 07 Elversberg": "sv-elversberg",
    "Preußen Münster": "preussen-muenster", "SSV Ulm 1846": "ssv-ulm-1846",
}

def scrape_kicker(club_name: str, today: date) -> list[dict]:
    global KICKER_ENABLED
    if not KICKER_ENABLED:
        return []
    slug = KICKER_SLUGS.get(club_name)
    if not slug:
        return []
    url = f"https://www.kicker.de/{slug}/spielplan"
    r = S.get(url, timeout=25)
    logging.info("KICKER DIAG %s: HTTP %s, %d bytes", club_name, r.status_code, len(r.text))
    if r.status_code != 200 or len(r.text) < 20000:
        KICKER_ENABLED = False
        logging.info("KICKER: WAF-заглушка, источник отключён на этот запуск")
        return []
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for row in soup.select("table tbody tr"):
        tds = row.find_all("td")
        if len(tds) < 4 or "Tests" not in tds[2].get_text(" ", strip=True):
            continue
        txt = tds[3].get_text(" ", strip=True)
        dm = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", txt)
        if not dm:
            continue
        d = date(int(dm[3]), int(dm[2]), int(dm[1]))
        if d < today or d > END_DATE:
            continue
        toks = txt.split()
        if len(toks) < 4:
            continue
        home, away = expand_club(toks[1]), expand_club(toks[-2])
        if not is_valid_team_name(home) or not is_valid_team_name(away):
            continue
        if not is_first_team_friendly(home, away):
            continue
        tm = re.search(r"\b\d{1,2}:\d{2}\b", txt)
        out.append({
            "date": d.isoformat(),
            "time": tm[0] if tm else "",
            "home": home, "away": away,
            "venue": "", "source": "kicker.de", "url": url,
        })
    return out

# ---------- Транслитерация ----------
def transliterate(s: str) -> str:
    return (s.replace("ä", "a").replace("ö", "o").replace("ü", "u")
             .replace("Ä", "A").replace("Ö", "O").replace("Ü", "U")
             .replace("ß", "ss"))

def search_variants(name: str) -> list[str]:
    variants = [name]
    prefixes = ["1. FC ", "2. FC ", "FC ", "SV ", "SpVgg ", "SC ", "VfL ", "SSV "]
    low = name.lower()
    for p in prefixes:
        if low.startswith(p.lower()):
            variants.append(name[len(p):].strip())
            break
    trans = transliterate(name)
    if trans != name:
        variants.append(trans)
    if len(variants) >= 2:
        trans_short = transliterate(variants[1])
        if trans_short != variants[1]:
            variants.append(trans_short)
    return list(dict.fromkeys(variants))

# ---------- Источник 3: API-Football ----------
def scrape_apifootball(today: date, cache: dict) -> list[dict]:
    key = os.environ.get("API_FOOTBALL_KEY")
    if not key:
        logging.info("API-FOOTBALL: нет ключа")
        return []
    H = {"x-apisports-key": key}

    teams = cache.get("af_teams") or {}
    missing = [c for c in CLUBS if norm_key(c["name"]) not in teams]
    if missing:
        logging.info("API-FOOTBALL: ищу %d недостающих команд", len(missing))
        for club in missing:
            try:
                found = False
                for v in search_variants(club["name"]):
                    r = api_get(f"{AF_BASE}/teams", params={"search": v}, headers=H)
                    if r.status_code != 200:
                        continue
                    for t in r.json().get("response", []):
                        nm = (t.get("team") or {}).get("name", "")
                        tid = (t.get("team") or {}).get("id")
                        country = (t.get("team") or {}).get("country", "")
                        if nm and tid and country.lower() == "germany":
                            teams[norm_key(club["name"])] = tid
                            found = True
                            logging.info("API-FOOTBALL: найдено %s -> %s (variant: %s)",
                                         club["name"], nm, v)
                            break
                    if found:
                        break
                if not found:
                    logging.warning("API-FOOTBALL: не найдено %s", club["name"])
            except Exception as e:
                logging.error("API-FOOTBALL %s: %s", club["name"], e)
        if teams:
            cache["af_teams"] = teams
            logging.info("API-FOOTBALL: в кэше %d команд", len(teams))

    out = []
    for club in CLUBS:
        tid = teams.get(norm_key(club["name"]))
        if not tid:
            continue
        try:
            r = api_get(f"{AF_BASE}/fixtures",
                        params={"team": tid, "season": 2026,
                                "from": today.isoformat(), "to": END_DATE.isoformat()},
                        headers=H)
            if r.status_code != 200:
                logging.error("API-FOOTBALL fixtures %s HTTP %s", club["name"], r.status_code)
                continue
            for f in r.json().get("response", []):
                league = ((f.get("league") or {}).get("name") or "").lower()
                if "friend" not in league:
                    continue
                fx = f.get("fixture") or {}
                dt_raw = fx.get("date", "")
                if not dt_raw:
                    continue
                try:
                    dt = datetime.fromisoformat(dt_raw.replace("Z", "+00:00")).astimezone(BERLIN)
                except Exception:
                    continue
                if dt.date() < today or dt.date() > END_DATE:
                    continue
                th = (f.get("teams") or {}).get("home") or {}
                ta = (f.get("teams") or {}).get("away") or {}
                home = expand_club(th.get("name", ""))
                away = expand_club(ta.get("name", ""))
                if not is_valid_team_name(home) or not is_valid_team_name(away):
                    continue
                if not is_first_team_friendly(home, away):
                    continue
                if not involves_our_club(home, away):
                    continue
                out.append({
                    "date": dt.date().isoformat(),
                    "time": dt.strftime("%H:%M"),
                    "home": home, "away": away,
                    "venue": ((fx.get("venue") or {}).get("name") or ""),
                    "source": "api-football",
                    "url": "https://www.api-football.com",
                })
        except Exception as e:
            logging.error("API-FOOTBALL %s: %s", club["name"], e)
    logging.info("API-FOOTBALL: %d Testspiele", len(out))
    return out

# ---------- Источник 4: Gemini ----------
def scrape_gemini(today: date, cache: dict):
    ck = cache_key_for_day(today, "gemini")
    if ck in cache:
        logging.info("GEMINI: кэш (%d)", len(cache[ck]))
        return cache[ck]
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        logging.info("GEMINI: нет ключа -> недоступен")
        return None
    clubs = ", ".join(c["name"] for c in CLUBS)
    prompt = (
        f"Today is {today.isoformat()}. Use Google Search to find ONLY confirmed upcoming "
        f"friendly matches (Testspiele / Freundschaftsspiele) of these 2. Bundesliga clubs "
        f"between {today.isoformat()} and {END_DATE.isoformat()}: {clubs}. "
        f"Priority sources: kicker.de, official club websites, club social media, local press. "
        f"Rules:\n"
        f"1. First teams only. NO U17/U19/U21/II/III/women/legends/reserves/intra-club matches.\n"
        f"2. source_url is MANDATORY. Skip any match without a real public URL.\n"
        f"3. Do NOT invent matches. Only include matches you can verify on the cited page.\n"
        f"4. Date format DD.MM.YYYY; time HH:MM or empty string.\n"
        f"Reply ONLY a valid JSON array of objects: "
        f'{{"date":"DD.MM.YYYY","time":"HH:MM","home":"...","away":"...","venue":"...","source_url":"https://..."}}. '
        f"No markdown. If nothing confirmed, reply [].\n"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.1}
    }
    try:
        r = requests.post(GEMINI_URL,
                          headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                          json=body, timeout=(15, 180))
    except Exception as e:
        logging.error("GEMINI network: %s -> недоступен", e)
        return None
    if r.status_code == 429:
        logging.warning("GEMINI: квота 429 -> недоступен сегодня")
        return None
    if r.status_code != 200:
        logging.error("GEMINI HTTP %s: %s -> недоступен", r.status_code, r.text[:200])
        return None
    try:
        data = r.json()
        content = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
    except Exception as e:
        logging.error("GEMINI parse: %s -> недоступен", e)
        return None
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    try:
        rows = json.loads(content)
    except Exception as e:
        logging.error("GEMINI JSON: %s -> недоступен", e)
        return None
    if not isinstance(rows, list):
        rows = []
    logging.info("GEMINI: кандидатов %d", len(rows))
    cache[ck] = rows
    return rows

# ---------- Источник 5: LLM2 ----------
def llm2_config():
    key = os.environ.get("LLM2_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    base = os.environ.get("LLM2_BASE_URL")
    if not base or not base.strip():
        base = "https://openrouter.ai/api/v1"
    model = os.environ.get("LLM2_MODEL")
    if not model or not model.strip():
        model = "google/gemma-3-27b-it:free"
    if key:
        return base, key, model
    dk = os.environ.get("DEEPSEEK_API_KEY")
    if dk:
        return "https://api.deepseek.com/v1", dk, "deepseek-chat"
    return None, None, None

def scrape_llm2(today: date, cache: dict):
    ck = cache_key_for_day(today, "llm2")
    if ck in cache:
        logging.info("LLM2: кэш (%d)", len(cache[ck]))
        return cache[ck]
    base, key, model = llm2_config()
    if not key:
        logging.info("LLM2: нет ключа -> недоступен")
        return None
    logging.info("LLM2: base=%s model=%s", base, model)

    clubs = ", ".join(c["name"] for c in CLUBS)
    hits = web_search(
        f"2. Bundesliga Testspiele Freundschaftsspiele Oktober November Dezember 2026 {clubs}"
    )
    if not hits:
        logging.info("LLM2: поиск вернул 0")
        return []
    urls_text = "\n".join(f"- {h['title']} | {h['url']} | {h['snippet']}" for h in hits[:5])
    prompt = (
        f"Below are web search snippets about upcoming friendly matches (Testspiele) of "
        f"2. Bundesliga clubs between {today.isoformat()} and {END_DATE.isoformat()}. "
        f"Extract ONLY confirmed friendly matches of first teams. "
        f"Skip U17/U19/U21/II/women/legends/mini-clubs. Do NOT invent data.\n\n"
        f"{urls_text}\n\n"
        f"Reply ONLY a JSON array of objects: "
        f'{{"date":"DD.MM.YYYY","time":"HH:MM","home":"...","away":"...","venue":"...","source_url":"https://..."}}. '
        f"No markdown. If nothing confirmed, reply [].\n"
    )
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 2000,
    }
    try:
        r = requests.post(f"{base}/chat/completions",
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=(15, 120))
    except Exception as e:
        logging.error("LLM2 network: %s -> недоступен", e)
        return None
    if r.status_code != 200:
        logging.error("LLM2 HTTP %s: %s -> недоступен", r.status_code, r.text[:200])
        return None
    try:
        content = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logging.error("LLM2 parse: %s -> недоступен", e)
        return None
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    try:
        rows = json.loads(content)
    except Exception:
        rows = []
    if not isinstance(rows, list):
        rows = []
    logging.info("LLM2: кандидатов %d", len(rows))
    cache[ck] = rows
    return rows

# ---------- Валидация ----------
def validate_llm_rows(rows: list, source: str) -> list[dict]:
    today = date.today()
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = (row.get("source_url") or "").strip()
        dm = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", row.get("date", ""))
        if not dm:
            continue
        d = date(int(dm[3]), int(dm[2]), int(dm[1]))
        if d < today or d > END_DATE:
            continue
        home = expand_club(row.get("home", ""))
        away = expand_club(row.get("away", ""))
        if not is_valid_team_name(home) or not is_valid_team_name(away):
            continue
        if not involves_our_club(home, away):
            continue
        if not is_first_team_friendly(home, away):
            continue
        if not url_relevant(url, home, away):
            logging.info("%s skip (URL invalid): %s vs %s %s", source, home, away, url)
            continue
        tm = re.search(r"\b\d{1,2}:\d{2}\b", row.get("time", ""))
        out.append({
            "date": d.isoformat(),
            "time": tm[0] if tm else "",
            "home": home, "away": away,
            "venue": (row.get("venue") or "").strip(),
            "source": source, "url": url,
        })
    return out

# ---------- Агрегатор ----------
def scrape_all() -> list[dict]:
    today = date.today()
    cache = load_cache()

    structural = {}

    def add(ms):
        for m in ms:
            h = match_hash(m)
            m["hash"] = h
            structural.setdefault(h, m)

    try:
        add(scrape_apifootball(today, cache))
    except Exception as e:
        logging.error("AF aggregate: %s", e)

    for club in CLUBS:
        ms = []
        try:
            ms += scrape_club(club, today)
        except Exception as e:
            logging.error("WF %s: %s", club["name"], e)
        try:
            ms += scrape_kicker(club["name"], today)
        except Exception as e:
            logging.error("KICKER %s: %s", club["name"], e)
        logging.info("✓ %s: %d Testspiele", club["name"], len(ms))
        add(ms)

    g = scrape_gemini(today, cache)
    l2 = scrape_llm2(today, cache)
    g_un, l2_un = (g is None), (l2 is None)
    gv = validate_llm_rows(g or [], "Gemini")
    lv = validate_llm_rows(l2 or [], "LLM2")
    save_cache(cache)

    confirmed = {}
    if g_un and l2_un:
        logging.info("Голосование: оба LLM недоступны, только структурные")
    elif g_un:
        logging.info("Голосование: Gemini недоступен, доверяем валидированному LLM2")
        confirmed = {match_hash(m): m for m in lv}
    elif l2_un:
        logging.info("Голосование: LLM2 недоступен, доверяем валидированному Gemini")
        confirmed = {match_hash(m): m for m in gv}
    else:
        both = {match_hash(m) for m in gv} & {match_hash(m) for m in lv}
        logging.info("Голосование: пересечение Gemini∩LLM2 = %d", len(both))
        for m in gv + lv:
            h = match_hash(m)
            if h in both:
                confirmed.setdefault(h, m)

    for h, m in confirmed.items():
        structural.setdefault(h, m)

    out = list(structural.values())
    for m in out:
        m.setdefault("hash", match_hash(m))

    logging.info("=== ИТОГО: всего=%d (структурные+LLM) ===", len(out))
    for m in sorted(out, key=lambda x: x["date"]):
        logging.info("  %s %s  %s vs %s  [%s]", m["date"], m["time"], m["home"], m["away"], m["source"])
    return out

# ---------- Telegram (резка ТОЛЬКО по границам строк) ----------
def send_telegram(text: str):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        logging.error("No TELEGRAM_TOKEN/CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(line) > 3900:          # страховка: одна строка не влезает
            line = line[:3900]
        if cur and len(cur) + len(line) + 1 > 4000:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)

    for chunk in chunks:
        try:
            r = requests.post(url, json={
                "chat_id": chat, "text": chunk,
                "parse_mode": "HTML", "disable_web_page_preview": True
            }, timeout=15)
            if r.status_code == 200:
                logging.info("Telegram: HTTP 200")
            else:
                logging.error("Telegram HTTP %s: %s", r.status_code, r.text[:300])
        except Exception as e:
            logging.error("Telegram error: %s", e)

def fmt_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"

def moved_from(con, h: str, home: str, away: str, d: str):
    row = con.execute(
        "SELECT date FROM matches WHERE home=? AND away=? AND sent=1 AND hash!=? AND date!=? "
        "ORDER BY rowid DESC LIMIT 1",
        (home, away, h, d)).fetchone()
    return row[0] if row else None

def format_digest(rows, new_hashes, con) -> str:
    today = date.today()
    total = len(rows)
    new_count = sum(1 for r in rows if r[0] in new_hashes)
    today_count = sum(1 for r in rows if r[1] == today.isoformat())

    head = f"⚽ <b>2. Bundesliga Testspiele</b>\n"
    head += f"📅 {today.strftime('%d.%m.%Y')} · всего {total}"
    if new_count:
        head += f" · 🆕 {new_count}"
    if today_count:
        head += f" · <b>сегодня {today_count}</b>"
    lines = [head, ""]

    last_date = None
    for h, d, t, home, away, venue, source, url in rows:
        if d != last_date:
            if last_date:
                lines.append("")
            lines.append(f"▫️ <b>{fmt_date(d)}</b>")
            last_date = d

        mark = "🆕 " if h in new_hashes else ""
        old = moved_from(con, h, home, away, d)
        moved = f" <i>(было {fmt_date(old)})</i>" if old else ""
        tt = f"{t} " if t else ""
        v = f" 🏟 {html.escape(venue)}" if venue else ""

        lines.append(
            f"{mark}{tt}<b>{html.escape(home)}</b> — <b>{html.escape(away)}</b>{v}{moved} · "
            f"<a href=\"{html.escape(url, quote=True)}\">{html.escape(source)}</a>"
        )
    return "\n".join(lines)

# ---------- Main ----------
if __name__ == "__main__":
    con = init_db()
    purge_invalid(con)   # чистим старый мусор из БД

    logging.info("Парсинг: api-football + weltfussball + kicker + Gemini + LLM2...")
    matches = scrape_all()
    logging.info("Всего найдено: %d", len(matches))

    new_hashes = set()
    for m in matches:
        row = con.execute("SELECT sent FROM matches WHERE hash=?", (m["hash"],)).fetchone()
        if not row:
            con.execute("INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,0)",
                        (m["hash"], m["date"], m["time"], m["home"], m["away"],
                         m["venue"], m["source"], m["url"]))
            new_hashes.add(m["hash"])
        elif row[0] == 0:
            new_hashes.add(m["hash"])
    con.commit()
    if new_hashes:
        for h in new_hashes:
            con.execute("UPDATE matches SET sent=1 WHERE hash=?", (h,))
        con.commit()

    today = date.today()
    rows = con.execute(
        "SELECT hash, date, time, home, away, venue, source, url FROM matches "
        "WHERE date >= ? ORDER BY date, time",
        (today.isoformat(),)).fetchall()
    logging.info("Дайджест: предстоящих=%d, новых=%d", len(rows), len(new_hashes))

    if rows:
        send_telegram(format_digest(rows, new_hashes, con))
    else:
        send_telegram("⚽ <b>2. Bundesliga Testspiele</b>\n\nПредстоящих матчей нет.")

    con.close()
