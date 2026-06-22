import math
import asyncio
import re
from typing import Optional
from geopy.geocoders import Nominatim, Photon
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

from config import NOMINATIM_UA

_geocoder = Nominatim(user_agent=NOMINATIM_UA, timeout=10)
_photon   = Photon(user_agent=NOMINATIM_UA, timeout=10)

# Viewbox half-size in degrees (~50 km)
_VIEWBOX_DEG = 0.5

# Approximate center coords for major Ukrainian cities
_UA_CITIES: dict[str, tuple[float, float]] = {
    "київ":                 (50.4501, 30.5234),
    "харків":               (49.9935, 36.2304),
    "одеса":                (46.4825, 30.7233),
    "дніпро":               (48.4647, 35.0462),
    "запоріжжя":            (47.8388, 35.1396),
    "львів":                (49.8397, 24.0297),
    "кривий ріг":           (47.9077, 33.3895),
    "миколаїв":             (46.9750, 31.9946),
    "херсон":               (46.6354, 32.6169),
    "полтава":              (49.5883, 34.5514),
    "чернігів":             (51.4982, 31.2893),
    "черкаси":              (49.4444, 32.0598),
    "суми":                 (50.9216, 34.8003),
    "житомир":              (50.2547, 28.6587),
    "хмельницький":         (49.4216, 26.9873),
    "рівне":                (50.6199, 26.2516),
    "вінниця":              (49.2331, 28.4682),
    "тернопіль":            (49.5535, 25.5948),
    "івано-франківськ":     (48.9226, 24.7111),
    "ужгород":              (48.6238, 22.2966),
    "луцьк":                (50.7472, 25.3254),
    "кропивницький":        (48.5079, 32.2623),
    "маріуполь":            (47.0968, 37.5417),
    "біла церква":          (49.8057, 30.1108),
    "краматорськ":          (48.7230, 37.5820),
    "дрогобич":             (49.3490, 23.5052),
    "мелітополь":           (46.8497, 35.3650),
    "ірпінь":               (50.5217, 30.2553),
    "буча":                 (50.5444, 30.2347),
}

# Street type prefixes to strip when building a "street only" query
_STREET_PREFIXES = re.compile(
    r"^(вул\.?|вулиця|просп\.?|проспект|пров\.?|провулок|пл\.?|площа|бульв\.?|бульвар|шосе|набережна|узвіз)\s+",
    re.IGNORECASE,
)

_HOUSE_RE = re.compile(r'\b(\d+[а-яіїєa-z]?(?:/\d+)?)\s*(?:,|$)', re.IGNORECASE)

# Matches city-type prefixes: "м.", "М . ", "місто", "смт", "мст"
# Also catches common typos: мысто (ы замість і), мисто, misto/mysto (транслітерація),
# город (рос.), змішана розкладка (Latin m/i поруч із кириличними літерами).
# Group 1 = city name (Cyrillic word after the prefix)
_CITY_PREFIX_RE = re.compile(
    r'\b(?:'
    r'[мm][іиыi]сто'   # місто / мисто / мысто / misто (кирилиця або Latin-m/i)
    r'|m[iy]sto'        # чисто латинська транслітерація: misto, mysto
    r'|город'           # російський синонім
    r'|м\s*\.?\s*'      # м. / М . / м /  (абревіатура)
    r'|смт\s*\.?\s*'    # смт.
    r'|мст\s*\.?\s*'    # мст.
    r')\s*([Ѐ-ӿ][Ѐ-ӿ\'\-]+)',
    re.IGNORECASE,
)


def _inject_housenumber(address_query: str, display: str) -> str:
    """If user typed a house number but OSM didn't return one, inject it into display."""
    m = _HOUSE_RE.search(address_query)
    if not m:
        return display
    house = m.group(1)
    if house in display:
        return display
    # Insert house number after the first comma (after street name)
    if ", " in display:
        street_part, rest = display.split(", ", 1)
        return f"{street_part}, {house}, {rest}"
    return f"{display}, {house}"


