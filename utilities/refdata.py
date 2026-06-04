"""Reference data tables extracted from overhead.py (pure literal data, no deps).

Re-imported into overhead.py so the display/classification helpers and the existing
tests resolve overhead.<NAME> unchanged.
"""

# ── Airport city names (IATA code → city) for log display only ───────────────
_AIRPORT_CITIES: dict[str, str] = {
    # Nevada / local
    "LAS": "Las Vegas",       "VGT": "N. Las Vegas",    "HSH": "Henderson",
    "BLD": "Boulder City",
    # US West
    "LAX": "Los Angeles",     "SFO": "San Francisco",   "SAN": "San Diego",
    "PHX": "Phoenix",         "TUS": "Tucson",           "ABQ": "Albuquerque",
    "ELP": "El Paso",         "SMF": "Sacramento",       "OAK": "Oakland",
    "SJC": "San Jose",        "BUR": "Burbank",          "LGB": "Long Beach",
    "ONT": "Ontario CA",      "PSP": "Palm Springs",     "SBA": "Santa Barbara",
    "FAT": "Fresno",          "RNO": "Reno",             "SLC": "Salt Lake City",
    "SEA": "Seattle",         "PDX": "Portland",         "BOI": "Boise",
    "GEG": "Spokane",         "MFR": "Medford",          "EUG": "Eugene",
    # US Mountain / Central
    "COS": "Colorado Springs","GJT": "Grand Junction",   "ASE": "Aspen",
    "EGE": "Eagle/Vail",      "HDN": "Hayden/Steamboat", "PUB": "Pueblo",
    "DEN": "Denver",          "DFW": "Dallas/Fort Worth","DAL": "Dallas",
    "AUS": "Austin",          "SAT": "San Antonio",      "HOU": "Houston",
    "IAH": "Houston",         "MSP": "Minneapolis",      "MCI": "Kansas City",
    "STL": "St. Louis",       "MKE": "Milwaukee",        "ORD": "Chicago",
    "MDW": "Chicago",         "DSM": "Des Moines",       "OMA": "Omaha",
    "MSN": "Madison",         "GRR": "Grand Rapids",     "FSD": "Sioux Falls",
    "RAP": "Rapid City",      "BIS": "Bismarck",         "GFK": "Grand Forks",
    "FAR": "Fargo",           "ABR": "Aberdeen SD",      "LNK": "Lincoln NE",
    # US Mountain West
    "BIL": "Billings",        "MSO": "Missoula",         "GTF": "Great Falls",
    "BZN": "Bozeman",         "IDA": "Idaho Falls",      "PIH": "Pocatello",
    "JAC": "Jackson Hole",    "SUN": "Sun Valley",       "TWF": "Twin Falls",
    # US East
    "ATL": "Atlanta",         "CLT": "Charlotte",        "MEM": "Memphis",
    "BNA": "Nashville",       "DTW": "Detroit",          "CLE": "Cleveland",
    "CMH": "Columbus",        "PIT": "Pittsburgh",       "IND": "Indianapolis",
    "CVG": "Cincinnati",      "PHL": "Philadelphia",     "EWR": "Newark",
    "JFK": "New York",        "LGA": "New York",         "BOS": "Boston",
    "BWI": "Baltimore",       "IAD": "Washington DC",    "DCA": "Washington DC",
    "RDU": "Raleigh-Durham",  "ORF": "Norfolk",          "RIC": "Richmond",
    "SYR": "Syracuse",        "ALB": "Albany",           "BDL": "Hartford",
    "PVD": "Providence",      "BUF": "Buffalo",          "ROC": "Rochester",
    "RFD": "Rockford",        "MLI": "Moline",           "CID": "Cedar Rapids",
    # US Southeast / South
    "MCO": "Orlando",         "TPA": "Tampa",            "MIA": "Miami",
    "FLL": "Fort Lauderdale", "RSW": "Fort Myers",       "PBI": "West Palm Beach",
    "SRQ": "Sarasota",        "JAX": "Jacksonville",     "SAV": "Savannah",
    "CHS": "Charleston",      "GSO": "Greensboro",       "GSP": "Greenville",
    "MSY": "New Orleans",     "BHM": "Birmingham",       "MOB": "Mobile",
    "LIT": "Little Rock",     "OKC": "Oklahoma City",    "TUL": "Tulsa",
    "XNA": "Fayetteville",    "TYS": "Knoxville",        "HSV": "Huntsville",
    "VPS": "Destin/Ft Walton","PNS": "Pensacola",        "MLB": "Melbourne FL",
    "ECP": "Panama City FL",  "PIE": "St. Petersburg",   "SFB": "Sanford FL",
    "DAB": "Daytona Beach",   "AGS": "Augusta",          "CAE": "Columbia SC",
    # US Hawaii / Alaska
    "HNL": "Honolulu",        "OGG": "Maui",             "KOA": "Kona",
    "LIH": "Lihue",           "ITO": "Hilo",
    "ANC": "Anchorage",       "FAI": "Fairbanks",        "JNU": "Juneau",
    "KTN": "Ketchikan",       "SIT": "Sitka",
    # Canada
    "YYZ": "Toronto",         "YVR": "Vancouver",        "YUL": "Montreal",
    "YYC": "Calgary",         "YEG": "Edmonton",         "YWG": "Winnipeg",
    "YOW": "Ottawa",          "YHZ": "Halifax",          "YYJ": "Victoria",
    "YKA": "Kamloops",        "YLW": "Kelowna",
    # Mexico
    "MEX": "Mexico City",     "CUN": "Cancún",           "GDL": "Guadalajara",
    "MTY": "Monterrey",       "SJD": "Los Cabos",        "PVR": "Puerto Vallarta",
    "MZT": "Mazatlán",        "ZIH": "Ixtapa",           "HMO": "Hermosillo",
    "BJX": "León",            "TLC": "Toluca",           "OAX": "Oaxaca",
    # Caribbean
    "NAS": "Nassau",          "GCM": "Grand Cayman",     "MBJ": "Montego Bay",
    "SJU": "San Juan",        "STT": "St. Thomas",
    # Europe
    "LHR": "London",          "LGW": "London Gatwick",   "MAN": "Manchester",
    "CDG": "Paris",           "ORY": "Paris Orly",       "FRA": "Frankfurt",
    "AMS": "Amsterdam",       "ZRH": "Zürich",           "MAD": "Madrid",
    "BCN": "Barcelona",       "FCO": "Rome",             "MXP": "Milan",
    "VIE": "Vienna",          "DUB": "Dublin",           "CPH": "Copenhagen",
    "ARN": "Stockholm",       "OSL": "Oslo",             "HEL": "Helsinki",
    "LIS": "Lisbon",          "ATH": "Athens",           "IST": "Istanbul",
    "BRU": "Brussels",        "MUC": "Munich",           "DUS": "Düsseldorf",
    "HAM": "Hamburg",         "BER": "Berlin",           "PRG": "Prague",
    "WAW": "Warsaw",          "BUD": "Budapest",         "GVA": "Geneva",
    # Middle East
    "DXB": "Dubai",           "DOH": "Doha",             "AUH": "Abu Dhabi",
    "TLV": "Tel Aviv",        "AMM": "Amman",
    # Asia / Pacific
    "ICN": "Seoul",           "GMP": "Seoul Gimpo",      "NRT": "Tokyo",
    "HND": "Tokyo",           "KIX": "Osaka",            "PEK": "Beijing",
    "PKX": "Beijing",         "PVG": "Shanghai",         "SHA": "Shanghai",
    "HKG": "Hong Kong",       "TPE": "Taipei",           "SIN": "Singapore",
    "KUL": "Kuala Lumpur",    "BKK": "Bangkok",          "HAN": "Hanoi",
    "SGN": "Ho Chi Minh City","CGK": "Jakarta",          "MNL": "Manila",
    "CEB": "Cebu",
    # Australia / New Zealand
    "SYD": "Sydney",          "MEL": "Melbourne",        "BNE": "Brisbane",
    "PER": "Perth",           "AKL": "Auckland",         "CHC": "Christchurch",
    # Latin America
    "BOG": "Bogotá",          "MDE": "Medellín",         "CLO": "Cali",
    "PTY": "Panama City",     "SJO": "San José",         "GUA": "Guatemala City",
    "SAP": "San Pedro Sula",  "LIM": "Lima",             "UIO": "Quito",
    "GIG": "Rio de Janeiro",  "GRU": "São Paulo",        "BSB": "Brasília",
    "SCL": "Santiago",        "EZE": "Buenos Aires",     "MVD": "Montevideo",
    "ASU": "Asunción",        "VVI": "Santa Cruz",
}


