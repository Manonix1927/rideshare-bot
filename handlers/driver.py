import json as _json
from datetime import datetime, date
from services.timezone import now as _now, today as _today
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database.models import Trip, User
from keyboards.keyboards import geo_or_text_kb, dest_kb, cancel_kb, main_menu_kb, confirm_address_kb, confirm_with_map_kb, addr_not_found_kb, city_picker_kb, date_picker_kb, time_picker_kb, seats_kb
from services import bot_settings as _s
from services.geo import geocode_address, geocode_address_multi, reverse_geocode, get_city_from_coords, _detect_city, _intended_locality
from services.matching import find_matches_for_trip, create_match
from services.notifications import notify_new_match
from states.states import DriverStates
from config import MAX_ACTIVE_TRIPS

router = Router()


async def _to_date_step(message: Message, address: str) -> None:
    """Confirm destination, drop the lingering reply keyboard, then show the
    inline date picker. A single message can't carry both ReplyKeyboardRemove
    and an inline keyboard, so we send two."""
    await message.answer(
        f"✅ Призначення: {address}", reply_markup=ReplyKeyboardRemove()
    )
    await message.answer(
        "🚗 <b>Крок 3/5</b>\n\nОберіть дату виїзду:",
        parse_mode="HTML",
        reply_markup=date_picker_kb(),
    )


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


@router.message(F.text.func(lambda t: t == _s.get("btn_driver")))
async def driver_start(message: Message, state: FSMContext, session: AsyncSession) -> None:
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

    await state.set_state(DriverStates.from_address)
    await message.answer(
        "🚗 <b>Нова поїздка — крок 1/5</b>\n\nВкажіть адресу відправлення:\n\n💡 <i>Приклад: Святошинська, 10, Київ</i>",
        parse_mode="HTML",
        reply_markup=geo_or_text_kb(),
    )


@router.message(DriverStates.from_address, F.location)
async def driver_from_location(message: Message, state: FSMContext, session: AsyncSession) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    address = await reverse_geocode(lat, lon)
    if not address:
        await message.answer("❌ Не вдалося визначити адресу. Спробуйте ще раз або введіть адресу вручну.")
        return
    city = await get_city_from_coords(lat, lon)
    await state.update_data(from_lat=lat, from_lon=lon, from_address=address, from_city=city)
    await state.set_state(DriverStates.to_address)
    await message.answer(
        f"✅ Відправлення: {address}\n\n🚗 <b>Крок 2/5</b>\n\nВкажіть адресу пункту призначення:\n\n💡 <i>Приклад: Святошинська, 10, Київ</i>",
        parse_mode="HTML",
        reply_markup=dest_kb(),
    )


@router.message(DriverStates.from_address, F.text)
async def driver_from_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    # Confirmation reply buttons
    if message.text == "✅ Адреса правильна":
        data = await state.get_data()
        if not data.get("pending_from_lat"):
            await message.answer("ℹ️ Спочатку введіть адресу.", reply_markup=geo_or_text_kb())
            return
        await state.update_data(
            from_lat=data["pending_from_lat"],
            from_lon=data["pending_from_lon"],
            from_address=data["pending_from_address"],
            from_city=data["pending_from_city"],
        )
        await state.set_state(DriverStates.to_address)
        await message.answer(
            f"✅ Відправлення: {data['pending_from_address']}\n\n🚗 <b>Крок 2/5</b>\n\nВкажіть адресу пункту призначення:\n\n💡 <i>Приклад: Святошинська, 10, Київ</i>",
            parse_mode="HTML",
            reply_markup=dest_kb(),
        )
        return

    if message.text == "🔄 Ввести інший":
        await state.update_data(pending_from_lat=None)
        await message.answer("🚗 Введіть адресу відправлення:\n\n💡 <i>Приклад: Святошинська, 10, Київ</i>", reply_markup=geo_or_text_kb())
        return

    user = await session.get(User, message.from_user.id)
    home_city = user.home_city if user else None

    candidates = await geocode_address_multi(message.text, home_city=home_city)
    if not candidates:
        await message.answer(
            "❌ Не вдалося знайти адресу.\n\n"
            "💡 Спробуйте у форматі:\n"
            "<code>Назва вулиці, номер, Місто</code>\n"
            "<i>Наприклад: Хрещатик 22, Київ</i>\n\n"
            "🗺 Якщо вулицю перейменовано і її не знаходить — натисніть "
            "«Обрати місце на карті» нижче і вкажіть точку.",
            parse_mode="HTML",
        )
        return

    if len(candidates) > 1:
        await state.update_data(city_candidates=candidates)
        await message.answer(
            "📍 Знайдено вулицю у кількох містах. Оберіть потрібне:\n\n"
            "💡 <i>Немає вашої вулиці? Введіть точніше — наприклад: Святошинська, 10, Київ</i>",
            parse_mode="HTML",
            reply_markup=city_picker_kb(candidates, role="driver", field="from"),
        )
        return

    lat, lon, address, city = candidates[0]
    await state.update_data(
        pending_from_lat=lat, pending_from_lon=lon,
        pending_from_address=address, pending_from_city=city,
    )
    await message.answer(
        f"📍 Знайдено: <b>{address}</b>\n\nЦе правильна адреса відправлення?",
        parse_mode="HTML",
        reply_markup=confirm_with_map_kb(lat, lon),
    )