def _extract_city_from_text(text: str) -> tuple[str | None, str]:
    """
    Parse city name from user-typed prefixes: "м. Бориспіль", "місто Васильків", "смт Обухів".
    Returns (city_name, text_without_the_prefix_and_city).
    Returns (None, text) when no prefix found.
    """
    m = _CITY_PREFIX_RE.search(text)
    if not m:
        return None, text
    city_name = m.group(1)
    cleaned = (text[:m.start()] + text[m.end():]).strip().strip(",").strip()
    return city_name, cleaned


def _detect_city(text: str) -> tuple[str | None, tuple[float, float] | None, str]:
    """
    Return (city_name, (lat, lon), query_without_city).
    Checks if any known Ukrainian city name appears in the text.
    """
    lower = text.lower()
    for city, coords in _UA_CITIES.items():
        # Match as whole word(s)
        pattern = r"(?<![а-яіїєа-я])" + re.escape(city) + r"(?![а-яіїєа-я])"
        if re.search(pattern, lower):
            # Remove the city name (and surrounding punctuation/spaces) from query
            cleaned = re.sub(r",?\s*" + re.escape(city) + r"\s*,?", "", text, flags=re.IGNORECASE).strip().strip(",").strip()
            return city, coords, cleaned
    return None, None, text


def _format_address(raw: dict) -> str:
    """
    Format Nominatim raw address dict into a short human-readable string.
    Result: "вул. Хрещатик, 22, Київ"  or  "Контрактова площа, Київ"
    """
    a = raw.get("address", {}) if "address" in raw else raw

    # "square" and "place" cover OSM-tagged plazas (e.g. Контрактова площа)
    road     = (a.get("road") or a.get("pedestrian") or a.get("footway")
                or a.get("square") or a.get("place") or "")
    house    = a.get("house_number", "")
    district = a.get("city_district") or a.get("suburb") or ""
    city     = a.get("city") or a.get("town") or a.get("municipality") or a.get("village") or ""

    parts = []
    if road:
        parts.append(f"{road}, {house}".rstrip(", ") if house else road)
    if district and not road:
        # No street — show district so address isn't just "Київ"
        parts.append(district)
    if city:
        parts.append(city)

    return ", ".join(p for p in parts if p) or city or "Невідома адреса"


async def _geocode_dict(street: str, city: str | None = None) -> Optional[object]:
    """
    Nominatim structured dict search — finds house numbers reliably.
    Tries the full street name first (important for "Проспект Науки" where stripping
    the prefix would leave only "Науки" and return wrong results), then the stripped form.
    """
    loop = asyncio.get_event_loop()
    street_clean = _STREET_PREFIXES.sub("", street).strip()

    # Build query list: full name first (if it differs from stripped), then stripped
    queries: list[str] = []
    if street_clean.lower() != street.strip().lower():
        queries.append(street.strip())   # "Проспект Науки" before "Науки"
    queries.append(street_clean)          # bare genitive form "Головатого"

    for q in queries:
        params: dict = {"street": q, "country": "Ukraine"}
        if city:
            params["city"] = city
        try:
            result = await loop.run_in_executor(
                None, lambda _p=params: _geocoder.geocode(_p, language="uk", addressdetails=True)
            )
            if result:
                return result
        except (GeocoderTimedOut, GeocoderServiceError):
            return None
    return None


async def _geocode_city(city_name: str) -> tuple[float, float] | None:
    """Geocode a city name (e.g. 'Бориспіль') to (lat, lon) for viewbox biasing."""
    loop = asyncio.get_event_loop()
    try:
        loc = await loop.run_in_executor(
            None,
            lambda: _geocoder.geocode(
                {"city": city_name, "country": "Ukraine"},
                language="uk",
            ),
        )
        if loc:
            return (loc.latitude, loc.longitude)
    except (GeocoderTimedOut, GeocoderServiceError):
        pass
    return None


