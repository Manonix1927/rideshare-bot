"""
Scheduled tasks:
  - Auto-close trips past their departure time → CLOSED
  - Send rating prompt 5 min after departure
"""
import urllib.parse
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.database import AsyncSessionLocal
from database.models import Trip, Match, User
from config import WEBAPP_URL


def _match_map_url(driver_trip: Trip, passenger_trip: Trip, role: str) -> str:
    """Build webapp URL with both driver and passenger coords."""
    if not WEBAPP_URL:
        return ""
    params = {
        "mode": "match",
        "role": role,
        "d_from_lat": driver_trip.from_lat,
        "d_from_lon": driver_trip.from_lon,
        "d_to_lat":   driver_trip.to_lat,
        "d_to_lon":   driver_trip.to_lon,
        "p_from_lat": passenger_trip.from_lat,
        "p_from_lon": passenger_trip.from_lon,
        "p_to_lat":   passenger_trip.to_lat,
        "p_to_lon":   passenger_trip.to_lon,
        "d_from_addr": driver_trip.from_address,
        "d_to_addr":   driver_trip.to_address,
        "p_from_addr": passenger_trip.from_address,
        "p_to_addr":   passenger_trip.to_address,
        "d_price":  f"{driver_trip.price:.0f}" if driver_trip.price else "",
        "d_time":   driver_trip.departure_time.strftime("%d.%m.%Y %H:%M"),
        "d_seats":  str(driver_trip.seats or ""),
    }
    return WEBAPP_URL.rstrip("/") + "/?" + urllib.parse.urlencode(params)


async def auto_close_expired_trips(bot) -> None:
    """Close all ACTIVE/MATCHING trips whose departure_time has passed."""
    now = datetime.utcnow()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Trip).where(
                Trip.status.in_(["ACTIVE", "MATCHING"]),
                Trip.departure_time <= now,
            )
        )
        trips = result.scalars().all()
        for trip in trips:
            trip.status = "CLOSED"
        await session.commit()


async def send_rating_prompts(bot) -> None:
    """5 min after departure, ask confirmed match participants whether the meeting happened."""
    from keyboards.keyboards import meeting_happened_kb

    now = datetime.utcnow()
    window_start = now - timedelta(minutes=10)
    window_end   = now - timedelta(minutes=5)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Match)
            .options(
                selectinload(Match.driver_trip).selectinload(Trip.user),
                selectinload(Match.passenger_trip).selectinload(Trip.user),
            )
            .where(Match.status == "CONFIRMED")
        )
        matches = result.scalars().all()

        for match in matches:
            driver_trip = match.driver_trip
            departure   = driver_trip.departure_time
            if not (window_start <= departure <= window_end):
                continue

            match.status = "CLOSED"
            driver_trip.status = "CLOSED"
            match.passenger_trip.status = "CLOSED"

            driver_user    = driver_trip.user
            passenger_user = match.passenger_trip.user

            text = "🤝 Чи відбулась ваша зустріч з попутником?"
            try:
                await bot.send_message(
                    driver_user.id, text,
                    reply_markup=meeting_happened_kb(match.id, "driver"),
                )
                await bot.send_message(
                    passenger_user.id, text,
                    reply_markup=meeting_happened_kb(match.id, "passenger"),
                )
            except Exception:
                pass

        await session.commit()


