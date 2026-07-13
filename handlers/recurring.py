"""
"Зробити поїздку регулярною" flow: offered right after a trip is created and
right after one completes (see driver.py / passenger.py / notifications.py).
Pattern → (daily | weekdays | custom day picker) → time → creates a
RecurringTrip template and links the source Trip to it.
"""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Trip, RecurringTrip
from keyboards.keyboards import (
    recur_pattern_kb, recur_days_kb, recur_time_kb, main_menu_kb,
)
from services.recurring import mask_label, spawn_next, MASK_DAILY, MASK_WEEKDAYS
from states.states import RecurringStates

router = Router()


async def _own_open_trip(session: AsyncSession, trip_id: int, user_id: int) -> Trip | None:
    """Trip must belong to the caller and not already be recurring."""
    trip = await session.get(Trip, trip_id)
    if not trip or trip.user_id != user_id or trip.recurring_id:
        return None
    return trip


@router.callback_query(F.data.startswith("rc:dismiss:"))
async def rc_dismiss(callback: CallbackQuery) -> None:
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data.startswith("rc:offer:"))
async def rc_offer(callback: CallbackQuery, session: AsyncSession) -> None:
    trip_id = int(callback.data.split(":")[2])
    trip = await _own_open_trip(session, trip_id, callback.from_user.id)
    if not trip:
        await callback.answer("Цю поїздку вже не можна зробити регулярною.", show_alert=True)
        return
    await callback.message.edit_text(
        "🔁 <b>Як часто повторювати цю поїздку?</b>",
        parse_mode="HTML",
        reply_markup=recur_pattern_kb(trip_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rc:pat:"))
async def rc_pattern(callback: CallbackQuery, session: AsyncSession) -> None:
    _, _, trip_id_s, pattern = callback.data.split(":")
    trip_id = int(trip_id_s)
    trip = await _own_open_trip(session, trip_id, callback.from_user.id)
    if not trip:
        await callback.answer("Цю поїздку вже не можна зробити регулярною.", show_alert=True)
        return

    if pattern == "daily":
        await callback.message.edit_text(
            "🕒 О котрій годині відправлятись щодня?",
            reply_markup=recur_time_kb(trip_id, MASK_DAILY),
        )
    elif pattern == "weekdays":
        await callback.message.edit_text(
            "🕒 О котрій годині відправлятись Пн–Пт?",
            reply_markup=recur_time_kb(trip_id, MASK_WEEKDAYS),
        )
    else:  # custom
        await callback.message.edit_text(
            "🗓 Оберіть дні (можна декілька), потім натисніть «Завершити вибір»:",
            reply_markup=recur_days_kb(trip_id, "0000000"),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("rc:day:"))
async def rc_day_toggle(callback: CallbackQuery) -> None:
    _, _, trip_id_s, mask = callback.data.split(":")
    trip_id = int(trip_id_s)
    await callback.message.edit_text(
        f"🗓 Обрано: {mask_label(mask)}\n\nОберіть ще дні або завершіть вибір:",
        reply_markup=recur_days_kb(trip_id, mask),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rc:done:"))
async def rc_days_done(callback: CallbackQuery) -> None:
    _, _, trip_id_s, mask = callback.data.split(":")
    trip_id = int(trip_id_s)
    await callback.message.edit_text(
        f"🗓 Дні: {mask_label(mask)}\n\n🕒 О котрій годині відправлятись?",
        reply_markup=recur_time_kb(trip_id, mask),
    )
    await callback.answer()


async def _finalize(session: AsyncSession, trip_id: int, mask: str, hour: int, minute: int, user_id: int) -> RecurringTrip | None:
    trip = await _own_open_trip(session, trip_id, user_id)
    if not trip:
        return None
    rt = RecurringTrip(
        user_id=trip.user_id, role=trip.role,
        from_address=trip.from_address, from_lat=trip.from_lat, from_lon=trip.from_lon,
        to_address=trip.to_address, to_lat=trip.to_lat, to_lon=trip.to_lon,
        price=trip.price, seats=trip.seats,
        departure_hour=hour, departure_minute=minute,
        days_mask=mask, is_active=True,
    )
    session.add(rt)
    await session.flush()
    trip.recurring_id = rt.id
    # If the source trip already finished (offered after completion), no future
    # closure will ever trigger spawn_next for it — seed the first upcoming
    # instance right now. If it's still open/upcoming, the natural closure hook
    # (auto_close_expired_trips / send_rating_prompts) will spawn the next one.
    if trip.status == "CLOSED":
        await spawn_next(session, trip)
    await session.commit()
    return rt


@router.callback_query(F.data.startswith("rc:time:"))
async def rc_time_chosen(callback: CallbackQuery, session: AsyncSession) -> None:
    _, _, trip_id_s, mask, hh, mm = callback.data.split(":")
    rt = await _finalize(session, int(trip_id_s), mask, int(hh), int(mm), callback.from_user.id)
    if not rt:
        await callback.answer("Цю поїздку вже не можна зробити регулярною.", show_alert=True)
        return
    await callback.message.edit_text(
        f"✅ <b>Поїздку зроблено регулярною!</b>\n\n"
        f"🗓 {mask_label(mask)}\n🕒 {hh}:{mm}\n\n"
        f"Нову заявку на той самий маршрут і час бот створюватиме автоматично після "
        f"завершення кожної поїздки. Керувати можна в «Мої поїздки → 🔁 Регулярні».",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rc:time_manual:"))
async def rc_time_manual_ask(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, trip_id_s, mask = callback.data.split(":")
    await state.set_state(RecurringStates.entering_time)
    await state.update_data(trip_id=int(trip_id_s), mask=mask)
    await callback.message.edit_text("🕒 Введіть час у форматі ГГ:ХХ (наприклад 07:30):")
    await callback.answer()


@router.message(RecurringStates.entering_time, F.text)
async def rc_time_manual_entered(message: Message, state: FSMContext, session: AsyncSession) -> None:
    text = message.text.strip()
    try:
        hh_s, mm_s = text.split(":")
        hour, minute = int(hh_s), int(mm_s)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        await message.answer("❌ Невірний формат. Введіть час як ГГ:ХХ, наприклад 07:30.")
        return

    data = await state.get_data()
    await state.clear()
    rt = await _finalize(session, data["trip_id"], data["mask"], hour, minute, message.from_user.id)
    if not rt:
        await message.answer("Цю поїздку вже не можна зробити регулярною.", reply_markup=main_menu_kb())
        return
    await message.answer(
        f"✅ <b>Поїздку зроблено регулярною!</b>\n\n"
        f"🗓 {mask_label(data['mask'])}\n🕒 {hour:02d}:{minute:02d}\n\n"
        f"Нову заявку на той самий маршрут і час бот створюватиме автоматично після "
        f"завершення кожної поїздки. Керувати можна в «Мої поїздки → 🔁 Регулярні».",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data.startswith("rc:cancel:"))
async def rc_cancel(callback: CallbackQuery, session: AsyncSession) -> None:
    rt_id = int(callback.data.split(":")[2])
    rt = await session.get(RecurringTrip, rt_id)
    if not rt or rt.user_id != callback.from_user.id:
        await callback.answer("Не знайдено.", show_alert=True)
        return
    rt.is_active = False
    await session.commit()
    await callback.message.edit_text("❌ Регулярність скасовано. Нові заявки більше не створюватимуться автоматично.")
    await callback.answer()
