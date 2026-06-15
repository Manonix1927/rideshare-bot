"""
Handles match confirmation/rejection flow and post-trip rating.
"""
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select

from database.models import Match, Trip, User, Rating, DriverLocation
from keyboards.keyboards import (
    rejection_reason_kb,
    meeting_happened_kb,
    trip_finished_kb,
    rating_kb,
    main_menu_kb,
)
from services.matching import confirm_match_side, reject_match, get_match_for_user
from services.tracking import build_track_url
from keyboards.keyboards import confirmed_trip_driver_kb, confirmed_trip_passenger_kb

router = Router()


async def _save_driver_location(user_id: int, lat: float, lon: float, session: AsyncSession) -> None:
    result = await session.execute(
        select(Match)
        .join(Trip, Match.driver_trip_id == Trip.id)
        .where(Trip.user_id == user_id, Match.status == "CONFIRMED")
    )
    match = result.scalars().first()
    if not match:
        return
    loc = await session.get(DriverLocation, match.id)
    if loc:
        loc.lat = lat
        loc.lon = lon
        loc.updated_at = datetime.utcnow()
    else:
        session.add(DriverLocation(match_id=match.id, lat=lat, lon=lon))
    await session.commit()


@router.message(F.location)
async def handle_driver_live_location(message: Message, session: AsyncSession) -> None:
    """Initial location share from driver."""
    await _save_driver_location(
        message.from_user.id,
        message.location.latitude,
        message.location.longitude,
        session,
    )


@router.edited_message(F.location)
async def handle_driver_live_location_update(message: Message, session: AsyncSession) -> None:
    """Live location updates — Telegram sends these as edited_message, not new messages."""
    await _save_driver_location(
        message.from_user.id,
        message.location.latitude,
        message.location.longitude,
        session,
    )


async def _get_match_with_trips(match_id: int, session: AsyncSession) -> Match | None:
    from sqlalchemy.orm import selectinload as sl
    result = await session.execute(
        select(Match)
        .options(
            sl(Match.driver_trip).selectinload(Trip.user),
            sl(Match.passenger_trip).selectinload(Trip.user),
        )
        .where(Match.id == match_id)
    )
    return result.scalars().first()