async def _photon_geocode(
    query: str,
    lat: float | None = None,
    lon: float | None = None,
) -> list[tuple[float, float, str, str]]:
    """
    Photon (Komoot) geocoder — fuzzy OSM-based.
    Returns [(lat, lon, display, city), ...] filtered to Ukraine.
    """
    loop = asyncio.get_event_loop()
    kwargs: dict = {"exactly_one": False, "limit": 5, "language": "uk"}
    if lat is not None and lon is not None:
        kwargs["location_bias"] = (lat, lon)
    try:
        results = await loop.run_in_executor(
            None, lambda: _photon.geocode(query, **kwargs)
        ) or []
    except Exception:
        return []
    if not isinstance(results, list):
        results = [results] if results else []

    output: list[tuple[float, float, str, str]] = []
    for loc in results:
        props = loc.raw.get("properties", {})
        cc = props.get("countrycode", "").upper()
        country = props.get("country", "").lower()
        # Filter to Ukraine
        if cc and cc != "UA":
            continue
        if not cc and country and "україн" not in country and "ukrain" not in country:
            continue
        street = props.get("street") or props.get("name") or ""
        housenumber = props.get("housenumber") or ""
        city = props.get("city") or props.get("county") or props.get("state") or ""
        parts = []
        if street:
            parts.append(f"{street}, {housenumber}".rstrip(", ") if housenumber else street)
        if city:
            parts.append(city)
        display = ", ".join(p for p in parts if p) or loc.address or "Невідома адреса"
        output.append((loc.latitude, loc.longitude, display, city))
    return output