# ── Airline display names (ICAO 3-letter prefix → human-readable name) ────────
_AIRLINE_NAMES: dict[str, str] = {
    # US majors
    "AAL": "American Airlines",   "DAL": "Delta Air Lines",
    "UAL": "United Airlines",     "SWA": "Southwest Airlines",
    "ASA": "Alaska Airlines",     "JBU": "JetBlue Airways",
    "NKS": "Spirit Airlines",     "FFT": "Frontier Airlines",
    "SCX": "Sun Country Airlines","AAY": "Allegiant Air",
    "HAL": "Hawaiian Airlines",   "VRD": "Virgin America",
    # US ULCCs / leisure
    "MXY": "Breeze Airways",      "VXP": "Avelo Airlines",
    "JSX": "JSX",
    # Canadian regional / leisure
    "ROU": "Air Canada Rouge",
    # Charter / private aviation
    "EJA": "NetJets",              "LXJ": "Flexjet",             "JRE": "flyExclusive",
    "TIV": "Thrive Aviation",      "CXK": "ATP Flight School",
    "TWY": "Solarius Aviation",    "KAI": "KaiserAir",
    # Las Vegas special (government contractor — Groom Lake / Area 51 shuttle)
    "JAN": "Janet Airlines",
    # US cargo
    "FDX": "FedEx Express",       "UPS": "UPS Airlines",
    "GTI": "Atlas Air",           "ABX": "ABX Air",
    "ASN": "Amazon Air",          "PAC": "Polar Air Cargo",
    "CKS": "Kalitta Air",         "WGN": "Western Global Airlines",
    "NCR": "Northern Air Cargo",  "SOU": "Southern Air",
    "DHK": "DHL Aviation",        "AGX": "Amerijet International",
    # US charters / military contract
    "OAE": "Omni Air International",
    # German leisure (Lufthansa Group)
    "OCN": "Discover Airlines",
    # Canadian
    "ACA": "Air Canada",          "WJA": "WestJet",
    "POE": "Porter Airlines",     "FLE": "Flair Airlines",
    "SWG": "Sunwing Airlines",
    # Mexican
    "AMX": "Aeroméxico",          "VOI": "Volaris",
    "VIV": "VivaAerobus",
    # European
    "BAW": "British Airways",     "VIR": "Virgin Atlantic",
    "AFR": "Air France",          "DLH": "Lufthansa",
    "KLM": "KLM",                 "UAE": "Emirates",
    "QTR": "Qatar Airways",       "SIA": "Singapore Airlines",
    "EIN": "Aer Lingus",          "IBE": "Iberia",
    "CFG": "Condor",              "EDW": "Edelweiss Air",
    "THY": "Turkish Airlines",    "ETD": "Etihad Airways",
    "SWR": "Swiss Int'l",         "AUA": "Austrian Airlines",
    "NAX": "Norwegian",           "EZY": "easyJet",
    "RYR": "Ryanair",             "TAP": "TAP Air Portugal",
    "FIN": "Finnair",             "BEL": "Brussels Airlines",
    # Asian / Pacific
    "KAL": "Korean Air",          "QFA": "Qantas",
    "ANA": "All Nippon Airways",  "JAL": "Japan Airlines",
    "CPA": "Cathay Pacific",      "EVA": "EVA Air",
    "CCA": "Air China",           "CSN": "China Southern",
    "ANZ": "Air New Zealand",
    # Latin American
    "CMP": "Copa Airlines",       "AVA": "Avianca",
    # Regional/commuter
    "SKW": "SkyWest Airlines",    "ENY": "Envoy Air",
    "RPA": "Republic Airways",    "QXE": "Horizon Air",
    "ASH": "Mesa Airlines",       "PDT": "Piedmont Airlines",
    "JIA": "PSA Airlines",        "UCA": "CommutAir",
    "CPZ": "Comair",              "MTN": "Mountain Air Cargo",
    "FLG": "Frontier (charter)",
}


