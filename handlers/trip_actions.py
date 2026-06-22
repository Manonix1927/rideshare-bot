"""
Pre-departure trip actions:
  - Driver: Виїхав до попутника
  - Passenger: Я на місці
  - Both: Відмінити поїздку
"""
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.models import Match, Trip
from keyboards.keyboards import (
    confirmed_trip_driver_kb,
    confirmed_trip_passenger_kb,
    passenger_alert_kb,
    map_only_kb,
    driver_cancel_reasons_kb,
    passenger_cancel_reasons_kb,
    cancel_reason_skip_kb,
)
from services.tracking import build_track_url
from states.states import CancelTripStates

router = Router()

_DRIVER_REASONS = {
    "trust":         "Пасажир не викликає довіри",
    "drunk":         "Пасажир п'яний/неадекватний",
    "no_answer":     "Пасажир не відповідає",
    "changed_terms": "Пасажир змінив умови",
    "car_issue":     "Технічна несправність авто",
    "force_majeure": "Форс-мажор",
    "other":         "Інше",
}
_PASSENGER_REASONS = {
    "trust":         "Водій не викликає довіри",
    "overprice":     "Водій запросив оплату більше ніж домовлено",
    "no_answer":     "Водій не відповідає",
    "no_show":       "Водій не приїхав",
    "late":          "Водій надто запізнюється",
    "force_majeure": "Форс-мажор",
    "other":         "Інше",
}


async def _load_match(match_id: int, session: AsyncSession) -> Match | None:
    result = await session.execute(
        select(Match)
        .options(
            selectinload(Match.driver_trip).selectinload(Trip.user),
            selectinload(Match.passenger_trip).selectinload(Trip.user),
        )
        .where(Match.id == match_id)
    )
    return result.scalars().first()


# ── Водій натиснув "Виїхав до попутника" ──────────────────────────────────────

