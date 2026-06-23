import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_IDS: list[int] = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]
WEBAPP_URL: str = os.getenv("WEBAPP_URL", "")
DB_PATH: str = os.getenv("DB_PATH", "rideshare.db")
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
REDIS_URL: str = os.getenv("REDIS_URL", "")
NOMINATIM_UA: str = os.getenv("NOMINATIM_UA", "rideshare_bot_v1")

# Google Geocoding API key — primary geocoder for manual address input.
GOOGLE_MAPS_API_KEY: str = os.getenv("GOOGLE_MAPS_API_KEY", "")

# OSM (Nominatim/Photon) fallback. Disabled by default: when Google finds nothing
# usable we'd rather show "address not found" than OSM's fuzzy village matches
# (e.g. Щекавицька 34 → Чайки). Flip to True (here or via env) to re-enable OSM,
# e.g. if Google quota/billing fails and you need geocoding to keep working.
OSM_FALLBACK_ENABLED: bool = os.getenv("OSM_FALLBACK_ENABLED", "false").lower() in (
    "1", "true", "yes", "on",
)

# Public URL of this service (Railway sets RAILWAY_PUBLIC_DOMAIN automatically)
_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
API_URL: str = f"https://{_domain}" if _domain else os.getenv("API_URL", "")

MATCH_RADIUS_KM: float = 10.0
MATCH_TIME_DELTA_HOURS: float = 2.0
MAX_ACTIVE_TRIPS: int = 2
RATING_DELAY_MINUTES: int = 5

ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "changeme")
