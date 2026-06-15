import math
import asyncio
from typing import Optional
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

from config import NOMINATIM_UA

_geocoder = Nominatim(user_agent=NOMINATIM_UA, timeout=10)

# Viewbox half-size in degrees (~50 km)
_VIEWBOX_DEG = 0.5


def _format_address(raw: dict) -> str:
    """
    Format Nominatim raw address dict into a short human-readable string.
    Result: "вул. Хрещатик, 22, Київ"  or  "вул. Хрещатик, 22, Шевченківський р-н, Київ"
    """
    a = raw.get("address", {}) if "address" in raw else raw

    road        = a.get("road") or a.get("pedestrian") or a.get("footway") or ""
    house       = a.get("house_number", "")
    district    = a.get("city_district") or a.get("suburb") or ""
    city        = a.get("city") or a.get("town") or a.get("municipality") or a.get("village") or ""

    parts = []
    if road:
        parts.append(f"{road}, {house}".rstrip(", ") if house else road)
    if district and not road:
        # No street info — show district so address isn't just "Київ"
        parts.append(district)
    if city:
        parts.append(city)

    return ", ".join(p for p in parts if p) or city or "Невідома адреса"


async def geocode_address(
    address: str,
    near_lat: float | None = None,
    near_lon: float | None = None,
) -> Optional[tuple[float, float, str]]:
    """
    Return (lat, lon, display_name) or None.

    If near_lat/near_lon are provided (e.g. the trip's starting point),
    Nominatim will prefer results inside a ±0.5° box around that point
    before falling back to a country-wide search.
    """
    loop = asyncio.get_event_loop()

    # Build viewbox from context point — geopy expects (lat, lon) pairs
    viewbox = None
    if near_lat is not None and near_lon is not None:
        viewbox = [
            (near_lat + _VIEWBOX_DEG, near_lon - _VIEWBOX_DEG),  # NW
            (near_lat - _VIEWBOX_DEG, near_lon + _VIEWBOX_DEG),  # SE
        ]

    async def _geocode(vb, bounded):
        kwargs = {"country_codes": "ua", "language": "uk", "addressdetails": True}
        if vb:
            kwargs["viewbox"] = vb
            kwargs["bounded"] = bounded
        try:
            return await loop.run_in_executor(
                None, lambda: _geocoder.geocode(address, **kwargs)
            )
        except (GeocoderTimedOut, GeocoderServiceError):
            return None

    # 1) Try biased search inside viewbox (bounded)
    if viewbox:
        loc = await _geocode(viewbox, bounded=True)
        if loc:
            return loc.latitude, loc.longitude, _format_address(loc.raw)

    # 2) Try biased but not strictly bounded (prefers the area)
    if viewbox:
        loc = await _geocode(viewbox, bounded=False)
        if loc:
            return loc.latitude, loc.longitude, _format_address(loc.raw)

    # 3) Country-wide fallback
    loc = await _geocode(None, False)
    if loc:
        return loc.latitude, loc.longitude, _format_address(loc.raw)

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


async def reverse_geocode(lat: float, lon: float) -> str:
    """Return human-readable address for coordinates."""
    loop = asyncio.get_event_loop()
    try:
        location = await loop.run_in_executor(
            None, lambda: _geocoder.reverse((lat, lon), language="uk")
        )
        if location:
            return _format_address(location.raw)
    except (GeocoderTimedOut, GeocoderServiceError):
        pass
    return f"{lat:.5f}, {lon:.5f}"


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
