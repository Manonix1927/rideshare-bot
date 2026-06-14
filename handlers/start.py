from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User
from keyboards.keyboards import main_menu_kb

router = Router()

START_TEXT = (
    "Привіт 👋 Даний бот допомагає швидко знаходити попутників по місту та області 🚗\n\n"
    "Оберіть потрібний варіант:"
)

HOW_IT_WORKS = (
    "ℹ️ <b>Як це працює:</b>\n\n"
    "1️⃣ Створіть поїздку або заявку.\n"
    "2️⃣ Ми підберемо підходящі варіанти.\n"
    "3️⃣ Підтвердіть поїздку.\n"
    "4️⃣ Отримайте контакти попутника.\n"
    "5️⃣ Після поїздки залиште оцінку.\n\n"
    "<b>🔒 Конфіденційність:</b> контакти відкриваються лише після взаємного підтвердження поїздки."
)


async def _ensure_user(user_id: int, username: str | None, first_name: str, session: AsyncSession) -> User:
    user = await session.get(User, user_id)
    if not user:
        user = User(id=user_id, username=username, first_name=first_name)
        session.add(user)
        await session.commit()
    else:
        if user.username != username or user.first_name != first_name:
            user.username = username
            user.first_name = first_name
            await session.commit()
    return user


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await _ensure_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        session,
    )
    await message.answer(START_TEXT, reply_markup=main_menu_kb())


@router.message(F.text == "🔙 Головне меню")
async def back_to_main(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Головне меню:", reply_markup=main_menu_kb())


@router.message(F.text == "ℹ️ Як це працює")
async def how_it_works(message: Message) -> None:
    await message.answer(HOW_IT_WORKS, parse_mode="HTML")