@router.callback_query(F.data.startswith("match_confirm:"))
async def match_confirm(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    match_id = int(callback.data.split(":")[1])
    match = await _get_match_with_trips(match_id, session)

    if not match or match.status not in ("PENDING", "MATCHING"):
        await callback.answer("Цей збіг вже неактуальний.", show_alert=True)
        return

    user_id = callback.from_user.id
    driver_trip = match.driver_trip
    passenger_trip = match.passenger_trip

    if driver_trip.user_id == user_id:
        user_trip = driver_trip
    elif passenger_trip.user_id == user_id:
        user_trip = passenger_trip
    else:
        await callback.answer("Ви не є учасником цієї поїздки.", show_alert=True)
        return

    both_confirmed = await confirm_match_side(match, user_trip, session)

    if both_confirmed:
        match = await _get_match_with_trips(match_id, session)
        driver_user = match.driver_trip.user
        passenger_user = match.passenger_trip.user

        contact_text = (
            "🎉 Поїздку підтверджено. Ось ваші контакти для зв'язку 👇\n"
            "Бажаємо приємної поїздки 🚗\n\n"
        )
        driver_contact = (
            f"👤 <b>Водій:</b> {driver_user.first_name}"
            + (f" (@{driver_user.username})" if driver_user.username else "")
        )
        passenger_contact = (
            f"👤 <b>Пасажир:</b> {passenger_user.first_name}"
            + (f" (@{passenger_user.username})" if passenger_user.username else "")
        )

        track_url = build_track_url(match, driver_user.id, passenger_user.id)

        try:
            await bot.send_message(
                driver_user.id,
                contact_text + passenger_contact + "\n\n"
                "📍 Натисніть «Виїхав до попутника» коли вирушите. "
                "Пасажир отримає сповіщення.",
                parse_mode="HTML",
                reply_markup=confirmed_trip_driver_kb(match.id, track_url),
            )
            await bot.send_message(
                passenger_user.id,
                contact_text + driver_contact + "\n\n"
                "📍 Очікуйте — водій натисне кнопку «Виїхав», "
                "тоді вам прийде сповіщення.",
                parse_mode="HTML",
                reply_markup=confirmed_trip_passenger_kb(match.id, track_url),
            )
        except Exception:
            pass

        await callback.message.edit_text("✅ Ви підтвердили поїздку. Контакти надіслано.")
    else:
        await callback.message.edit_text(
            "✅ Ви підтвердили поїздку. Очікуємо підтвердження від іншого учасника."
        )
    await callback.answer()


@router.callback_query(F.data.startswith("match_reject:"))
async def match_reject_start(callback: CallbackQuery) -> None:
    match_id = int(callback.data.split(":")[1])
    await callback.message.edit_text(
        "❌ Вкажіть причину відмови:",
        reply_markup=rejection_reason_kb(match_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("reject_reason:"))
async def reject_reason(callback: CallbackQuery, session: AsyncSession) -> None:
    _, match_id_str, reason_code = callback.data.split(":")
    match_id = int(match_id_str)

    reasons = {
        "expensive": "Дорого",
        "time": "Не підходить час",
        "route": "Не підходить маршрут",
        "found": "Вже знайшов поїздку",
        "other": "Інше",
    }

    match = await session.get(Match, match_id)
    if not match or match.status == "REJECTED":
        await callback.answer("Збіг вже закрито.", show_alert=True)
        return

    await reject_match(match, reasons.get(reason_code, "Інше"), session)
    await callback.message.edit_text("Відмову зафіксовано. Продовжуємо пошук.")
    await callback.answer()


# ─── Post-trip rating flow ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("meeting_yes:"))
async def meeting_yes(callback: CallbackQuery, session: AsyncSession) -> None:
    _, match_id_str, role = callback.data.split(":")
    match_id = int(match_id_str)
    match = await _get_match_with_trips(match_id, session)

    if not match:
        await callback.answer("Поїздка не знайдена.", show_alert=True)
        return

    user_id = callback.from_user.id
    driver_trip = match.driver_trip
    passenger_trip = match.passenger_trip

    await callback.message.edit_text(
        "✅ Чудово! Після завершення поїздки натисніть кнопку нижче.",
        reply_markup=trip_finished_kb(match_id, role),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("meeting_no:"))
async def meeting_no(callback: CallbackQuery, session: AsyncSession) -> None:
    _, match_id_str, role = callback.data.split(":")
    match_id = int(match_id_str)

    await callback.message.edit_text(
        "😔 Шкода. Чи зв'язувались ви з водієм/попутником?",
        reply_markup=_contacted_kb(match_id, role),
    )
    await callback.answer()


def _contacted_kb(match_id: int, role: str):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Так", callback_data=f"contacted_yes:{match_id}:{role}"),
        InlineKeyboardButton(text="❌ Ні", callback_data=f"contacted_no:{match_id}:{role}"),
    )
    return builder.as_markup()


@router.callback_query(F.data.startswith("contacted_yes:"))
async def contacted_yes(callback: CallbackQuery, session: AsyncSession) -> None:
    _, match_id_str, role = callback.data.split(":")
    match_id = int(match_id_str)
    match = await _get_match_with_trips(match_id, session)
    if not match:
        return

    user_id = callback.from_user.id
    other_user_id = (
        match.passenger_trip.user_id if match.driver_trip.user_id == user_id
        else match.driver_trip.user_id
    )

    # Decrement stats for the other user (failure)
    other_user = await session.get(User, other_user_id)
    if other_user:
        other_user.failed_trips += 1
        await session.commit()

    await callback.message.edit_text(
        "Оцініть вашого попутника від 1 до 5 зірок:",
        reply_markup=rating_kb(match_id, other_user_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("contacted_no:"))
async def contacted_no(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Зрозуміло. Дякуємо за відповідь.")
    await callback.answer()


@router.callback_query(F.data.startswith("trip_done:"))
async def trip_done(callback: CallbackQuery, session: AsyncSession) -> None:
    _, match_id_str, role = callback.data.split(":")
    match_id = int(match_id_str)
    match = await _get_match_with_trips(match_id, session)

    if not match:
        await callback.answer("Поїздка не знайдена.", show_alert=True)
        return

    user_id = callback.from_user.id
    if match.driver_trip.user_id == user_id:
        other_user_id = match.passenger_trip.user_id
    else:
        other_user_id = match.driver_trip.user_id

    # Update successful trips for current user
    user = await session.get(User, user_id)
    if user:
        user.successful_trips += 1
        user.trips_count += 1
        await session.commit()

    await callback.message.edit_text(
        "Розкажіть, як пройшла ваша поїздка 🙂\n\nОцініть вашого попутника:",
        reply_markup=rating_kb(match_id, other_user_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rate:"))
async def rate_user(callback: CallbackQuery, session: AsyncSession) -> None:
    parts = callback.data.split(":")
    match_id = int(parts[1])
    to_user_id = int(parts[2])
    score = int(parts[3])

    # Check duplicate rating
    existing = await session.execute(
        select(Rating).where(
            Rating.match_id == match_id,
            Rating.from_user_id == callback.from_user.id,
        )
    )
    if existing.scalars().first():
        await callback.answer("Ви вже залишили оцінку.", show_alert=True)
        return

    rating = Rating(
        match_id=match_id,
        from_user_id=callback.from_user.id,
        to_user_id=to_user_id,
        score=score,
    )
    session.add(rating)

    # Recalculate user rating
    to_user = await session.get(User, to_user_id)
    if to_user:
        all_ratings = await session.execute(
            select(Rating).where(Rating.to_user_id == to_user_id)
        )
        all_scores = [r.score for r in all_ratings.scalars().all()] + [score]
        to_user.rating = sum(all_scores) / len(all_scores)

    await session.commit()

    stars = "⭐" * score
    await callback.message.edit_text(f"Дякуємо за оцінку! {stars}")
    await callback.answer()


@router.callback_query(F.data.startswith("manual_close:"))
async def manual_close(callback: CallbackQuery, session: AsyncSession) -> None:
    match_id = int(callback.data.split(":")[1])
    match = await session.get(Match, match_id)
    if match:
        match.status = "CLOSED"
        driver_trip = await session.get(Trip, match.driver_trip_id)
        passenger_trip = await session.get(Trip, match.passenger_trip_id)
        if driver_trip:
            driver_trip.status = "CLOSED"
        if passenger_trip:
            passenger_trip.status = "CLOSED"
        await session.commit()

    await callback.message.edit_text("✅ Поїздку завершено.")
    await callback.answer()


@router.callback_query(F.data.startswith("show_contact:"))
async def show_contact(callback: CallbackQuery, session: AsyncSession) -> None:
    match_id = int(callback.data.split(":")[1])
    match = await _get_match_with_trips(match_id, session)

    if not match or match.status not in ("CONFIRMED", "CLOSED"):
        await callback.answer("Контакти недоступні.", show_alert=True)
        return

    user_id = callback.from_user.id
    if match.driver_trip.user_id == user_id:
        other = match.passenger_trip.user
        label = "Пасажир"
    else:
        other = match.driver_trip.user
        label = "Водій"

    contact = f"👤 <b>{label}:</b> {other.first_name}"
    if other.username:
        contact += f"\n✈️ @{other.username}"

    await callback.answer(contact.replace("<b>", "").replace("</b>", ""), show_alert=True)
