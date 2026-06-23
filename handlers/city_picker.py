"""Shared callback handler for city disambiguation during address input."""
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext

from keyboards.keyboards import dest_kb, date_picker_kb

router = Router()


@router.callback_query(F.data.startswith("pick_city:"))
async def pick_city(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    role, field, idx = parts[1], parts[2], int(parts[3])

    data = await state.get_data()
    candidates = data.get("city_candidates", [])
    if idx >= len(candidates):
        await callback.answer("Помилка. Введіть адресу знову.", show_alert=True)
        return

    lat, lon, address, city = candidates[idx]
    role_emoji = "🚗" if role == "driver" else "🙋"

    await state.update_data(city_candidates=None)

    if field == "from":
        await state.update_data(from_lat=lat, from_lon=lon, from_address=address, from_city=city)
        from states.states import DriverStates, PassengerStates
        next_state = DriverStates.to_address if role == "driver" else PassengerStates.to_address
        await state.set_state(next_state)
        await callback.message.edit_text(f"✅ Відправлення: {address}")
        await callback.message.answer(
            f"{role_emoji} <b>Крок 2/5</b>\n\nВкажіть адресу пункту призначення:\n\n💡 <i>Приклад: Святошинська, 10, Київ</i>",
            parse_mode="HTML",
            reply_markup=dest_kb(),
        )

    elif field == "to":
        await state.update_data(to_lat=lat, to_lon=lon, to_address=address)
        from states.states import DriverStates, PassengerStates
        next_state = DriverStates.departure_time if role == "driver" else PassengerStates.departure_time
        await state.set_state(next_state)
        await callback.message.edit_text(f"✅ Призначення: {address}")
        await callback.message.answer(
            f"{role_emoji} <b>Крок 3/5</b>\n\nОберіть дату виїзду:",
            parse_mode="HTML",
            reply_markup=date_picker_kb(),
        )

    await callback.answer()
