from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import SupportTicket
from keyboards.keyboards import support_type_kb, main_menu_kb, cancel_kb
from services import bot_settings as _s
from states.states import SupportStates
from config import ADMIN_IDS

router = Router()

TYPE_LABELS = {
    "bug": "🐞 Проблема",
    "improvement": "💡 Покращення",
    "feedback": "⭐ Відгук",
    "contact": "📞 Звернення до підтримки",
}


@router.message(F.text.func(lambda t: t == _s.get("btn_support")))
async def support_menu(message: Message) -> None:
    await message.answer(
        "🛟 <b>Підтримка</b>\n\nОберіть тип звернення:",
        parse_mode="HTML",
        reply_markup=support_type_kb(),
    )


@router.callback_query(F.data.startswith("support:"))
async def support_type_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    ticket_type = callback.data.split(":")[1]
    await state.update_data(ticket_type=ticket_type)
    await state.set_state(SupportStates.writing_message)

    await callback.message.answer(
        f"📝 <b>{TYPE_LABELS.get(ticket_type, 'Звернення')}</b>\n\nНапишіть ваше повідомлення:",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(SupportStates.writing_message, F.text)
async def support_message(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    data = await state.get_data()
    ticket_type = data.get("ticket_type", "contact")

    ticket = SupportTicket(
        user_id=message.from_user.id,
        type=ticket_type,
        message=message.text,
    )
    session.add(ticket)
    await session.commit()

    await state.clear()
    await message.answer(
        "Дякуємо. Ваше повідомлення надіслано і буде розглянуто.",
        reply_markup=main_menu_kb(),
    )

    # Notify admins
    user = message.from_user
    admin_text = (
        f"📩 <b>Нове звернення #{ticket.id}</b>\n"
        f"Тип: {TYPE_LABELS.get(ticket_type)}\n"
        f"Від: {user.first_name} (@{user.username or '—'}) [ID: {user.id}]\n\n"
        f"{message.text}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text, parse_mode="HTML")
        except Exception:
            pass
