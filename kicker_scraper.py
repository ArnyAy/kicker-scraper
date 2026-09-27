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
S = requests.Session()
S.headers.update(HEADERS)

OUR_KEYS = {norm_key(c["name"]) for c in CLUBS}
KICKER_ENABLED = True

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

# ---------- Источник 1: weltfussball.de ----------
LABEL_RE = re.compile(
    r"Fu[ßs]ball\s+Freundschaft\s+Vereine\s+Kalenderwoche\s+(.+?)\s+-\s+(.+?)\s+am\s+"
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
        out.append({
            "date": d.isoformat(),
            "time": m.group(6),
            "home": home,
            "away": away,
            "venue": "",
            "source": club["name"],
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
            "home": home,
            "away": away,
            "venue": "",
            "source": "kicker.de",
            "url": url,
        })
    return out

# ---------- Источник 3: KIMI (Moonshot) + веб-поиск ----------
KIMI_BASE = os.environ.get("MOONSHOT_BASE", "https://api.moonshot.ai/v1")
KIMI_MODEL = os.environ.get("KIMI_MODEL", "kimi-k2-0905-preview")

def scrape_kimi(today: date) -> list[dict]:
    key = os.environ.get("MOONSHOT_API_KEY")
    if not key:
        logging.info("KIMI: нет MOONSHOT_API_KEY, пропускаю")
        return []
    clubs = ", ".join(c["name"] for c in CLUBS)
    prompt = (
        f"Today is {today.isoformat()}. Use web search to find ALL confirmed upcoming "
        f"friendly matches (Testspiele / Freundschaftsspiele) of these 2. Bundesliga clubs "
        f"between {today.isoformat()} and {END_DATE.isoformat()}: {clubs}. "
        f"Search kicker.de, official club websites, club social media, local press. "
        f"Rules: first teams only; NO U17/U19/U21/II/women/legends; ONLY matches with a "
        f"public source URL; date format DD.MM.YYYY; time HH:MM or empty string if unknown; "
        f"venue or empty string. Reply ONLY with a JSON array of objects: "
        f'{{"date":"DD.MM.YYYY","time":"HH:MM","home":"...","away":"...","venue":"...","source_url":"https://..."}}. '
        f"No markdown, no comments. If nothing found, reply []."
    )
    body = {
        "model": KIMI_MODEL,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{"type": "builtin_function", "function": {"name": "$web_search"}}],
    }
    try:
        r = requests.post(f"{KIMI_BASE}/chat/completions",
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=(10, 180))
    except Exception as e:
        logging.error("KIMI network: %s", e)
        return []
    if r.status_code != 200:
        logging.error("KIMI HTTP %s: %s", r.status_code, r.text[:300])
        return []
    try:
        content = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logging.error("KIMI parse: %s", e)
        return []
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    try:
        rows = json.loads(content)
    except Exception as e:
        logging.error("KIMI JSON: %s | %s", e, content[:300])
        return []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = (row.get("source_url") or "").strip()
        if not url.startswith("http"):
            continue
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
        tm = re.search(r"\b\d{1,2}:\d{2}\b", row.get("time", ""))
        out.append({
            "date": d.isoformat(),
            "time": tm[0] if tm else "",
            "home": home,
            "away": away,
            "venue": (row.get("venue") or "").strip(),
            "source": "KIMI search",
            "url": url,
        })
    logging.info("KIMI: кандидатов %d", len(out))
    return out

# ---------- Агрегатор ----------
def scrape_all() -> list[dict]:
    today = date.today()
    seen, out = set(), []
    for club in CLUBS:
        ms = []
        try:
            ms = scrape_club(club, today)
        except Exception as e:
            logging.error("WF %s: %s", club["name"], e)
        try:
            ms += scrape_kicker(club["name"], today)
        except Exception as e:
            logging.error("KICKER %s: %s", club["name"], e)
        logging.info("✓ %s: %d Testspiele", club["name"], len(ms))
        for m in ms:
            h = match_hash(m)
            if h not in seen:
                seen.add(h)
                m["hash"] = h
                out.append(m)
    try:
        for m in scrape_kimi(today):
            h = match_hash(m)
            if h not in seen:
                seen.add(h)
                m["hash"] = h
                out.append(m)
    except Exception as e:
        logging.error("KIMI aggregate: %s", e)
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

    logging.info("Парсинг: weltfussball.de + kicker.de + KIMI (18 клубов)...")
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
