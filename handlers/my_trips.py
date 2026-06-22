from datetime import datetime
import json as _json
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.models import Trip, Match, User
from services.matching import get_remaining_seats
from keyboards.keyboards import (
    my_trips_menu_kb,
    active_trip_actions_kb,
    edit_trip_fields_kb,
    confirm_delete_kb,
    confirmed_trip_contact_kb,
    cancel_confirmed_trip_kb,
    geo_or_text_kb,
    dest_kb,
    cancel_kb,
    main_menu_kb,
)
from services.geo import geocode_address, reverse_geocode
from services import bot_settings as _s
from states.states import EditTripStates
from config import WEBAPP_URL

router = Router()


def _trip_card(trip: Trip, show_id: bool = True) -> str:
    role_emoji = "🚗" if trip.role == "driver" else "🙋"
    role_label = "Водій" if trip.role == "driver" else "Пасажир"
    price_label = (
        f"{trip.price:.0f} грн" if trip.role == "driver"
        else f"до {trip.price:.0f} грн"
    )
    seats_label = (
        f"💺 {trip.seats} місць" if trip.role == "driver"
        else f"👥 {trip.seats} пас."
    )
    status_map = {
        "ACTIVE": "🟢 Активна",
        "MATCHING": "🔄 Знайдено збіг",
        "CONFIRMED": "✅ Підтверджено",
        "CLOSED": "⛔ Закрита",
    }
    id_line = f"🆔 Поїздка #{trip.id}\n" if show_id else ""
    return (
        f"{id_line}"
        f"{role_emoji} <b>{role_label}</b>\n"
        f"📍 З: {trip.from_address}\n"
        f"🏁 Куди: {trip.to_address}\n"
        f"🕒 {trip.departure_time.strftime('%d.%m.%Y %H:%M')}\n"
        f"💰 {price_label} | {seats_label}\n"
        f"📊 {status_map.get(trip.status, trip.status)}"
    )


def _confirmed_passengers_kb(confirmed_matches: list[Match]) -> object:
    """Inline keyboard listing each confirmed passenger with contact + close buttons."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    for m in confirmed_matches:
        pax = m.passenger_trip.user if m.passenger_trip else None
        name = pax.first_name if pax else "Пасажир"
        builder.row(
            InlineKeyboardButton(text=f"📞 {name}", callback_data=f"show_contact:{m.id}"),
            InlineKeyboardButton(text="🏁 Завершити", callback_data=f"manual_close:{m.id}"),
        )
    return builder.as_markup()


@router.message(F.text.func(lambda t: t == _s.get("btn_mytrips")))
async def my_trips(message: Message, session: AsyncSession) -> None:
    result = await session.execute(
        select(Trip)
        .where(Trip.user_id == message.from_user.id)
    )
    trips = result.scalars().all()

    active    = sum(1 for t in trips if t.status in ("ACTIVE", "MATCHING"))
    confirmed = sum(1 for t in trips if t.status == "CONFIRMED")
    closed    = sum(1 for t in trips if t.status in ("CLOSED", "CANCELLED"))

    await message.answer(
        "📋 <b>Ваші поїздки</b>\n\nОберіть категорію:",
        parse_mode="HTML",
        reply_markup=my_trips_menu_kb(active=active, confirmed=confirmed, closed=closed),
    )


@router.callback_query(F.data == "mytrips:active")
async def my_active_trips(callback: CallbackQuery, session: AsyncSession) -> None:
    result = await session.execute(
        select(Trip)
        .options(
            selectinload(Trip.driver_matches).selectinload(Match.passenger_trip).selectinload(Trip.user),
        )
        .where(
            Trip.user_id == callback.from_user.id,
            Trip.status.in_(["ACTIVE", "MATCHING"]),
        ).order_by(Trip.departure_time.asc())
    )
    trips = result.scalars().all()

    if not trips:
        await callback.answer("Немає активних поїздок.", show_alert=True)
        return

    await callback.message.answer("🟢 <b>Активні поїздки:</b>", parse_mode="HTML")
    for trip in trips:
        # Build remaining-seats label for drivers
        remaining = None
        if trip.role == "driver":
            remaining = await get_remaining_seats(trip, session)
            total = trip.seats or 1
            seats_extra = f"\n💺 Вільних місць: {remaining}/{total}"
        else:
            seats_extra = ""

        card = _trip_card(trip) + seats_extra

        import urllib.parse
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from aiogram.types import InlineKeyboardButton, WebAppInfo

        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"trip_edit:{trip.id}"))
        builder.row(InlineKeyboardButton(text="🗑 Видалити", callback_data=f"trip_delete:{trip.id}"))
        if WEBAPP_URL:
            base = WEBAPP_URL.rstrip("/")
            url = (
                f"{base}/?mode=single"
                f"&from_lat={trip.from_lat}&from_lon={trip.from_lon}"
                f"&to_lat={trip.to_lat}&to_lon={trip.to_lon}"
                f"&from_addr={urllib.parse.quote(trip.from_address)}"
                f"&to_addr={urllib.parse.quote(trip.to_address)}"
                f"&time={urllib.parse.quote(trip.departure_time.strftime('%d.%m.%Y %H:%M'))}"
                f"&price={trip.price:.0f}&seats={trip.seats}&role={trip.role}"
            )
            builder.row(InlineKeyboardButton(text="🗺 Переглянути на карті", web_app=WebAppInfo(url=url)))

        await callback.message.answer(card, parse_mode="HTML", reply_markup=builder.as_markup())

        # Show confirmed passengers for this driver trip (multi-passenger)
        if trip.role == "driver":
            confirmed = [m for m in trip.driver_matches if m.status == "CONFIRMED"]
            if confirmed:
                await callback.message.answer(
                    f"👥 <b>Підтверджені пасажири ({len(confirmed)}):</b>",
                    parse_mode="HTML",
                    reply_markup=_confirmed_passengers_kb(confirmed),
                )

    await callback.answer()


def _confirmed_trip_kb(match: Match, user_trip: Trip):
    """Keyboard for confirmed trip: contact + close + cancel.
    Map button intentionally omitted — appears in 10-min reminder and after «Виїхав»."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="📞 Контакт попутника", callback_data=f"show_contact:{match.id}"
    ))
    builder.row(InlineKeyboardButton(
        text="🏁 Поїздка завершена", callback_data=f"manual_close:{match.id}"
    ))
    builder.row(InlineKeyboardButton(
        text="❌ Скасувати поїздку", callback_data=f"cancel_confirmed_ask:{match.id}"
    ))
    return builder.as_markup()


