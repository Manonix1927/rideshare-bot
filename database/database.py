from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from config import DB_PATH, DATABASE_URL
from database.models import Base, FAQ

if DATABASE_URL:
    # Normalize to postgresql+asyncpg:// regardless of what Railway provides
    _url = DATABASE_URL
    _url = _url.replace("postgresql://", "postgresql+asyncpg://", 1)
    _url = _url.replace("postgres://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(_url, echo=False)
else:
    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}", echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def _run_migrations() -> None:
    """Safely add new columns to existing tables (idempotent)."""
    is_pg = "postgresql" in str(engine.url)
    new_cols = [
        ("matches", "reminder_sent",    "BOOLEAN DEFAULT FALSE"),
        ("matches", "driver_departed",  "BOOLEAN DEFAULT FALSE"),
        ("matches", "passenger_ready",  "BOOLEAN DEFAULT FALSE"),
        ("matches", "cancelled_by",     "VARCHAR"),
        ("matches", "cancel_reason",    "VARCHAR"),
    ]
    async with engine.begin() as conn:
        for table, col, col_type in new_cols:
            if is_pg:
                sql = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"
            else:
                sql = f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
            try:
                await conn.execute(text(sql))
            except Exception:
                pass  # column already exists


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _run_migrations()
    await _seed_faq()


async def _seed_faq() -> None:
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        result = await session.execute(select(FAQ))
        if result.scalars().first():
            return
        defaults = [
            FAQ(
                question="Як це працює?",
                answer=(
                    "1️⃣ Створіть поїздку або заявку.\n"
                    "2️⃣ Ми підберемо підходящі варіанти.\n"
                    "3️⃣ Підтвердіть поїздку.\n"
                    "4️⃣ Отримайте контакти попутника.\n"
                    "5️⃣ Після поїздки залиште оцінку."
                ),
                order_idx=1,
            ),
            FAQ(
                question="Як публікується моя заявка?",
                answer=(
                    "Після створення заявка з'являється у розділі «📢 Всі оголошення» "
                    "та стає доступною для гео-пошуку. Контакти відкриваються лише "
                    "після взаємного підтвердження поїздки."
                ),
                order_idx=2,
            ),
            FAQ(
                question="Скільки активних заявок я можу мати?",
                answer="Не більше 2 активних заявок одночасно.",
                order_idx=3,
            ),
            FAQ(
                question="Як відбувається оцінювання?",
                answer=(
                    "Через 5 хвилин після запланованого часу зустрічі бот надішле "
                    "питання «Чи відбулась поїздка?». Після завершення ви зможете "
                    "оцінити попутника від 1 до 5 зірок."
                ),
                order_idx=4,
            ),
            FAQ(
                question="Що таке рейтинг?",
                answer=(
                    "Рейтинг — середній бал на основі оцінок від інших користувачів. "
                    "Часті скасування знижують рейтинг. Перед підтвердженням поїздки "
                    "ви завжди бачите рейтинг потенційного попутника."
                ),
                order_idx=5,
            ),
        ]
        session.add_all(defaults)
        await session.commit()


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
