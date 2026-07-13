"""
Search trips near user's location (within 3 km).
"""
import json as _json
import math
import urllib.parse
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.models import Trip, User
from keyboards.keyboards import geo_or_text_kb, main_menu_kb
from services.geo import geocode_address, reverse_geocode, haversine_km, _UA_CITIES
from services.rich_cards import send_trip_card, fmt_rating
from services.matching import get_remaining_seats
from services import bot_settings as _s
from states.states import SearchStates
from config import WEBAPP_URL

router = Router()

SEARCH_RADIUS_KM = 3.0
RESULTS_PER_PAGE = 5


def _bbox(lat: float, lon: float, radius_km: float):
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


async def _find_nearby(
    session: AsyncSession,
    lat: float,
    lon: float,
    role: str,
    exclude_user_id: int,
) -> list[tuple[Trip, float, int | None]]:
    """Returns list of (trip, distance_km, remaining_seats_or_None)."""
    min_lat, max_lat, min_lon, max_lon = _bbox(lat, lon, SEARCH_RADIUS_KM)

    result = await session.execute(
        select(Trip)
        .options(selectinload(Trip.user))
        .where(
            Trip.role == role,
            # Include MATCHING and BOARDING — driver may still have seats
            Trip.status.in_(["ACTIVE", "MATCHING", "BOARDING"]),
            Trip.user_id != exclude_user_id,
            Trip.from_lat.between(min_lat, max_lat),
            Trip.from_lon.between(min_lon, max_lon),
        )
        .order_by(Trip.departure_time.asc())
    )
    trips = result.scalars().all()

    nearby = []
    for t in trips:
        dist = haversine_km(lat, lon, t.from_lat, t.from_lon)
        if dist > SEARCH_RADIUS_KM:
            continue
        if role == "driver":
            remaining = await get_remaining_seats(t, session)
            if remaining <= 0:
                continue  # fully booked
        else:
            remaining = None
        nearby.append((t, dist, remaining))

    nearby.sort(key=lambda x: x[1])
    return nearby


def _trip_card(trip: Trip, dist_km: float) -> str:
    role_emoji = "🚗" if trip.role == "driver" else "🙋"
    price_str = (
        f"{trip.price:.0f} грн" if trip.role == "driver" else f"до {trip.price:.0f} грн"
    )
    seats_str = (
        f"💺 {trip.seats} місць" if trip.role == "driver" else f"👥 {trip.seats} пас."
    )
    rating = trip.user.rating if trip.user else None
    return (
        f"{role_emoji} {', '.join(trip.from_address.split(',')[:2]).strip()} → {', '.join(trip.to_address.split(',')[:2]).strip()}\n"
        f"🕒 {trip.departure_time.strftime('%d.%m %H:%M')}  💰 {price_str}  {seats_str}\n"
        f"⭐ {fmt_rating(rating)}  📍 {dist_km:.1f} км від вас"
    )


def _map_kb(trip: Trip):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton, WebAppInfo
    from keyboards.keyboards import offer_trip_kb

    builder = InlineKeyboardBuilder()
    if WEBAPP_URL:
        url = (
            f"{WEBAPP_URL.rstrip('/')}/?mode=single"
            f"&from_lat={trip.from_lat}&from_lon={trip.from_lon}"
            f"&to_lat={trip.to_lat}&to_lon={trip.to_lon}"
            f"&from_addr={urllib.parse.quote(trip.from_address)}"
            f"&to_addr={urllib.parse.quote(trip.to_address)}"
            f"&time={urllib.parse.quote(trip.departure_time.strftime('%d.%m.%Y %H:%M'))}"
            f"&price={trip.price:.0f}&seats={trip.seats}&role={trip.role}"
        )
        builder.row(InlineKeyboardButton(
            text="🗺 Маршрут на карті", web_app=WebAppInfo(url=url)
        ))
    builder.row(InlineKeyboardButton(
        text="👉 Запропонувати поїздку", callback_data=f"offer_trip:{trip.id}"
    ))
    return builder.as_markup()


