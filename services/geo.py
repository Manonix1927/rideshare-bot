import math
import asyncio
import logging
import re
import time
from typing import Optional

import aiohttp
from geopy.geocoders import Nominatim, Photon
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

from config import NOMINATIM_UA, GOOGLE_MAPS_API_KEY, OSM_FALLBACK_ENABLED

logger = logging.getLogger(__name__)

_geocoder = Nominatim(user_agent=NOMINATIM_UA, timeout=10)
_photon   = Photon(user_agent=NOMINATIM_UA, timeout=10)

_GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_PLACES_TEXT_URL    = "https://places.googleapis.com/v1/places:searchText"
# After a hard auth/billing failure we pause an API for a cooldown and fall through,
# then automatically retry. This way a transient denial (e.g. restriction change
# still propagating, or Places API not yet enabled) self-heals without a redeploy.
_GOOGLE_COOLDOWN_SEC = 600  # 10 minutes
_google_cooldown_until = 0.0
_places_cooldown_until = 0.0

# OSM is used when explicitly enabled, OR always when there's no Google key
# (so geocoding never dies just because the key was removed).
_OSM_ACTIVE = OSM_FALLBACK_ENABLED or not GOOGLE_MAPS_API_KEY

# Startup diagnostic — makes it obvious in Railway logs whether the running
# process actually sees the key (vs. an old deploy / empty env var).
# WARNING level so it survives even though this module is imported before
# main.py calls logging.basicConfig() (Python emits WARNING+ to stderr by default).
logger.warning(
    "Geocoder init: Google=%s | order: Places→Geocoding→%s",
    f"ENABLED (key …{GOOGLE_MAPS_API_KEY[-4:]})" if GOOGLE_MAPS_API_KEY
    else "DISABLED — no GOOGLE_MAPS_API_KEY",
    "OSM" if OSM_FALLBACK_ENABLED else "(OSM off)",
)

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


def _intended_locality(text: str) -> str | None:
    """
    Best guess of the settlement the user means: a known city named anywhere, a
    "м./місто X" prefix, or the last comma-separated token when it's a plain word
    (catches villages not in our list, e.g. "Ділова 2, Крюківщина"). Returns None
    when no locality is implied (so we don't over-constrain bare street queries).
    """
    known = _detect_city(text)[0] or _extract_city_from_text(text)[0]
    if known:
        return known
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) >= 2:
        last = parts[-1]
        # Drop a house number stuck to the settlement when the comma landed after the
        # street name ("Одеська, 2 Віта-Поштова" → last token "2 Віта-Поштова" → village).
        last = re.sub(r"^\d+[а-яіїєa-zA-Z]?\s+", "", last).strip()
        # A locality is alphabetic (Cyrillic) with no digits — excludes a bare house
        # number ("Хрещатик, 22"). Admin areas (район/область) aren't settlements and
        # would never equal the result's city, so don't constrain on them.
        if (re.search(r"[А-Яа-яІіЇїЄєҐґ]", last)
                and not re.search(r"\d", last)
                and not re.search(r"район|область|обл\.?", last, re.IGNORECASE)):
            return last
    return None


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
            # Remove the city name plus an optional preceding city-type prefix
            # ("місто Київ", "м. Київ", "смт Київ") so the leftover street query
            # doesn't carry a dangling "місто" that breaks structured search.
            cleaned = re.sub(
                r",?\s*(?:місто|м\.?|смт\.?|мст\.?)?\s*" + re.escape(city) + r"\s*,?",
                "", text, flags=re.IGNORECASE,
            ).strip().strip(",").strip()
            return city, coords, cleaned
    return None, None, text


def _city_of(a: dict) -> str:
    """
    Pick the cleanest settlement name from a Nominatim address dict.
    Prefers concrete settlement (city/town/village) over administrative
    aggregates (municipality), so we show "Боярка", not "Боярська міська громада".
    """
    return (a.get("city") or a.get("town") or a.get("village")
            or a.get("municipality") or "")


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
    city     = _city_of(a)

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