@router.callback_query(F.data.startswith("trip_departed:"))
async def trip_departed(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    match_id = int(callback.data.split(":")[1])
    match = await _load_match(match_id, session)

    if not match or match.status != "CONFIRMED":
        await callback.answer("Поїздка вже недоступна.", show_alert=True)
        return

    if match.driver_trip.user_id != callback.from_user.id:
        await callback.answer("Ця дія тільки для водія.", show_alert=True)
        return

    if match.driver_departed:
        await callback.answer("Ви вже надіслали це сповіщення.", show_alert=True)
        return

    match.driver_departed = True
    await session.commit()

    driver_user = match.driver_trip.user
    passenger_user = match.passenger_trip.user
    track_url = build_track_url(match, driver_user.id, passenger_user.id)

    # Оновлюємо повідомлення водія
    await callback.message.edit_text(
        "🚀 <b>Пасажир сповіщений!</b>\n\n"
        "Ваш пасажир знає, що ви вже їдете. "
        "Відкрийте карту для навігації.",
        parse_mode="HTML",
        reply_markup=map_only_kb(track_url, "🗺 Відкрити карту поїздки"),
    )

    # Сповіщаємо пасажира
    try:
        await bot.send_message(
            passenger_user.id,
            "🚗 <b>Водій вже їде до вас!</b>\n\n"
            "Натисніть «Я на місці» як тільки прийдете до точки зустрічі.",
            parse_mode="HTML",
            reply_markup=passenger_alert_kb(match_id, track_url),
        )
    except Exception:
        pass

    await callback.answer("Пасажира сповіщено! 🚀")


# ── Пасажир натиснув "Я на місці" ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("passenger_ready:"))
async def passenger_arrived(callback: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    match_id = int(callback.data.split(":")[1])
    match = await _load_match(match_id, session)

    if not match or match.status != "CONFIRMED":
        await callback.answer("Поїздка вже недоступна.", show_alert=True)
        return

    if match.passenger_trip.user_id != callback.from_user.id:
        await callback.answer("Ця дія тільки для пасажира.", show_alert=True)
        return

    if match.passenger_ready:
        await callback.answer("Ви вже надіслали це сповіщення.", show_alert=True)
        return

    match.passenger_ready = True
    await session.commit()

    driver_user = match.driver_trip.user
    passenger_user = match.passenger_trip.user
    track_url = build_track_url(match, driver_user.id, passenger_user.id)

    # Оновлюємо повідомлення пасажира
    await callback.message.edit_text(
        "✅ <b>Водія сповіщено!</b>\n\nВи вже на місці. Водій скоро під'їде 🙂",
        parse_mode="HTML",
        reply_markup=map_only_kb(track_url, "🗺 Відстежити водія"),
    )

    # Сповіщаємо водія
    try:
        await bot.send_message(
            driver_user.id,
            "✅ <b>Пасажир вже на місці!</b>\n\nВін чекає на вас 🤝",
            parse_mode="HTML",
            reply_markup=map_only_kb(track_url, "🗺 Відкрити карту поїздки"),
        )
    except Exception:
        pass

    await callback.answer("Водія сповіщено! 👍")


# ── Відмінити поїздку — вибір причини ─────────────────────────────────────────

@router.callback_query(F.data.startswith("trip_cancel:"))
async def trip_cancel_start(callback: CallbackQuery, session: AsyncSession) -> None:
    parts = callback.data.split(":")
    match_id, role = int(parts[1]), parts[2]
    match = await _load_match(match_id, session)

    if not match or match.status != "CONFIRMED":
        await callback.answer("Поїздка вже недоступна.", show_alert=True)
        return

    caller = callback.from_user.id
    if role == "driver" and match.driver_trip.user_id != caller:
        await callback.answer("Помилка доступу.", show_alert=True)
        return
    if role == "passenger" and match.passenger_trip.user_id != caller:
        await callback.answer("Помилка доступу.", show_alert=True)
        return

    kb = driver_cancel_reasons_kb(match_id) if role == "driver" else passenger_cancel_reasons_kb(match_id)
    await callback.message.edit_text("Вкажіть причину відміни поїздки:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("trip_cancel_back:"))
async def trip_cancel_back(callback: CallbackQuery, session: AsyncSession) -> None:
    parts = callback.data.split(":")
    match_id, role = int(parts[1]), parts[2]
    match = await _load_match(match_id, session)
    if not match:
        await callback.answer()
        return

    if role == "driver":
        kb = confirmed_trip_driver_kb(match_id, None)
        text = "🚗 <b>Ваша підтверджена поїздка</b>\n\nНатисніть «Виїхав» коли вирушите до пасажира."
    else:
        kb = confirmed_trip_passenger_kb(match_id, None)
        text = "🙋 <b>Ваша підтверджена поїздка</b>\n\nОчікуйте повідомлення від водія."

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_reason:"))
async def cancel_reason_chosen(callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext) -> None:
    parts = callback.data.split(":")
    match_id, role, code = int(parts[1]), parts[2], parts[3]
    match = await _load_match(match_id, session)

    if not match or match.status != "CONFIRMED":
        await callback.answer("Поїздка вже недоступна.", show_alert=True)
        return

    if code == "other":
        await state.set_state(CancelTripStates.typing_reason)
        await state.update_data(match_id=match_id, role=role)
        await callback.message.edit_text(
            "✍️ Напишіть причину скасування:",
            reply_markup=cancel_reason_skip_kb(match_id, role),
        )
        await callback.answer()
        return

    reason_map = _DRIVER_REASONS if role == "driver" else _PASSENGER_REASONS
    reason_label = reason_map.get(code, "Інше")
    await _do_cancel(match, role, reason_label, session, bot, callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_reason_skip:"))
async def cancel_reason_skip(callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    parts = callback.data.split(":")
    match_id, role = int(parts[1]), parts[2]
    match = await _load_match(match_id, session)

    if not match or match.status != "CONFIRMED":
        await callback.answer("Поїздка вже недоступна.", show_alert=True)
        return

    await _do_cancel(match, role, "Інше", session, bot, callback.message)
    await callback.answer()


@router.message(CancelTripStates.typing_reason)
async def cancel_custom_reason(message: Message, session: AsyncSession, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()

    match_id = data.get("match_id")
    role = data.get("role")
    match = await _load_match(match_id, session)

    if not match or match.status != "CONFIRMED":
        await message.answer("Поїздка вже недоступна.")
        return

    reason_label = f"Інше: {message.text.strip()}"
    await _do_cancel(match, role, reason_label, session, bot, message)


async def _do_cancel(match: Match, role: str, reason_label: str, session: AsyncSession, bot: Bot, reply_target) -> None:
    match.status = "CANCELLED"
    match.cancelled_by = role
    match.cancel_reason = reason_label
    match.driver_trip.status = "ACTIVE"
    match.passenger_trip.status = "ACTIVE"
    await session.commit()

    if role == "driver":
        notify_id = match.passenger_trip.user_id
        notify_text = (
            f"😔 <b>Водій скасував поїздку</b>\n"
            f"Причина: {reason_label}\n\n"
            "Ваш пошук відновлено автоматично."
        )
    else:
        notify_id = match.driver_trip.user_id
        notify_text = (
            f"😔 <b>Пасажир скасував поїздку</b>\n"
            f"Причина: {reason_label}\n\n"
            "Ваш пошук відновлено автоматично."
        )

    try:
        await bot.send_message(notify_id, notify_text, parse_mode="HTML")
    except Exception:
        pass

    result_text = (
        f"✅ Поїздку скасовано.\n"
        f"Причина: <i>{reason_label}</i>\n\n"
        "Ваш пошук відновлено — шукаємо нові варіанти."
    )
    if hasattr(reply_target, "edit_text"):
        await reply_target.edit_text(result_text, parse_mode="HTML")
    else:
        await reply_target.answer(result_text, parse_mode="HTML")