async def send_trip_reminders(bot) -> None:
    """10 min before departure — remind both participants with action buttons."""
    from keyboards.keyboards import confirmed_trip_driver_kb, confirmed_trip_passenger_kb
    from services.tracking import build_track_url

    now = datetime.utcnow()
    window_start = now + timedelta(minutes=9)
    window_end   = now + timedelta(minutes=11)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Match)
            .options(
                selectinload(Match.driver_trip).selectinload(Trip.user),
                selectinload(Match.passenger_trip).selectinload(Trip.user),
            )
            .where(
                Match.status == "CONFIRMED",
                Match.reminder_sent.is_(False),
            )
        )
        matches = result.scalars().all()

        for match in matches:
            departure = match.driver_trip.departure_time
            if not (window_start <= departure <= window_end):
                continue

            match.reminder_sent = True

            driver_user    = match.driver_trip.user
            passenger_user = match.passenger_trip.user

            from_addr = ", ".join(match.driver_trip.from_address.split(",")[:2]).strip()
            to_addr   = ", ".join(match.driver_trip.to_address.split(",")[:2]).strip()
            time_str  = match.driver_trip.departure_time.strftime("%H:%M")

            text = (
                f"⏰ <b>Ваша поїздка через 10 хвилин!</b>\n\n"
                f"🗺 {from_addr} → {to_addr}\n"
                f"🕒 {time_str}\n\n"
                f"Попутник: {passenger_user.first_name} ⭐{passenger_user.rating:.1f}"
            )
            passenger_text = (
                f"⏰ <b>Ваша поїздка через 10 хвилин!</b>\n\n"
                f"🗺 {from_addr} → {to_addr}\n"
                f"🕒 {time_str}\n\n"
                f"Водій: {driver_user.first_name} ⭐{driver_user.rating:.1f}"
            )

            track_url = build_track_url(match, driver_user.id, passenger_user.id)

            try:
                await bot.send_message(
                    driver_user.id,
                    text,
                    parse_mode="HTML",
                    reply_markup=confirmed_trip_driver_kb(match.id, track_url),
                )
                await bot.send_message(
                    passenger_user.id,
                    passenger_text,
                    parse_mode="HTML",
                    reply_markup=confirmed_trip_passenger_kb(match.id, track_url),
                )
            except Exception:
                pass

        await session.commit()


async def notify_new_match(bot, trip: Trip, matched_trip: Trip, match: "Match") -> None:
    """Notify one party about a potential match, with map button."""
    from keyboards.keyboards import trip_offer_response_kb
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    # Determine roles
    if trip.role == "passenger":
        driver_trip    = matched_trip
        passenger_trip = trip
        role_for_url   = "passenger"
    else:
        driver_trip    = trip
        passenger_trip = matched_trip
        role_for_url   = "driver"

    role_emoji = "🚗" if matched_trip.role == "driver" else "🙋"
    role_label = "Водій" if matched_trip.role == "driver" else "Пасажир"
    price_prefix = "" if matched_trip.role == "driver" else "до "

    # Load user for rating
    async with AsyncSessionLocal() as session:
        matched_user = await session.get(User, matched_trip.user_id)
        rating_str = f"{matched_user.rating:.1f}" if matched_user else "—"

    text = (
        f"🎉 Знайдено підходящий варіант!\n\n"
        f"{role_emoji} <b>{role_label}</b>\n"
        f"📍 {matched_trip.from_address.split(',')[0]}\n"
        f"🏁 {matched_trip.to_address.split(',')[0]}\n"
        f"🕒 {matched_trip.departure_time.strftime('%d.%m.%Y %H:%M')}\n"
        f"💰 {price_prefix}{matched_trip.price:.0f} грн\n"
        f"⭐ Рейтинг: {rating_str}\n\n"
        f"Підтвердити поїздку?"
    )

    # Build keyboard: confirm/reject + optional map button
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"match_confirm:{match.id}"),
        InlineKeyboardButton(text="❌ Відмовитися", callback_data=f"match_reject:{match.id}"),
    )

    map_url = _match_map_url(driver_trip, passenger_trip, role_for_url)
    if map_url:
        builder.row(
            InlineKeyboardButton(
                text="🗺 Переглянути маршрут на карті",
                web_app=WebAppInfo(url=map_url),
            )
        )

    try:
        await bot.send_message(
            trip.user_id,
            text,
            parse_mode="HTML",
            reply_markup=builder.as_markup(),
        )
    except Exception:
        pass
