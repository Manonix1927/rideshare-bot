from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from database.models import Trip, User, Match
from keyboards.keyboards import offer_trip_kb, confirm_send_offer_kb, main_menu_kb
from services.matching import create_match
from services.notifications import notify_new_match
from services.rich_cards import send_trip_card

router = Router()

TRIPS_PER_PAGE = 5


def _format_trip_card(trip: Trip, user: User) -> str:
    role_emoji = "🚗" if trip.role == "driver" else "🙋"
    role_label = "Водій" if trip.role == "driver" else "Шукаю поїздку"
    price_label = f"{trip.price:.0f} грн" if trip.role == "driver" else f"до {trip.price:.0f} грн"
    seats_label = f"💺 {trip.seats} місць" if trip.role == "driver" else f"👥 {trip.seats} пас."

    return (
        f"{role_emoji} <b>{role_label}</b> | "
        f"{', '.join(trip.from_address.split(',')[:2]).strip()} → {', '.join(trip.to_address.split(',')[:2]).strip()}\n"
        f"🕒 {trip.departure_time.strftime('%d.%m.%Y %H:%M')} | "
        f"💰 {price_label} | {seats_label}\n"
        f"⭐ Рейтинг: {user.rating:.1f} | 🔄 У пошуку"
    )


@router.message(F.text == "📢 Всі оголошення")
async def all_announcements(message: Message, session: AsyncSession) -> None:
    driver_count = await session.scalar(
        select(func.count()).select_from(Trip).where(
            Trip.role == "driver", Trip.status.in_(["ACTIVE", "MATCHING"])
        )
    )
    passenger_count = await session.scalar(
        select(func.count()).select_from(Trip).where(
            Trip.role == "passenger", Trip.status.in_(["ACTIVE", "MATCHING"])
        )
    )

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"🚗 Водії ({driver_count})", callback_data="list:driver:0"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=f"🙋 Пасажири ({passenger_count})", callback_data="list:passenger:0"
        )
    )

    await message.answer(
        f"📢 <b>Всі оголошення</b>\n\n"
        f"🔥 Зараз активно: водіїв — {driver_count}, пасажирів — {passenger_count}",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("list:"))
async def list_trips(callback: CallbackQuery, session: AsyncSession) -> None:
    _, role, page_str = callback.data.split(":")
    page = int(page_str)
    offset = page * TRIPS_PER_PAGE

    result = await session.execute(
        select(Trip)
        .options(selectinload(Trip.user))
        .where(Trip.role == role, Trip.status.in_(["ACTIVE", "MATCHING"]))
        .order_by(Trip.departure_time.asc())
        .limit(TRIPS_PER_PAGE)
        .offset(offset)
    )
    trips = result.scalars().all()

    total = await session.scalar(
        select(func.count()).select_from(Trip).where(
            Trip.role == role, Trip.status.in_(["ACTIVE", "MATCHING"])
        )
    )

    if not trips:
        await callback.answer("Оголошень поки немає.", show_alert=True)
        return

    role_label = "Водії" if role == "driver" else "Пасажири"
    header = f"{'🚗' if role == 'driver' else '🙋'} <b>{role_label}</b> — сторінка {page + 1}\n\n"

    for trip in trips:
        if trip.user_id == callback.from_user.id:
            await send_trip_card(
                bot=callback.bot,
                chat_id=callback.message.chat.id,
                trip=trip,
                user=trip.user,
                extra_text="Ваше оголошення",
            )
        else:
            await send_trip_card(
                bot=callback.bot,
                chat_id=callback.message.chat.id,
                trip=trip,
                user=trip.user,
                reply_markup=offer_trip_kb(trip.id),
            )

    # Pagination
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️ Назад", callback_data=f"list:{role}:{page - 1}")
    if offset + TRIPS_PER_PAGE < total:
        nav.button(text="▶️ Далі", callback_data=f"list:{role}:{page + 1}")
    nav.button(text="↩️ До меню оголошень", callback_data="back_to_ann")
    nav.adjust(2)

    await callback.message.answer(
        f"Показано {offset + 1}–{min(offset + TRIPS_PER_PAGE, total)} з {total}",
        reply_markup=nav.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "back_to_ann")
async def back_to_announcements(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.message.delete()
    await all_announcements(callback.message, session)
    await callback.answer()


@router.callback_query(F.data.startswith("offer_trip:"))
async def offer_trip_start(callback: CallbackQuery, session: AsyncSession) -> None:
    trip_id = int(callback.data.split(":")[1])
    trip = await session.get(Trip, trip_id, options=[selectinload(Trip.user)])

    if not trip or trip.status not in ("ACTIVE", "MATCHING"):
        await callback.answer("Це оголошення вже неактуальне.", show_alert=True)
        return

    if trip.user_id == callback.from_user.id:
        await callback.answer("Це ваше власне оголошення.", show_alert=True)
        return

    await send_trip_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        trip=trip,
        user=trip.user,
        extra_text="Надіслати пропозицію користувачу?",
        reply_markup=confirm_send_offer_kb(trip_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("offer_yes:"))
async def offer_yes(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    trip_id = int(callback.data.split(":")[1])
    target_trip = await session.get(Trip, trip_id, options=[selectinload(Trip.user)])

    if not target_trip or target_trip.status not in ("ACTIVE", "MATCHING"):
        await callback.answer("Оголошення вже неактуальне.", show_alert=True)
        return

    # Find the initiator's active trip of opposite role
    initiator_role = "driver" if target_trip.role == "passenger" else "passenger"
    result = await session.execute(
        select(Trip).where(
            Trip.user_id == callback.from_user.id,
            Trip.role == initiator_role,
            Trip.status.in_(["ACTIVE", "MATCHING"]),
        ).order_by(Trip.created_at.desc()).limit(1)
    )
    initiator_trip = result.scalars().first()

    if not initiator_trip:
        await callback.answer(
            f"Спочатку створіть {'поїздку' if initiator_role == 'driver' else 'заявку пасажира'}!",
            show_alert=True,
        )
        return

    driver_trip = initiator_trip if initiator_role == "driver" else target_trip
    passenger_trip = target_trip if initiator_role == "driver" else initiator_trip

    match = await create_match(driver_trip, passenger_trip, session)
    if not match:
        await callback.answer("Пропозицію вже надіслано.", show_alert=True)
        return

    await notify_new_match(bot, initiator_trip, target_trip, match)
    await notify_new_match(bot, target_trip, initiator_trip, match)

    await callback.message.edit_text(
        "✅ Пропозицію надіслано. Очікуємо відповіді."
    )
    await callback.answer()


@router.callback_query(F.data == "offer_no")
async def offer_no(callback: CallbackQuery) -> None:
    await callback.message.delete()
    await callback.answer("Скасовано.")
