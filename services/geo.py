import math
import asyncio
from typing import Optional
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

from config import NOMINATIM_UA

_geocoder = Nominatim(user_agent=NOMINATIM_UA, timeout=10)

# Viewbox half-size in degrees (~50 km)
_VIEWBOX_DEG = 0.5


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

    # Build viewbox from context point: (west, north, east, south)
    viewbox = None
    if near_lat is not None and near_lon is not None:
        viewbox = [
            (near_lon - _VIEWBOX_DEG, near_lat + _VIEWBOX_DEG),
            (near_lon + _VIEWBOX_DEG, near_lat - _VIEWBOX_DEG),
        ]

    async def _geocode(vb, bounded):
        kwargs = {"country_codes": "ua", "language": "uk"}
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
            return loc.latitude, loc.longitude, loc.address

    # 2) Try biased but not strictly bounded (prefers the area)
    if viewbox:
        loc = await _geocode(viewbox, bounded=False)
        if loc:
            return loc.latitude, loc.longitude, loc.address

    # 3) Country-wide fallback
    loc = await _geocode(None, False)
    if loc:
        return loc.latitude, loc.longitude, loc.address

    return None


async def reverse_geocode(lat: float, lon: float) -> str:
    """Return human-readable address for coordinates."""
    loop = asyncio.get_event_loop()
    try:
        location = await loop.run_in_executor(
            None, lambda: _geocoder.reverse((lat, lon), language="uk")
        )
        if location:
            return location.address
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
