import os
import re
import sys
import logging
import requests
from datetime import date, datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

API_URL = "https://v3.football.api-sports.io/fixtures"
LEAGUE_ID = 667                 # Club Friendlies (клубные товарищеские матчи)
SEASON = 2026
END_DATE = date(2026, 12, 31)
TIMEOUT_CONFIG = (5, 20)

# Ключевые слова клубов 2. Bundesliga 2026/27.
# ПРОВЕРЬТЕ список: здесь 17 клубов, 18-й добавьте сами.
TEAM_KEYS = [
    "hertha", "nürnberg", "nurnberg", "nuremberg", "heidenheim", "wolfsburg",
    "kaiserslautern", "magdeburg", "cottbus", "pauli", "bochum", "hannover",
    "osnabrück", "osnabruck", "fürth", "furth", "braunschweig", "dresden",
    "karlsruhe", "darmstadt", "kiel",
]

# статус API -> (описание, флаг)
STATUS_MAP = {
    "NS": ("Запланирован", "🟢"),
    "TBD": ("Время уточняется", "🟡"),
    "PST": ("ОТЛОЖЕН", "🔴"),
    "CANC": ("ОТМЕНЕН", "🔴"),
    "ABD": ("Матч прерван", "🔴"),
    "SUSP": ("Приостановлен", "🔴"),
    "INT": ("Прерван", "🔴"),
}


class ScrapeError(Exception):
    """Ошибка получения данных (не путать с «матчей нет»)."""


def is_2bl_team(name):
    n = name.lower()
    # пропускаем резервные, юношеские и женские команды
    if re.search(r"\b(ii|iii|u\d{2})\b", n) or "frauen" in n or " w" == n[-2:]:
        return False
    return any(k in n for k in TEAM_KEYS)


def scrape_kicker_testspiele():
    api_key = os.environ.get("API_FOOTBALL_KEY")
    if not api_key:
        raise ScrapeError("не задан секрет API_FOOTBALL_KEY")

    params = {
        "league": LEAGUE_ID,
        "season": SEASON,
        "from": date.today().isoformat(),
        "to": END_DATE.isoformat(),
        "timezone": "Europe/Berlin",
    }
    try:
        r = requests.get(API_URL, headers={"x-apisports-key": api_key},
                         params=params, timeout=TIMEOUT_CONFIG)
    except requests.RequestException as e:
        raise ScrapeError(f"сетевая ошибка: {e}")

    logging.info("GET %s -> %s, %d байт", r.url, r.status_code, len(r.text))
    if r.status_code != 200:
        raise ScrapeError(f"HTTP {r.status_code}: {r.text[:200]}")

    payload = r.json()
    if payload.get("errors"):
        raise ScrapeError(f"ошибка API: {payload['errors']}")

    fixtures = payload.get("response", [])
    logging.info("Всего товарищеских матчей в периоде: %d", len(fixtures))

    matches = []
    for f in fixtures:
        try:
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            if not (is_2bl_team(home) or is_2bl_team(away)):
                continue

            dt = datetime.fromisoformat(f["fixture"]["date"])
            status_short = f["fixture"]["status"]["short"]
            status_text, flag = STATUS_MAP.get(status_short, (f["fixture"]["status"]["long"], "🟡"))

            venue = f["fixture"].get("venue") or {}
            venue_str = ", ".join(x for x in [venue.get("name"), venue.get("city")] if x)
            comment = status_text + (f" · {venue_str}" if venue_str else "")

            matches.append({
                "sort": dt,
                "teams": f"{home} — {away}",
                "date": dt.strftime("%d.%m.%Y %H:%M"),
                "comment": comment,
                "flag": flag,
            })
        except (KeyError, ValueError) as err:
            logging.warning(f"Ошибка обработки матча: {err}")
            continue

    matches.sort(key=lambda m: m["sort"])
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
    header += "<i>Маркировка: 🟢 Запланирован | 🟡 Есть сомнения | 🔴 Отменен/отложен</i>\n\n"
    current_msg = header

    for m in matches:
        card = (
            f"{m['flag']} <b>{m['teams']}</b>\n"
            f"📅 Дата: {m['date']}\n"
            f"ℹ️ Статус: {m['comment']}\n\n"
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
            send_telegram_payload(token, chat_id, f"⚠️ <b>Парсер не смог получить данные:</b>\n{e}")
        sys.exit(1)
    send_telegram_message(data)
