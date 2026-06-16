from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database.models import FAQ
from services import bot_settings as _s

router = Router()


@router.message(F.text.func(lambda t: t == _s.get("btn_faq")))
async def faq_list(message: Message, session: AsyncSession) -> None:
    result = await session.execute(select(FAQ).order_by(FAQ.order_idx.asc()))
    faqs = result.scalars().all()

    if not faqs:
        await message.answer("FAQ поки що не заповнено.")
        return

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    builder = InlineKeyboardBuilder()
    for faq in faqs:
        builder.row(
            InlineKeyboardButton(
                text=f"❓ {faq.question}", callback_data=f"faq:{faq.id}"
            )
        )

    await message.answer(
        "❓ <b>Часті питання</b>\n\nОберіть питання:",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("faq:"))
async def faq_answer(callback: CallbackQuery, session: AsyncSession) -> None:
    faq_id = int(callback.data.split(":")[1])
    faq = await session.get(FAQ, faq_id)

    if not faq:
        await callback.answer("Питання не знайдено.", show_alert=True)
        return

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="↩️ До питань", callback_data="faq_back"))

    await callback.message.answer(
        f"❓ <b>{faq.question}</b>\n\n{faq.answer}",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "faq_back")
async def faq_back(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.message.delete()
    await faq_list(callback.message, session)
    await callback.answer()
