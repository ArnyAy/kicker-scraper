# Клубы 2. Bundesliga 2026/27. ID — из URL transfermarkt.de (например /verein/166 = Hertha)
# ВАЖНО: проверь состав в мае 2026 (после вылетов/повышений).
CLUBS = [
    {"id": "166",  "name": "Hertha BSC",            "slug": "hertha-bsc"},
    {"id": "131",  "name": "Hannover 96",           "slug": "hannover-96"},
    {"id": "2158", "name": "1. FC Kaiserslautern",  "slug": "1-fc-kaiserslautern"},
    {"id": "2110", "name": "1. FC Magdeburg",       "slug": "1-fc-magdeburg"},
    {"id": "60",   "name": "1. FC Nürnberg",        "slug": "1-fc-nurnberg"},
    {"id": "318",  "name": "Karlsruher SC",         "slug": "karlsruher-sc"},
    {"id": "610",  "name": "SV Darmstadt 98",       "slug": "sv-darmstadt-98"},
    {"id": "35",   "name": "Dynamo Dresden",        "slug": "dynamo-dresden"},
    {"id": "136",  "name": "Greuther Fürth",        "slug": "spvgg-greuther-furth"},
    {"id": "95",   "name": "FC Schalke 04",         "slug": "fc-schalke-04"},
    {"id": "1314", "name": "SC Paderborn 07",       "slug": "sc-paderborn-07"},
    {"id": "22",   "name": "VfL Bochum",            "slug": "vfl-bochum"},
    {"id": "1386", "name": "Fortuna Düsseldorf",    "slug": "fortuna-dusseldorf"},
    {"id": "2036", "name": "Holstein Kiel",         "slug": "holstein-kiel"},
    {"id": "105",  "name": "Eintracht Braunschweig","slug": "eintracht-braunschweig"},
    {"id": "717",  "name": "SV Elversberg",         "slug": "sv-elversberg"},
    {"id": "1257", "name": "Preußen Münster",       "slug": "sc-preussen-06-munster"},
    {"id": "33",   "name": "SSV Ulm 1846",          "slug": "ssv-ulm-1846-fussball"},
]

def friendlies_url(club_id: str, season: str = "2026") -> str:
    return f"https://www.transfermarkt.de/verein/testspiele/verein/{club_id}/saison_id/{season}"
