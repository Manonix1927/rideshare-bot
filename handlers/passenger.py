import json as _json
from datetime import datetime, date
from services.timezone import now as _now, today as _today
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database.models import Trip, User
from keyboards.keyboards import geo_or_text_kb, dest_kb, cancel_kb, main_menu_kb, confirm_address_kb, city_picker_kb, date_picker_kb, time_picker_kb, passengers_count_kb
from services.geo import geocode_address, geocode_address_multi, reverse_geocode, get_city_from_coords, _detect_city
from services.matching import find_matches_for_trip, create_match
from services.notifications import notify_new_match
from services import bot_settings as _s
from states.states import PassengerStates
from config import MAX_ACTIVE_TRIPS

router = Router()


def _parse_datetime(text: str) -> datetime | None:
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M", "%H:%M"):
        try:
            dt = datetime.strptime(text.strip(), fmt)
            if fmt == "%H:%M":
                kyiv = _now()
                dt = dt.replace(year=kyiv.year, month=kyiv.month, day=kyiv.day)
            elif fmt == "%d.%m %H:%M":
                dt = dt.replace(year=_now().year)
            return dt
        except ValueError:
            continue
    return None


@router.message(F.text.func(lambda t: t == _s.get("btn_passenger")))
async def passenger_start(message: Message, state: FSMContext, session: AsyncSession) -> None:
    user = await session.get(User, message.from_user.id)
    if user and user.is_blocked:
        await message.answer("🚫 Ваш акаунт заблоковано.")
        return

    active_count = await session.scalar(
        select(func.count()).select_from(Trip).where(
            Trip.user_id == message.from_user.id,
            Trip.status.in_(["ACTIVE", "MATCHING"]),
        )
    )
    if active_count >= MAX_ACTIVE_TRIPS:
        await message.answer(
            "⚠️ У вас уже є активні заявки. Спочатку завершіть або закрийте поточні поїздки.",
            reply_markup=main_menu_kb(),
        )
        return

    await state.set_state(PassengerStates.from_address)
    await message.answer(
        "🙋 <b>Нова заявка — крок 1/5</b>\n\nВкажіть адресу відправлення:",
        parse_mode="HTML",
        reply_markup=geo_or_text_kb(),
    )


@router.message(PassengerStates.from_address, F.location)
async def passenger_from_location(message: Message, state: FSMContext) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    address = await reverse_geocode(lat, lon)
    city = await get_city_from_coords(lat, lon)
    await state.update_data(from_lat=lat, from_lon=lon, from_address=address, from_city=city)
    await state.set_state(PassengerStates.to_address)
    await message.answer(
        f"✅ Відправлення: {address}\n\n🙋 <b>Крок 2/5</b>\n\nВкажіть адресу пункту призначення:",
        parse_mode="HTML",
        reply_markup=dest_kb(),
    )


@router.message(PassengerStates.from_address, F.text)
async def passenger_from_text(message: Message, state: FSMContext) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    candidates = await geocode_address_multi(message.text)
    if not candidates:
        await message.answer("❌ Не вдалося знайти адресу. Спробуйте ще раз або надішліть геолокацію.")
        return

    if len(candidates) > 1:
        await state.update_data(city_candidates=candidates)
        await message.answer(
            "📍 Знайдено вулицю у кількох містах. Оберіть потрібне:",
            reply_markup=city_picker_kb(candidates, role="passenger", field="from"),
        )
        return

    lat, lon, address, city = candidates[0]
    await state.update_data(from_lat=lat, from_lon=lon, from_address=address, from_city=city)
    await state.set_state(PassengerStates.to_address)
    await message.answer(
        f"✅ Відправлення: {address}\n\n🙋 <b>Крок 2/5</b>\n\nВкажіть адресу пункту призначення:",
        parse_mode="HTML",
        reply_markup=dest_kb(),
    )


@router.message(PassengerStates.from_address, F.web_app_data)
async def passenger_from_webapp(message: Message, state: FSMContext) -> None:
    await message.delete()
    try:
        data = _json.loads(message.web_app_data.data)
        lat, lon = float(data["lat"]), float(data["lon"])
        address = data.get("address") or f"{lat:.5f}, {lon:.5f}"
    except Exception:
        await message.answer("❌ Помилка отримання даних з карти. Спробуйте ще раз.")
        return

    city = await get_city_from_coords(lat, lon)
    await state.update_data(from_lat=lat, from_lon=lon, from_address=address, from_city=city)
    await state.set_state(PassengerStates.to_address)
    await message.answer(
        f"✅ Відправлення: {address}\n\n🙋 <b>Крок 2/5</b>\n\nВкажіть адресу пункту призначення:",
        parse_mode="HTML",
        reply_markup=dest_kb(),
    )