async def _geocode_in_city(query: str, city: str) -> Optional[object]:
    """
    Geocode a street strictly within `city`. Returns a geopy Location whose
    settlement actually matches `city`, or None.

    If the exact house number isn't present on that street in the city, Nominatim's
    structured search silently jumps to another city where it exists (e.g. house 10
    on "Святошинська" lives in Вишневе, not Київ). To prevent that, we verify the
    returned city and, on mismatch, retry with the house number stripped so the
    result is pinned to the street centroid in the requested city. The caller then
    injects the original house number into the display string.
    """
    city_l = city.lower()
    for q in (query, "вулиця " + query):
        loc = await _geocode_dict(q, city)
        if loc and city_l in _city_of(loc.raw.get("address", {})).lower():
            return loc

    m = _HOUSE_RE.search(query)
    if m:
        street_only = (query[:m.start()] + query[m.end():]).strip().strip(",").strip()
        if street_only and street_only != query:
            for q in (street_only, "вулиця " + street_only):
                loc = await _geocode_dict(q, city)
                if loc and city_l in _city_of(loc.raw.get("address", {})).lower():
                    return loc
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


def _google_component(components: list, *types: str) -> str:
    """Pick the long_name of the first address component matching any given type."""
    for comp in components:
        if any(t in comp.get("types", []) for t in types):
            return comp.get("long_name", "")
    return ""


# Result types that represent a concrete, routable place (street or POI).
_GOOGLE_SPECIFIC_TYPES = {
    "street_address", "premise", "subpremise", "route", "intersection",
    "establishment", "point_of_interest", "transit_station", "park", "airport",
}
# Allowlist: only accept results whose types include something concrete enough
# to be a pickup point — a real address/POI, a city, or a city district.
# Everything else (country, administrative_area_*, colloquial_area, political-only)
# is a fuzzy region match and gets dropped, e.g. typo "Либідьська" → "Слобідська
# Україна" (colloquial_area). Falls back to OSM, which is better than a wrong region.
_GOOGLE_OK_TYPES = _GOOGLE_SPECIFIC_TYPES | {
    "locality", "sublocality", "sublocality_level_1",
    "neighborhood", "postal_code", "plus_code",
}


def _google_is_usable(result: dict) -> bool:
    """Accept only concrete address/POI/city/district results, reject fuzzy regions."""
    return bool(set(result.get("types", [])) & _GOOGLE_OK_TYPES)


def _google_format(result: dict) -> tuple[str, str]:
    """
    Build ("Хрещатик, 22, Київ", "Київ") or ("Либідська, Київ", "Київ") from a
    Google geocode result. Handles both street addresses and POIs (metro,
    landmarks). Returns (display, city).
    """
    comps = result.get("address_components", [])
    route = _google_component(comps, "route")
    house = _google_component(comps, "street_number")
    poi   = _google_component(comps, "establishment", "point_of_interest",
                              "transit_station")
    district = _google_component(comps, "sublocality", "sublocality_level_1",
                                 "neighborhood")
    city  = (_google_component(comps, "locality")
             or _google_component(comps, "administrative_area_level_2")
             or _google_component(comps, "administrative_area_level_1"))

    if route:
        head = f"{route}, {house}".rstrip(", ") if house else route
    elif poi:
        head = poi          # metro station / landmark name
    elif district:
        head = district     # city district, e.g. "Святошинський район"
    else:
        head = ""

    parts = [p for p in (head, city) if p]
    display = ", ".join(parts)
    # Fallback to Google's formatted_address (minus country/postcode noise)
    if not display:
        display = result.get("formatted_address", "").replace(", Україна", "").strip()
    return display or "Невідома адреса", city


