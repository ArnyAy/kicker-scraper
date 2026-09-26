import os
import re
import sqlite3
import logging
import hashlib
import html
import requests
from datetime import date
from bs4 import BeautifulSoup
from clubs import CLUBS, schedule_url, END_DATE
from normalize import expand_club, is_first_team_friendly

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
    slug = KICKER_SLUGS.get(club_name)
    if not slug:
        return []
    url = f"https://www.kicker.de/{slug}/spielplan"
    r = S.get(url, timeout=25)
    logging.info("KICKER DIAG %s: HTTP %s, %d bytes", club_name, r.status_code, len(r.text))
    if r.status_code != 200 or len(r.text) < 20000:  # заглушка WAF = мало байт
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
        tm = re.search(r"\b\d{1,2}:\d{2}\b", txt)  # время; счёт "0 : 15" не матчится
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
        lines.append(
            f"📅 <b>{m['date']}</b>{t}\n"
            f"<b>{html.escape(m['home'])}</b> — <b>{html.escape(m['away'])}</b>\n"
            f"🔗 <a href=\"{html.escape(m['url'], quote=True)}\">{html.escape(m['source'])}</a>\n"
        )
    return "\n".join(lines)

# ---------- Main ----------
if __name__ == "__main__":
    con = init_db()

    logging.info("Парсинг: weltfussball.de + kicker.de (18 клубов)...")
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
