import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_IDS: list[int] = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]
WEBAPP_URL: str = os.getenv("WEBAPP_URL", "")
DB_PATH: str = os.getenv("DB_PATH", "rideshare.db")
NOMINATIM_UA: str = os.getenv("NOMINATIM_UA", "rideshare_bot_v1")

MATCH_RADIUS_KM: float = 3.0
MATCH_TIME_DELTA_HOURS: float = 1.0
MAX_ACTIVE_TRIPS: int = 2
RATING_DELAY_MINUTES: int = 5
