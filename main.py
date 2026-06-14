import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN
from database.database import init_db, AsyncSessionLocal
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
            return await handler(event, data)


async def main() -> None:
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Register session middleware
    dp.update.middleware(DbSessionMiddleware())

    # Register routers
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

    # Scheduled tasks
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        auto_close_expired_trips,
        "interval",
        minutes=5,
        args=[bot],
    )
    scheduler.add_job(
        send_rating_prompts,
        "interval",
        minutes=1,
        args=[bot],
    )
    scheduler.start()

    logger.info("Bot started")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
