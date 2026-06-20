"""
"Всі поїздки" section — all active driver/passenger trips + nearby search.
"""
import urllib.parse
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, WebAppInfo
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from database.models import Trip
from keyboards.keyboards import main_menu_kb, geo_or_text_kb
from services.rich_cards import send_trip_card
from services import bot_settings as _s
from states.states import SearchStates
from config import WEBAPP_URL

router = Router()

PAGE_SIZE = 5


def _all_trips_menu_kb(drivers: int, passengers: int) -> object:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=f"🚗 Водії — {drivers}", callback_data="all:driver:0"),
        InlineKeyboardButton(text=f"🙋 Пасажири — {passengers}", callback_data="all:passenger:0"),
    )
    return builder.as_markup()


def _trip_map_kb(trip: Trip) -> object:
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
        builder.row(InlineKeyboardButton(text="🗺 Маршрут на карті", web_app=WebAppInfo(url=url)))
    builder.row(InlineKeyboardButton(text="👉 Запропонувати поїздку", callback_data=f"offer_trip:{trip.id}"))
    return builder.as_markup()


async def _count_active(session: AsyncSession) -> tuple[int, int]:
    drivers = (await session.execute(
        select(func.count()).where(Trip.role == "driver", Trip.status.in_(["ACTIVE", "MATCHING"]))
    )).scalar() or 0
    passengers = (await session.execute(
        select(func.count()).where(Trip.role == "passenger", Trip.status.in_(["ACTIVE", "MATCHING"]))
    )).scalar() or 0
    return drivers, passengers


# ── Entry ──────────────────────────────────────────────────────────────────────

@router.message(F.text.func(lambda t: t == _s.get("btn_all_trips")))
async def all_trips_menu(message: Message, session: AsyncSession) -> None:
    drivers, passengers = await _count_active(session)
    await message.answer(
        "🗺 <b>Всі поїздки</b>\n\nОберіть категорію:",
        parse_mode="HTML",
        reply_markup=_all_trips_menu_kb(drivers, passengers),
    )


# ── Paginated list ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("all:driver:") | F.data.startswith("all:passenger:"))
async def all_trips_page(callback: CallbackQuery, session: AsyncSession) -> None:
    parts = callback.data.split(":")
    role, page = parts[1], int(parts[2])

    result = await session.execute(
        select(Trip)
        .options(selectinload(Trip.user))
        .where(Trip.role == role, Trip.status.in_(["ACTIVE", "MATCHING"]))
        .order_by(Trip.departure_time.asc())
    )
    trips = result.scalars().all()

    total = len(trips)
    if total == 0:
        await callback.answer("Поїздок немає.", show_alert=True)
        return

    role_label = "Водії" if role == "driver" else "Пасажири"
    role_emoji = "🚗" if role == "driver" else "🙋"
    start = page * PAGE_SIZE
    page_items = trips[start: start + PAGE_SIZE]

    await callback.message.answer(
        f"{role_emoji} <b>{role_label}</b> — {total} поїздок:",
        parse_mode="HTML",
    )

    for trip in page_items:
        await send_trip_card(
            bot=callback.bot,
            chat_id=callback.message.chat.id,
            trip=trip,
            user=trip.user,
            dist_km=None,
            reply_markup=_trip_map_kb(trip),
        )

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️ Назад", callback_data=f"all:{role}:{page - 1}")
    if start + PAGE_SIZE < total:
        nav.button(text="▶️ Далі", callback_data=f"all:{role}:{page + 1}")
    nav.adjust(2)
    nav.row(InlineKeyboardButton(text="🔍 Поїздки поруч", callback_data="all:nearby"))
    nav.row(InlineKeyboardButton(text="↩️ До меню", callback_data="all:back"))

    await callback.message.answer(
        f"Показано {start + 1}–{min(start + PAGE_SIZE, total)} з {total}",
        reply_markup=nav.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "all:back")
async def all_trips_back(callback: CallbackQuery, session: AsyncSession) -> None:
    drivers, passengers = await _count_active(session)
    await callback.message.answer(
        "🗺 <b>Всі поїздки</b>\n\nОберіть категорію:",
        parse_mode="HTML",
        reply_markup=_all_trips_menu_kb(drivers, passengers),
    )
    await callback.answer()


# ── Nearby (delegates to SearchStates flow) ────────────────────────────────────

@router.callback_query(F.data == "all:nearby")
async def all_trips_nearby(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchStates.waiting_location)
    await callback.message.answer(
        "🔍 <b>Поїздки поруч</b>\n\n"
        "Вкажіть вашу точку відправлення — надішліть геолокацію або введіть адресу:",
        parse_mode="HTML",
        reply_markup=geo_or_text_kb(),
    )
    await callback.answer()