async def _google_call(query: str, bounds: str | None) -> list[tuple[float, float, str, str]]:
    """One Google Geocoding API request → filtered [(lat, lon, display, city), ...]."""
    global _google_cooldown_until
    params = {
        "address": query,
        "key": GOOGLE_MAPS_API_KEY,
        "language": "uk",
        "region": "ua",
        "components": "country:UA",
    }
    if bounds:
        params["bounds"] = bounds

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_GOOGLE_GEOCODE_URL, params=params,
                                   timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
    except Exception as e:
        logger.warning("Google geocode network error: %s", e)
        return []

    status = data.get("status")
    if status in ("REQUEST_DENIED", "OVER_DAILY_LIMIT", "OVER_QUERY_LIMIT"):
        # Hard failure (bad key / billing / quota / restriction). Pause Google for
        # a cooldown, then auto-retry — no redeploy needed once the cause is fixed.
        logger.error("Google geocode paused %ds, status=%s msg=%s",
                     _GOOGLE_COOLDOWN_SEC, status, data.get("error_message", ""))
        _google_cooldown_until = time.monotonic() + _GOOGLE_COOLDOWN_SEC
        return []
    if status != "OK":
        return []  # ZERO_RESULTS etc. — soft miss

    out: list[tuple[float, float, str, str]] = []
    for r in data.get("results", []):
        if not _google_is_usable(r):
            continue  # skip whole-region / country fuzzy matches
        loc = r.get("geometry", {}).get("location", {})
        lat, lon = loc.get("lat"), loc.get("lng")
        if lat is None or lon is None:
            continue
        display, city = _google_format(r)
        # Dedup by ~5 km proximity so the same street in one city isn't repeated.
        if any(haversine_km(lat, lon, u[0], u[1]) < 5.0 for u in out):
            continue
        out.append((lat, lon, display, city))
        if len(out) >= 5:
            break
    return out


