from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from database.models import Trip, User, Match
from keyboards.keyboards import offer_trip_kb, confirm_send_offer_kb, seats_picker_kb, main_menu_kb
from services.matching import create_match, get_remaining_seats
from services.notifications import notify_new_match
from services.rich_cards import send_trip_card

router = Router()

TRIPS_PER_PAGE = 5


def _format_trip_card(trip: Trip, user: User) -> str:
    role_emoji = "🚗" if trip.role == "driver" else "🙋"
    role_label = "Водій" if trip.role == "driver" else "Шукаю поїздку"
    price_label = f"{trip.price:.0f} грн" if trip.role == "driver" else f"до {trip.price:.0f} грн"
    seats_label = f"💺 {trip.seats} місць" if trip.role == "driver" else f"👥 {trip.seats} пас."
    rating_label = f"{user.rating:.1f}" if user.rating is not None else "Без рейтингу"

    return (
        f"{role_emoji} <b>{role_label}</b> | "
        f"{', '.join(trip.from_address.split(',')[:2]).strip()} → {', '.join(trip.to_address.split(',')[:2]).strip()}\n"
        f"🕒 {trip.departure_time.strftime('%d.%m.%Y %H:%M')} | "
        f"💰 {price_label} | {seats_label}\n"
        f"⭐ Рейтинг: {rating_label} | 🔄 У пошуку"
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


ACTIVE_STATUSES = ("ACTIVE", "MATCHING", "BOARDING")


@router.callback_query(F.data.startswith("offer_trip:"))
async def offer_trip_start(callback: CallbackQuery, session: AsyncSession) -> None:
    trip_id = int(callback.data.split(":")[1])
    trip = await session.get(Trip, trip_id, options=[selectinload(Trip.user)])

    if not trip or trip.status not in ACTIVE_STATUSES:
        await callback.answer("Це оголошення вже неактуальне.", show_alert=True)
        return

    if trip.user_id == callback.from_user.id:
        await callback.answer("Це ваше власне оголошення.", show_alert=True)
        return

    # For driver trips: ask how many seats the passenger needs
    if trip.role == "driver":
        remaining = await get_remaining_seats(trip, session)
        if remaining <= 0:
            await callback.answer("На жаль, всі місця вже заброньовано.", show_alert=True)
            return
        await callback.message.answer(
            f"💺 Скільки місць вам потрібно?\n"
            f"(доступно: {remaining})",
            reply_markup=seats_picker_kb(trip_id, remaining),
        )
        await callback.answer()
        return

    # For passenger trips (driver offering) — go straight to confirmation
    await send_trip_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        trip=trip,
        user=trip.user,
        extra_text="Надіслати пропозицію користувачу?",
        reply_markup=confirm_send_offer_kb(trip_id),
    )
    await callback.answer()


@router.callback_query(F.data == "offer_seats_cancel")
async def offer_seats_cancel(callback: CallbackQuery) -> None:
    await callback.message.delete()
    await callback.answer("Скасовано.")


@router.callback_query(F.data.startswith("offer_seats:"))
async def offer_seats_chosen(callback: CallbackQuery, session: AsyncSession) -> None:
    """Passenger selected seat count → show trip card for confirmation."""
    _, trip_id_str, seats_str = callback.data.split(":")
    trip_id, seats = int(trip_id_str), int(seats_str)
    trip = await session.get(Trip, trip_id, options=[selectinload(Trip.user)])

    if not trip or trip.status not in ACTIVE_STATUSES:
        await callback.answer("Оголошення вже неактуальне.", show_alert=True)
        return

    remaining = await get_remaining_seats(trip, session)
    if seats > remaining:
        await callback.answer(
            f"Доступно лише {remaining} місць. Оберіть меншу кількість.",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        f"✅ Обрано: {seats} місць\n\nПідтвердіть відправку пропозиції:",
        reply_markup=confirm_send_offer_kb(trip_id, seats=seats),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("offer_yes:"))
async def offer_yes(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = callback.data.split(":")
    trip_id = int(parts[1])
    seats_wanted = int(parts[2]) if len(parts) > 2 else 1

    target_trip = await session.get(Trip, trip_id, options=[selectinload(Trip.user)])

    if not target_trip or target_trip.status not in ACTIVE_STATUSES:
        await callback.answer("Оголошення вже неактуальне.", show_alert=True)
        return

    # Double-check remaining seats before creating match
    if target_trip.role == "driver":
        remaining = await get_remaining_seats(target_trip, session)
        if seats_wanted > remaining:
            await callback.answer(
                f"Залишилось лише {remaining} місць.", show_alert=True
            )
            return

    initiator_role = "driver" if target_trip.role == "passenger" else "passenger"
    result = await session.execute(
        select(Trip).where(
            Trip.user_id == callback.from_user.id,
            Trip.role == initiator_role,
            Trip.status.in_(list(ACTIVE_STATUSES)),
        ).order_by(Trip.created_at.desc()).limit(1)
    )
    initiator_trip = result.scalars().first()

    # Auto-create shadow trip with the correct seat count
    if not initiator_trip:
        initiator_trip = Trip(
            user_id=callback.from_user.id,
            role=initiator_role,
            from_lat=target_trip.from_lat,
            from_lon=target_trip.from_lon,
            from_address=target_trip.from_address,
            to_lat=target_trip.to_lat,
            to_lon=target_trip.to_lon,
            to_address=target_trip.to_address,
            departure_time=target_trip.departure_time,
            price=target_trip.price,
            seats=seats_wanted,
            status="MATCHING",
        )
        session.add(initiator_trip)
        await session.flush()

    driver_trip = initiator_trip if initiator_role == "driver" else target_trip
    passenger_trip = target_trip if initiator_role == "driver" else initiator_trip

    match = await create_match(driver_trip, passenger_trip, session)
    if not match:
        await callback.answer("Пропозицію вже надіслано.", show_alert=True)
        return

    # Pre-confirm initiator's side — one tap from the target is enough to close the deal
    if initiator_role == "driver":
        match.driver_confirmed = True
    else:
        match.passenger_confirmed = True
    await session.commit()

    if initiator_role == "passenger":
        intro = f"🙋 Пасажир хоче поїхати з вами ({seats_wanted} місць)!"
    else:
        intro = "🚗 Водій хоче взяти вас попутником по своєму маршруту!"
    await notify_new_match(bot, target_trip, initiator_trip, match, intro=intro)

    await callback.message.edit_text(
        "✅ Пропозицію надіслано. Очікуємо відповіді від водія."
        if initiator_role == "passenger" else
        "✅ Пропозицію надіслано. Очікуємо відповіді від пасажира."
    )
    await callback.answer()


@router.callback_query(F.data == "offer_no")
async def offer_no(callback: CallbackQuery) -> None:
    await callback.message.delete()
    await callback.answer("Скасовано.")
