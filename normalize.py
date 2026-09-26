import html, re

# Лексикон: признак НЕ первой команды / не товарищеского матча
SKIP_TOKENS = ("u17", "u19", "u21", "u23", " ii", "frauen", "women",
               "legenden", "traditionself", "allstars", "junioren", "a-junioren")

# Лексикон статусов (kicker / weltfussball)
STATUS_WORDS = {"ende": "finished", "live": "live", "abgesagt": "cancelled",
                "verlegt": "postponed", "ausgefallen": "cancelled",
                "n.v.": "finished", "i.e.": "finished"}

# Сокращения → официальные имена (единый вид для всех источников)
ABBR = {
    "k'lautern": "1. FC Kaiserslautern", "kaiserslautern": "1. FC Kaiserslautern",
    "gr furth": "SpVgg Greuther Fürth", "furth": "SpVgg Greuther Fürth",
    "braunschweig": "Eintracht Braunschweig", "braunschwg": "Eintracht Braunschweig",
    "magdeburg": "1. FC Magdeburg", "nurnberg": "1. FC Nürnberg",
    "paderborn": "SC Paderborn 07", "bochum": "VfL Bochum",
    "schalke": "FC Schalke 04", "s04": "FC Schalke 04",
    "kiel": "Holstein Kiel", "dusseldorf": "Fortuna Düsseldorf",
    "elversberg": "SV 07 Elversberg", "munster": "Preußen Münster",
    "ulm": "SSV Ulm 1846", "hertha": "Hertha BSC", "hannover": "Hannover 96",
    "darmstadt": "SV Darmstadt 98", "dresden": "Dynamo Dresden",
    "karlsruhe": "Karlsruher SC", "ksc": "Karlsruher SC",
}

def clean_name(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

def norm_key(s: str) -> str:
    s = clean_name(s).lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", s)).strip()

def expand_club(s: str) -> str:
    return ABBR.get(norm_key(s), clean_name(s))

def is_first_team_friendly(home: str, away: str) -> bool:
    t = (home + " " + away).lower()
    return not any(tok in t for tok in SKIP_TOKENS)
