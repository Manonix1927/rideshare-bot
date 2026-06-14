from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database.models import Trip, User
from keyboards.keyboards import geo_or_text_kb, cancel_kb, main_menu_kb
from services.geo import geocode_address, reverse_geocode
from services.matching import find_matches_for_trip, create_match
from services.notifications import notify_new_match
from states.states import PassengerStates
from config import MAX_ACTIVE_TRIPS

router = Router()


def _parse_datetime(text: str) -> datetime | None:
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M", "%H:%M"):
        try:
            dt = datetime.strptime(text.strip(), fmt)
            if fmt == "%H:%M":
                now = datetime.now()
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
            elif fmt == "%d.%m %H:%M":
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    return None


@router.message(F.text == "🙋 Я пасажир")
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
    await state.update_data(from_lat=lat, from_lon=lon, from_address=address)
    await state.set_state(PassengerStates.to_address)
    await message.answer(
        f"✅ Відправлення: {address}\n\n🙋 <b>Крок 2/5</b>\n\nВкажіть адресу пункту призначення:",
        parse_mode="HTML",
        reply_markup=geo_or_text_kb(),
    )


@router.message(PassengerStates.from_address, F.text)
async def passenger_from_text(message: Message, state: FSMContext) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    result = await geocode_address(message.text)
    if not result:
        await message.answer("❌ Не вдалося знайти адресу. Спробуйте ще раз або надішліть геолокацію.")
        return

    lat, lon, address = result
    await state.update_data(from_lat=lat, from_lon=lon, from_address=address)
    await state.set_state(PassengerStates.to_address)
    await message.answer(
        f"✅ Відправлення: {address}\n\n🙋 <b>Крок 2/5</b>\n\nВкажіть адресу пункту призначення:",
        parse_mode="HTML",
        reply_markup=geo_or_text_kb(),
    )


@router.message(PassengerStates.to_address, F.location)
async def passenger_to_location(message: Message, state: FSMContext) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    address = await reverse_geocode(lat, lon)
    await state.update_data(to_lat=lat, to_lon=lon, to_address=address)
    await state.set_state(PassengerStates.departure_time)
    await message.answer(
        f"✅ Призначення: {address}\n\n🙋 <b>Крок 3/5</b>\n\n"
        "Вкажіть бажаний час поїздки:\n<i>(Формат: ДД.ММ.РРРР ГГ:ХХ або ГГ:ХХ для сьогодні)</i>",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
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

    result = await geocode_address(message.text, near_lat=near_lat, near_lon=near_lon)
    if not result:
        await message.answer("❌ Не вдалося знайти адресу. Спробуйте ще раз або надішліть геолокацію.")
        return

    lat, lon, address = result
    await state.update_data(to_lat=lat, to_lon=lon, to_address=address)
    await state.set_state(PassengerStates.departure_time)
    await message.answer(
        f"✅ Призначення: <b>{address}</b>\n"
        f"<i>Якщо це не той населений пункт — поверніться і введіть адресу з назвою міста.</i>\n\n"
        "🙋 <b>Крок 3/5</b>\n\n"
        "Вкажіть бажаний час поїздки:\n<i>(Формат: ДД.ММ.РРРР ГГ:ХХ або ГГ:ХХ для сьогодні)</i>",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )


@router.message(PassengerStates.departure_time, F.text)
async def passenger_time(message: Message, state: FSMContext) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    dt = _parse_datetime(message.text)
    if not dt:
        await message.answer(
            "❌ Неправильний формат. Введіть час, наприклад: <code>25.06.2025 14:30</code>",
            parse_mode="HTML",
        )
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
        reply_markup=cancel_kb(),
    )


@router.message(PassengerStates.passengers_count, F.text)
async def passenger_count(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    try:
        count = int(message.text.strip())
        if count < 1 or count > 8:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введіть кількість пасажирів від 1 до 8.")
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
