import os
import re
import sqlite3
import logging
import requests
from datetime import date, datetime
from bs4 import BeautifulSoup
from clubs import CLUBS, friendlies_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
}
DB_PATH = "matches.db"

# ---------- DB ----------
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            hash TEXT PRIMARY KEY,
            date TEXT, time TEXT, home TEXT, away TEXT,
            venue TEXT, source TEXT, url TEXT, sent INTEGER DEFAULT 0
        )
    """)
    con.commit()
    return con

def match_hash(m: dict) -> str:
    raw = f"{m['date']}|{m['home']}|{m['away']}".lower()
    import hashlib
    return hashlib.sha1(raw.encode()).hexdigest()

# ---------- Parser ----------
def parse_tm_date(s: str) -> date | None:
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    if not m: return None
    try:
        return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None

def scrape_club(club: dict, today: date) -> list[dict]:
    url = friendlies_url(club["id"])
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    out = []

    # Transfermarkt: таблица тестшпилей
    for row in soup.select("table.items tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 5: continue
        try:
            d = parse_tm_date(cells[0].get_text(" ", strip=True))
            if not d or d < today: continue
            
            home_a = cells[2].find("a")
            away_a = cells[4].find("a")
            home = home_a.get_text(strip=True) if home_a else cells[2].get_text(strip=True)
            away = away_a.get_text(strip=True) if away_a else cells[4].get_text(strip=True)
            
            # Время (если есть)
            time_cell = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            time_str = time_cell if re.match(r"\d{1,2}:\d{2}", time_cell) else ""
            
            out.append({
                "date": d.isoformat(),
                "time": time_str,
                "home": home,
                "away": away,
                "venue": "",
                "source": club["name"],
                "url": url,
            })
        except Exception as e:
            logging.debug(f"skip row {club['name']}: {e}")
    return out

def scrape_all() -> list[dict]:
    today = date.today()
    seen, all_matches = set(), []
    for club in CLUBS:
        try:
            for m in scrape_club(club, today):
                h = match_hash(m)
                if h not in seen:
                    seen.add(h)
                    m["hash"] = h
                    all_matches.append(m)
            logging.info(f"✓ {club['name']}")
        except Exception as e:
            logging.error(f"✗ {club['name']}: {e}")
    return all_matches

# ---------- Telegram ----------
def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logging.error("No TELEGRAM_TOKEN/CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Режем по 4000 символов
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        requests.post(url, json={
            "chat_id": chat_id, "text": chunk,
            "parse_mode": "HTML", "disable_web_page_preview": True
        }, timeout=15)

def format_message(matches: list[dict]) -> str:
    if not matches:
        return "⚽ <b>2. Bundesliga Testspiele</b>\n\nНет новых матчей."
    
    lines = ["⚽ <b>2. Bundesliga: новые товарищеские матчи</b>\n"]
    for m in sorted(matches, key=lambda x: x["date"]):
        t = f" {m['time']}" if m["time"] else ""
        lines.append(
            f"📅 {m['date']}{t}\n"
            f"<b>{m['home']}</b> — <b>{m['away']}</b>\n"
            f"🔗 <a href=\"{m['url']}\">{m['source']}</a>\n"
        )
    return "\n".join(lines)

# ---------- Main ----------
if __name__ == "__main__":
    con = init_db()
    matches = scrape_all()
    logging.info(f"Найдено матчей: {len(matches)}")
    
    new_matches = []
    for m in matches:
        cur = con.execute("SELECT sent FROM matches WHERE hash=?", (m["hash"],))
        row = cur.fetchone()
        if not row:
            con.execute(
                "INSERT INTO matches VALUES (?,?,?,?,?,?,?,0)",
                (m["hash"], m["date"], m["time"], m["home"], m["away"],
                 m["venue"], m["source"], m["url"])
            )
            new_matches.append(m)
        elif row[0] == 0:
            new_matches.append(m)
    
    con.commit()
    
    if new_matches:
        msg = format_message(new_matches)
        send_telegram(msg)
        for m in new_matches:
            con.execute("UPDATE matches SET sent=1 WHERE hash=?", (m["hash"],))
        con.commit()
        logging.info(f"Отправлено: {len(new_matches)}")
    else:
        logging.info("Новых матчей нет")
    
    con.close()
