from datetime import timedelta

from services import bot_settings as _s
from services.timezone import today as _today
from config import WEBAPP_URL

from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

_DAYS_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def date_picker_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    today = _today()
    for i in range(7):
        d = today + timedelta(days=i)
        if i == 0:
            label = f"Сьогодні {d.strftime('%d.%m')}"
        elif i == 1:
            label = f"Завтра {d.strftime('%d.%m')}"
        else:
            label = f"{_DAYS_UA[d.weekday()]} {d.strftime('%d.%m')}"
        builder.button(text=label, callback_data=f"dt_date:{d.isoformat()}")
    builder.adjust(3, 3, 1)
    builder.row(InlineKeyboardButton(text="✏️ Ввести вручну", callback_data="dt_manual"))
    return builder.as_markup()


def time_picker_kb(date_iso: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for hour in range(6, 24):
        builder.button(text=f"{hour:02d}:00", callback_data=f"dt_time:{date_iso}:{hour:02d}:00")
    builder.adjust(4)
    builder.row(InlineKeyboardButton(text="✏️ Ввести вручну", callback_data=f"dt_manual_time:{date_iso}"))
    return builder.as_markup()


def main_menu_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text=_s.get("btn_driver")),
        KeyboardButton(text=_s.get("btn_passenger")),
    )
    builder.row(
        KeyboardButton(text=_s.get("btn_all_trips")),
        KeyboardButton(text=_s.get("btn_mytrips")),
    )
    builder.row(
        KeyboardButton(text=_s.get("btn_rating")),
        KeyboardButton(text=_s.get("btn_support")),
        KeyboardButton(text=_s.get("btn_faq")),
    )
    return builder.as_markup(resize_keyboard=True)


def geo_or_text_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="📍 Надіслати геолокацію", request_location=True))
    if WEBAPP_URL:
        builder.row(KeyboardButton(
            text="🗺 Обрати місце на карті",
            web_app=WebAppInfo(url=f"{WEBAPP_URL}/?mode=pick"),
        ))
    builder.row(KeyboardButton(text="🔙 Головне меню"))
    return builder.as_markup(resize_keyboard=True)


def dest_kb() -> ReplyKeyboardMarkup:
    """Клавіатура для введення пункту призначення — без геолокації (поточне місце ≠ куди їдемо)."""
    builder = ReplyKeyboardBuilder()
    if WEBAPP_URL:
        builder.row(KeyboardButton(
            text="🗺 Обрати місце на карті",
            web_app=WebAppInfo(url=f"{WEBAPP_URL}/?mode=pick"),
        ))
    builder.row(KeyboardButton(text="🔙 Головне меню"))
    return builder.as_markup(resize_keyboard=True)


def cancel_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="🔙 Головне меню"))
    return builder.as_markup(resize_keyboard=True)


def city_picker_kb(candidates: list, role: str, field: str) -> InlineKeyboardMarkup:
    """Inline keyboard with city buttons for disambiguation."""
    builder = InlineKeyboardBuilder()
    for i, (_lat, _lon, _addr, city) in enumerate(candidates):
        builder.button(text=city, callback_data=f"pick_city:{role}:{field}:{i}")
    builder.adjust(3)
    return builder.as_markup()


def confirm_address_kb(role: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Вірно", callback_data=f"addr_ok:{role}"),
        InlineKeyboardButton(text="🔄 Ввести інший", callback_data=f"addr_retry:{role}"),
    )
    return builder.as_markup()


def confirm_send_offer_kb(trip_id: int, seats: int = 1) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Так", callback_data=f"offer_yes:{trip_id}:{seats}"),
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


def seats_picker_kb(trip_id: int, max_seats: int) -> InlineKeyboardMarkup:
    """Ask passenger how many seats they need before sending an offer."""
    builder = InlineKeyboardBuilder()
    buttons = [
        InlineKeyboardButton(
            text=str(n), callback_data=f"offer_seats:{trip_id}:{n}"
        )
        for n in range(1, max_seats + 1)
    ]
    builder.row(*buttons)
    builder.row(InlineKeyboardButton(text="◀️ Скасувати", callback_data="offer_seats_cancel"))
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


def cancel_confirmed_trip_kb(match_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    reasons = [
        ("🔄 Змінились плани", "plans"),
        ("🚑 Надзвичайна ситуація", "emergency"),
        ("⏰ Не встигаю на цей час", "time"),
        ("🚗 Знайшов інший варіант", "found_other"),
        ("Інше", "other"),
    ]
    for label, code in reasons:
        builder.row(
            InlineKeyboardButton(
                text=label, callback_data=f"cancel_confirmed:{match_id}:{code}"
            )
        )
    builder.row(
        InlineKeyboardButton(text="◀️ Назад", callback_data=f"cancel_confirmed_back:{match_id}")
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
    builder.adjust(3, 2)
    return builder.as_markup()


def my_trips_menu_kb(active: int = 0, confirmed: int = 0, closed: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=f"🟢 Активні — {active}", callback_data="mytrips:active"))
    builder.row(InlineKeyboardButton(text=f"✅ Підтверджені — {confirmed}", callback_data="mytrips:confirmed"))
    builder.row(InlineKeyboardButton(text=f"🏁 Завершені — {closed}", callback_data="mytrips:closed"))
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
    return builder.as_markup()


def offer_trip_kb(trip_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="👇 Пропонувати поїздку", callback_data=f"offer_trip:{trip_id}")
    )
    return builder.as_markup()


