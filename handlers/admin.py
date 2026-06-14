from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database.models import User, Trip, Match, SupportTicket, FAQ, Rating
from keyboards.keyboards import admin_main_kb, admin_user_actions_kb
from states.states import AdminStates
from config import ADMIN_IDS

router = Router()


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@router.message(Command("admin"))
async def admin_panel(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    await message.answer("🛠 <b>Панель адміністратора</b>", parse_mode="HTML", reply_markup=admin_main_kb())


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return

    users_total = await session.scalar(select(func.count()).select_from(User))
    trips_total = await session.scalar(select(func.count()).select_from(Trip))
    confirmed = await session.scalar(
        select(func.count()).select_from(Match).where(Match.status == "CONFIRMED")
    )
    tickets = await session.scalar(
        select(func.count()).select_from(SupportTicket).where(SupportTicket.is_read == False)
    )

    ratings_result = await session.execute(select(User.rating))
    all_ratings = ratings_result.scalars().all()
    avg_rating = sum(all_ratings) / len(all_ratings) if all_ratings else 0

    await callback.message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Користувачів: {users_total}\n"
        f"🚗 Поїздок: {trips_total}\n"
        f"✅ Успішних збігів: {confirmed}\n"
        f"⭐ Середній рейтинг: {avg_rating:.2f}\n"
        f"📩 Нових звернень: {tickets}",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return

    result = await session.execute(
        select(User).order_by(User.created_at.desc()).limit(20)
    )
    users = result.scalars().all()

    text = "👥 <b>Останні 20 користувачів:</b>\n\n"
    for u in users:
        blocked = "🚫" if u.is_blocked else "✅"
        text += (
            f"{blocked} {u.first_name} (@{u.username or '—'}) "
            f"[ID: {u.id}] ⭐{u.rating:.1f} | поїздок: {u.trips_count}\n"
        )

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔍 Знайти за ID", callback_data="admin:find_user"))
    builder.row(InlineKeyboardButton(text="↩️ Назад", callback_data="admin:back"))

    await callback.message.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin:trips")
async def admin_trips(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return

    result = await session.execute(
        select(Trip).where(Trip.status.in_(["ACTIVE", "MATCHING", "CONFIRMED"]))
        .order_by(Trip.departure_time.asc()).limit(20)
    )
    trips = result.scalars().all()

    if not trips:
        await callback.answer("Немає активних поїздок.", show_alert=True)
        return

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    for trip in trips:
        role_emoji = "🚗" if trip.role == "driver" else "🙋"
        text = (
            f"{role_emoji} #{trip.id} | {trip.from_address.split(',')[0]} → "
            f"{trip.to_address.split(',')[0]}\n"
            f"🕒 {trip.departure_time.strftime('%d.%m %H:%M')} | "
            f"💰 {trip.price:.0f} грн | 📊 {trip.status}\n"
            f"👤 User ID: {trip.user_id}"
        )
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="🗑 Закрити поїздку", callback_data=f"admin_close_trip:{trip.id}"
            )
        )
        await callback.message.answer(text, reply_markup=builder.as_markup())

    await callback.answer()


@router.callback_query(F.data.startswith("admin_close_trip:"))
async def admin_close_trip(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return
    trip_id = int(callback.data.split(":")[1])
    trip = await session.get(Trip, trip_id)
    if trip:
        trip.status = "CLOSED"
        await session.commit()
    await callback.message.edit_text(f"✅ Поїздку #{trip_id} закрито.")
    await callback.answer()


@router.callback_query(F.data == "admin:tickets")
async def admin_tickets(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return

    result = await session.execute(
        select(SupportTicket).where(SupportTicket.is_read == False)
        .order_by(SupportTicket.created_at.desc()).limit(10)
    )
    tickets = result.scalars().all()

    if not tickets:
        await callback.answer("Немає нових звернень.", show_alert=True)
        return

    for ticket in tickets:
        type_labels = {
            "bug": "🐞 Проблема",
            "improvement": "💡 Покращення",
            "feedback": "⭐ Відгук",
            "contact": "📞 Звернення",
        }
        text = (
            f"📩 <b>Звернення #{ticket.id}</b>\n"
            f"Тип: {type_labels.get(ticket.type, ticket.type)}\n"
            f"User ID: {ticket.user_id}\n"
            f"Дата: {ticket.created_at.strftime('%d.%m.%Y %H:%M')}\n\n"
            f"{ticket.message}"
        )
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton

        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="✅ Прочитано", callback_data=f"admin_read_ticket:{ticket.id}"
            )
        )
        await callback.message.answer(text, parse_mode="HTML", reply_markup=builder.as_markup())

    await callback.answer()