@router.message(PassengerStates.to_address, F.web_app_data)
async def passenger_to_webapp(message: Message, state: FSMContext) -> None:
    await message.delete()
    try:
        data = _json.loads(message.web_app_data.data)
        lat, lon = float(data["lat"]), float(data["lon"])
        address = data.get("address") or f"{lat:.5f}, {lon:.5f}"
    except Exception:
        await message.answer("❌ Помилка отримання даних з карти. Спробуйте ще раз.")
        return

    await state.update_data(to_lat=lat, to_lon=lon, to_address=address)
    await state.set_state(PassengerStates.departure_time)
    await message.answer(
        f"✅ Призначення: {address}\n\n🙋 <b>Крок 3/5</b>\n\nОберіть бажану дату поїздки:",
        parse_mode="HTML",
        reply_markup=date_picker_kb(),
    )


@router.message(PassengerStates.to_address, F.location)
async def passenger_to_location(message: Message, state: FSMContext) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    address = await reverse_geocode(lat, lon)
    await state.update_data(to_lat=lat, to_lon=lon, to_address=address)
    await state.set_state(PassengerStates.departure_time)
    await message.answer(
        f"✅ Призначення: {address}\n\n🙋 <b>Крок 3/5</b>\n\nОберіть бажану дату поїздки:",
        parse_mode="HTML",
        reply_markup=date_picker_kb(),
    )


@router.message(PassengerStates.to_address, F.text)
async def passenger_to_text(message: Message, state: FSMContext) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    data = await state.get_data()
    near_lat = data.get("from_lat")
    near_lon = data.get("from_lon")
    from_city = data.get("from_city", "")

    query = message.text
    city_in_query, _, _ = _detect_city(query)
    if not city_in_query and from_city and from_city.lower() not in query.lower():
        query = f"{query}, {from_city}"

    candidates = await geocode_address_multi(query, near_lat=near_lat, near_lon=near_lon)
    if not candidates:
        await message.answer("❌ Не вдалося знайти адресу. Спробуйте ще раз або надішліть геолокацію.")
        return

    if len(candidates) > 1:
        await state.update_data(city_candidates=candidates)
        await message.answer(
            "📍 Знайдено вулицю у кількох містах. Оберіть потрібне:",
            reply_markup=city_picker_kb(candidates, role="passenger", field="to"),
        )
        return

    lat, lon, address, _city = candidates[0]
    await state.update_data(pending_to_lat=lat, pending_to_lon=lon, pending_to_address=address)
    await message.answer(
        f"📍 Знайдено: <b>{address}</b>\n\nЦе правильна адреса?",
        parse_mode="HTML",
        reply_markup=confirm_address_kb("passenger"),
    )


@router.callback_query(F.data == "addr_ok:passenger", PassengerStates.to_address)
async def passenger_addr_ok(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(
        to_lat=data["pending_to_lat"],
        to_lon=data["pending_to_lon"],
        to_address=data["pending_to_address"],
    )
    await state.set_state(PassengerStates.departure_time)
    await callback.message.edit_text(
        f"✅ Призначення: {data['pending_to_address']}\n\n"
        "🙋 <b>Крок 3/5</b>\n\nОберіть бажану дату поїздки:",
        parse_mode="HTML",
        reply_markup=date_picker_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "addr_retry:passenger", PassengerStates.to_address)
async def passenger_addr_retry(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🙋 Введіть адресу пункту призначення:\n"
        "<i>Для точного результату вказуйте місто, наприклад: Телиги 50, Київ</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dt_date:"), PassengerStates.departure_time)
