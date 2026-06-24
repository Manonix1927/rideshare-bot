"""
Scheduled tasks:
  - Auto-close trips past their departure time → CLOSED
  - Send rating prompt 5 min after departure
"""
import logging
import urllib.parse
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.database import AsyncSessionLocal
from database.models import Trip, Match, User
from config import WEBAPP_URL
from services import bot_settings as _s
from services.timezone import now as _now

logger = logging.getLogger(__name__)


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
    now = _now()
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
    """5 min after departure close confirmed match and ask both sides if meeting happened.

    Uses a simple threshold (departure <= now - 5min) instead of a narrow window so
    matches are never left in CONFIRMED state forever if the bot was restarted.
    """
    from keyboards.keyboards import meeting_happened_kb

    now = _now()
    threshold = now - timedelta(minutes=5)

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
            if departure > threshold:
                continue  # departure hasn't passed the 5-min threshold yet

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

    now = _now()
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

            reminder_tpl = _s.get("msg_reminder")
            base = reminder_tpl.replace("{route}", f"{from_addr} → {to_addr}").replace("{time}", time_str)
            text = (
                f"{base}\n\n"
                f"Попутник: {passenger_user.first_name} ⭐{passenger_user.rating:.1f if passenger_user.rating is not None else 'н/р'}"
            )
            passenger_text = (
                f"{base}\n\n"
                f"Водій: {driver_user.first_name} ⭐{driver_user.rating:.1f if driver_user.rating is not None else 'н/р'}"
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


async def rematch_active_trips(bot) -> None:
    """Periodic safety net (every few minutes): pair up ACTIVE trips that didn't get
    matched at creation time — e.g. the counterpart was created later, or an earlier
    match was cancelled. Matching otherwise runs only once, at creation, so without
    this two compatible trips can sit ACTIVE forever.

    Skips any pair that already has a match record (PENDING/CONFIRMED/REJECTED/…) so a
    rejected or cancelled pair is never silently recreated.
    """
    from services.matching import find_matches_for_trip, create_match

    created = 0
    async with AsyncSessionLocal() as session:
        passengers = (await session.execute(
            select(Trip).where(Trip.role == "passenger", Trip.status == "ACTIVE")
            .options(selectinload(Trip.user))
        )).scalars().all()

        for p_trip in passengers:
            candidates = await find_matches_for_trip(p_trip, session)
            for d_trip in candidates:
                # Never re-create a pair that was already matched once (incl. rejected).
                existing = (await session.execute(
                    select(Match).where(
                        Match.driver_trip_id == d_trip.id,
                        Match.passenger_trip_id == p_trip.id,
                    )
                )).scalars().first()
                if existing:
                    continue
                match = await create_match(d_trip, p_trip, session)
                if match:
                    created += 1
                    await notify_new_match(bot, d_trip, p_trip, match)
                    await notify_new_match(bot, p_trip, d_trip, match)
                    break  # passenger is now MATCHING — one pending offer at a time

    if created:
        logger.info("rematch_active_trips: created %d new match(es)", created)


async def check_pending_match_timeouts(bot) -> None:
    """Every minute: remind and eventually auto-cancel PENDING matches that are ignored.

    Rules:
    - Trip within 1 hour: warn at +5 min ("5 хв до закриття"), cancel at +10 min.
    - Trip > 1 hour away: remind at +10 min, final warn at +20 min ("скасуємо через 10 хв"),
      cancel at +30 min.
    """
    from datetime import datetime as _dt_utc
    from keyboards.keyboards import trip_offer_response_kb

    now_utc  = _dt_utc.utcnow()   # for created_at comparison (stored as UTC)
    now_kyiv = _now()              # for departure_time comparison (stored as Kyiv time)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Match)
            .options(
                selectinload(Match.driver_trip),
                selectinload(Match.passenger_trip),
            )
            .where(Match.status == "PENDING")
        )
        matches = result.scalars().all()

        to_cancel: list[Match] = []

        for match in matches:
            departure     = match.driver_trip.departure_time
            age_min       = (now_utc - match.created_at).total_seconds() / 60
            until_dep_sec = (departure - now_kyiv).total_seconds()

            # Already past departure — cancel immediately
            if until_dep_sec < 0:
                to_cancel.append(match)
                continue

            soon = until_dep_sec < 3600  # within 1 hour

            if soon:
                if age_min >= 10:
                    to_cancel.append(match)
                elif age_min >= 5 and not match.pending_reminder_1_sent:
                    match.pending_reminder_1_sent = True
                    await _send_pending_reminder(
                        match, bot, trip_offer_response_kb,
                        warning=True, cancel_in_min=5,
                    )
            else:
                if age_min >= 30 and match.pending_reminder_2_sent:
                    to_cancel.append(match)
                elif age_min >= 20 and match.pending_reminder_1_sent and not match.pending_reminder_2_sent:
                    match.pending_reminder_2_sent = True
                    await _send_pending_reminder(
                        match, bot, trip_offer_response_kb,
                        warning=True, cancel_in_min=10,
                    )
                elif age_min >= 10 and not match.pending_reminder_1_sent:
                    match.pending_reminder_1_sent = True
                    await _send_pending_reminder(
                        match, bot, trip_offer_response_kb,
                        warning=False, cancel_in_min=10,
                    )

        for match in to_cancel:
            match.status = "REJECTED"
            match.rejection_reason = "Автоматичне скасування — не підтверджено вчасно"
            d_trip = await session.get(Trip, match.driver_trip_id)
            p_trip = await session.get(Trip, match.passenger_trip_id)
            if d_trip and d_trip.status == "MATCHING":
                d_trip.status = "ACTIVE"
            if p_trip and p_trip.status == "MATCHING":
                p_trip.status = "ACTIVE"
            await _notify_timeout_cancel(match, bot)

        await session.commit()


