import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN
from database.database import init_db, AsyncSessionLocal
from database.models import DriverLocation
from handlers import start, driver, passenger, announcements, my_trips, rating, support, faq, admin, matching
from services.notifications import auto_close_expired_trips, send_rating_prompts

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

async def handle_location(request: web.Request) -> web.Response:
    """Return driver's current live location for a given match."""
    try:
        match_id = int(request.match_info["match_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "invalid match_id"}, status=400)

    async with AsyncSessionLocal() as session:
        loc = await session.get(DriverLocation, match_id)
        if not loc:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(
            {
                "lat": loc.lat,
                "lon": loc.lon,
                "updated_at": loc.updated_at.isoformat(),
            },
            headers={"Access-Control-Allow-Origin": "*"},
        )


async def handle_preflight(request: web.Request) -> web.Response:
    return web.Response(
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
        }
    )


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
    app.router.add_options("/location/{match_id}", handle_preflight)
    app.router.add_get("/debug", handle_debug)
    return app


# ── Bot ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.middleware(DbSessionMiddleware())

    dp.include_router(start.router)
    dp.include_router(driver.router)
    dp.include_router(passenger.router)
    dp.include_router(announcements.router)
    dp.include_router(my_trips.router)
    dp.include_router(matching.router)
    dp.include_router(rating.router)
    dp.include_router(support.router)
    dp.include_router(faq.router)
    dp.include_router(admin.router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_close_expired_trips, "interval", minutes=5, args=[bot])
    scheduler.add_job(send_rating_prompts, "interval", minutes=1, args=[bot])
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
