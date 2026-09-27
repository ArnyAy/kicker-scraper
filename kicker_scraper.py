import os
import re
import json
import sqlite3
import logging
import hashlib
import html
import requests
from datetime import date
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

OUR_KEYS = {norm_key(c["name"]) for c in CLUBS}
KICKER_ENABLED = True

GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

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

# ---------- Кэш LLM на сутки ----------
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

# ---------- Веб-поиск: Brave (если ключ) -> DuckDuckGo (без ключа) ----------
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
            else:
                logging.warning("SEARCH: Brave HTTP %s, fallback DDG", r.status_code)
        except Exception as e:
            logging.warning("SEARCH: Brave error %s, fallback DDG", e)
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
            "home": home,
            "away": away,
            "venue": "",
            "source": "weltfussball",
            "url": url,
        })
    return out

# ---------- Источник 2: kicker.de (автоотключение при WAF) ----------
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

# ---------- Источник 3: Gemini + Google Search ----------
def scrape_gemini(today: date, cache: dict) -> list:
    key = os.environ.get("GEMINI_API_KEY")
    ck = cache_key_for_day(today, "gemini")
    if ck in cache:
        logging.info("GEMINI: кэш (%d)", len(cache[ck]))
        return cache[ck]
    if not key:
        logging.info("GEMINI: нет GEMINI_API_KEY")
        return []
    clubs = ", ".join(c["name"] for c in CLUBS)
    prompt = (
        f"Today is {today.isoformat()}. Use Google Search to find ONLY confirmed upcoming "
        f"friendly matches (Testspiele / Freundschaftsspiele) of these 2. Bundesliga clubs "
        f"between {today.isoformat()} and {END_DATE.isoformat()}: {clubs}. "
        f"Priority sources: kicker.de, official club websites, club social media, local press. "
        f"Rules:\n"
        f"1. First teams only. NO U17/U19/U21/II/III/women/legends/reserves/intra-club/mini-club matches.\n"
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
        logging.error("GEMINI network: %s", e)
        return []
    if r.status_code == 429:
        logging.warning("GEMINI: квота 429, пропускаем")
        return []
    if r.status_code != 200:
        logging.error("GEMINI HTTP %s: %s", r.status_code, r.text[:300])
        return []
    try:
        data = r.json()
        content = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
    except Exception as e:
        logging.error("GEMINI parse: %s", e)
        return []
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    try:
        rows = json.loads(content)
    except Exception as e:
        logging.error("GEMINI JSON: %s | %s", e, content[:300])
        return []
    if not isinstance(rows, list):
        return []
    logging.info("GEMINI: кандидатов %d", len(rows))
    cache[ck] = rows
    return rows

# ---------- Источник 4: веб-поиск + DeepSeek ----------
def scrape_deepseek(today: date, cache: dict) -> list:
    key = os.environ.get("DEEPSEEK_API_KEY")
    ck = cache_key_for_day(today, "deepseek")
    if ck in cache:
        logging.info("DEEPSEEK: кэш (%d)", len(cache[ck]))
        return cache[ck]
    if not key:
        logging.info("DEEPSEEK: нет ключа")
        return []
    clubs = ", ".join(c["name"] for c in CLUBS)
    hits = web_search(
        f"2. Bundesliga Testspiele Freundschaftsspiele Oktober November Dezember 2026 "
        f"{clubs}"
    )
    if not hits:
        logging.info("DEEPSEEK: поиск вернул 0")
        return []
    urls_text = "\n".join(f"- {h['title']} | {h['url']} | {h['snippet']}" for h in hits[:8])
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
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    try:
        r = requests.post(DEEPSEEK_URL,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=(15, 120))
    except Exception as e:
        logging.error("DEEPSEEK network: %s", e)
        return []
    if r.status_code != 200:
        logging.error("DEEPSEEK HTTP %s: %s", r.status_code, r.text[:300])
        return []
    try:
        content = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logging.error("DEEPSEEK parse: %s", e)
        return []
    try:
        rows = json.loads(content)
    except Exception:
        rows = []
    if not isinstance(rows, list):
        rows = []
    logging.info("DEEPSEEK: кандидатов %d", len(rows))
    cache[ck] = rows
    return rows

# ---------- Валидация сырых строк от LLM ----------
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

# ---------- Агрегатор с голосованием ----------
def scrape_all() -> list[dict]:
    today = date.today()
    cache = load_cache()

    structural = {}
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
        for m in ms:
            structural[match_hash(m)] = m

    gemini_raw = []
    try:
        gemini_raw = scrape_gemini(today, cache)
    except Exception as e:
        logging.error("GEMINI aggregate: %s", e)
    gemini_valid = validate_llm_rows(gemini_raw, "Gemini")

    deepseek_raw = []
    try:
        deepseek_raw = scrape_deepseek(today, cache)
    except Exception as e:
        logging.error("DEEPSEEK aggregate: %s", e)
    deepseek_valid = validate_llm_rows(deepseek_raw, "DeepSeek")

    save_cache(cache)

    gemini_hashes = {match_hash(m) for m in gemini_valid}
    deepseek_hashes = {match_hash(m) for m in deepseek_valid}
    both_llm = gemini_hashes & deepseek_hashes
    confirmed_ll = {}
    for m in gemini_valid + deepseek_valid:
        h = match_hash(m)
        if h in structural or h in both_llm:
            confirmed_ll.setdefault(h, m)

    out = list(structural.values())
    for h, m in confirmed_ll.items():
        if h not in structural:
            out.append(m)

    logging.info("=== ИТОГО: структурные=%d, LLM после голосования=%d ===",
                 len(structural), len(confirmed_ll))
    for m in sorted(out, key=lambda x: x["date"]):
        logging.info("  %s %s  %s vs %s  [%s]", m["date"], m["time"], m["home"], m["away"], m["source"])
    return out

# ---------- Telegram ----------
def send_telegram(text: str):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        logging.error("No TELEGRAM_TOKEN/CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i in range(0, len(text), 4000):
        chunk = text[i:i + 4000]
        try:
            r = requests.post(url, json={
                "chat_id": chat, "text": chunk,
                "parse_mode": "HTML", "disable_web_page_preview": True
            }, timeout=15)
            logging.info("Telegram: HTTP %s", r.status_code)
        except Exception as e:
            logging.error("Telegram error: %s", e)

def format_msg(matches: list[dict]) -> str:
    lines = [f"⚽ <b>2. Bundesliga: новые Testspiele ({len(matches)})</b>\n"]
    for m in sorted(matches, key=lambda x: x["date"]):
        t = f" {m['time']}" if m["time"] else ""
        v = f"\n🏟 {html.escape(m['venue'])}" if m.get("venue") else ""
        lines.append(
            f"📅 <b>{m['date']}</b>{t}\n"
            f"<b>{html.escape(m['home'])}</b> — <b>{html.escape(m['away'])}</b>{v}\n"
            f"🔗 <a href=\"{html.escape(m['url'], quote=True)}\">{html.escape(m['source'])}</a>\n"
        )
    return "\n".join(lines)

# ---------- Main ----------
if __name__ == "__main__":
    con = init_db()

    logging.info("Парсинг: weltfussball + kicker + Gemini + DeepSeek (18 клубов)...")
    matches = scrape_all()
    logging.info("Всего найдено: %d", len(matches))

    new = []
    for m in matches:
        row = con.execute("SELECT sent FROM matches WHERE hash=?", (m["hash"],)).fetchone()
        if not row:
            con.execute("INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,0)",
                        (m["hash"], m["date"], m["time"], m["home"], m["away"],
                         m["venue"], m["source"], m["url"]))
            new.append(m)
        elif row[0] == 0:
            new.append(m)
    con.commit()

    if new:
        send_telegram(format_msg(new))
        for m in new:
            con.execute("UPDATE matches SET sent=1 WHERE hash=?", (m["hash"],))
        con.commit()
        logging.info("Отправлено в Telegram: %d", len(new))
    else:
        logging.info("Новых матчей нет (сообщение не отправляем)")

    con.close()
