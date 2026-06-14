from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database.models import User, Trip

router = Router()


@router.message(F.text == "⭐ Мій рейтинг")
async def my_rating(message: Message, session: AsyncSession) -> None:
    user = await session.get(User, message.from_user.id)
    if not user:
        await message.answer("❌ Користувача не знайдено. Напишіть /start")
        return

    total = await session.scalar(
        select(func.count()).select_from(Trip).where(Trip.user_id == user.id)
    )

    stars = "⭐" * round(user.rating)
    await message.answer(
        f"⭐ <b>Мій рейтинг</b>\n\n"
        f"🌟 Ваш рейтинг: <b>{user.rating:.1f} / 5.0</b> {stars}\n"
        f"🚗 Поїздок всього: {total or 0}\n"
        f"✅ Успішних: {user.successful_trips}\n"
        f"❌ Неуспішних: {user.failed_trips}",
        parse_mode="HTML",
    )