async def _send_pending_reminder(match: Match, bot, kb_fn, warning: bool, cancel_in_min: int) -> None:
    if warning:
        text = (
            f"⚠️ Пропозиція поїздки досі чекає вашої відповіді.\n\n"
            f"Через <b>{cancel_in_min} хв</b> заявку буде автоматично скасовано. "
            "Підтвердьте або відхиліть."
        )
    else:
        text = (
            "⏰ Нагадування: є пропозиція поїздки, яка очікує вашої відповіді.\n\n"
            f"Якщо не відповісти протягом {cancel_in_min} хв — надійде останнє попередження."
        )

    kb = kb_fn(match.id)
    targets = []
    if not match.driver_confirmed:
        targets.append(match.driver_trip.user_id)
    if not match.passenger_confirmed:
        targets.append(match.passenger_trip.user_id)

    for user_id in targets:
        try:
            await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


async def _notify_timeout_cancel(match: Match, bot) -> None:
    driver_uid    = match.driver_trip.user_id
    passenger_uid = match.passenger_trip.user_id

    if match.driver_confirmed and not match.passenger_confirmed:
        msgs = {
            driver_uid:    "❌ Пасажир не підтвердив поїздку вчасно. Заявку скасовано — пошук продовжується.",
            passenger_uid: "❌ Заявку скасовано через відсутність вашого підтвердження. Пошук продовжується.",
        }
    elif match.passenger_confirmed and not match.driver_confirmed:
        msgs = {
            passenger_uid: "❌ Водій не підтвердив поїздку вчасно. Заявку скасовано — пошук продовжується.",
            driver_uid:    "❌ Заявку скасовано через відсутність вашого підтвердження. Пошук продовжується.",
        }
    else:
        msg = "❌ Заявку скасовано — жодна зі сторін не підтвердила вчасно. Пошук продовжується."
        msgs = {driver_uid: msg, passenger_uid: msg}

    for uid, text in msgs.items():
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass


async def notify_new_match(
    bot, trip: Trip, matched_trip: Trip, match: "Match", intro: str | None = None
) -> None:
    """Notify one party about a potential match, with map button.

    ``intro`` overrides the default "Знайдено підходящий варіант!" header —
    used for direct offers so the recipient sees context-aware wording.
    """
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
        rating_str = (f"{matched_user.rating:.1f}" if matched_user and matched_user.rating is not None else "Без рейтингу")

    header = intro or "🎉 Знайдено підходящий варіант!"
    text = (
        f"{header}\n\n"
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