@router.callback_query(F.data == "mytrips:confirmed")
async def my_confirmed_trips(callback: CallbackQuery, session: AsyncSession) -> None:
    result = await session.execute(
        select(Trip)
        .options(
            selectinload(Trip.driver_matches).selectinload(Match.passenger_trip),
            selectinload(Trip.passenger_matches).selectinload(Match.driver_trip),
        )
        .where(
            Trip.user_id == callback.from_user.id,
            Trip.status == "CONFIRMED",
        ).order_by(Trip.departure_time.asc())
    )
    trips = result.scalars().all()

    if not trips:
        await callback.answer("Немає підтверджених поїздок.", show_alert=True)
        return

    await callback.message.answer("✅ <b>Підтверджені поїздки:</b>", parse_mode="HTML")
    for trip in trips:
        all_matches = list(trip.driver_matches) + list(trip.passenger_matches)
        confirmed_match = next(
            (m for m in all_matches if m.status == "CONFIRMED"), None
        )
        card = _trip_card(trip)
        if confirmed_match:
            await callback.message.answer(
                card, parse_mode="HTML",
                reply_markup=_confirmed_trip_kb(confirmed_match, trip),
            )
        else:
            await callback.message.answer(card, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "mytrips:closed")
async def my_closed_trips(callback: CallbackQuery, session: AsyncSession) -> None:
    result = await session.execute(
        select(Trip).where(
            Trip.user_id == callback.from_user.id,
            Trip.status == "CLOSED",
        ).order_by(Trip.departure_time.desc()).limit(10)
    )
    trips = result.scalars().all()

    if not trips:
        await callback.answer("Немає завершених поїздок.", show_alert=True)
        return

    await callback.message.answer("🏁 <b>Завершені поїздки (останні 10):</b>", parse_mode="HTML")
    for trip in trips:
        await callback.message.answer(_trip_card(trip), parse_mode="HTML")
    await callback.answer()


