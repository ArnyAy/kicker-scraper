import os, re, sqlite3, logging, hashlib, time
import requests
from datetime import date
from bs4 import BeautifulSoup
from clubs import CLUBS, friendlies_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Referer": "https://www.transfermarkt.de/",
}
DB_PATH = "matches.db"
S = requests.Session()
S.headers.update(HEADERS)

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS matches (
        hash TEXT PRIMARY KEY, date TEXT, time TEXT, home TEXT, away TEXT,
        venue TEXT, source TEXT, url TEXT, sent INTEGER DEFAULT 0)""")
    con.commit()
    return con

def match_hash(m):
    return hashlib.sha1(f"{m['date']}|{m['home']}|{m['away']}".lower().encode()).hexdigest()

def scrape_club(club, today):
    url = friendlies_url(club["id"])
    r = S.get(url, timeout=20)
    diag = {"status": r.status_code, "bytes": len(r.text), "final": r.url}
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    rows = soup.select("table.items tbody tr")
    diag["tables"] = len(soup.select("table.items"))
    diag["rows"] = len(rows)
    diag["sample"] = rows[0].prettify()[:1500] if rows else r.text[:600]

    out = []
    for row in rows:
        txt = row.get_text(" ", strip=True)
        dm = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", txt)
        if not dm:
            continue
        d = date(int(dm[3]), int(dm[2]), int(dm[1]))
        if d < today:
            continue
        links = [a.get_text(strip=True) for a in row.find_all("a") if a.get_text(strip=True)]
        opp = next((x for x in links if x and x != club["name"]), "")
        if not opp:
            continue
        tm = re.search(r"\b(\d{1,2}:\d{2})\b", txt)
        out.append({
            "date": d.isoformat(),
            "time": tm[1] if tm else "",
            "home": club["name"],
            "away": opp,
            "venue": "",
            "source": club["name"],
            "url": url,
        })
    return out, diag

def scrape_all():
    today = date.today()
    seen, out = set(), []
    for i, club in enumerate(CLUBS):
        try:
            ms, diag = scrape_club(club, today)
            logging.info("DIAG %s: HTTP %s | %d bytes | tables=%s rows=%s | %s",
                         club["name"], diag["status"], diag["bytes"],
                         diag["tables"], diag["rows"], diag["final"])
            if i < 2:
                logging.info("SAMPLE[%s]:\n%s", club["name"], diag["sample"])
            for m in ms:
                h = match_hash(m)
                if h not in seen:
                    seen.add(h); m["hash"] = h; out.append(m)
        except Exception as e:
            logging.error("ERR %s: %s", club["name"], e)
        time.sleep(0.5)
    return out

def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        logging.error("No TELEGRAM_TOKEN/CHAT_ID"); return
    for i in range(0, len(text), 4000):
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text[i:i+4000],
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=15)

def format_message(ms):
    if not ms:
        return "⚽ <b>2. Bundesliga Testspiele</b>\n\nНет новых матчей."
    lines = ["⚽ <b>2. Bundesliga: новые товарищеские матчи</b>\n"]
    for m in sorted(ms, key=lambda x: x["date"]):
        t = f" {m['time']}" if m["time"] else ""
        lines.append(f"📅 {m['date']}{t}\n<b>{m['home']}</b> — <b>{m['away']}</b>\n"
                     f"🔗 <a href=\"{m['url']}\">{m['source']}</a>\n")
    return "\n".join(lines)

if __name__ == "__main__":
    con = init_db()
    matches = scrape_all()
    logging.info("Найдено матчей: %d", len(matches))
    new = []
    for m in matches:
        row = con.execute("SELECT sent FROM matches WHERE hash=?", (m["hash"],)).fetchone()
        if not row:
            con.execute("INSERT INTO matches VALUES (?,?,?,?,?,?,?,0)",
                        (m["hash"], m["date"], m["time"], m["home"], m["away"],
                         m["venue"], m["source"], m["url"]))
            new.append(m)
        elif row[0] == 0:
            new.append(m)
    con.commit()
    if new:
        send_telegram(format_message(new))
        for m in new:
            con.execute("UPDATE matches SET sent=1 WHERE hash=?", (m["hash"],))
        con.commit()
        logging.info("Отправлено: %d", len(new))
    else:
        logging.info("Новых матчей нет")
    con.close()