def _create_trip_kb() -> object:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🚗 Я водій", callback_data="new_trip:driver"),
        InlineKeyboardButton(text="🙋 Я пасажир", callback_data="new_trip:passenger"),
    )
    return builder.as_markup()


def _role_filter_kb(lat: float, lon: float, driver_n: int, passenger_n: int):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"🚗 Водії ({driver_n})",
            callback_data=f"search:driver:0:{lat}:{lon}",
        ),
        InlineKeyboardButton(
            text=f"🙋 Пасажири ({passenger_n})",
            callback_data=f"search:passenger:0:{lat}:{lon}",
        ),
    )
    return builder.as_markup()


# ── Entry point ────────────────────────────────────────────────────────────────

@router.message(F.text.func(lambda t: t == _s.get("btn_search")))
async def search_start(message: Message, state: FSMContext) -> None:
    await state.set_state(SearchStates.waiting_location)
    await message.answer(
        "🔍 <b>Пошук поїздок поруч</b>\n\n"
        "Вкажіть вашу точку відправлення — надішліть геолокацію або введіть адресу:\n\n💡 <i>Приклад: Святошинська, 10, Київ</i>",
        parse_mode="HTML",
        reply_markup=geo_or_text_kb(),
    )


@router.message(SearchStates.waiting_location, F.location)
async def search_by_location(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    lat, lon = message.location.latitude, message.location.longitude
    await _show_search_results(message, session, lat, lon, message.from_user.id)


@router.message(SearchStates.waiting_location, F.web_app_data)
async def search_by_webapp(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Point picked on the map (🗺 Обрати місце на карті) in the nearby-search flow.
    Without this the picked data had no handler — Telegram showed the grey
    'data sent' notice and nothing happened on some clients."""
    try:
        await message.delete()  # remove the service "data sent" message
    except Exception:
        pass
    try:
        data = _json.loads(message.web_app_data.data)
        lat, lon = float(data["lat"]), float(data["lon"])
    except Exception:
        await message.answer("❌ Помилка отримання даних з карти. Спробуйте ще раз.")
        return
    await state.clear()
    await _show_search_results(message, session, lat, lon, message.from_user.id)


@router.message(SearchStates.waiting_location, F.text)
async def search_by_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.text == "🔙 Головне меню":
        await state.clear()
        await message.answer("Головне меню:", reply_markup=main_menu_kb())
        return

    # Bias geocoding to user's home city so "Івасюка, 40" finds Kyiv, not another city
    user = await session.get(User, message.from_user.id)
    home_city = user.home_city if user else None
    near_lat, near_lon = None, None
    if home_city:
        coords = _UA_CITIES.get(home_city.lower())
        if coords:
            near_lat, near_lon = coords

    result = await geocode_address(message.text, near_lat=near_lat, near_lon=near_lon)
    if not result:
        await message.answer("❌ Не вдалося знайти адресу. Спробуйте ще раз або надішліть геолокацію.")
        return

    await state.clear()
    lat, lon, address = result
    await _show_search_results(message, session, lat, lon, message.from_user.id)


async def _show_search_results(
    message: Message,
    session: AsyncSession,
    lat: float,
    lon: float,
    user_id: int,
) -> None:
    drivers = await _find_nearby(session, lat, lon, "driver", user_id)
    passengers = await _find_nearby(session, lat, lon, "passenger", user_id)

    total = len(drivers) + len(passengers)
    if total == 0:
        await message.answer(
            f"😔 Поїздок у радіусі {SEARCH_RADIUS_KM:.0f} км не знайдено.\n\n"
            "Створіть власну поїздку — і вас знайдуть:",
            reply_markup=_create_trip_kb(),
        )
        return

    await message.answer(
        f"📍 Знайдено <b>{total}</b> поїздок у радіусі {SEARCH_RADIUS_KM:.0f} км:\n\n"
        "Оберіть категорію:",
        parse_mode="HTML",
        reply_markup=_role_filter_kb(lat, lon, len(drivers), len(passengers)),
    )


# ── Paginated results ──────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^search:(driver|passenger):"))
async def search_results_page(callback: CallbackQuery, session: AsyncSession) -> None:
    parts = callback.data.split(":")
    role, page_str, lat_str, lon_str = parts[1], parts[2], parts[3], parts[4]
    page = int(page_str)
    lat, lon = float(lat_str), float(lon_str)

    nearby = await _find_nearby(session, lat, lon, role, callback.from_user.id)

    if not nearby:
        await callback.answer("Поїздок не знайдено.", show_alert=True)
        return

    role_label = "Водії" if role == "driver" else "Пасажири"
    role_emoji = "🚗" if role == "driver" else "🙋"
    total = len(nearby)
    start = page * RESULTS_PER_PAGE
    page_items = nearby[start : start + RESULTS_PER_PAGE]

    await callback.message.answer(
        f"{role_emoji} <b>{role_label}</b> поруч — {total} результатів:",
        parse_mode="HTML",
    )

    for trip, dist, remaining in page_items:
        await send_trip_card(
            bot=callback.bot,
            chat_id=callback.message.chat.id,
            trip=trip,
            user=trip.user,
            dist_km=dist,
            reply_markup=_map_kb(trip),
            remaining_seats=remaining,
        )

    # Pagination nav
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️ Назад", callback_data=f"search:{role}:{page-1}:{lat}:{lon}")
    if start + RESULTS_PER_PAGE < total:
        nav.button(text="▶️ Далі", callback_data=f"search:{role}:{page+1}:{lat}:{lon}")
    nav.adjust(2)
    nav.row(InlineKeyboardButton(text="↩️ Повернутись", callback_data=f"search:back:{lat}:{lon}"))
    nav.row(
        InlineKeyboardButton(text="🚗 Я водій", callback_data="new_trip:driver"),
        InlineKeyboardButton(text="🙋 Я пасажир", callback_data="new_trip:passenger"),
    )

    await callback.message.answer(
        f"Показано {start+1}–{min(start+RESULTS_PER_PAGE, total)} з {total}\n\n"
        "Або створіть власну поїздку:",
        reply_markup=nav.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("search:back:"))
async def search_back(callback: CallbackQuery, session: AsyncSession) -> None:
    parts = callback.data.split(":")
    lat, lon = float(parts[2]), float(parts[3])
    drivers = await _find_nearby(session, lat, lon, "driver", callback.from_user.id)
    passengers = await _find_nearby(session, lat, lon, "passenger", callback.from_user.id)
    await callback.message.answer(
        f"📍 Знайдено <b>{len(drivers) + len(passengers)}</b> поїздок у радіусі {SEARCH_RADIUS_KM:.0f} км:",
        parse_mode="HTML",
        reply_markup=_role_filter_kb(lat, lon, len(drivers), len(passengers)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("new_trip:"))
async def new_trip_from_search(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    from database.models import User
    from sqlalchemy import func as _func
    from states.states import DriverStates, PassengerStates

    role = callback.data.split(":")[1]
    user_id = callback.from_user.id

    user = await session.get(User, user_id)
    if user and user.is_blocked:
        await callback.answer("Ваш акаунт заблоковано.", show_alert=True)
        return

    active_count = await session.scalar(
        select(_func.count()).select_from(Trip).where(
            Trip.user_id == user_id,
            Trip.status.in_(["ACTIVE", "MATCHING", "BOARDING"]),
            Trip.recurring_id.is_(None),  # regular trips don't count toward the quota
        )
    )
    if active_count and active_count >= 3:
        await callback.answer("У вас вже є активні заявки.", show_alert=True)
        return

    await state.clear()
    if role == "driver":
        await state.set_state(DriverStates.from_address)
        await callback.message.answer(
            "🚗 <b>Нова поїздка — крок 1/5</b>\n\nВкажіть адресу відправлення:\n\n💡 <i>Приклад: Святошинська, 10, Київ</i>",
            parse_mode="HTML",
            reply_markup=geo_or_text_kb(),
        )
    else:
        await state.set_state(PassengerStates.from_address)
        await callback.message.answer(
            "🙋 <b>Нова заявка — крок 1/5</b>\n\nВкажіть адресу відправлення:\n\n💡 <i>Приклад: Святошинська, 10, Київ</i>",
            parse_mode="HTML",
            reply_markup=geo_or_text_kb(),
        )
    await callback.answer()
