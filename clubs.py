from datetime import date

# 2. Bundesliga 2026/27: ID + slug с weltfussball.de
# Формат URL: https://www.weltfussball.de/teams/{id}/{slug}/2027/2/
CLUBS = [
    {"id": "te977",   "slug": "hertha-bsc",              "name": "Hertha BSC"},
    {"id": "te951",   "slug": "hannover-96",             "name": "Hannover 96"},
    {"id": "te6",     "slug": "1-fc-kaiserslautern",     "name": "1. FC Kaiserslautern"},
    {"id": "te11",    "slug": "1-fc-magdeburg",          "name": "1. FC Magdeburg"},
    {"id": "te12",    "slug": "1-fc-nuernberg",          "name": "1. FC Nürnberg"},
    {"id": "te1109",  "slug": "karlsruher-sc",           "name": "Karlsruher SC"},
    {"id": "te1852",  "slug": "sv-darmstadt-98",         "name": "SV Darmstadt 98"},
    {"id": "te515",   "slug": "dynamo-dresden",          "name": "Dynamo Dresden"},
    {"id": "te1787",  "slug": "spvgg-greuther-fuerth",   "name": "SpVgg Greuther Fürth"},
    {"id": "te694",   "slug": "fc-schalke-04",           "name": "FC Schalke 04"},
    {"id": "te1651",  "slug": "sc-paderborn-07",         "name": "SC Paderborn 07"},
    {"id": "te2079",  "slug": "vfl-bochum",              "name": "VfL Bochum"},
    {"id": "te826",   "slug": "fortuna-duesseldorf",     "name": "Fortuna Düsseldorf"},
    {"id": "te995",   "slug": "holstein-kiel",           "name": "Holstein Kiel"},
    {"id": "te528",   "slug": "eintracht-braunschweig",  "name": "Eintracht Braunschweig"},
    {"id": "te13214", "slug": "sv-07-elversberg",        "name": "SV 07 Elversberg"},
    {"id": "te1498",  "slug": "preussen-muenster",       "name": "Preußen Münster"},
    {"id": "te1798",  "slug": "ssv-ulm-1846",            "name": "SSV Ulm 1846"},
]

END_DATE = date(2026, 12, 31)

def schedule_url(club: dict) -> str:
    return f"https://www.weltfussball.de/teams/{club['id']}/{club['slug']}/2027/2/"
