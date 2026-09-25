"""Conservative sport labels inferred from Sky programme titles.

Keep category separate from broadcast status: an MMA promo is MMA, not LIVE.
Unknown or ambiguous programmes return Other rather than guessing a sport
from the channel carrying them.
"""

from __future__ import annotations

import re

# Order matters: College Football is not soccer; Canoe Polo is not field polo;
# WSL may also mean Women's Super League (football).
SPORT_PATTERNS = (
    ("MMA", r"\b(?:UFC|PFL|MMA|mixed martial arts|ONE Championship)\b"),
    ("Canoe Polo", r"\b(?:canoe polo|ICF Polo World Championships?)\b"),
    ("Rugby League", r"\b(?:NRL|NRLW|rugby league|super league rugby)\b"),
    ("Rugby Union", r"\b(?:RWC|NPC|FPC|WXV|All Blacks|Black Ferns|rugby union|rugby sevens|ultimate sevens|rugby heaven|six nations|super rugby)\b"),
    ("Aussie Rules", r"\b(?:AFL|AFLW|Australian rules|Aussie rules)\b"),
    ("American Football", r"\b(?:NFL|NCAA football|college football|American football)\b"),
    ("Cricket", r"\b(?:cricket|ODI|T20|IPL|Big Bash|the Hundred|CPL cricket|ICC (?:Men's|Women's)? ?(?:Cricket|World Cup))\b"),
    ("Basketball", r"\b(?:NBA|WNBA|NBL|Tauihi|basketball|EuroLeague basketball|Rapid League:\s*Whai\s+v\s+Kahu)\b"),
    ("Baseball", r"\b(?:MLB|baseball|World Series baseball)\b"),
    ("Golf", r"\b(?:PGA|LPGA|DP World Tour|Korn Ferry Tour|HotelPlanner Tour|Presidents Cup|Charles Tour|golf)\b"),
    ("Tennis", r"\b(?:tennis|Laver Cup|Davis Cup|Wimbledon|Roland Garros|ATP|WTA)\b"),
    ("Motorsport", r"\b(?:motorsport|Formula\s*[123E]|F[123]\b|MotoGP|Moto2|Moto3|FIA\s+(?:Karting|F[123]|Formula)|karting|Supercars|rally|Grand Prix motor racing|DTM)\b"),
    ("Cycling", r"\b(?:cycling|UCI Road|UCI Track|Tour de France|Giro d'Italia|Vuelta a Espa[nñ]a|CRO Race)\b"),
    ("Racing", r"\b(?:Trackside|horse racing|harness racing|greyhound racing|Racing Replay)\b"),
    ("Netball", r"\b(?:netball|NETBUSTERS)\b"),
    ("Darts", r"\b(?:darts|PDC World)\b"),
    ("Squash", r"\b(?:squash|PSA Qatar Classic)\b"),
    ("Sailing", r"\b(?:sailing|America's Cup sailing)\b"),
    ("Surfing", r"\b(?:surfing|World Surf League|Lexus Trestles Pro|WSL:.*\bPro\b)\b"),
    ("Triathlon", r"\b(?:triathlon|T100 Triathlon)\b"),
    ("Trail Running", r"\b(?:trail running|Golden Trail|GTWS|Myoko Trail)\b"),
    ("Lawn Bowls", r"\b(?:lawn bowls|the bowls show|bowls championship)\b"),
    ("Soccer", r"\b(?:soccer|football|FIFA|UEFA|Premier League|Champions League|La Liga|Bundesliga|Serie A|ESPN FC|PL GOATS|PL MASTERCLASS|PL MOMENTS|PL STORIES|GENERATION xG|The Starting Eleven)\b"),
)
_COMPILED_PATTERNS = tuple(
    (sport, re.compile(pattern, re.IGNORECASE))
    for sport, pattern in SPORT_PATTERNS
)


def classify_sport(program_title: str) -> str:
    """Return a normalized sport label or Other for ambiguous titles."""
    title = (program_title or "").strip()
    if not title:
        return "Other"
    for sport, pattern in _COMPILED_PATTERNS:
        if pattern.search(title):
            return sport
    return "Other"
