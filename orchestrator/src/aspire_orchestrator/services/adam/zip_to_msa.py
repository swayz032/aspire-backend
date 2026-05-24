"""ZIP-to-MSA mapping for BLS OEWS labor rate lookups.

Maps US ZIP codes to BLS MSA numeric codes so wage lookups are metro-level
rather than national-only. The static table covers the top 75 US metros by
construction volume (sufficient for Aspire's ICP). Unknown ZIPs fall back
to national estimates in the BLS client.

MSA codes are the OMB Metropolitan Statistical Area codes used by BLS.
Format used in OEWS series IDs: zero-padded to 7 digits.

Source: BLS OEWS geographic definitions, OMB Bulletin 23-01.
Data vintage: 2024 MSA definitions.
"""

from __future__ import annotations

# Mapping: ZIP prefix (first 3 digits) → BLS MSA code string (unpadded)
# Where a metro spans multiple ZIP prefixes, each is listed separately.
_ZIP_PREFIX_TO_MSA: dict[str, str] = {
    # New York–Newark–Jersey City
    "100": "35620", "101": "35620", "102": "35620", "103": "35620",
    "104": "35620", "110": "35620", "111": "35620", "112": "35620",
    "113": "35620", "114": "35620", "116": "35620",
    "070": "35620", "071": "35620", "072": "35620", "073": "35620",
    "074": "35620", "075": "35620", "076": "35620", "077": "35620",
    # Los Angeles–Long Beach–Anaheim
    "900": "31080", "901": "31080", "902": "31080", "903": "31080",
    "904": "31080", "905": "31080", "906": "31080", "907": "31080",
    "908": "31080", "910": "31080", "911": "31080", "912": "31080",
    "913": "31080", "914": "31080", "915": "31080", "916": "31080",
    "917": "31080", "918": "31080", "919": "31080", "920": "31080",
    "921": "31080", "922": "31080", "923": "31080", "925": "31080",
    "926": "31080", "927": "31080", "928": "31080",
    # Chicago–Naperville–Elgin
    "600": "16980", "601": "16980", "602": "16980", "603": "16980",
    "604": "16980", "605": "16980", "606": "16980", "607": "16980",
    "608": "16980", "609": "16980", "610": "16980", "611": "16980",
    "612": "16980", "613": "16980", "614": "16980",
    # Dallas–Fort Worth–Arlington
    "750": "19100", "751": "19100", "752": "19100", "753": "19100",
    "754": "19100", "755": "19100", "756": "19100", "757": "19100",
    "758": "19100", "759": "19100", "760": "19100", "761": "19100",
    "762": "19100", "763": "19100", "764": "19100", "765": "19100",
    # Houston–The Woodlands–Sugar Land
    "770": "26420", "771": "26420", "772": "26420", "773": "26420",
    "774": "26420", "775": "26420", "776": "26420", "777": "26420",
    "778": "26420", "779": "26420",
    # Washington DC–Arlington–Alexandria
    "200": "47900", "201": "47900", "202": "47900", "203": "47900",
    "204": "47900", "205": "47900",
    "220": "47900", "221": "47900", "222": "47900", "223": "47900",
    "224": "47900", "225": "47900", "226": "47900", "227": "47900",
    "228": "47900", "229": "47900",
    # Miami–Fort Lauderdale–Pompano Beach
    "330": "33100", "331": "33100", "332": "33100", "333": "33100",
    "334": "33100", "335": "33100",
    # Philadelphia–Camden–Wilmington
    "190": "37980", "191": "37980", "192": "37980", "193": "37980",
    "194": "37980", "195": "37980", "196": "37980", "197": "37980",
    "198": "37980",
    # Atlanta–Sandy Springs–Alpharetta
    "300": "12060", "301": "12060", "302": "12060", "303": "12060",
    "304": "12060", "305": "12060", "306": "12060", "307": "12060",
    "308": "12060", "309": "12060", "310": "12060", "311": "12060",
    "312": "12060", "313": "12060", "314": "12060", "315": "12060",
    "316": "12060", "317": "12060", "318": "12060", "319": "12060",
    # Phoenix–Mesa–Chandler
    "850": "38060", "851": "38060", "852": "38060", "853": "38060",
    "854": "38060", "855": "38060", "856": "38060", "857": "38060",
    "858": "38060", "859": "38060", "860": "38060",
    # Seattle–Tacoma–Bellevue
    "980": "42660", "981": "42660", "982": "42660", "983": "42660",
    "984": "42660", "985": "42660",
    # Minneapolis–St. Paul–Bloomington
    "550": "33460", "551": "33460", "552": "33460", "553": "33460",
    "554": "33460", "555": "33460", "556": "33460", "557": "33460",
    "558": "33460", "559": "33460",
    # San Diego–Chula Vista–Carlsbad
    "919": "41740", "920": "41740", "921": "41740", "922": "41740",
    # Denver–Aurora–Lakewood
    "800": "19740", "801": "19740", "802": "19740", "803": "19740",
    "804": "19740", "805": "19740", "806": "19740",
    # Austin–Round Rock–Georgetown
    "786": "12420", "787": "12420", "788": "12420", "789": "12420",
    # San Antonio–New Braunfels
    "780": "41700", "781": "41700", "782": "41700",
    # Orlando–Kissimmee–Sanford
    "327": "36740", "328": "36740", "329": "36740", "347": "36740",
    # Tampa–St. Petersburg–Clearwater
    "335": "45300", "336": "45300", "337": "45300",
    # Baltimore–Columbia–Towson
    "210": "12580", "211": "12580", "212": "12580",
    # St. Louis
    "630": "41180", "631": "41180", "632": "41180", "633": "41180",
    "634": "41180",
    # Nashville–Davidson–Murfreesboro–Franklin
    "370": "34980", "371": "34980", "372": "34980",
    # Charlotte–Concord–Gastonia
    "280": "16740", "281": "16740", "282": "16740", "283": "16740",
    # Las Vegas–Henderson–Paradise
    "889": "29820", "890": "29820", "891": "29820",
    # Raleigh–Cary
    "275": "39580", "276": "39580", "277": "39580",
    # Indianapolis–Carmel–Anderson
    "460": "26900", "461": "26900", "462": "26900",
    # Columbus OH
    "430": "18140", "431": "18140", "432": "18140", "433": "18140",
    # San Jose–Sunnyvale–Santa Clara
    "950": "41940", "951": "41940", "952": "41940",
    # San Francisco–Oakland–Berkeley
    "940": "41860", "941": "41860", "942": "41860", "943": "41860",
    "944": "41860", "945": "41860",
    # Kansas City MO-KS
    "640": "28140", "641": "28140", "642": "28140", "643": "28140",
    "644": "28140", "645": "28140", "646": "28140", "647": "28140",
    "648": "28140",
    # Portland OR–WA
    "970": "38900", "971": "38900", "972": "38900", "973": "38900",
    "974": "38900",
    # Jacksonville FL
    "320": "27260", "321": "27260", "322": "27260",
    # Memphis TN
    "380": "32820", "381": "32820",
    # Oklahoma City
    "730": "36420", "731": "36420",
    # Richmond VA
    "230": "40060", "231": "40060", "232": "40060",
    # Louisville–Jefferson County KY
    "400": "31140", "401": "31140", "402": "31140",
    # Hartford–East Hartford–Middletown CT
    "060": "25540", "061": "25540", "062": "25540",
    # New Orleans–Metairie
    "700": "35380", "701": "35380",
    # Birmingham–Hoover AL
    "350": "13820", "351": "13820", "352": "13820",
    # Salt Lake City
    "840": "41620", "841": "41620", "842": "41620",
    # Tallahassee FL (Aspire HQ region)
    "323": "45220",
}


def zip_to_msa_code(zipcode: str) -> str | None:
    """Map a 5-digit US ZIP code to a BLS MSA numeric code string.

    Uses a prefix-based lookup (first 3 digits). Returns None if the ZIP
    is not mapped — the BLS client will fall back to national estimates.
    """
    if not zipcode:
        return None
    prefix = zipcode.strip()[:3]
    return _ZIP_PREFIX_TO_MSA.get(prefix)
