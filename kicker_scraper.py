import os
import re
import sys
import logging
import requests
from datetime import date
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

        if week == 0:
            with open("debug.html", "w", encoding="utf-8") as
