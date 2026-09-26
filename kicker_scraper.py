import os, re, sqlite3, logging, hashlib, html
import requests
from datetime import date, datetime
from bs4 import BeautifulSoup
from clubs import CLUBS, schedule_url, END_DATE

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

MONTHS_DE = {
    "01": "jan", "02": "feb", "03": "mar", "04": "apr",
    "05": "may", "06": "jun", "07": "jul", "08": "aug",
    "09": "sep", "10": "oct", "11": "nov", "12": "dec"
}

# ---------- DB ----------
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS matches (
        hash TEXT PRIMARY KEY, date TEXT, time TEXT, home TEXT, away TEXT,
        venue TEXT, source TEXT, url TEXT, sent INTEGER DEFAULT 0)""")
    con.commit()
    return con

def match_hash(m):
    return hashlib.sha1(f"{m['date']}|{m['home']}|{m['away']}".lower().encode()).hexdigest()

# ---------- Parser ----------
def parse_friendly_date(day_str: str, month_str: str, today: date) -> date:
    """Парсит дату вида Di 29.09. в объект date.
    Если месяц меньше текущего — добавляем год."""
    month = int(month_str)
    day = int(day_str)
    year = today.year
    if month < today.month:
        year += 1
    return date(year, month, day)

def scrape_club(club: dict, today: date) -> list[dict]:
    url = schedule_url(club)
    r = S.get(url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    
    matches = []
    
    # Ищем заголовки "Freundschaft Vereine"
    for heading in soup.find_all(string=re.compile(r"Freundschaft\s+Vereine", re.I)):
        container = heading.find_parent()
        if not container:
            continue
        
        # Находим следующий блок с матчами (обычно <div> или <table>)
        next_sib = container.find_next_sibling()
        while next_sib:
            # Проверяем, есть ли тут матчи
            text = next_sib.get_text(" ", strip=True)
            
            # Ищем паттерн: "День ДД.ММ. ЧЧ:ММ"
            date_match = re.search(r'\b(Mo|Di|Mi|Do|Fr|Sa|So)\s+(\d{2})\.(\d{2})\.\s+(\d{1,2}:\d{2})', text)
            if date_match:
                day_str, month_str = date_match.group(2), date_match.group(3)
                time_str = date_match.group(4)
                match_date = parse_friendly_date(day_str, month_str, today)
                
                # Пропускаем прошедшие
                if match_date < today:
                    break
                
                # Пропускаем после END_DATE
                if match_date > END_DATE:
                    break
                
                # Извлекаем команды из блока
                lines = [l.strip() for l in next_sib.get_text("\n").split("\n") if l.strip()]
                # Обычно: [Команда1, счёт1, Команда2, счёт2, ...]
                teams = [l for l in lines if l and not re.match(r'^\d+$', l) and l not in ["Ende", "Live"]]
                
                if len(teams) >= 2:
                    home = teams[0]
                    away = teams[1]
                    
                    matches.append({
                        "date": match_date.isoformat(),
                        "time": time_str,
                        "home": home,
                        "away": away,
                        "venue": "",
                        "source": club["name"],
                        "url": url,
                    })
                    break  # Берём только один матч на блок
            
            next_sib = next_sib.find_next_sibling()
    
    return matches

def scrape_all() -> list[dict]:
    today = date.today()
    seen, out = set(), []
    
    for club in CLUBS:
        try:
            ms = scrape_club(club, today)
            logging.info(f"✓ {club['name']}: {len(ms)} Testspiele")
            for m in ms:
                h = match_hash(m)
                if h not in seen:
                    seen.add(h)
                    m["hash"] = h
                    out.append(m)
        except Exception as e:
            logging.error(f"✗ {club['name']}: {e}")
    
    return out

# ---------- Telegram ----------
def send_telegram(text: str):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        logging.error("No TELEGRAM_TOKEN/CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        try:
            r = requests.post(url, json={
                "chat_id": chat, "text": chunk,
                "parse_mode": "HTML", "disable_web_page_preview": True
            }, timeout=15)
            logging.info(f"Telegram: HTTP {r.status_code}")
        except Exception as e:
            logging.error(f"Telegram error: {e}")

def format_msg(matches: list[dict]) -> str:
    if not matches:
        return "⚽ <b>2. Bundesliga Testspiele</b>\n\nНа ближайшие даты (до 31.12.2026) новых товарищеских матчей нет."
    
    lines = [f"⚽ <b>2. Bundesliga: найдено {len(matches)} Testspiele</b>\n"]
    for m in sorted(matches, key=lambda x: x["date"]):
        lines.append(
            f"📅 <b>{m['date']}</b> {m['time']}\n"
            f"<b>{html.escape(m['home'])}</b> — <b>{html.escape(m['away'])}</b>\n"
            f"🔗 <a href=\"{html.escape(m['url'], quote=True)}\">{m['source']}</a>\n"
        )
    return "\n".join(lines)

# ---------- Main ----------
if __name__ == "__main__":
    con = init_db()
    
    logging.info("Запуск парсинга weltfussball.de (18 клубов)...")
    matches = scrape_all()
    logging.info(f"Всего найдено: {len(matches)}")
    
    # Фильтр новых
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
        msg = format_msg(new)
        send_telegram(msg)
        for m in new:
            con.execute("UPDATE matches SET sent=1 WHERE hash=?", (m["hash"],))
        con.commit()
        logging.info(f"Отправлено в Telegram: {len(new)}")
    else:
        logging.info("Новых матчей нет")
        send_telegram(format_msg([]))
    
    con.close()