# ─── Delete flow ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("trip_delete:") & ~F.data.startswith("trip_delete_confirm:") & ~F.data.startswith("trip_delete_cancel:"))
async def trip_delete_ask(callback: CallbackQuery) -> None:
    trip_id = int(callback.data.split(":")[1])
    await callback.message.answer(
        "Ви впевнені, що хочете видалити цю поїздку?",
        reply_markup=confirm_delete_kb(trip_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("trip_delete_confirm:"))
async def trip_delete_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    trip_id = int(callback.data.split(":")[1])
    trip = await session.get(Trip, trip_id)

    if not trip or trip.user_id != callback.from_user.id:
        await callback.answer("Поїздку не знайдено.", show_alert=True)
        return

    trip.status = "CLOSED"
    await session.commit()
    await callback.message.edit_text("✅ Поїздку закрито.")
    await callback.answer()


@router.callback_query(F.data.startswith("trip_delete_cancel:"))
async def trip_delete_cancel(callback: CallbackQuery) -> None:
    await callback.message.delete()
    await callback.answer("Скасовано.")


# ─── Edit flow ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("trip_edit:"))
async def trip_edit_menu(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    trip_id = int(callback.data.split(":")[1])
    trip = await session.get(Trip, trip_id)

    if not trip or trip.user_id != callback.from_user.id:
        await callback.answer("Поїздку не знайдено.", show_alert=True)
        return

    await state.update_data(editing_trip_id=trip_id)
    await state.set_state(EditTripStates.choosing_field)
    await callback.message.answer(
        f"✏️ <b>Редагування поїздки #{trip_id}</b>\n\nЩо змінити?",
        parse_mode="HTML",
        reply_markup=edit_trip_fields_kb(trip_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_field:"))
async def edit_field_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    _, trip_id_str, field = callback.data.split(":")
    await state.update_data(editing_trip_id=int(trip_id_str), editing_field=field)

    prompts = {
        "from": ("editing_from", "Введіть нову адресу відправлення або надішліть геолокацію:"),
        "to": ("editing_to", "Введіть нову адресу призначення або надішліть геолокацію:"),
        "time": ("editing_time", "Введіть новий час відправлення (ДД.ММ.РРРР ГГ:ХХ):"),
        "price": ("editing_price", "Введіть нову вартість/бюджет (грн):"),
        "seats": ("editing_seats", "Введіть нову кількість місць:"),
    }
    new_state_name, prompt = prompts[field]
    state_map = {
        "editing_from": EditTripStates.editing_from,
        "editing_to": EditTripStates.editing_to,
        "editing_time": EditTripStates.editing_time,
        "editing_price": EditTripStates.editing_price,
        "editing_seats": EditTripStates.editing_seats,
    }
    await state.set_state(state_map[new_state_name])
    kb = geo_or_text_kb() if field == "from" else dest_kb() if field == "to" else cancel_kb()
    await callback.message.answer(prompt, reply_markup=kb)
    await callback.answer()


async def _apply_edit(state: FSMContext, session: AsyncSession, **kwargs) -> int | None:
    """Apply edits and return trip_id (int) — avoids expired-object issues after commit."""
    data = await state.get_data()
    trip_id = data.get("editing_trip_id")
    trip = await session.get(Trip, trip_id)
    if not trip:
        return None
    for k, v in kwargs.items():
        setattr(trip, k, v)
    await session.commit()
    return trip_id


async def _notify_match_partner(trip_id: int, text: str, session: AsyncSession, bot: Bot) -> None:
    """Notify the matched partner (if any MATCHING/CONFIRMED match) about an edit."""
    result = await session.execute(
        select(Match).where(
            Match.status.in_(["MATCHING", "CONFIRMED"]),
            (Match.driver_trip_id == trip_id) | (Match.passenger_trip_id == trip_id),
        )
    )
    match = result.scalars().first()
    if not match:
        return
    partner_trip_id = match.passenger_trip_id if match.driver_trip_id == trip_id else match.driver_trip_id
    partner_trip = await session.get(Trip, partner_trip_id)
    if partner_trip:
        try:
            await bot.send_message(partner_trip.user_id, text, parse_mode="HTML")
        except Exception:
            pass


@router.message(EditTripStates.editing_from, F.web_app_data)
async def edit_from_webapp(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    await message.delete()
    try:
        data = _json.loads(message.web_app_data.data)
        lat, lon = float(data["lat"]), float(data["lon"])
        address = data.get("address") or await reverse_geocode(lat, lon)
    except Exception:
        await message.answer("❌ Помилка даних карти.")
        return
    if not address:
        await message.answer("❌ Не вдалося визначити адресу. Спробуйте ще раз або введіть вручну.")
        return
    trip = await _apply_edit(state, session, from_address=address, from_lat=lat, from_lon=lon)
    await state.clear()
    await message.answer(f"✅ Адреса відправлення оновлена: {address}", reply_markup=main_menu_kb())
    if trip:
        await _notify_match_partner(trip, f"📍 Попутник змінив адресу відправлення:\n<b>{address}</b>", session, bot)


@router.message(EditTripStates.editing_from, F.location)
async def edit_from_location(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    address = await reverse_geocode(lat, lon)
    if not address:
        await message.answer("❌ Не вдалося визначити адресу. Спробуйте ще раз або введіть вручну.")
        return
    trip = await _apply_edit(state, session, from_address=address, from_lat=lat, from_lon=lon)
    await state.clear()
    await message.answer(f"✅ Адреса відправлення оновлена: {address}", reply_markup=main_menu_kb())
    if trip:
        await _notify_match_partner(trip, f"📍 Попутник змінив адресу відправлення:\n<b>{address}</b>", session, bot)


@router.message(EditTripStates.editing_from, F.text)
async def edit_from_text(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return
    result = await geocode_address(message.text)
    if not result:
        await message.answer("❌ Адресу не знайдено. Спробуйте ще раз.")
        return
    lat, lon, address = result
    trip = await _apply_edit(state, session, from_address=address, from_lat=lat, from_lon=lon)
    await state.clear()
    await message.answer(f"✅ Адреса відправлення оновлена: {address}", reply_markup=main_menu_kb())
    if trip:
        await _notify_match_partner(trip, f"📍 Попутник змінив адресу відправлення:\n<b>{address}</b>", session, bot)


@router.message(EditTripStates.editing_to, F.web_app_data)
async def edit_to_webapp(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    await message.delete()
    try:
        data = _json.loads(message.web_app_data.data)
        lat, lon = float(data["lat"]), float(data["lon"])
        address = data.get("address") or await reverse_geocode(lat, lon)
    except Exception:
        await message.answer("❌ Помилка даних карти.")
        return
    if not address:
        await message.answer("❌ Не вдалося визначити адресу. Спробуйте ще раз або введіть вручну.")
        return
    trip = await _apply_edit(state, session, to_address=address, to_lat=lat, to_lon=lon)
    await state.clear()
    await message.answer(f"✅ Адреса призначення оновлена: {address}", reply_markup=main_menu_kb())
    if trip:
        await _notify_match_partner(trip, f"🏁 Попутник змінив адресу призначення:\n<b>{address}</b>", session, bot)


@router.message(EditTripStates.editing_to, F.location)
async def edit_to_location(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    address = await reverse_geocode(lat, lon)
    if not address:
        await message.answer("❌ Не вдалося визначити адресу. Спробуйте ще раз або введіть вручну.")
        return
    trip = await _apply_edit(state, session, to_address=address, to_lat=lat, to_lon=lon)
    await state.clear()
    await message.answer(f"✅ Адреса призначення оновлена: {address}", reply_markup=main_menu_kb())
    if trip:
        await _notify_match_partner(trip, f"🏁 Попутник змінив адресу призначення:\n<b>{address}</b>", session, bot)


@router.message(EditTripStates.editing_to, F.text)
async def edit_to_text(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return
    result = await geocode_address(message.text)
    if not result:
        await message.answer("❌ Адресу не знайдено.")
        return
    lat, lon, address = result
    trip = await _apply_edit(state, session, to_address=address, to_lat=lat, to_lon=lon)
    await state.clear()
    await message.answer(f"✅ Адреса призначення оновлена: {address}", reply_markup=main_menu_kb())
    if trip:
        await _notify_match_partner(trip, f"🏁 Попутник змінив адресу призначення:\n<b>{address}</b>", session, bot)


@router.message(EditTripStates.editing_time, F.text)
async def edit_time(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return
    from handlers.driver import _parse_datetime
    dt = _parse_datetime(message.text)
    if not dt:
        await message.answer("❌ Неправильний формат.")
        return
    await _apply_edit(state, session, departure_time=dt)
    await state.clear()
    await message.answer(f"✅ Час оновлено: {dt.strftime('%d.%m.%Y %H:%M')}", reply_markup=main_menu_kb())


@router.message(EditTripStates.editing_price, F.text)
async def edit_price(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return
    try:
        price = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Введіть коректну суму.")
        return
    await _apply_edit(state, session, price=price)
    await state.clear()
    await message.answer(f"✅ Вартість оновлено: {price:.0f} грн", reply_markup=main_menu_kb())


@router.message(EditTripStates.editing_seats, F.text)
async def edit_seats(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return
    try:
        seats = int(message.text.strip())
        if seats < 1 or seats > 4:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введіть число від 1 до 4.")
        return
    data = await state.get_data()
    trip_obj = await session.get(Trip, data.get("editing_trip_id"))
    label = "місць" if trip_obj and trip_obj.role == "driver" else "пасажирів"
    trip_id = await _apply_edit(state, session, seats=seats)
    await state.clear()
    await message.answer(f"✅ Кількість місць оновлено: {seats}", reply_markup=main_menu_kb())
    if trip_id:
        await _notify_match_partner(
            trip_id,
            f"💺 Попутник змінив кількість {label}: <b>{seats}</b>",
            session, bot,
        )


# ─── Cancel confirmed trip flow ───────────────────────────────────────────────

@router.callback_query(F.data.startswith("cancel_confirmed_ask:"))
async def cancel_confirmed_ask(callback: CallbackQuery) -> None:
    match_id = int(callback.data.split(":")[1])
    await callback.message.answer(
        "❌ <b>Скасування підтвердженої поїздки</b>\n\n"
        "Вкажіть причину скасування — ваш попутник отримає повідомлення:",
        parse_mode="HTML",
        reply_markup=cancel_confirmed_trip_kb(match_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_confirmed_back:"))
async def cancel_confirmed_back(callback: CallbackQuery) -> None:
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_confirmed:"))
async def cancel_confirmed_reason(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    parts = callback.data.split(":")
    match_id, reason_code = int(parts[1]), parts[2]

    reasons = {
        "plans":       "Змінились плани",
        "emergency":   "Надзвичайна ситуація",
        "time":        "Не встигаю на цей час",
        "found_other": "Знайшов інший варіант",
        "other":       "Інше",
    }
    reason_text = reasons.get(reason_code, "Інше")

    result = await session.execute(
        select(Match)
        .options(
            selectinload(Match.driver_trip).selectinload(Trip.user),
            selectinload(Match.passenger_trip).selectinload(Trip.user),
        )
        .where(Match.id == match_id)
    )
    match = result.scalars().first()

    if not match or match.status != "CONFIRMED":
        await callback.answer("Поїздку вже скасовано або вона не існує.", show_alert=True)
        return

    user_id = callback.from_user.id
    driver_trip = match.driver_trip
    passenger_trip = match.passenger_trip

    if driver_trip.user_id != user_id and passenger_trip.user_id != user_id:
        await callback.answer("Ви не є учасником цієї поїздки.", show_alert=True)
        return

    # Canceller's trip → CLOSED; partner's trip → ACTIVE (they're still looking)
    match.status = "REJECTED"
    match.rejection_reason = f"Скасовано учасником: {reason_text}"
    if driver_trip.user_id == user_id:
        canceller_role = "Водій"
        driver_trip.status = "CLOSED"
        passenger_trip.status = "ACTIVE"
        partner_id = passenger_trip.user_id
    else:
        canceller_role = "Пасажир"
        passenger_trip.status = "CLOSED"
        driver_trip.status = "ACTIVE"
        partner_id = driver_trip.user_id
    await session.commit()

    # Notify partner
    try:
        await bot.send_message(
            partner_id,
            f"😔 <b>{canceller_role} скасував поїздку.</b>\n\n"
            f"📝 Причина: {reason_text}\n\n"
            "Ваша заявка знову активна — шукаємо нового попутника.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await callback.message.edit_text(
        f"✅ Поїздку скасовано.\n📝 Причина: {reason_text}"
    )
    await callback.answer()