@router.message(DriverStates.from_address, F.web_app_data)
async def driver_from_webapp(message: Message, state: FSMContext, bot: Bot) -> None:
    await message.delete()
    try:
        data = _json.loads(message.web_app_data.data)
        lat, lon = float(data["lat"]), float(data["lon"])
        address = data.get("address") or await reverse_geocode(lat, lon)
    except Exception:
        await message.answer("❌ Помилка отримання даних з карти. Спробуйте ще раз.")
        return
    if not address:
        await message.answer("❌ Не вдалося визначити адресу. Спробуйте ще раз або введіть вручну.")
        return
    city = await get_city_from_coords(lat, lon)
    await state.update_data(from_lat=lat, from_lon=lon, from_address=address, from_city=city)
    await state.set_state(DriverStates.to_address)
    await message.answer(
        f"✅ Відправлення: {address}\n\n🚗 <b>Крок 2/5</b>\n\nВкажіть адресу пункту призначення:\n\n💡 <i>Приклад: Святошинська, 10, Київ</i>",
        parse_mode="HTML",
        reply_markup=dest_kb(),
    )


@router.callback_query(F.data == "addr_ok:driver:from", DriverStates.from_address)
async def driver_from_addr_ok(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(
        from_lat=data["pending_from_lat"],
        from_lon=data["pending_from_lon"],
        from_address=data["pending_from_address"],
        from_city=data["pending_from_city"],
        pending_confirm_msg_id=None,
    )
    await state.set_state(DriverStates.to_address)
    await callback.message.edit_text(
        f"✅ Відправлення: {data['pending_from_address']}",
        parse_mode="HTML",
    )
    await callback.message.answer(
        "🚗 <b>Крок 2/5</b>\n\nВкажіть адресу пункту призначення:\n\n💡 <i>Приклад: Святошинська, 10, Київ</i>",
        parse_mode="HTML",
        reply_markup=dest_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "addr_retry:driver:from", DriverStates.from_address)
async def driver_from_addr_retry(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🚗 Введіть адресу відправлення:\n\n💡 <i>Приклад: Святошинська, 10, Київ</i>\n"
        "<i>Для точного результату вказуйте місто, наприклад: Святошинська, 10, Київ</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(DriverStates.to_address, F.web_app_data)
async def driver_to_webapp(message: Message, state: FSMContext, bot: Bot) -> None:
    await message.delete()
    try:
        data = _json.loads(message.web_app_data.data)
        lat, lon = float(data["lat"]), float(data["lon"])
        address = data.get("address") or await reverse_geocode(lat, lon)
    except Exception:
        await message.answer("❌ Помилка отримання даних з карти. Спробуйте ще раз.")
        return
    if not address:
        await message.answer("❌ Не вдалося визначити адресу. Спробуйте ще раз або введіть вручну.")
        return
    await state.update_data(to_lat=lat, to_lon=lon, to_address=address)
    await state.set_state(DriverStates.departure_time)
    await _to_date_step(message, address)


@router.message(DriverStates.to_address, F.location)
async def driver_to_location(message: Message, state: FSMContext) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    address = await reverse_geocode(lat, lon)
    if not address:
        await message.answer("❌ Не вдалося визначити адресу. Спробуйте ще раз або введіть адресу вручну.")
        return
    await state.update_data(to_lat=lat, to_lon=lon, to_address=address)
    await state.set_state(DriverStates.departure_time)
    await _to_date_step(message, address)


@router.message(DriverStates.to_address, F.text)
async def driver_to_text(message: Message, state: FSMContext) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    # Confirmation reply buttons
    if message.text == "✅ Адреса правильна":
        data = await state.get_data()
        if not data.get("pending_to_lat"):
            await message.answer("ℹ️ Спочатку введіть адресу.", reply_markup=dest_kb())
            return
        await state.update_data(
            to_lat=data["pending_to_lat"],
            to_lon=data["pending_to_lon"],
            to_address=data["pending_to_address"],
        )
        await state.set_state(DriverStates.departure_time)
        await _to_date_step(message, data["pending_to_address"])
        return

    if message.text == "🔄 Ввести інший":
        await state.update_data(pending_to_lat=None)
        await message.answer("🚗 Введіть адресу призначення:\n\n💡 <i>Приклад: Святошинська, 10, Київ</i>", reply_markup=dest_kb())
        return

    data = await state.get_data()
    near_lat = data.get("from_lat")
    near_lon = data.get("from_lon")
    from_city = data.get("from_city", "")

    query = message.text
    # Only append the origin city when the user named NO locality at all. Use the
    # broad check so villages ("…, Віта-Поштова") count — otherwise we'd append
    # "Київ" and resolve the village street to Kyiv.
    if not _intended_locality(query) and from_city and from_city.lower() not in query.lower():
        query = f"{query}, {from_city}"

    candidates = await geocode_address_multi(query, near_lat=near_lat, near_lon=near_lon)
    if not candidates:
        await message.answer(
            "❌ Не вдалося знайти адресу.\n\n"
            "💡 Спробуйте у форматі:\n"
            "<code>Назва вулиці, номер, Місто</code>\n"
            "<i>Наприклад: Сагайдачного 5, Київ</i>\n\n"
            "🗺 Якщо вулицю перейменовано і її не знаходить — натисніть "
            "«Обрати місце на карті» нижче і вкажіть точку.",
            parse_mode="HTML",
        )
        return

    if len(candidates) > 1:
        await state.update_data(city_candidates=candidates)
        await message.answer(
            "📍 Знайдено вулицю у кількох містах. Оберіть потрібне:\n\n"
            "💡 <i>Немає вашої вулиці? Введіть точніше — наприклад: Святошинська, 10, Київ</i>",
            parse_mode="HTML",
            reply_markup=city_picker_kb(candidates, role="driver", field="to"),
        )
        return

    lat, lon, address, _city = candidates[0]
    await state.update_data(pending_to_lat=lat, pending_to_lon=lon, pending_to_address=address)
    await message.answer(
        f"📍 Знайдено: <b>{address}</b>\n\nЦе правильна адреса призначення?",
        parse_mode="HTML",
        reply_markup=confirm_with_map_kb(lat, lon),
    )


@router.callback_query(F.data == "addr_ok:driver:to", DriverStates.to_address)
async def driver_addr_ok(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(
        to_lat=data["pending_to_lat"],
        to_lon=data["pending_to_lon"],
        to_address=data["pending_to_address"],
    )
    await state.set_state(DriverStates.departure_time)
    await callback.message.edit_text(
        f"✅ Призначення: {data['pending_to_address']}\n\n"
        "🚗 <b>Крок 3/5</b>\n\nОберіть дату виїзду:",
        parse_mode="HTML",
        reply_markup=date_picker_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "addr_retry:driver:to", DriverStates.to_address)
async def driver_addr_retry(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "🚗 Введіть адресу пункту призначення:\n"
        "<i>Для точного результату вказуйте місто, наприклад: Святошинська, 10, Київ</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dt_date:"), DriverStates.departure_time)
async def driver_dt_date(callback: CallbackQuery, state: FSMContext) -> None:
    date_iso = callback.data.split(":", 1)[1]
    await state.update_data(_dt_date=date_iso)
    d = date.fromisoformat(date_iso)
    label = "сьогодні" if d == _today() else d.strftime("%d.%m.%Y")
    await callback.message.edit_text(
        f"📅 Дата: <b>{label}</b>\n\nОберіть час виїзду:",
        parse_mode="HTML",
        reply_markup=time_picker_kb(date_iso),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dt_time:"), DriverStates.departure_time)
async def driver_dt_time(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")  # ["dt_time", "2026-06-15", "14", "00"]
    date_iso, hour, minute = parts[1], parts[2], parts[3]
    dt = datetime.strptime(f"{date_iso} {hour}:{minute}", "%Y-%m-%d %H:%M")
    if dt < _now():
        await callback.answer("❌ Цей час вже минув!", show_alert=True)
        return
    await state.update_data(departure_time=dt.isoformat())
    await state.set_state(DriverStates.price)
    await callback.message.edit_text(
        f"✅ Час виїзду: <b>{dt.strftime('%d.%m.%Y %H:%M')}</b>\n\n"
        "🚗 <b>Крок 4/5</b>\n\nВкажіть вартість поїздки для одного пасажира (грн):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "dt_manual", DriverStates.departure_time)
async def driver_dt_manual(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(_dt_mode="full")
    await callback.message.edit_text(
        "✏️ Введіть дату та час виїзду:\n"
        "<i>Формат: ДД.ММ ГГ:ХХ або ДД.ММ.РРРР ГГ:ХХ\n"
        "Наприклад: 25.06 14:30</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dt_manual_time:"), DriverStates.departure_time)
async def driver_dt_manual_time(callback: CallbackQuery, state: FSMContext) -> None:
    date_iso = callback.data.split(":", 1)[1]
    await state.update_data(_dt_date=date_iso, _dt_mode="time_only")
    d = date.fromisoformat(date_iso)
    await callback.message.edit_text(
        f"📅 Дата: <b>{d.strftime('%d.%m.%Y')}</b>\n\n"
        "✏️ Введіть час виїзду:\n<i>Формат: ГГ:ХХ, наприклад: 14:30</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(DriverStates.departure_time, F.text)
async def driver_time(message: Message, state: FSMContext) -> None:
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
    await state.set_state(DriverStates.price)
    await message.answer(
        f"✅ Час виїзду: {dt.strftime('%d.%m.%Y %H:%M')}\n\n"
        "🚗 <b>Крок 4/5</b>\n\nВкажіть вартість поїздки для одного пасажира (грн):",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )


@router.message(DriverStates.price, F.text)
async def driver_price(message: Message, state: FSMContext) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    try:
        price = float(message.text.replace(",", "."))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введіть коректну суму, наприклад: <code>150</code>", parse_mode="HTML")
        return

    await state.update_data(price=price)
    await state.set_state(DriverStates.seats)
    await message.answer(
        f"✅ Вартість: {price:.0f} грн/пасажир\n\n"
        "🚗 <b>Крок 5/5</b>\n\nСкільки вільних місць у вашому авто?",
        parse_mode="HTML",
        reply_markup=seats_kb(),
    )


@router.callback_query(F.data.startswith("seats:"), DriverStates.seats)
async def driver_seats_cb(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    seats = int(callback.data.split(":")[1])
    data = await state.get_data()
    dt = datetime.fromisoformat(data["departure_time"])

    trip = Trip(
        user_id=callback.from_user.id,
        role="driver",
        from_address=data["from_address"],
        from_lat=data["from_lat"],
        from_lon=data["from_lon"],
        to_address=data["to_address"],
        to_lat=data["to_lat"],
        to_lon=data["to_lon"],
        departure_time=dt,
        price=data["price"],
        seats=seats,
        status="ACTIVE",
    )
    session.add(trip)

    # Save home_city if intra-city trip and user has none yet
    from_city = data.get("from_city", "")
    to_city = await get_city_from_coords(data["to_lat"], data["to_lon"])
    if from_city and to_city and from_city.lower() == to_city.lower():
        user = await session.get(User, callback.from_user.id)
        if user and not user.home_city:
            user.home_city = from_city

    await session.commit()
    await session.refresh(trip)

    await state.clear()
    await callback.message.edit_text(f"✅ Місць: {seats}")
    await callback.message.answer(
        "Чудово 👌 Вашу поїздку опубліковано. Якщо з'являться підходящі пасажири — ми одразу повідомимо.",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()

    matches = await find_matches_for_trip(trip, session)
    for matched_trip in matches:
        match = await create_match(trip, matched_trip, session)
        if match:
            await notify_new_match(bot, trip, matched_trip, match)
            await notify_new_match(bot, matched_trip, trip, match)


@router.message(DriverStates.seats, F.text)
async def driver_seats(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    try:
        seats = int(message.text.strip())
        if seats < 1 or seats > 4:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введіть кількість місць від 1 до 4.")
        return

    data = await state.get_data()
    dt = datetime.fromisoformat(data["departure_time"])

    trip = Trip(
        user_id=message.from_user.id,
        role="driver",
        from_address=data["from_address"],
        from_lat=data["from_lat"],
        from_lon=data["from_lon"],
        to_address=data["to_address"],
        to_lat=data["to_lat"],
        to_lon=data["to_lon"],
        departure_time=dt,
        price=data["price"],
        seats=seats,
        status="ACTIVE",
    )
    session.add(trip)

    from_city = data.get("from_city", "")
    to_city = await get_city_from_coords(data["to_lat"], data["to_lon"])
    if from_city and to_city and from_city.lower() == to_city.lower():
        user = await session.get(User, message.from_user.id)
        if user and not user.home_city:
            user.home_city = from_city

    await session.commit()
    await session.refresh(trip)

    await state.clear()
    await message.answer(
        "Чудово 👌 Вашу поїздку опубліковано. Якщо з'являться підходящі пасажири — ми одразу повідомимо.",
        reply_markup=main_menu_kb(),
    )

    # Search for matching passengers
    matches = await find_matches_for_trip(trip, session)
    for matched_trip in matches:
        match = await create_match(trip, matched_trip, session)
        if match:
            await notify_new_match(bot, trip, matched_trip, match)
            await notify_new_match(bot, matched_trip, trip, match)
