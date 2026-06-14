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
NOMINATIM_UA: str = os.getenv("NOMINATIM_UA", "rideshare_bot_v1")

# Public URL of this service (Railway sets RAILWAY_PUBLIC_DOMAIN automatically)
_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
API_URL: str = f"https://{_domain}" if _domain else os.getenv("API_URL", "")

MATCH_RADIUS_KM: float = 3.0
MATCH_TIME_DELTA_HOURS: float = 1.0
MAX_ACTIVE_TRIPS: int = 2
RATING_DELAY_MINUTES: int = 5
