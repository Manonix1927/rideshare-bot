# 🚗 RideShare Bot — BlaBlaCar для Telegram

Telegram-бот для пошуку попутників всередині міста та між містами.

## Швидкий старт

### 1. Встановлення залежностей

```bash
cd bot
pip install -r requirements.txt
```

### 2. Налаштування .env

Скопіюйте `.env.example` → `.env` та заповніть:

```
BOT_TOKEN=токен_від_BotFather
ADMIN_IDS=ваш_telegram_id
WEBAPP_URL=https://ваш-домен.com/webapp    # URL хостингу Mini App
DB_PATH=rideshare.db
```

### 3. Запуск бота

```bash
python main.py
```

---

## Mini App (карта маршруту)

Файли у папці `webapp/`:
- `index.html` — розмітка
- `styles.css` — стилі (адаптовані під теми Telegram)
- `app.js` — логіка карти (Leaflet + OSRM routing)

### Хостинг Mini App

Необхідний HTTPS. Варіанти:

**GitHub Pages (безкоштовно):**
```bash
# Завантажте папку webapp/ у репозиторій GitHub
# Увімкніть Pages у Settings → Pages
# WEBAPP_URL = https://<username>.github.io/<repo>/
```

**Netlify (безкоштовно):**
```bash
# Перетягніть папку webapp/ на netlify.com/drop
# WEBAPP_URL = згенерований URL
```

**Vercel:**
```bash
npx vercel bot/webapp
```

**Власний сервер (nginx):**
```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;
    root /path/to/bot/webapp;
    index index.html;
}
```

---

## Структура проекту

```
bot/
├── main.py                 # Точка входу, планувальник
├── config.py               # Конфігурація з .env
├── requirements.txt
├── database/
│   ├── models.py           # SQLAlchemy моделі
│   └── database.py         # Ініціалізація БД, сідінг FAQ
├── handlers/
│   ├── start.py            # /start, головне меню
│   ├── driver.py           # Флоу водія (FSM)
│   ├── passenger.py        # Флоу пасажира (FSM)
│   ├── announcements.py    # Всі оголошення + пропозиції
│   ├── matching.py         # Підтвердження/відмова + рейтинг
│   ├── my_trips.py         # Мої поїздки (перегляд/редагування/видалення)
│   ├── rating.py           # Мій рейтинг
│   ├── support.py          # Підтримка
│   ├── faq.py              # Часті питання
│   └── admin.py            # Панель адміністратора (/admin)
├── services/
│   ├── geo.py              # Nominatim геокодинг, haversine
│   ├── matching.py         # Алгоритм підбору ±3км / ±1год
│   └── notifications.py    # Авто-закриття, запит оцінки
├── keyboards/
│   └── keyboards.py        # Всі клавіатури
├── states/
│   └── states.py           # FSM стани
└── webapp/
    ├── index.html          # Telegram Mini App
    ├── styles.css
    └── app.js              # Leaflet + OSRM маршрут
```

---

## Алгоритм підбору

| Параметр | Допуск |
|----------|--------|
| Відправлення | ±3 км |
| Призначення | ±3 км |
| Час | ±1 година |

Геокодинг: **Nominatim (OpenStreetMap)** — безкоштовно, без ключів.

---

## Адмін-панель

Доступна лише для ID з `ADMIN_IDS`. Команда: `/admin`

Функції:
- 📊 Статистика (користувачі, поїздки, збіги, рейтинги)
- 👥 Список користувачів + блокування
- 🚗 Активні поїздки + примусове закриття
- 📩 Звернення підтримки
- 📉 Аналіз причин відмов
- ❓ Управління FAQ (без зміни коду)

---

## Статуси заявок

| Статус | Значення |
|--------|----------|
| `ACTIVE` | Активна, шукає збіг |
| `MATCHING` | Знайдено потенційний збіг |
| `CONFIRMED` | Обидві сторони підтвердили |
| `CLOSED` | Завершена або закрита |
