from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder


def main_menu_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="🚗 Я водій"),
        KeyboardButton(text="🙋 Я пасажир"),
    )
    builder.row(
        KeyboardButton(text="📢 Всі оголошення"),
        KeyboardButton(text="📋 Мої поїздки"),
    )
    builder.row(
        KeyboardButton(text="⭐ Мій рейтинг"),
        KeyboardButton(text="🛟 Підтримка"),
    )
    builder.row(
        KeyboardButton(text="❓ Часті питання"),
        KeyboardButton(text="ℹ️ Як це працює"),
    )
    return builder.as_markup(resize_keyboard=True)


def geo_or_text_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="📍 Надіслати геолокацію", request_location=True))
    builder.row(KeyboardButton(text="🔙 Головне меню"))
    return builder.as_markup(resize_keyboard=True)


def cancel_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="🔙 Головне меню"))
    return builder.as_markup(resize_keyboard=True)


def confirm_address_kb(role: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Вірно", callback_data=f"addr_ok:{role}"),
        InlineKeyboardButton(text="🔄 Ввести інший", callback_data=f"addr_retry:{role}"),
    )
    return builder.as_markup()


def confirm_send_offer_kb(trip_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Так", callback_data=f"offer_yes:{trip_id}"),
        InlineKeyboardButton(text="❌ Ні", callback_data="offer_no"),
    )
    return builder.as_markup()


def trip_offer_response_kb(match_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"match_confirm:{match_id}"),
        InlineKeyboardButton(text="❌ Відмовитися", callback_data=f"match_reject:{match_id}"),
    )
    return builder.as_markup()


def rejection_reason_kb(match_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    reasons = [
        ("💰 Дорого", "expensive"),
        ("🕒 Не підходить час", "time"),
        ("📍 Не підходить маршрут", "route"),
        ("🚫 Вже знайшов поїздку", "found"),
        ("Інше", "other"),
    ]
    for label, code in reasons:
        builder.row(
            InlineKeyboardButton(
                text=label, callback_data=f"reject_reason:{match_id}:{code}"
            )
        )
    return builder.as_markup()


def meeting_happened_kb(match_id: int, role: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Так", callback_data=f"meeting_yes:{match_id}:{role}"),
        InlineKeyboardButton(text="❌ Ні", callback_data=f"meeting_no:{match_id}:{role}"),
    )
    return builder.as_markup()


def trip_finished_kb(match_id: int, role: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🏁 Поїздка завершена", callback_data=f"trip_done:{match_id}:{role}"
        )
    )
    return builder.as_markup()


def rating_kb(match_id: int, to_user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    stars = ["⭐", "⭐⭐", "⭐⭐⭐", "⭐⭐⭐⭐", "⭐⭐⭐⭐⭐"]
    for i, star in enumerate(stars, 1):
        builder.button(
            text=star,
            callback_data=f"rate:{match_id}:{to_user_id}:{i}",
        )
    builder.adjust(5)
    return builder.as_markup()


def my_trips_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🟢 Активні", callback_data="mytrips:active"))
    builder.row(InlineKeyboardButton(text="✅ Підтверджені", callback_data="mytrips:confirmed"))
    builder.row(InlineKeyboardButton(text="🏁 Завершені", callback_data="mytrips:closed"))
    return builder.as_markup()


def active_trip_actions_kb(trip_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"trip_edit:{trip_id}"),
        InlineKeyboardButton(text="🗑 Видалити", callback_data=f"trip_delete:{trip_id}"),
    )
    return builder.as_markup()


def edit_trip_fields_kb(trip_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📍 Звідки", callback_data=f"edit_field:{trip_id}:from"))
    builder.row(InlineKeyboardButton(text="🏁 Куди", callback_data=f"edit_field:{trip_id}:to"))
    builder.row(InlineKeyboardButton(text="🕒 Час відправлення", callback_data=f"edit_field:{trip_id}:time"))
    builder.row(InlineKeyboardButton(text="💰 Вартість / бюджет", callback_data=f"edit_field:{trip_id}:price"))
    builder.row(InlineKeyboardButton(text="💺 Кількість місць", callback_data=f"edit_field:{trip_id}:seats"))
    builder.row(InlineKeyboardButton(text="↩️ Назад", callback_data="mytrips:active"))
    return builder.as_markup()


def confirm_delete_kb(trip_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Так, видалити", callback_data=f"trip_delete_confirm:{trip_id}"),
        InlineKeyboardButton(text="❌ Скасувати", callback_data=f"trip_delete_cancel:{trip_id}"),
    )
    return builder.as_markup()


def confirmed_trip_contact_kb(match_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📞 Контакт попутника", callback_data=f"show_contact:{match_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🏁 Поїздка завершена", callback_data=f"manual_close:{match_id}"
        )
    )
    return builder.as_markup()


def support_type_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🐞 Повідомити про проблему", callback_data="support:bug"))
    builder.row(InlineKeyboardButton(text="💡 Запропонувати покращення", callback_data="support:improvement"))
    builder.row(InlineKeyboardButton(text="⭐ Залишити відгук", callback_data="support:feedback"))
    builder.row(InlineKeyboardButton(text="📞 Зв'язатися з підтримкою", callback_data="support:contact"))
    return builder.as_markup()


def offer_trip_kb(trip_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="👇 Пропонувати поїздку", callback_data=f"offer_trip:{trip_id}")
    )
    return builder.as_markup()


def map_view_kb(webapp_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🗺 Переглянути на карті", web_app=WebAppInfo(url=webapp_url))
    )
    return builder.as_markup()


def admin_main_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"))
    builder.row(InlineKeyboardButton(text="👥 Користувачі", callback_data="admin:users"))
    builder.row(InlineKeyboardButton(text="🚗 Активні поїздки", callback_data="admin:trips"))
    builder.row(InlineKeyboardButton(text="📩 Звернення", callback_data="admin:tickets"))
    builder.row(InlineKeyboardButton(text="📉 Причини відмов", callback_data="admin:rejections"))
    builder.row(InlineKeyboardButton(text="❓ FAQ", callback_data="admin:faq"))
    return builder.as_markup()


def admin_user_actions_kb(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🚫 Заблокувати", callback_data=f"admin_block:{user_id}"),
        InlineKeyboardButton(text="✅ Розблокувати", callback_data=f"admin_unblock:{user_id}"),
    )
    return builder.as_markup()
