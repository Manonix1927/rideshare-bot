from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.models import Trip, Match, User
from keyboards.keyboards import (
    my_trips_menu_kb,
    active_trip_actions_kb,
    edit_trip_fields_kb,
    confirm_delete_kb,
    confirmed_trip_contact_kb,
    geo_or_text_kb,
    cancel_kb,
    main_menu_kb,
)
from services.geo import geocode_address, reverse_geocode
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


@router.message(F.text == "📋 Мої поїздки")
async def my_trips(message: Message, session: AsyncSession) -> None:
    result = await session.execute(
        select(Trip)
        .where(Trip.user_id == message.from_user.id)
        .order_by(Trip.created_at.desc())
    )
    trips = result.scalars().all()
    count = len(trips)

    await message.answer(
        f"📋 <b>Ваші поїздки ({count})</b>\n\nОберіть категорію:",
        parse_mode="HTML",
        reply_markup=my_trips_menu_kb(),
    )


@router.callback_query(F.data == "mytrips:active")
async def my_active_trips(callback: CallbackQuery, session: AsyncSession) -> None:
    result = await session.execute(
        select(Trip).where(
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
        card = _trip_card(trip)
        # Show map button if webapp is configured
        if WEBAPP_URL:
            from keyboards.keyboards import map_view_kb
            import urllib.parse
            url = (
                f"{WEBAPP_URL}?from_lat={trip.from_lat}&from_lon={trip.from_lon}"
                f"&to_lat={trip.to_lat}&to_lon={trip.to_lon}"
                f"&from_addr={urllib.parse.quote(trip.from_address)}"
                f"&to_addr={urllib.parse.quote(trip.to_address)}"
                f"&time={urllib.parse.quote(trip.departure_time.strftime('%d.%m.%Y %H:%M'))}"
                f"&price={trip.price:.0f}"
                f"&seats={trip.seats}"
                f"&role={trip.role}"
            )
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            from aiogram.types import InlineKeyboardButton, WebAppInfo
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"trip_edit:{trip.id}"))
            builder.row(InlineKeyboardButton(text="🗑 Видалити", callback_data=f"trip_delete:{trip.id}"))
            builder.row(InlineKeyboardButton(text="🗺 Переглянути на карті", web_app=WebAppInfo(url=url)))
            await callback.message.answer(card, parse_mode="HTML", reply_markup=builder.as_markup())
        else:
            await callback.message.answer(
                card, parse_mode="HTML", reply_markup=active_trip_actions_kb(trip.id)
            )
    await callback.answer()


@router.callback_query(F.data == "mytrips:confirmed")
async def my_confirmed_trips(callback: CallbackQuery, session: AsyncSession) -> None:
    result = await session.execute(
        select(Trip)
        .options(selectinload(Trip.driver_matches), selectinload(Trip.passenger_matches))
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
        # Find confirmed match
        all_matches = list(trip.driver_matches) + list(trip.passenger_matches)
        confirmed_match = next(
            (m for m in all_matches if m.status == "CONFIRMED"), None
        )
        card = _trip_card(trip)
        if confirmed_match:
            await callback.message.answer(
                card, parse_mode="HTML",
                reply_markup=confirmed_trip_contact_kb(confirmed_match.id),
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
    kb = geo_or_text_kb() if field in ("from", "to") else cancel_kb()
    await callback.message.answer(prompt, reply_markup=kb)
    await callback.answer()


async def _apply_edit(state: FSMContext, session: AsyncSession, **kwargs) -> Trip | None:
    data = await state.get_data()
    trip_id = data.get("editing_trip_id")
    trip = await session.get(Trip, trip_id)
    if not trip:
        return None
    for k, v in kwargs.items():
        setattr(trip, k, v)
    await session.commit()
    return trip


@router.message(EditTripStates.editing_from, F.location)
async def edit_from_location(message: Message, state: FSMContext, session: AsyncSession) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    address = await reverse_geocode(lat, lon)
    trip = await _apply_edit(state, session, from_address=address, from_lat=lat, from_lon=lon)
    await state.clear()
    await message.answer(f"✅ Адреса відправлення оновлена: {address}", reply_markup=main_menu_kb())


@router.message(EditTripStates.editing_from, F.text)
async def edit_from_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return
    result = await geocode_address(message.text)
    if not result:
        await message.answer("❌ Адресу не знайдено. Спробуйте ще раз.")
        return
    lat, lon, address = result
    await _apply_edit(state, session, from_address=address, from_lat=lat, from_lon=lon)
    await state.clear()
    await message.answer(f"✅ Адреса відправлення оновлена: {address}", reply_markup=main_menu_kb())


@router.message(EditTripStates.editing_to, F.location)
async def edit_to_location(message: Message, state: FSMContext, session: AsyncSession) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    address = await reverse_geocode(lat, lon)
    await _apply_edit(state, session, to_address=address, to_lat=lat, to_lon=lon)
    await state.clear()
    await message.answer(f"✅ Адреса призначення оновлена: {address}", reply_markup=main_menu_kb())


@router.message(EditTripStates.editing_to, F.text)
async def edit_to_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return
    result = await geocode_address(message.text)
    if not result:
        await message.answer("❌ Адресу не знайдено.")
        return
    lat, lon, address = result
    await _apply_edit(state, session, to_address=address, to_lat=lat, to_lon=lon)
    await state.clear()
    await message.answer(f"✅ Адреса призначення оновлена: {address}", reply_markup=main_menu_kb())


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
async def edit_seats(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return
    try:
        seats = int(message.text.strip())
        if seats < 1 or seats > 8:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введіть число від 1 до 8.")
        return
    await _apply_edit(state, session, seats=seats)
    await state.clear()
    await message.answer(f"✅ Кількість місць оновлено: {seats}", reply_markup=main_menu_kb())
