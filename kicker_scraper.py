import os
import re
import logging
import requests
from datetime import datetime
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"
}

URL_TESTSPIELE = "https://www.kicker.de/2-bundesliga/testspiele"
TIMEOUT_CONFIG = (5, 15)

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

def is_date_in_2026(date_str):
    """Проверка: относится ли дата к интервалу с авг 2026 по 31.12.2026"""
    if not date_str or "уточняется" in date_str.lower():
        return True, "🟡" # Оставляем с желтым флагом сомнения
    
    # Ищем года в строке даты
    year_match = re.search(r'\b(202[4-9])\b', date_str)
    if year_match:
        year = int(year_match.group(1))
        if year > 2026:
            return False, "🟢" # Игнорируем матчи 2027+
    return True, "🟢"

def scrape_kicker_testspiele():
    logging.info("Запрос товарищеских матчей 2. Бундеслиги до конца 2026 года...")
    try:
        response = requests.get(URL_TESTSPIELE, headers=HEADERS, timeout=TIMEOUT_CONFIG)
        if response.status_code != 200:
            logging.error(f"Ошибка HTTP {response.status_code}")
            return []
    except requests.RequestException as e:
        logging.error(f"Сетевая ошибка: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    matches_data = []

    match_rows = soup.find_all("div", class_=re.compile(r"kick__v100-gameList__gameRow|kick__matchrow"))
    logging.info(f"Найдено матчей в расписании: {len(match_rows)}")

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
            date_str = "Дата уточняется"
            if date_tag:
                header_date = date_tag.find("div", class_=re.compile(r"kick__v100-gameList__header|kick__date"))
                if header_date:
                    date_str = header_date.text.strip()

            # Проверка соответствия периоду до конца 2026 года
            valid_date, date_flag = is_date_in_2026(date_str)
            if not valid_date:
                continue

            info_tag = row.find("div", class_=re.compile(r"kick__v100-gameCell__info|kick__matchrow__info"))
            venue_comment_raw = info_tag.text.strip() if info_tag else ""
            
            translated_comment, status_flag = translate_and_flag(venue_comment_raw)

            # Если дата вызывает сомнения, приоритет за желтым флагом
            final_flag = date_flag if date_flag == "🟡" and status_flag == "🟢" else status_flag

            matches_data.append({
                "teams": teams_str,
                "date": date_str,
                "comment": translated_comment,
                "flag": final_flag,
                "link": match_link
            })
        except Exception as err:
            logging.warning(f"Ошибка обработки строки: {err}")
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
    data = scrape_kicker_testspiele()
    send_telegram_message(data)