async def _google_geocode(
    address: str,
    near_lat: float | None = None,
    near_lon: float | None = None,
    home_city: str | None = None,
) -> list[tuple[float, float, str, str]]:
    """
    Geocode via Google Geocoding API. Returns [(lat, lon, display, city), ...].
    Empty list on any failure (missing key, quota, network) so callers fall back
    to OSM. Restricted to Ukraine; softly biased toward a city center when known.
    """
    if not GOOGLE_MAPS_API_KEY:
        return []
    if time.monotonic() < _google_cooldown_until:
        return []  # in cooldown after a recent failure — use OSM for now

    # Soft bias (not restriction): explicit near-point > home city center.
    bias_lat, bias_lon = None, None
    if near_lat is not None and near_lon is not None:
        bias_lat, bias_lon = near_lat, near_lon
    elif home_city:
        bias_lat, bias_lon = _UA_CITIES.get(home_city.lower(), (None, None))
    bounds = None
    if bias_lat is not None:
        d = _VIEWBOX_DEG
        bounds = f"{bias_lat - d},{bias_lon - d}|{bias_lat + d},{bias_lon + d}"

    # Locality the user intends: a known city anywhere in the text, OR the last
    # comma-separated token when it's a word (covers villages/settlements not in
    # our city list, e.g. "Ділова 2, Крюківщина").
    typed_city = _intended_locality(address)

    def _matches_city(results: list) -> bool:
        if not typed_city:
            return True
        ec = typed_city.lower()
        return any(r[3] and (ec in r[3].lower() or r[3].lower() in ec) for r in results)

    def _is_coarse(results: list) -> bool:
        # locality-only result: display is just the settlement name, no street/POI
        return bool(results) and results[0][2].strip().lower() == (results[0][3] or "").strip().lower()

    # Street part = the query minus the named locality. Non-empty alphabetic content
    # means the user asked for a specific street, not just a settlement.
    street_part = address
    if typed_city:
        street_part = re.sub(re.escape(typed_city), "", address, flags=re.IGNORECASE).strip().strip(",").strip()
    has_street = bool(re.search(r"[А-Яа-яІіЇїЄєҐґ]", street_part))

    out = await _google_call(address, bounds)

    # If the bare query already landed on the typed locality, remember its coords —
    # that geographically confirms the settlement, so a later street retry near it is
    # valid even when Google labels that street by raion instead of the village name.
    anchor = None
    if typed_city and out and _matches_city(out):
        anchor = (out[0][0], out[0][1])

    # Retry with the "вулиця " prefix when the bare query: found nothing usable;
    # OR resolved to a different city than typed; OR (user named a street + city but)
    # Google only pinned the settlement centre. The prefix forces a street reading —
    # fixes nominative surnames ("Жмаченко"), streets in small settlements mis-pinned
    # to the nearest big city ("Ділова 2, Крюківщина"), and bare-locality fallbacks
    # ("Лесі Українки, Святопетрівське" → "вул. Лесі Українки, Святопетрівське").
    need_retry = (
        (not out)
        or (typed_city and not _matches_city(out))
        or (typed_city and has_street and _is_coarse(out))
    )
    if need_retry and not _STREET_PREFIXES.match(address.strip()):
        retried = await _google_call(f"вулиця {address}", bounds)
        if retried:
            rlat, rlon = retried[0][0], retried[0][1]
            accept = False
            if not out:
                accept = True
            elif _matches_city(retried):
                accept = (not _is_coarse(retried)) or _is_coarse(out)
            elif (anchor and not _is_coarse(retried)
                  and haversine_km(anchor[0], anchor[1], rlat, rlon) < 5.0
                  and (not retried[0][3]
                       or re.search(r"район|district", retried[0][3], re.IGNORECASE))):
                # Street sits on the confirmed settlement but Google tagged it by raion
                # (not the village name). Distinguish this from a street that genuinely
                # lives in a NEIGHBOURING village (which Google labels with that real
                # village name, e.g. "Тарасівка") — only relabel raion-tagged hits, and
                # keep ONLY this one result so nearby-village matches don't leak in.
                village = typed_city
                rdisp, rcity = retried[0][2], retried[0][3]
                if rcity and rcity in rdisp:
                    rdisp = rdisp.replace(rcity, village)
                elif village.lower() not in rdisp.lower():
                    rdisp = f"{rdisp}, {village}"
                retried = [(rlat, rlon, rdisp, village)]
                accept = True
                typed_city = None
            if accept:
                out = retried

    # If the user named a locality, don't silently switch it. When the street doesn't
    # exist there (renamed "Ломоносова" in Київ), Google returns the nearest real match
    # elsewhere — drop those so we report "not found" rather than a wrong settlement.
    if typed_city and out:
        ec = typed_city.lower()
        matched = [r for r in out if r[3] and (ec in r[3].lower() or r[3].lower() in ec)]
        if not matched:
            logger.info("Google %r: typed city %r not matched (got %s) — dropping",
                        address, typed_city, [r[3] for r in out])
        out = matched

    logger.info("Google geocode %r -> %d result(s)", address, len(out))
    return out


def _place_new_as_geo(place: dict) -> dict:
    """Adapt a Places API (New) place to the geocoding-result shape so we can reuse
    _google_is_usable (same address-component types)."""
    comps = [
        {"long_name": c.get("longText", ""), "types": c.get("types", [])}
        for c in place.get("addressComponents", [])
    ]
    return {
        "address_components": comps,
        "types": place.get("types", []),
        "formatted_address": place.get("formattedAddress", ""),
    }