# ── ICAO aircraft type code → human-readable translation ──────────────────────
_AIRCRAFT_TYPE_MAP: dict[str, str] = {
    # Boeing narrow-body
    "B731": "Boeing 737-100",   "B732": "Boeing 737-200",
    "B733": "Boeing 737-300",   "B734": "Boeing 737-400",
    "B735": "Boeing 737-500",   "B736": "Boeing 737-600",
    "B737": "Boeing 737-700",   "B738": "Boeing 737-800",
    "B739": "Boeing 737-900",   "B37M": "Boeing 737 MAX 7",
    "B38M": "Boeing 737 MAX 8", "B39M": "Boeing 737 MAX 9",
    "B3XM": "Boeing 737 MAX 10",
    # Boeing wide-body
    "B744": "Boeing 747-400",   "B748": "Boeing 747-8",
    "B752": "Boeing 757-200",   "B753": "Boeing 757-300",
    "B762": "Boeing 767-200",   "B763": "Boeing 767-300",
    "B764": "Boeing 767-400",
    "B772": "Boeing 777-200",   "B77L": "Boeing 777-200LR",
    "B773": "Boeing 777-300",   "B77W": "Boeing 777-300ER",
    "B778": "Boeing 777X-8",    "B779": "Boeing 777X-9",
    "B788": "Boeing 787-8",     "B789": "Boeing 787-9",
    "B78X": "Boeing 787-10",
    # Airbus narrow-body
    "A318": "Airbus A318",      "A319": "Airbus A319",
    "A320": "Airbus A320",      "A321": "Airbus A321",
    "A19N": "Airbus A319neo",   "A20N": "Airbus A320neo",
    "A21N": "Airbus A321neo",   "A22N": "Airbus A321XLR",
    # Airbus wide-body
    "A332": "Airbus A330-200",  "A333": "Airbus A330-300",
    "A338": "Airbus A330-800neo","A339": "Airbus A330-900neo",
    "A342": "Airbus A340-200",  "A343": "Airbus A340-300",
    "A345": "Airbus A340-500",  "A346": "Airbus A340-600",
    "A359": "Airbus A350-900",  "A35K": "Airbus A350-1000",
    "A388": "Airbus A380",
    # Embraer
    "E170": "Embraer E170",     "E175": "Embraer E175",
    "E190": "Embraer E190",     "E195": "Embraer E195",
    "E75L": "Embraer E175-E2",  "E290": "Embraer E190-E2",
    "E295": "Embraer E195-E2",
    # Bombardier CRJ
    "CRJ1": "Bombardier CRJ-100","CRJ2": "Bombardier CRJ-200",
    "CRJ7": "Bombardier CRJ-700","CRJ9": "Bombardier CRJ-900",
    "CRJX": "Bombardier CRJ-1000",
    # Dash 8 / Q-series
    "DH8A": "Dash 8-100",       "DH8B": "Dash 8-200",
    "DH8C": "Dash 8-300",       "DH8D": "Dash 8-400",
    "DHC8": "Dash 8",
    # ATR
    "AT43": "ATR 42-300",       "AT45": "ATR 42-500",
    "AT72": "ATR 72-200",       "AT75": "ATR 72-500",
    "AT76": "ATR 72-600",
    # Business jets — Cessna Citation
    "C25A": "Citation CJ2",     "C25B": "Citation CJ3",
    "C25C": "Citation CJ4",     "C510": "Citation Mustang",
    "C525": "Citation CJ1",     "C550": "Citation II",
    "C56X": "Citation Excel",   "C560": "Citation V",
    "C680": "Citation Sovereign","C750": "Citation X",
    # Business jets — Gulfstream
    "GLF4": "Gulfstream IV",    "GLF5": "Gulfstream V",
    "G150": "Gulfstream G150",  "G280": "Gulfstream G280",
    "G550": "Gulfstream G550",  "G650": "Gulfstream G650",
    "G700": "Gulfstream G700",
    # Business jets — Bombardier Global/Challenger
    "CL30": "Bombardier Challenger 300",
    "CL35": "Bombardier Challenger 350",
    "CL60": "Bombardier Challenger 600",
    "GL5T": "Bombardier Global 5000",
    "GLEX": "Bombardier Global Express",
    "GL7T": "Bombardier Global 7500",
    # Business jets — Learjet
    "LJ35": "Learjet 35",       "LJ45": "Learjet 45",
    "LJ60": "Learjet 60",       "LJ75": "Learjet 75",
    # Business jets — Dassault Falcon
    "F2TH": "Falcon 2000",      "FA7X": "Falcon 7X",
    "F900": "Falcon 900",       "F8EX": "Falcon 8X",
    # Legacy MD / Boeing
    "MD11": "MD-11",            "MD80": "MD-80",
    "MD82": "MD-82",            "MD83": "MD-83",
    "MD88": "MD-88",            "MD90": "MD-90",
    # GA — Cessna piston
    "C172": "Cessna 172",       "C182": "Cessna 182",
    "C208": "Cessna Caravan",   "C210": "Cessna 210",
    # GA — Piper
    "P28A": "Piper PA-28",      "P28B": "Piper PA-28",
    "PA34": "Piper Seneca",     "PA46": "Piper Malibu/Meridian",
    # GA — Beechcraft
    "BE20": "King Air 200",     "BE35": "Bonanza 35",
    "BE36": "Bonanza 36",       "BE58": "Baron 58",
    "BE9L": "King Air 90",      "BE99": "Beechcraft 1900",
    # Helicopters
    "B06":  "Bell 206",         "B407": "Bell 407",
    "B412": "Bell 412",         "EC35": "EC135",
    "EC45": "EC145",            "H125": "Airbus H125",
    "R22":  "Robinson R22",     "R44":  "Robinson R44",
    "S76":  "Sikorsky S-76",
}


