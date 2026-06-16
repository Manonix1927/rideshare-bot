"""
In-memory cache for bot_settings table.
Sync access (get/s) for use in synchronous keyboard functions.
Refreshed at startup and every minute by the scheduler.
"""
from sqlalchemy import select

from database.database import AsyncSessionLocal
from database.models import BotSetting

# Defaults — same as admin/routes.py DEFAULT_SETTINGS
DEFAULTS: dict[str, str] = {
    "btn_driver":           "🚗 Я водій",
    "btn_passenger":        "🙋 Я пасажир",
    "btn_search":           "🔍 Поїздки поруч",
    "btn_mytrips":          "📋 Мої поїздки",
    "btn_rating":           "⭐ Мій рейтинг",
    "btn_support":          "🛟 Підтримка",
    "btn_faq":              "❓ Часті питання",
    "btn_confirm":          "✅ Підтвердити",
    "btn_reject":           "❌ Відмовитися",
    "btn_departed":         "🚀 Виїхав до попутника",
    "btn_map_driver":       "🗺 Відкрити карту поїздки",
    "btn_cancel_driver":    "❌ Відмінити поїздку",
    "btn_map_passenger":    "🗺 Відстежити водія на карті",
    "btn_cancel_passenger": "❌ Відмінити поїздку",
    "btn_ready":            "✅ Я на місці!",
    "btn_map_pax":          "🗺 Відстежити водія",
    "btn_cancel_pax":       "❌ Відмінити поїздку",
    "btn_map_rem":          "🗺 Відкрити карту поїздки",
    "msg_welcome":          "Вітаю! Я допоможу знайти попутника або пасажира. Оберіть дію 👇",
    "msg_confirmed_driver": "🎉 Поїздку підтверджено. Натисніть «Виїхав» коли вирушите до пасажира.",
    "msg_confirmed_pax":    "🎉 Поїздку підтверджено. Очікуйте — водій натисне кнопку «Виїхав», тоді вам прийде сповіщення.",
    "msg_reminder":         "⏰ Ваша поїздка через 10 хвилин!\n\n🗺 {route}\n🕒 {time}",
    "msg_departed_pax":     "🚗 Водій вже їде до вас! Натисніть «Я на місці» як тільки прийдете до точки зустрічі.",
    "msg_ready_driver":     "✅ Пасажир вже на місці! Він чекає на вас 🤝",
}

_cache: dict[str, str] = dict(DEFAULTS)


def get(key: str) -> str:
    """Sync read from in-memory cache."""
    return _cache.get(key, DEFAULTS.get(key, key))


async def reload() -> None:
    """Reload all settings from DB. Called at startup and by scheduler."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(BotSetting))).scalars().all()
    _cache.clear()
    _cache.update(DEFAULTS)
    for row in rows:
        _cache[row.key] = row.value
