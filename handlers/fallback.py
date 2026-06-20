from aiogram import Router, F
from aiogram.types import Message
from aiogram.fsm.state import default_state

from keyboards.keyboards import main_menu_kb

router = Router()


@router.message(default_state, F.text)
async def unknown_message(message: Message) -> None:
    await message.answer(
        "Натисніть /start або оберіть дію в меню нижче:",
        reply_markup=main_menu_kb(),
    )
