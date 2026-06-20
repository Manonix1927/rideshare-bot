from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from config import DB_PATH, DATABASE_URL
from database.models import Base, FAQ, BotSetting

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
    """Safely add new columns / change column types in existing tables (idempotent)."""
    is_pg = "postgresql" in str(engine.url)

    # ── New columns ────────────────────────────────────────────────────────────
    new_cols = [
        ("matches", "reminder_sent",              "BOOLEAN DEFAULT FALSE"),
        ("matches", "driver_departed",            "BOOLEAN DEFAULT FALSE"),
        ("matches", "passenger_ready",            "BOOLEAN DEFAULT FALSE"),
        ("matches", "cancelled_by",               "VARCHAR"),
        ("matches", "cancel_reason",              "VARCHAR"),
        ("matches", "pending_reminder_1_sent",    "BOOLEAN DEFAULT FALSE"),
        ("matches", "pending_reminder_2_sent",    "BOOLEAN DEFAULT FALSE"),
    ]
    for table, col, col_type in new_cols:
        sql = (
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"
            if is_pg else
            f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
        )
        async with engine.begin() as conn:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass

    # ── Reset default 5.0 rating for users who have never made a trip ──────────
    async with engine.begin() as conn:
        try:
            await conn.execute(text(
                "UPDATE users SET rating = NULL WHERE trips_count = 0 AND rating = 5.0"
            ))
        except Exception:
            pass

    # ── Telegram user IDs: INTEGER → BIGINT (IDs can exceed 2^31) ─────────────
    if is_pg:
        bigint_steps = [
            # 1. drop FK constraints that reference users.id
            "ALTER TABLE trips DROP CONSTRAINT IF EXISTS trips_user_id_fkey",
            "ALTER TABLE support_tickets DROP CONSTRAINT IF EXISTS support_tickets_user_id_fkey",
            "ALTER TABLE ratings DROP CONSTRAINT IF EXISTS ratings_from_user_id_fkey",
            "ALTER TABLE ratings DROP CONSTRAINT IF EXISTS ratings_to_user_id_fkey",
            # 2. widen the columns
            "ALTER TABLE users ALTER COLUMN id TYPE BIGINT",
            "ALTER TABLE trips ALTER COLUMN user_id TYPE BIGINT",
            "ALTER TABLE support_tickets ALTER COLUMN user_id TYPE BIGINT",
            "ALTER TABLE ratings ALTER COLUMN from_user_id TYPE BIGINT",
            "ALTER TABLE ratings ALTER COLUMN to_user_id TYPE BIGINT",
            # 3. re-add FK constraints
            "ALTER TABLE trips ADD CONSTRAINT trips_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id)",
            "ALTER TABLE support_tickets ADD CONSTRAINT support_tickets_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id)",
            "ALTER TABLE ratings ADD CONSTRAINT ratings_from_user_id_fkey FOREIGN KEY (from_user_id) REFERENCES users(id)",
            "ALTER TABLE ratings ADD CONSTRAINT ratings_to_user_id_fkey FOREIGN KEY (to_user_id) REFERENCES users(id)",
        ]
        for sql in bigint_steps:
            async with engine.begin() as conn:
                try:
                    await conn.execute(text(sql))
                except Exception:
                    pass  # already BIGINT / constraint already exists


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
