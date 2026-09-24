import os
import re
import sys
import logging
import requests
from datetime import date
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"
}

BASE_URL = "https://www.kicker.de/vereine-freundschaftsspiele/spieltag/2026-27"
TIMEOUT_CONFIG = (5, 15)
MAX_WEEKS = 14                  # сколько недель (Spieltag = календарная неделя) просматривать вперёд
END_DATE = date(2026, 12, 31)
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")

# Ключевые слова клубов 2. Bundesliga 2026/27.
# ПРОВЕРЬТЕ список: здесь 17 клубов, 18-й добавьте сами.
TEAM_KEYS = [
    "hertha", "nürnberg", "heidenheim", "wolfsburg", "kaiserslautern",
    "magdeburg", "cottbus", "pauli", "bochum", "hannover", "osnabrück",
    "fürth", "braunschweig", "dresden", "karlsruhe", "darmstadt", "kiel",
]

EXACT_TRANSLATIONS = {
    "unter Ausschluss der Öffentlichkeit": ("Без зрителей (закрытый матч)", "🟢"),
    "Generalprobe": ("Генеральная репетиция", "🟢"),
    "abgesagt": ("ОТМЕНЕН", "🔴"),
    "Abbruch": ("Матч прерван", "🔴"),
    "Platz": ("Поле", "🟢"),
    "Kunstrasenplatz": ("Искусственное поле", "🟢"),
    "Stadion": ("Стадион", "🟢"),
    "Trainingszentrum": ("Тренировочная база", "🟢")
}


class ScrapeError(Exception):
    """Ошибка получения/разбора страницы (не путать с «матчей нет»)."""


def translate_and_flag(text):
    if not text:
        return "Без комментариев", "🟢"

    flag = "🟢"
    translated = text
    matched = False

    for de_term, (ru_term, status_flag) in EXACT_TRANSLATIONS.items():
        if re.search(rf"\b{re.escape(de_term)}\b", translated, flags=re.IGNORECASE):
            translated = re.sub(rf"\b{re.escape(de_term)}\b", ru_term, translated, flags=re.IGNORECASE)
            matched = True
            if status_flag != "🟢":
                flag = status_flag

    if not matched and re.search(r'[a-zA-ZäöüÄÖÜß]', translated):
        flag = "🟡"

    return translated, flag


def is_2bl_team(name):
    n = name.lower()
    # пропускаем резервные и женские команды
    if re.search(r"\b(ii|iii|u\d{2})\b", n) or "frauen" in n:
        return False
    return any(k in n for k in TEAM_KEYS)


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT_CONFIG)
    logging.info("GET %s -> %s, %d байт", url, r.status_code, len(r.text))
    # сохраняем ответ для отладки (виден как артефакт в GitHub Actions)
    Path("debug.html").write_text(r.text, encoding="utf-8")
    if r.status_code != 200 or len(r.text) < 5000:
        raise ScrapeError(f"HTTP {r.status_code}, {len(r.text)} байт ({url})")
    return r.text


def find_next_url(soup, current):
    """Ищет ссылку «nächster Spieltag» (следующая неделя)."""
    for a in soup.find_all("a", href=True):
        label = " ".join([a.get_text(" ", strip=True), a.get("title", ""), a.get("aria-label", "")])
        if re.search(r"n(ä|ae)chster", label, re.I):
            return urljoin(current, a["href"])
    return None


def scrape_kicker_testspiele():
    logging.info("Запрос товарищеских матчей клубов 2. Бундеслиги до конца 2026 года...")
    today = date.today()
    matches, seen = [], set()
    url, total_rows = BASE_URL, 0

    for week in range(MAX_WEEKS):
        try:
            html = fetch(url)
        except requests.RequestException as e:
            raise ScrapeError(f"сетевая ошибка: {e}")

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("div", class_=re.compile(r"gameRow|matchrow"))
        total_rows += len(rows)
        logging.info("Неделя %d: строк матчей на странице: %d", week, len(rows))
        last_date = None

        for row in rows:
            try:
                teams = row.find_all("div", class_=re.compile(r"gameCell__team|teamName"))
                names = [t.get_text(strip=True) for t in teams if t.get_text(strip=True)]
                if len(names) < 2:
                    continue
                home, away = names[0], names[1]
                if not (is_2bl_team(home) or is_2bl_team(away)):
                    continue

                # ближайшая дата выше строки в документе
                node = row.find_previous(string=DATE_RE)
                m = DATE_RE.search(node) if node else None
                d = date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
                if d:
                    last_date = d
                    if not (today <= d <= END_DATE):
                        continue
                date_str = d.strftime("%d.%m.%Y") if d else "Дата уточняется"

                key = (home, away, date_str)
                if key in seen:
                    continue
                seen.add(key)

                link_tag = row.find("a", href=True)
                link = urljoin(url, link_tag["href"]) if link_tag else url

                info_tag = row.find("div", class_=re.compile(r"gameCell__info|matchrow__info"))
                comment, flag = translate_and_flag(info_tag.get_text(strip=True) if info_tag else "")
                if d is None and flag == "🟢":
                    flag = "🟡"

                matches.append({
                    "teams": f"{home} — {away}",
                    "date": date_str,
                    "comment": comment,
                    "flag": flag,
                    "link": link
                })
            except Exception as err:
                logging.warning(f"Ошибка обработки строки: {err}")
                continue

        nxt = find_next_url(soup, url)
        if not nxt or nxt == url or (last_date and last_date > END_DATE):
            break
        url = nxt

    if total_rows == 0:
        raise ScrapeError("страница загрузилась, но строки матчей не распознаны "
                          "(изменилась вёрстка или страница-заглушка/блокировка)")
    return matches


def send_telegram_payload(token, chat_id, text_message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text_message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=TIMEOUT_CONFIG)
    except requests.RequestException as e:
        logging.error(f"Ошибка отправки в Telegram: {e}")


def send_telegram_message(matches):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return

    if not matches:
        empty_msg = "⚽ <b>2. Bundesliga: Товарищеские матчи (до конца 2026)</b>\n\nℹ️ Запланированных матчей до конца 2026 года не найдено."
        send_telegram_payload(token, chat_id, empty_msg)
        return

    header = "⚽ <b>2. Bundesliga: Матчи до конца 2026 года</b>\n"
    header += "<i>Маркировка: 🟢 Точно | 🟡 Есть сомнения / Перевод | 🔴 Отменен</i>\n\n"
    current_msg = header

    for m in matches:
        card = (
            f"{m['flag']} <b>{m['teams']}</b>\n"
            f"📅 Дата: {m['date']}\n"
            f"ℹ️ Статус: {m['comment']}\n"
            f"🔗 <a href='{m['link']}'>Ссылка на Kicker</a>\n\n"
        )
        if len(current_msg) + len(card) > 3500:
            send_telegram_payload(token, chat_id, current_msg)
            current_msg = "⚽ <b>2. Bundesliga (продолжение):</b>\n\n" + card
        else:
            current_msg += card

    if current_msg:
        send_telegram_payload(token, chat_id, current_msg)


if __name__ == "__main__":
    try:
        data = scrape_kicker_testspiele()
    except ScrapeError as e:
        logging.error(e)
        token = os.environ.get("TELEGRAM_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if token and chat_id:
            send_telegram_payload(token, chat_id, f"⚠️ <b>Парсер Kicker не смог получить данные:</b>\n{e}")
        sys.exit(1)
    send_telegram_message(data)