async def geocode_address_multi(
    address: str,
    near_lat: float | None = None,
    near_lon: float | None = None,
    home_city: str | None = None,
) -> list[tuple[float, float, str, str]]:
    """
    Return up to 5 unique-city results: [(lat, lon, display_address, city_name), ...].
    Used to offer city disambiguation when the same street exists in multiple cities.
    home_city biases results toward user's known city when no city is explicit in query.
    """
    loop = asyncio.get_event_loop()

    _, city_coords, _ = _detect_city(address)

    # Dynamic detection: "м. Бориспіль", "місто Васильків", etc.
    # _dyn_cleaned = address without the "місто CityName" prefix (street-only part)
    _dyn_city: str | None = None
    _dyn_cleaned: str = address
    if not city_coords:
        _dyn_city, _dyn_cleaned = _extract_city_from_text(address)
        if _dyn_city:
            city_coords = await _geocode_city(_dyn_city)

    vb_lat, vb_lon = None, None
    if city_coords:
        vb_lat, vb_lon = city_coords
    elif near_lat is not None and near_lon is not None:
        vb_lat, vb_lon = near_lat, near_lon
    elif home_city:
        home_coords = _UA_CITIES.get(home_city.lower())
        if home_coords:
            vb_lat, vb_lon = home_coords

    kwargs: dict = {
        "country_codes": "ua",
        "language": "uk",
        "addressdetails": True,
        "exactly_one": False,
        "limit": 5,
    }
    if vb_lat is not None:
        kwargs["viewbox"] = [
            (vb_lat + _VIEWBOX_DEG, vb_lon - _VIEWBOX_DEG),
            (vb_lat - _VIEWBOX_DEG, vb_lon + _VIEWBOX_DEG),
        ]
        kwargs["bounded"] = False

    try:
        results = await loop.run_in_executor(
            None, lambda: _geocoder.geocode(address, **kwargs)
        ) or []
    except (GeocoderTimedOut, GeocoderServiceError):
        results = []

    # Retry with "вулиця " prefix if bare genitive form wasn't found
    if not results:
        try:
            results = await loop.run_in_executor(
                None, lambda: _geocoder.geocode("вулиця " + address, **kwargs)
            ) or []
        except (GeocoderTimedOut, GeocoderServiceError):
            results = []

    # If dynamic city was extracted, retry Nominatim with the cleaned street + city
    # e.g. "Вулиця головатого 9 місто бориспіль" → "Вулиця головатого 9, бориспіль"
    if not results and _dyn_city and _dyn_cleaned != address:
        _clean_q = f"{_dyn_cleaned}, {_dyn_city}"
        try:
            results = await loop.run_in_executor(
                None, lambda: _geocoder.geocode(_clean_q, **kwargs)
            ) or []
        except (GeocoderTimedOut, GeocoderServiceError):
            results = []

    if not isinstance(results, list):
        results = [results]

    seen_cities: set[str] = set()
    unique: list[tuple[float, float, str, str]] = []
    for loc in results:
        a = loc.raw.get("address", {})
        city = a.get("city") or a.get("town") or a.get("municipality") or a.get("village") or ""
        formatted = _format_address(loc.raw)
        # Skip empty or vague results
        if not formatted or formatted == "Невідома адреса":
            continue
        # Dedup by city; allow city-less results if address has street info
        dedup_key = city or formatted
        if dedup_key in seen_cities:
            continue
        seen_cities.add(dedup_key)
        unique.append((loc.latitude, loc.longitude, formatted, city))

    # If exactly one city found — try structured dict search to get house number
    city_name, _, street_only = _detect_city(address)
    known_city = city_name or (home_city if unique and unique[0][3] else None)
    if len(unique) == 1 and known_city:
        street_q = street_only if city_name else address
        dict_loc = await _geocode_dict(street_q, known_city)
        if dict_loc and dict_loc.raw.get("address", {}).get("house_number"):
            a = dict_loc.raw.get("address", {})
            city = a.get("city") or a.get("town") or a.get("municipality") or a.get("village") or unique[0][3]
            unique[0] = (dict_loc.latitude, dict_loc.longitude, _format_address(dict_loc.raw), city)

    # Structured dict search with dynamically extracted city
    # Covers: "Вулиця головатого 9 місто Бориспіль" → street="Вулиця головатого 9", city="Бориспіль"
    if not unique and _dyn_city and _dyn_cleaned and _dyn_cleaned != address:
        dict_loc = await _geocode_dict(_dyn_cleaned, _dyn_city)
        if not dict_loc:
            dict_loc = await _geocode_dict("вулиця " + _dyn_cleaned, _dyn_city)
        if dict_loc:
            a = dict_loc.raw.get("address", {})
            _c = a.get("city") or a.get("town") or a.get("municipality") or a.get("village") or _dyn_city
            if _c not in seen_cities:
                seen_cities.add(_c)
                unique.append((dict_loc.latitude, dict_loc.longitude, _format_address(dict_loc.raw), _c))

    # Last resort: structured dict search with home_city when free-text failed completely
    if not unique and home_city:
        dict_loc = await _geocode_dict(address, home_city)
        if not dict_loc:
            dict_loc = await _geocode_dict("вулиця " + address, home_city)
        if dict_loc:
            a = dict_loc.raw.get("address", {})
            city = a.get("city") or a.get("town") or a.get("municipality") or a.get("village") or home_city
            unique.append((dict_loc.latitude, dict_loc.longitude, _format_address(dict_loc.raw), city))

    # Photon fallback — fuzzy matching catches typos and abbreviations Nominatim misses
    if not unique:
        _photon_q = f"{_dyn_cleaned}, {_dyn_city}" if (_dyn_city and _dyn_cleaned != address) else address
        photon_results = await _photon_geocode(_photon_q, vb_lat, vb_lon)
        for p_lat, p_lon, p_display, p_city in photon_results:
            # Allow results even without a city field — use display as dedup key
            dedup_key = p_city or p_display
            if dedup_key and dedup_key not in seen_cities:
                seen_cities.add(dedup_key)
                unique.append((p_lat, p_lon, _inject_housenumber(address, p_display), p_city))

    # Inject user-typed house number if OSM didn't return one
    if unique:
        lat, lon, disp, city = unique[0]
        unique[0] = (lat, lon, _inject_housenumber(address, disp), city)

    return unique


