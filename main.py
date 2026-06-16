import asyncio
import hashlib
import hmac
import json as _json
import logging
import os
import urllib.parse as _urlparse
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, REDIS_URL
from database.database import init_db, AsyncSessionLocal
from database.models import DriverLocation, PassengerLocation
from handlers import start, driver, passenger, announcements, my_trips, rating, support, faq, admin, matching, search, trip_actions
from services.notifications import auto_close_expired_trips, send_rating_prompts, send_trip_reminders
from admin.routes import setup_admin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        async with AsyncSessionLocal() as session:
            data["session"] = session
            # Auto-create user on first interaction
            # event is Update — from_user lives inside message/callback_query/etc.
            tg_user = None
            for attr in ("message", "callback_query", "edited_message", "channel_post"):
                sub = getattr(event, attr, None)
                if sub and getattr(sub, "from_user", None):
                    tg_user = sub.from_user
                    break
            if tg_user and tg_user.id:
                from database.models import User
                user = await session.get(User, tg_user.id)
                if not user:
                    session.add(User(
                        id=tg_user.id,
                        username=tg_user.username,
                        first_name=tg_user.first_name or "",
                    ))
                    await session.commit()
            return await handler(event, data)


# ── HTTP API for Mini App ──────────────────────────────────────────────────────

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Telegram-Init-Data",
}


def _verify_init_data(init_data: str, bot_token: str) -> int | None:
    """Validate Telegram WebApp initData HMAC. Returns user_id or None."""
    try:
        params = dict(_urlparse.parse_qsl(init_data, keep_blank_values=True))
        received = params.pop("hash", None)
        if not received:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, received):
            return None
        user = _json.loads(params.get("user", "{}"))
        return int(user.get("id", 0)) or None
    except Exception:
        return None


async def handle_location(request: web.Request) -> web.Response:
    """GET — return latest GPS for both driver and passenger."""
    try:
        match_id = int(request.match_info["match_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "invalid match_id"}, status=400)

    async with AsyncSessionLocal() as session:
        dloc = await session.get(DriverLocation, match_id)
        ploc = await session.get(PassengerLocation, match_id)

    result = {}
    if dloc:
        result["driver"] = {"lat": dloc.lat, "lon": dloc.lon,
                            "updated_at": dloc.updated_at.isoformat()}
    if ploc:
        result["passenger"] = {"lat": ploc.lat, "lon": ploc.lon,
                               "updated_at": ploc.updated_at.isoformat()}

    if not result:
        return web.json_response({"error": "not found"}, status=404,
                                 headers=_CORS)
    return web.json_response(result, headers=_CORS)


async def handle_location_post(request: web.Request) -> web.Response:
    """POST — Mini App sends its GPS coordinates."""
    try:
        match_id = int(request.match_info["match_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "invalid"}, status=400, headers=_CORS)

    try:
        body = await request.json()
        lat, lon = float(body["lat"]), float(body["lon"])
    except Exception:
        return web.json_response({"error": "bad body"}, status=400, headers=_CORS)

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    user_id = _verify_init_data(init_data, BOT_TOKEN)
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401, headers=_CORS)

    async with AsyncSessionLocal() as session:
        from database.models import Match, Trip
        match = await session.get(Match, match_id)
        if not match or match.status != "CONFIRMED":
            return web.json_response({"error": "not found"}, status=404, headers=_CORS)

        driver_trip = await session.get(Trip, match.driver_trip_id)
        passenger_trip = await session.get(Trip, match.passenger_trip_id)
        now = datetime.utcnow()

        if driver_trip.user_id == user_id:
            loc = await session.get(DriverLocation, match_id)
            if loc:
                loc.lat, loc.lon, loc.updated_at = lat, lon, now
            else:
                session.add(DriverLocation(match_id=match_id, lat=lat, lon=lon))
        elif passenger_trip.user_id == user_id:
            loc = await session.get(PassengerLocation, match_id)
            if loc:
                loc.lat, loc.lon, loc.updated_at = lat, lon, now
            else:
                session.add(PassengerLocation(match_id=match_id, lat=lat, lon=lon))
        else:
            return web.json_response({"error": "forbidden"}, status=403, headers=_CORS)

        await session.commit()

    return web.json_response({"ok": True}, headers=_CORS)


async def handle_preflight(request: web.Request) -> web.Response:
    return web.Response(headers=_CORS)


async def handle_debug(request: web.Request) -> web.Response:
    from config import WEBAPP_URL, API_URL
    import urllib.parse
    test_url = WEBAPP_URL.rstrip("/") + "/?" + urllib.parse.urlencode({
        "mode": "single", "from_lat": 50.45, "from_lon": 30.52,
        "to_lat": 50.40, "to_lon": 30.55, "role": "driver",
    }) if WEBAPP_URL else "(not set)"
    return web.Response(text=(
        f"WEBAPP_URL={repr(WEBAPP_URL)}\n"
        f"API_URL={repr(API_URL)}\n\n"
        f"Generated URL:\n{test_url}"
    ), content_type="text/plain")


def build_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/location/{match_id}", handle_location)
    app.router.add_post("/location/{match_id}", handle_location_post)
    app.router.add_options("/location/{match_id}", handle_preflight)
    app.router.add_get("/debug", handle_debug)
    setup_admin(app)
    return app


# ── Bot ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    if REDIS_URL:
        storage = RedisStorage.from_url(REDIS_URL)
        logger.info("FSM storage: Redis")
    else:
        storage = MemoryStorage()
        logger.info("FSM storage: Memory (no REDIS_URL set)")

    dp = Dispatcher(storage=storage)

    dp.update.middleware(DbSessionMiddleware())

    dp.include_router(start.router)
    dp.include_router(driver.router)
    dp.include_router(passenger.router)
    dp.include_router(search.router)
    dp.include_router(announcements.router)
    dp.include_router(my_trips.router)
    dp.include_router(matching.router)
    dp.include_router(trip_actions.router)
    dp.include_router(rating.router)
    dp.include_router(support.router)
    dp.include_router(faq.router)
    dp.include_router(admin.router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_close_expired_trips, "interval", minutes=5, args=[bot])
    scheduler.add_job(send_rating_prompts, "interval", minutes=1, args=[bot])
    scheduler.add_job(send_trip_reminders, "interval", minutes=1, args=[bot])
    scheduler.start()

    # Start HTTP API server
    port = int(os.getenv("PORT", 8080))
    web_app = build_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"API server started on port {port}")

    logger.info("Bot started")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
