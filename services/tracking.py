"""Build Mini App tracking URL from a confirmed Match."""
import urllib.parse
from config import WEBAPP_URL, API_URL


def build_track_url(match, driver_user_id: int, passenger_user_id: int) -> str | None:
    if not WEBAPP_URL or not API_URL:
        return None
    d = match.driver_trip
    p = match.passenger_trip
    params = {
        "mode": "track",
        "match_id": match.id,
        "api_url": f"{API_URL}/location",
        "driver_user_id": driver_user_id,
        "passenger_user_id": passenger_user_id,
        "d_from_lat": d.from_lat, "d_from_lon": d.from_lon,
        "d_to_lat": d.to_lat,   "d_to_lon": d.to_lon,
        "p_from_lat": p.from_lat, "p_from_lon": p.from_lon,
        "p_to_lat": p.to_lat,   "p_to_lon": p.to_lon,
        "d_from_addr": d.from_address, "d_to_addr": d.to_address,
    }
    return WEBAPP_URL.rstrip("/") + "/?" + urllib.parse.urlencode(params)