@router.callback_query(F.data.startswith("admin_read_ticket:"))
async def admin_read_ticket(callback: CallbackQuery, session: AsyncSession) -> None:
    ticket_id = int(callback.data.split(":")[1])
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket:
        ticket.is_read = True
        await session.commit()
    await callback.message.edit_text(f"✅ Звернення #{ticket_id} позначено як прочитане.")
    await callback.answer()


@router.callback_query(F.data == "admin:rejections")
async def admin_rejections(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return

    result = await session.execute(
        select(Match.rejection_reason, func.count().label("cnt"))
        .where(Match.status == "REJECTED", Match.rejection_reason != None)
        .group_by(Match.rejection_reason)
        .order_by(func.count().desc())
    )
    rows = result.all()

    if not rows:
        await callback.answer("Немає даних про відмови.", show_alert=True)
        return

    text = "📉 <b>Причини відмов:</b>\n\n"
    for reason, count in rows:
        text += f"• {reason}: {count} разів\n"

    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin:faq")
async def admin_faq(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return

    result = await session.execute(select(FAQ).order_by(FAQ.order_idx.asc()))
    faqs = result.scalars().all()

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    builder = InlineKeyboardBuilder()
    for faq in faqs:
        builder.row(
            InlineKeyboardButton(
                text=f"✏️ {faq.question[:40]}...", callback_data=f"admin_edit_faq:{faq.id}"
            )
        )
    builder.row(InlineKeyboardButton(text="➕ Додати питання", callback_data="admin_add_faq"))

    await callback.message.answer(
        "❓ <b>FAQ Management</b>", parse_mode="HTML", reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data == "admin_add_faq")
async def admin_add_faq(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.editing_faq_question)
    await state.update_data(faq_id=None)
    await callback.message.answer("Введіть питання для FAQ:")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_edit_faq:"))
async def admin_edit_faq(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return
    faq_id = int(callback.data.split(":")[1])
    faq = await session.get(FAQ, faq_id)
    if not faq:
        await callback.answer("FAQ не знайдено.", show_alert=True)
        return
    await state.set_state(AdminStates.editing_faq_question)
    await state.update_data(faq_id=faq_id)
    await callback.message.answer(
        f"Поточне питання: <i>{faq.question}</i>\n\nВведіть нове питання (або надішліть те ж саме):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminStates.editing_faq_question, F.text)
async def admin_faq_question(message: Message, state: FSMContext) -> None:
    await state.update_data(faq_question=message.text)
    await state.set_state(AdminStates.editing_faq_answer)
    await message.answer("Введіть відповідь на це питання:")


@router.message(AdminStates.editing_faq_answer, F.text)
async def admin_faq_answer(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    faq_id = data.get("faq_id")
    question = data.get("faq_question")
    answer = message.text

    if faq_id:
        faq = await session.get(FAQ, faq_id)
        if faq:
            faq.question = question
            faq.answer = answer
    else:
        faq = FAQ(question=question, answer=answer)
        session.add(faq)

    await session.commit()
    await state.clear()
    await message.answer("✅ FAQ оновлено.")


@router.callback_query(F.data.startswith("admin_block:"))
async def admin_block_user(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return
    user_id = int(callback.data.split(":")[1])
    user = await session.get(User, user_id)
    if user:
        user.is_blocked = True
        await session.commit()
    await callback.answer(f"Користувача {user_id} заблоковано.", show_alert=True)


@router.callback_query(F.data.startswith("admin_unblock:"))
async def admin_unblock_user(callback: CallbackQuery, session: AsyncSession) -> None:
    if not _is_admin(callback.from_user.id):
        return
    user_id = int(callback.data.split(":")[1])
    user = await session.get(User, user_id)
    if user:
        user.is_blocked = False
        await session.commit()
    await callback.answer(f"Користувача {user_id} розблоковано.", show_alert=True)


@router.callback_query(F.data == "admin:back")
async def admin_back(callback: CallbackQuery) -> None:
    await callback.message.edit_text("🛠 Панель адміністратора", reply_markup=admin_main_kb())
    await callback.answer()