def _clean_iata(code):
    """Return a real 3-letter IATA airport code, else '' (rendered as '?').
    OpenSky and other feeds sometimes report FAA local identifiers ('NV98'), 4-char ICAO,
    or junk — none are IATA airports the hierarchy can place, and a non-3-char code doesn't
    fit the display.  Applied at get_route's boundary only; raw codes stay in debug logs."""
    code = (code or "").strip().upper()
    return code if (len(code) == 3 and code.isalpha()) else ""


def _route_display(origin: str, dest: str) -> str:
    """
    Build a log-friendly route string with city names where known.
    'LAS->MKE (Las Vegas to Milwaukee)'
    Falls back gracefully: 'LAS->XYZ (Las Vegas)' or 'LAS->MKE' if neither known.
    """
    o = (origin or "?").upper()
    d = (dest   or "?").upper()
    route  = f"{o}->{d}"
    o_city = _AIRPORT_CITIES.get(o)
    d_city = _AIRPORT_CITIES.get(d)
    if o_city and d_city:
        return f"{route} ({o_city} to {d_city})"
    if o_city:
        return f"{route} ({o_city})"
    if d_city:
        return f"{route} (to {d_city})"
    return route


def _airline_display(callsign: str) -> str:
    """Return 'SWA123 (Southwest Airlines)' if prefix known, else just callsign."""
    if not callsign or len(callsign) < 3:
        return callsign
    name = _AIRLINE_NAMES.get(callsign[:3].upper())
    return f"{callsign} ({name})" if name else callsign


def _translate_type(type_str: str) -> str:
    """Map a raw ICAO type code (e.g. 'B738') to a readable name if known."""
    if not type_str:
        return type_str
    return _AIRCRAFT_TYPE_MAP.get(type_str.strip().upper(), type_str)
