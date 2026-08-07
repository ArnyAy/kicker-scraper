import os
import re
import logging
import requests
from bs4 import BeautifulSoup

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"
}

URL_TESTSPIELE = "https://www.kicker.de/2-bundesliga/testspiele"
TIMEOUT_CONFIG = (5, 15)  # (connect timeout, read timeout)

def translate_terms(text):
    if not text:
        return "Нет данных"
    dict_terms = {
        r"\bunter Ausschluss der Öffentlichkeit\b": "Без зрителей (закрытый матч)",
        r"\bGeneralprobe\b": "Генеральная репетиция (финальный тест)",
        r"\babgesagt\b": "ОТМЕНЕН",
        r"\bAbbruch\b": "Матч прерван",
        r"\bPlatz\b": "Поле",
        r"\bKunstrasenplatz\b": "Искусственное поле",
        r"\bStadion\b": "Стадион",
        r"\bTrainingszentrum\b": "Тренировочная база"
    }
    translated = text
    for de_pattern, ru_translation in dict_terms.items():
        translated = re.sub(de_pattern, ru_translation, translated, flags=re.IGNORECASE)
    return translated

def scrape_kicker_testspiele():
    logging.info("Запрос данных с kicker.de...")
    try:
        response = requests.get(URL_TESTSPIELE, headers=HEADERS, timeout=TIMEOUT_CONFIG)
        if response.status_code != 200:
            logging.error(f"Ошибка загрузки страницы Kicker: HTTP {response.status_code}")
            return []
    except requests.RequestException as e:
        logging.error(f"Сетевая ошибка при запросе к Kicker: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    matches_data = []

    match_rows = soup.find_all("div", class_=re.compile(r"kick__v100-gameList__gameRow|kick__matchrow"))
    logging.info(f"Найдено строк с матчами: {len(match_rows)}")

    for row in match_rows:
        try:
            link_tag = row.find("a", href=True)
            match_link = f"https://www.kicker.de{link_tag['href']}" if link_tag else URL_TESTSPIELE
            
            teams = row.find_all("div", class_=re.compile(r"kick__v100-gameCell__team|kick__matchrow__teamName"))
            if len(teams) >= 2:
                team_home = teams[0].text.strip()
                team_away = teams[1].text.strip()
                teams_str = f"{team_home} — {team_away}"
            else:
                continue

            date_tag = row.find_parent("div", class_=re.compile(r"kick__v100-gameList"))
            date_str = "Дата не указана"
            if date_tag:
                header_date = date_tag.find("div", class_=re.compile(r"kick__v100-gameList__header|kick__date"))
                if header_date:
                    date_str = header_date.text.strip()

            info_tag = row.find("div", class_=re.compile(r"kick__v100-gameCell__info|kick__matchrow__info"))
            venue_comment_raw = info_tag.text.strip() if info_tag else ""
            venue_comment = translate_terms(venue_comment_raw)

            matches_data.append({
                "teams": teams_str,
                "date": date_str,
                "comment": venue_comment,
                "link": match_link
            })
        except Exception as err:
            logging.warning(f"Ошибка при обработке строки матча: {err}")
            continue

    return matches_data

def send_telegram_payload(token, chat_id, text_message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text_message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT_CONFIG)
        if resp.status_code == 200:
            logging.info("Сообщение успешно доставлено в Telegram!")
        else:
            logging.error(f"Ошибка Telegram API: {resp.status_code}, {resp.text}")
    except requests.RequestException as e:
        logging.error(f"Сетевая ошибка при отправке в Telegram: {e}")

def send_telegram_message(matches):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logging.error("Секреты TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не установлены!")
        return

    if not matches:
        empty_msg = "⚽ <b>2. Bundesliga: Товарищеские матчи</b>\n\nℹ️ На сегодня товарищеских матчей не найдено."
        send_telegram_payload(token, chat_id, empty_msg)
        return

    header = "⚽ <b>2. Bundesliga: Товарищеские матчи</b>\n\n"
    current_msg = header

    for m in matches:
        card = (
            f"🏆 <b>{m['teams']}</b>\n"
            f"📅 Дата: {m['date']}\n"
            f"ℹ️ Детали: {m['comment']}\n"
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
    data = scrape_kicker_testspiele()
    send_telegram_message(data)