async def geocode_address(
    address: str,
    near_lat: float | None = None,
    near_lon: float | None = None,
) -> Optional[tuple[float, float, str]]:
    """
    Return (lat, lon, display_name) or None.

    Strategy (in order):
    1. If user mentioned a known Ukrainian city — structured geocode (street + city).
    2. Biased free-text search inside viewbox of that city (or near_lat/near_lon).
    3. Country-wide free-text fallback.
    """
    loop = asyncio.get_event_loop()

    city_name, city_coords, street_only = _detect_city(address)

    # Dynamic detection: "м. Бориспіль", "місто Васильків", "смт Обухів", etc.
    if not city_name:
        city_name, street_only = _extract_city_from_text(address)
        if city_name:
            city_coords = await _geocode_city(city_name)

    # Choose viewbox center: explicit city in query wins over from_lat/from_lon
    if city_coords:
        vb_lat, vb_lon = city_coords
    elif near_lat is not None and near_lon is not None:
        vb_lat, vb_lon = near_lat, near_lon
    else:
        vb_lat, vb_lon = None, None

    viewbox = None
    if vb_lat is not None:
        viewbox = [
            (vb_lat + _VIEWBOX_DEG, vb_lon - _VIEWBOX_DEG),
            (vb_lat - _VIEWBOX_DEG, vb_lon + _VIEWBOX_DEG),
        ]

    base_kwargs = {"country_codes": "ua", "language": "uk", "addressdetails": True}

    async def _try(query: str, vb, bounded: bool) -> Optional[object]:
        kwargs = dict(base_kwargs)
        if vb:
            kwargs["viewbox"] = vb
            kwargs["bounded"] = bounded
        try:
            return await loop.run_in_executor(
                None, lambda: _geocoder.geocode(query, **kwargs)
            )
        except (GeocoderTimedOut, GeocoderServiceError):
            return None

    candidates = []

    # 1) Structured dict search with city (most precise — finds house numbers)
    if city_name and street_only:
        loc = await _geocode_dict(street_only, city_name)
        if loc:
            candidates.append(loc)

    # 2) Free-text inside viewbox (bounded)
    if viewbox and not candidates:
        loc = await _try(address, viewbox, bounded=True)
        if loc:
            candidates.append(loc)

    # 3) Free-text with "вулиця " prefix inside viewbox — handles bare genitive street names
    if viewbox and not candidates:
        loc = await _try("вулиця " + address, viewbox, bounded=True)
        if loc:
            candidates.append(loc)

    # 4) Free-text biased (not bounded) — viewbox is a hint
    if viewbox and not candidates:
        loc = await _try(address, viewbox, bounded=False)
        if loc:
            candidates.append(loc)

    # 5) Free-text with "вулиця " prefix, biased
    if viewbox and not candidates:
        loc = await _try("вулиця " + address, viewbox, bounded=False)
        if loc:
            candidates.append(loc)

    # 6) Country-wide free-text fallback (Nominatim ranks major cities higher)
    if not candidates:
        loc = await _try(address, None, False)
        if loc:
            candidates.append(loc)

    # 7) Country-wide with "вулиця " prefix
    if not candidates:
        loc = await _try("вулиця " + address, None, False)
        if loc:
            candidates.append(loc)

    # 8) Photon fallback — fuzzy OSM, handles typos and abbreviations
    if not candidates:
        photon_results = await _photon_geocode(address, vb_lat, vb_lon)
        if photon_results:
            lat, lon, display, _ = photon_results[0]
            return lat, lon, _inject_housenumber(address, display)

    # 9) Photon with explicit city appended (helps bare "вул. Головатого Бориспіль")
    if not candidates and city_name and street_only and street_only != address:
        photon_results = await _photon_geocode(f"{street_only}, {city_name}", vb_lat, vb_lon)
        if photon_results:
            lat, lon, display, _ = photon_results[0]
            return lat, lon, _inject_housenumber(address, display)

    if candidates:
        loc = candidates[0]
        display = _format_address(loc.raw)
        display = _inject_housenumber(address, display)
        return loc.latitude, loc.longitude, display

    return None