def seats_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    labels = ["1 місце", "2 місця", "3 місця", "4 місця"]
    for i, label in enumerate(labels, 1):
        builder.button(text=label, callback_data=f"seats:{i}")
    builder.adjust(4)
    return builder.as_markup()


def passengers_count_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    labels = ["1 пасажир", "2 пасажири", "3 пасажири", "4 пасажири"]
    for i, label in enumerate(labels, 1):
        builder.button(text=label, callback_data=f"pax_count:{i}")
    builder.adjust(4)
    return builder.as_markup()


def map_view_kb(webapp_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🗺 Переглянути на карті", web_app=WebAppInfo(url=webapp_url))
    )
    return builder.as_markup()


_DRIVER_CANCEL_REASONS = [
    ("Пасажир не викликає довіри",      "trust"),
    ("Пасажир п'яний/неадекватний",     "drunk"),
    ("Пасажир не відповідає",           "no_answer"),
    ("Пасажир змінив умови",            "changed_terms"),
    ("Технічна несправність авто",      "car_issue"),
    ("Форс-мажор",                      "force_majeure"),
    ("Інше",                            "other"),
]

_PASSENGER_CANCEL_REASONS = [
    ("Водій не викликає довіри",                    "trust"),
    ("Водій запросив оплату більше ніж домовлено",  "overprice"),
    ("Водій не відповідає",                         "no_answer"),
    ("Водій не приїхав",                            "no_show"),
    ("Водій надто запізнюється",                    "late"),
    ("Форс-мажор",                                  "force_majeure"),
    ("Інше",                                        "other"),
]


def confirmed_trip_driver_kb(match_id: int, track_url: str | None) -> InlineKeyboardMarkup:
    """Driver: map + Виїхав + Відмінити."""
    builder = InlineKeyboardBuilder()
    if track_url:
        builder.row(InlineKeyboardButton(
            text=_s.get("btn_map_driver"), web_app=WebAppInfo(url=track_url),
        ))
    builder.row(InlineKeyboardButton(
        text=_s.get("btn_departed"), callback_data=f"trip_departed:{match_id}",
    ))
    builder.row(InlineKeyboardButton(
        text=_s.get("btn_cancel_driver"), callback_data=f"trip_cancel:{match_id}:driver",
    ))
    return builder.as_markup()


def confirmed_trip_passenger_kb(match_id: int, track_url: str | None) -> InlineKeyboardMarkup:
    """Passenger: map + Відмінити."""
    builder = InlineKeyboardBuilder()
    if track_url:
        builder.row(InlineKeyboardButton(
            text=_s.get("btn_map_passenger"), web_app=WebAppInfo(url=track_url),
        ))
    builder.row(InlineKeyboardButton(
        text=_s.get("btn_cancel_passenger"), callback_data=f"trip_cancel:{match_id}:passenger",
    ))
    return builder.as_markup()


def passenger_alert_kb(match_id: int, track_url: str | None) -> InlineKeyboardMarkup:
    """Passenger: Я на місці + map + Відмінити (shown when driver departed)."""
    builder = InlineKeyboardBuilder()
    if track_url:
        builder.row(InlineKeyboardButton(
            text=_s.get("btn_map_pax"), web_app=WebAppInfo(url=track_url),
        ))
    builder.row(InlineKeyboardButton(
        text=_s.get("btn_ready"), callback_data=f"passenger_ready:{match_id}",
    ))
    builder.row(InlineKeyboardButton(
        text=_s.get("btn_cancel_pax"), callback_data=f"trip_cancel:{match_id}:passenger",
    ))
    return builder.as_markup()


def map_only_kb(track_url: str | None, label: str = "") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if track_url:
        builder.row(InlineKeyboardButton(
            text=label or _s.get("btn_map_rem"), web_app=WebAppInfo(url=track_url),
        ))
    return builder.as_markup()


def driver_cancel_reasons_kb(match_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for label, code in _DRIVER_CANCEL_REASONS:
        builder.row(InlineKeyboardButton(
            text=label, callback_data=f"cancel_reason:{match_id}:driver:{code}",
        ))
    builder.row(InlineKeyboardButton(text="↩️ Назад", callback_data=f"trip_cancel_back:{match_id}:driver"))
    return builder.as_markup()


def passenger_cancel_reasons_kb(match_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for label, code in _PASSENGER_CANCEL_REASONS:
        builder.row(InlineKeyboardButton(
            text=label, callback_data=f"cancel_reason:{match_id}:passenger:{code}",
        ))
    builder.row(InlineKeyboardButton(text="↩️ Назад", callback_data=f"trip_cancel_back:{match_id}:passenger"))
    return builder.as_markup()


def cancel_reason_skip_kb(match_id: int, role: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="Пропустити", callback_data=f"cancel_reason_skip:{match_id}:{role}",
    ))
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