def _places_format(place: dict) -> tuple[str, str]:
    """Build (display, city) from a Places API (New) place. Uses displayName for the
    POI/landmark label (addressComponents don't carry the place name)."""
    comps = [
        {"long_name": c.get("longText", ""), "types": c.get("types", [])}
        for c in place.get("addressComponents", [])
    ]
    route = _google_component(comps, "route")
    house = _google_component(comps, "street_number")
    district = _google_component(comps, "sublocality", "sublocality_level_1", "neighborhood")
    city = (_google_component(comps, "locality")
            or _google_component(comps, "administrative_area_level_2")
            or _google_component(comps, "administrative_area_level_1"))
    poi = (place.get("displayName") or {}).get("text", "").strip()

    if route:
        head = f"{route}, {house}".rstrip(", ") if house else route
    elif poi and poi.lower() != city.lower():
        head = poi
    elif district:
        head = district
    else:
        head = ""
    parts = [p for p in (head, city) if p]
    display = ", ".join(parts)
    return (display or place.get("formattedAddress", "") or "Невідома адреса"), city


async def _places_geocode(
    address: str,
    near_lat: float | None = None,
    near_lon: float | None = None,
    home_city: str | None = None,
) -> list[tuple[float, float, str, str]]:
    """
    Geocode via Google Places API (New) Text Search — understands "вулиця, село"
    and POIs better than the Geocoding API. Returns [(lat, lon, display, city), ...],
    or [] on any failure so the caller falls back to the Geocoding API.
    """
    global _places_cooldown_until
    if not GOOGLE_MAPS_API_KEY or time.monotonic() < _places_cooldown_until:
        return []

    body: dict = {"textQuery": address, "languageCode": "uk", "regionCode": "UA"}
    bias_lat, bias_lon = None, None
    if near_lat is not None and near_lon is not None:
        bias_lat, bias_lon = near_lat, near_lon
    elif home_city:
        bias_lat, bias_lon = _UA_CITIES.get(home_city.lower(), (None, None))
    if bias_lat is not None:
        body["locationBias"] = {"circle": {
            "center": {"latitude": bias_lat, "longitude": bias_lon},
            "radius": 50000.0,
        }}

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": ("places.location,places.formattedAddress,"
                             "places.addressComponents,places.types,places.displayName"),
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(_PLACES_TEXT_URL, json=body, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status in (401, 403, 429):
                    txt = (await resp.text())[:200]
                    logger.error("Places(New) paused %ds, http=%s %s",
                                 _GOOGLE_COOLDOWN_SEC, resp.status, txt)
                    _places_cooldown_until = time.monotonic() + _GOOGLE_COOLDOWN_SEC
                    return []
                if resp.status != 200:
                    return []
                data = await resp.json()
    except Exception as e:
        logger.warning("Places network error: %s", e)
        return []

    out: list[tuple[float, float, str, str]] = []
    for place in data.get("places", []):
        loc = place.get("location", {})
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat is None or lon is None:
            continue
        if not _google_is_usable(_place_new_as_geo(place)):
            continue
        display, city = _places_format(place)
        if any(haversine_km(lat, lon, u[0], u[1]) < 5.0 for u in out):
            continue
        out.append((lat, lon, display, city))
        if len(out) >= 5:
            break

    # Respect an explicitly named locality (same rule as the Geocoding path).
    typed_city = _intended_locality(address)
    if typed_city and out:
        ec = typed_city.lower()
        out = [r for r in out if r[3] and (ec in r[3].lower() or r[3].lower() in ec)]

    if out:
        logger.info("Places geocode %r -> %d result(s)", address, len(out))
    return out


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

    Order: Places API (primary) → Geocoding API (fallback) → OSM (off by default).
    """
    places = await _places_geocode(address, near_lat, near_lon, home_city)
    if places:
        return places

    # Geocoding API fallback — resolves cities reliably with its retry/anchor logic.
    google = await _google_geocode(address, near_lat, near_lon, home_city)
    if google:
        return google

    if not _OSM_ACTIVE:
        return []  # OSM fallback off — show "not found" instead of fuzzy villages

    loop = asyncio.get_event_loop()

    city_name, city_coords, street_only = _detect_city(address)

    # Dynamic detection: "м. Бориспіль", "місто Васильків", etc.
    # _dyn_cleaned = address without the "місто CityName" prefix (street-only part)
    _dyn_city: str | None = None
    _dyn_cleaned: str = address
    if not city_coords:
        _dyn_city, _dyn_cleaned = _extract_city_from_text(address)
        if _dyn_city:
            city_coords = await _geocode_city(_dyn_city)

    # Did the user explicitly name a city anywhere in the query?
    explicit_city = city_name or _dyn_city

    # User explicitly named a city → pin the result to that city and skip the
    # multi-city disambiguation picker entirely. Without this a query like
    # "Святошинська, 10, Київ" would still surface the same street from Вишневе,
    # Глеваха, etc., forcing a pointless "оберіть місто" prompt.
    if explicit_city:
        street_q = _dyn_cleaned if _dyn_city else (street_only or address)
        loc = await _geocode_in_city(street_q, explicit_city)
        if loc:
            a = loc.raw.get("address", {})
            return [(
                loc.latitude, loc.longitude,
                _inject_housenumber(street_q, _format_address(loc.raw)),
                _city_of(a) or explicit_city,
            )]
        # In-city lookup failed — fall through to the broad search below.

    # No city typed, but the user has a saved home city → default to it and skip
    # the multi-city picker. OSM often has the same street+house in tiny villages
    # near a metro (e.g. "Оболонська 25" exists in both Київ and village Новосілки),
    # which produced a confusing "оберіть місто" prompt. Typing an explicit city
    # still overrides this via the branch above.
    if home_city and not explicit_city:
        loc = await _geocode_in_city(address, home_city)
        if loc:
            a = loc.raw.get("address", {})
            return [(
                loc.latitude, loc.longitude,
                _inject_housenumber(address, _format_address(loc.raw)),
                _city_of(a) or home_city,
            )]
        # home_city has no such street — fall through to the broad search.

    # Bias center: explicit city > current location > user's saved home city.
    vb_lat, vb_lon = None, None
    if city_coords:
        vb_lat, vb_lon = city_coords
    elif near_lat is not None and near_lon is not None:
        vb_lat, vb_lon = near_lat, near_lon
    elif home_city:
        vb_lat, vb_lon = _UA_CITIES.get(home_city.lower(), (None, None))
        if vb_lat is None:
            # home_city not in the hardcoded list — geocode it for a viewbox
            coords = await _geocode_city(home_city)
            if coords:
                vb_lat, vb_lon = coords

    def _kwargs() -> dict:
        k: dict = {
            "country_codes": "ua",
            "language": "uk",
            "addressdetails": True,
            "exactly_one": False,
            "limit": 20,   # high limit so dedup can still surface up to 5 distinct cities
        }
        if vb_lat is not None:
            k["viewbox"] = [
                (vb_lat + _VIEWBOX_DEG, vb_lon - _VIEWBOX_DEG),
                (vb_lat - _VIEWBOX_DEG, vb_lon + _VIEWBOX_DEG),
            ]
            k["bounded"] = False
        return k

    async def _search(query: str) -> list:
        try:
            r = await loop.run_in_executor(
                None, lambda: _geocoder.geocode(query, **_kwargs())
            ) or []
        except (GeocoderTimedOut, GeocoderServiceError):
            return []
        return r if isinstance(r, list) else [r]

    # Collect raw results from several query variants to maximise city coverage.
    # Both the bare query and the "вулиця "-prefixed form (handles genitive street
    # names like "Головатого" that Nominatim otherwise misranks).
    raw = await _search(address)
    raw += await _search("вулиця " + address)
    if _dyn_city and _dyn_cleaned != address:
        raw += await _search(f"{_dyn_cleaned}, {_dyn_city}")

    # Dedup by geographic proximity (~5 km) rather than by city string. This collapses
    # near-duplicates that share coordinates ("Боярка" / "Боярська міська громада") while
    # keeping genuinely different cities apart — and yields more distinct city buttons.
    unique: list[tuple[float, float, str, str]] = []

    def _add(lat: float, lon: float, formatted: str, city: str, *, front: bool = False) -> bool:
        if not formatted or formatted == "Невідома адреса":
            return False
        for u_lat, u_lon, _, _ in unique:
            if haversine_km(lat, lon, u_lat, u_lon) < 5.0:
                return False
        item = (lat, lon, formatted, city)
        unique.insert(0, item) if front else unique.append(item)
        return True

    for loc in raw:
        if len(unique) >= 5:
            break
        _add(loc.latitude, loc.longitude, _format_address(loc.raw),
             _city_of(loc.raw.get("address", {})))

    # Enrich a single result with a house number via structured search.
    known_city = explicit_city or (home_city if (len(unique) == 1 and unique[0][3]) else None)
    if len(unique) == 1 and known_city:
        street_q = _dyn_cleaned if _dyn_city else (street_only if city_name else address)
        dict_loc = await _geocode_dict(street_q, known_city)
        if dict_loc and dict_loc.raw.get("address", {}).get("house_number"):
            a = dict_loc.raw.get("address", {})
            unique[0] = (dict_loc.latitude, dict_loc.longitude,
                         _format_address(dict_loc.raw), _city_of(a) or unique[0][3])

    # Structured dict fallback with the dynamically extracted city.
    if not unique and _dyn_city and _dyn_cleaned != address:
        dict_loc = (await _geocode_dict(_dyn_cleaned, _dyn_city)
                    or await _geocode_dict("вулиця " + _dyn_cleaned, _dyn_city))
        if dict_loc:
            a = dict_loc.raw.get("address", {})
            _add(dict_loc.latitude, dict_loc.longitude,
                 _format_address(dict_loc.raw), _city_of(a) or _dyn_city)

    # Guarantee the user's saved home city is offered (and shown first) when they did
    # NOT name a city — for a bare street this is the strongest signal we have.
    if home_city and not explicit_city:
        hc = home_city.lower()
        if not any(c and hc in c.lower() for _, _, _, c in unique):
            hc_loc = await _geocode_in_city(address, home_city)
            if hc_loc:
                a = hc_loc.raw.get("address", {})
                _add(hc_loc.latitude, hc_loc.longitude,
                     _inject_housenumber(address, _format_address(hc_loc.raw)),
                     _city_of(a) or home_city, front=True)

    # Fallback to Kyiv when no city was named and the user has no saved home city.
    if not explicit_city and not home_city and len(unique) < 5:
        if not any(c and "київ" in c.lower() for _, _, _, c in unique):
            kyiv_loc = await _geocode_in_city(address, "київ")
            if kyiv_loc:
                a = kyiv_loc.raw.get("address", {})
                _add(kyiv_loc.latitude, kyiv_loc.longitude,
                     _inject_housenumber(address, _format_address(kyiv_loc.raw)),
                     _city_of(a) or "Київ", front=True)

    # Photon fallback — fuzzy matching catches typos and abbreviations Nominatim misses.
    if not unique:
        _photon_q = f"{_dyn_cleaned}, {_dyn_city}" if (_dyn_city and _dyn_cleaned != address) else address
        for p_lat, p_lon, p_display, p_city in await _photon_geocode(_photon_q, vb_lat, vb_lon):
            if len(unique) >= 5:
                break
            _add(p_lat, p_lon, _inject_housenumber(address, p_display), p_city)

    # Inject user-typed house number into the primary result if OSM omitted it.
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
    0. Places API (primary) → Geocoding API (fallback) → OSM (off by default).
    """
    # Places API first.
    places = await _places_geocode(address, near_lat, near_lon)
    if places:
        lat, lon, display, _city = places[0]
        return lat, lon, display

    # Geocoding API fallback.
    google = await _google_geocode(address, near_lat, near_lon)
    if google:
        lat, lon, display, _city = google[0]
        return lat, lon, display

    if not _OSM_ACTIVE:
        return None  # OSM fallback off — report "not found" rather than a fuzzy match

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
            return _city_of(loc.raw["address"])
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