async def get_city_from_coords(lat: float, lon: float) -> str:
    """Return city/town name for coordinates (used to bias destination geocoding)."""
    loop = asyncio.get_event_loop()
    try:
        loc = await loop.run_in_executor(
            None, lambda: _geocoder.reverse((lat, lon), language="uk")
        )
        if loc and loc.raw.get("address"):
            a = loc.raw["address"]
            return a.get("city") or a.get("town") or a.get("municipality") or a.get("village") or ""
    except (GeocoderTimedOut, GeocoderServiceError):
        pass
    return ""


async def reverse_geocode(lat: float, lon: float) -> str | None:
    """Return human-readable address for coordinates, or None if geocoding fails."""
    loop = asyncio.get_event_loop()
    for _ in range(2):
        try:
            location = await loop.run_in_executor(
                None, lambda: _geocoder.reverse((lat, lon), language="uk")
            )
            if location:
                return _format_address(location.raw)
        except (GeocoderTimedOut, GeocoderServiceError):
            await asyncio.sleep(1)
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _point_to_segment(
    p_lat: float, p_lon: float,
    a_lat: float, a_lon: float,
    b_lat: float, b_lon: float,
) -> tuple[float, float]:
    """
    Return (distance_km, t) where:
      - distance_km = shortest distance from P to segment AB
      - t = projection parameter: 0.0 = at A, 1.0 = at B (can be <0 or >1 if outside segment)
    Uses flat-earth approximation (accurate enough within ~500 km).
    """
    cos_lat = math.cos(math.radians((a_lat + b_lat + p_lat) / 3))
    km_per_deg_lon = 111.0 * cos_lat
    km_per_deg_lat = 111.0

    # Translate to km offsets relative to A
    bx = (b_lon - a_lon) * km_per_deg_lon
    by = (b_lat - a_lat) * km_per_deg_lat
    px = (p_lon - a_lon) * km_per_deg_lon
    py = (p_lat - a_lat) * km_per_deg_lat

    ab_sq = bx * bx + by * by
    if ab_sq < 1e-10:  # A == B (degenerate segment)
        return math.sqrt(px * px + py * py), 0.0

    t = (px * bx + py * by) / ab_sq

    # Nearest point on segment (clamped to [0, 1] for distance calc)
    t_c = max(0.0, min(1.0, t))
    dx = px - t_c * bx
    dy = py - t_c * by
    return math.sqrt(dx * dx + dy * dy), t


def routes_compatible(
    a_from_lat: float, a_from_lon: float, a_to_lat: float, a_to_lon: float,
    b_from_lat: float, b_from_lon: float, b_to_lat: float, b_to_lon: float,
    radius_km: float,
) -> bool:
    """
    Return True if routes A and B are compatible for ridesharing.

    Compatible means one of:
    1. Both endpoints are close (same-length routes in similar area)
    2. Route B is a sub-segment of route A (B's from/to both project onto A within radius)
    3. Route A is a sub-segment of route B
    """
    # Case 1: both endpoints close — standard same-route match
    if (haversine_km(a_from_lat, a_from_lon, b_from_lat, b_from_lon) <= radius_km and
            haversine_km(a_to_lat, a_to_lon, b_to_lat, b_to_lon) <= radius_km):
        return True

    # Case 2: B's route lies within A's route (short passenger, long driver)
    d_bf, t_bf = _point_to_segment(b_from_lat, b_from_lon, a_from_lat, a_from_lon, a_to_lat, a_to_lon)
    d_bt, t_bt = _point_to_segment(b_to_lat, b_to_lon, a_from_lat, a_from_lon, a_to_lat, a_to_lon)
    if d_bf <= radius_km and d_bt <= radius_km and 0.0 <= t_bf <= t_bt <= 1.0:
        return True

    # Case 3: A's route lies within B's route (short driver, long passenger — less common)
    d_af, t_af = _point_to_segment(a_from_lat, a_from_lon, b_from_lat, b_from_lon, b_to_lat, b_to_lon)
    d_at, t_at = _point_to_segment(a_to_lat, a_to_lon, b_from_lat, b_from_lon, b_to_lat, b_to_lon)
    if d_af <= radius_km and d_at <= radius_km and 0.0 <= t_af <= t_at <= 1.0:
        return True

    return False