async def passenger_dt_date(callback: CallbackQuery, state: FSMContext) -> None:
    date_iso = callback.data.split(":", 1)[1]
    await state.update_data(_dt_date=date_iso)
    d = date.fromisoformat(date_iso)
    label = "сьогодні" if d == _today() else d.strftime("%d.%m.%Y")
    await callback.message.edit_text(
        f"📅 Дата: <b>{label}</b>\n\nОберіть бажаний час поїздки:",
        parse_mode="HTML",
        reply_markup=time_picker_kb(date_iso),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dt_time:"), PassengerStates.departure_time)
async def passenger_dt_time(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")  # ["dt_time", "2026-06-15", "14", "00"]
    date_iso, hour, minute = parts[1], parts[2], parts[3]
    dt = datetime.strptime(f"{date_iso} {hour}:{minute}", "%Y-%m-%d %H:%M")
    if dt < _now():
        await callback.answer("❌ Цей час вже минув!", show_alert=True)
        return
    await state.update_data(departure_time=dt.isoformat())
    await state.set_state(PassengerStates.budget)
    await callback.message.edit_text(
        f"✅ Час поїздки: <b>{dt.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
        "🙋 <b>Крок 4/5</b>\n\nВкажіть ваш плановий бюджет на поїздку (грн):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "dt_manual", PassengerStates.departure_time)
async def passenger_dt_manual(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(_dt_mode="full")
    await callback.message.edit_text(
        "✏️ Введіть дату та час поїздки:\n"
        "<i>Формат: ДД.ММ ГГ:ХХ або ДД.ММ.РРРР ГГ:ХХ\n"
        "Наприклад: 25.06 14:30</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dt_manual_time:"), PassengerStates.departure_time)
async def passenger_dt_manual_time(callback: CallbackQuery, state: FSMContext) -> None:
    date_iso = callback.data.split(":", 1)[1]
    await state.update_data(_dt_date=date_iso, _dt_mode="time_only")
    d = date.fromisoformat(date_iso)
    await callback.message.edit_text(
        f"📅 Дата: <b>{d.strftime('%d.%m.%Y')}</b>\n\n"
        "✏️ Введіть бажаний час поїздки:\n<i>Формат: ГГ:ХХ, наприклад: 14:30</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(PassengerStates.departure_time, F.text)
async def passenger_time(message: Message, state: FSMContext) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    data = await state.get_data()
    dt_mode = data.get("_dt_mode", "full")

    if dt_mode == "time_only":
        date_iso = data.get("_dt_date")
        try:
            d = date.fromisoformat(date_iso)
            t = datetime.strptime(message.text.strip(), "%H:%M").time()
            dt = datetime.combine(d, t)
        except ValueError:
            await message.answer(
                "❌ Неправильний формат. Введіть час, наприклад: <code>14:30</code>",
                parse_mode="HTML",
            )
            return
    else:
        dt = _parse_datetime(message.text)
        if not dt:
            await message.answer(
                "❌ Неправильний формат. Введіть: <code>25.06 14:30</code> або <code>25.06.2026 14:30</code>",
                parse_mode="HTML",
            )
            return

    if dt < _now():
        await message.answer("❌ Час поїздки вже минув. Введіть майбутній час.")
        return

    await state.update_data(departure_time=dt.isoformat())
    await state.set_state(PassengerStates.budget)
    await message.answer(
        f"✅ Час поїздки: {dt.strftime('%d.%m.%Y %H:%M')}\n\n"
        "🙋 <b>Крок 4/5</b>\n\nВкажіть ваш плановий бюджет на поїздку (грн):",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )


@router.message(PassengerStates.budget, F.text)
async def passenger_budget(message: Message, state: FSMContext) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    try:
        budget = float(message.text.replace(",", "."))
        if budget <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введіть коректну суму, наприклад: <code>120</code>", parse_mode="HTML")
        return

    await state.update_data(budget=budget)
    await state.set_state(PassengerStates.passengers_count)
    await message.answer(
        f"✅ Бюджет: до {budget:.0f} грн\n\n"
        "🙋 <b>Крок 5/5</b>\n\nСкільки пасажирів?",
        parse_mode="HTML",
        reply_markup=passengers_count_kb(),
    )


@router.callback_query(F.data.startswith("pax_count:"), PassengerStates.passengers_count)
async def passenger_count_cb(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    count = int(callback.data.split(":")[1])
    data = await state.get_data()
    dt = datetime.fromisoformat(data["departure_time"])

    trip = Trip(
        user_id=callback.from_user.id,
        role="passenger",
        from_address=data["from_address"],
        from_lat=data["from_lat"],
        from_lon=data["from_lon"],
        to_address=data["to_address"],
        to_lat=data["to_lat"],
        to_lon=data["to_lon"],
        departure_time=dt,
        price=data["budget"],
        seats=count,
        status="ACTIVE",
    )
    session.add(trip)
    await session.commit()
    await session.refresh(trip)

    await state.clear()
    await callback.message.edit_text(f"✅ Пасажирів: {count}")
    await callback.message.answer(
        "Чудово 👌 Вашу заявку створено. Ми вже шукаємо для вас підходящі варіанти і повідомимо, як тільки вони з'являться.",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()

    matches = await find_matches_for_trip(trip, session)
    for matched_trip in matches:
        match = await create_match(matched_trip, trip, session)
        if match:
            await notify_new_match(bot, trip, matched_trip, match)
            await notify_new_match(bot, matched_trip, trip, match)


@router.message(PassengerStates.passengers_count, F.text)
async def passenger_count(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    try:
        count = int(message.text.strip())
        if count < 1 or count > 4:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введіть кількість пасажирів від 1 до 4.")
        return

    data = await state.get_data()
    dt = datetime.fromisoformat(data["departure_time"])

    trip = Trip(
        user_id=message.from_user.id,
        role="passenger",
        from_address=data["from_address"],
        from_lat=data["from_lat"],
        from_lon=data["from_lon"],
        to_address=data["to_address"],
        to_lat=data["to_lat"],
        to_lon=data["to_lon"],
        departure_time=dt,
        price=data["budget"],
        seats=count,
        status="ACTIVE",
    )
    session.add(trip)
    await session.commit()
    await session.refresh(trip)

    await state.clear()
    await message.answer(
        "Чудово 👌 Вашу заявку створено. Ми вже шукаємо для вас підходящі варіанти і повідомимо, як тільки вони з'являться.",
        reply_markup=main_menu_kb(),
    )

    # Search for matching drivers
    matches = await find_matches_for_trip(trip, session)
    for matched_trip in matches:
        match = await create_match(matched_trip, trip, session)
        if match:
            await notify_new_match(bot, trip, matched_trip, match)
            await notify_new_match(bot, matched_trip, trip, match)
