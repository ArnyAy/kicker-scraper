import requests
from bs4 import BeautifulSoup
import pandas as pd
import re

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"
}

URL_TESTSPIELE = "https://www.kicker.de/2-bundesliga/testspiele"

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
    response = requests.get(URL_TESTSPIELE, headers=HEADERS)
    if response.status_code != 200:
        print(f"Ошибка доступа к Kicker.de: статус {response.status_code}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    matches_data = []

    match_rows = soup.find_all("div", class_=re.compile(r"kick__v100-gameList__gameRow|kick__matchrow"))
    
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
                "Команды": teams_str,
                "Место проведения": venue_comment if "Стадион" in venue_comment or "Поле" in venue_comment else "См. карточку",
                "Дата": date_str,
                "Рабочая ссылка": match_link,
                "Комментарии": venue_comment
            })
        except Exception:
            continue

    return matches_data

if __name__ == "__main__":
    data = scrape_kicker_testspiele()
    df = pd.DataFrame(data)
    if not df.empty:
        print(df.to_markdown(index=False))
    else:
        print("На данный момент актуальные товарищеские матчи не найдены или структура страницы обновилась.")
