import asyncio
from database.database import AsyncSessionLocal, init_db
from database.models import Trip, User, Match
from sqlalchemy import select
from services.geo import haversine_km

async def check():
    await init_db()
    async with AsyncSessionLocal() as s:
        trips = (await s.execute(
            select(Trip).order_by(Trip.created_at.desc()).limit(10)
        )).scalars().all()

        print("=== ПОЇЗДКИ (останні 10) ===")
        for t in trips:
            print(f"ID={t.id} | {t.role:9} | {t.status:10} | user={t.user_id}")
            print(f"  from: {t.from_address[:50]}")
            print(f"        ({t.from_lat:.5f}, {t.from_lon:.5f})")
            print(f"  to:   {t.to_address[:50]}")
            print(f"        ({t.to_lat:.5f}, {t.to_lon:.5f})")
            print(f"  time={t.departure_time}  price={t.price}  seats={t.seats}")
            print()

        # Cross-check active drivers vs passengers
        active = [t for t in trips if t.status in ("ACTIVE", "MATCHING", "CONFIRMED")]
        drivers    = [t for t in active if t.role == "driver"]
        passengers = [t for t in active if t.role == "passenger"]

        print("=== АНАЛІЗ ЗБІГІВ ===")
        if not drivers:
            print("Немає активних водіїв")
        if not passengers:
            print("Немає активних пасажирів")

        for d in drivers:
            for p in passengers:
                if d.user_id == p.user_id:
                    continue
                df = haversine_km(d.from_lat, d.from_lon, p.from_lat, p.from_lon)
                dt = haversine_km(d.to_lat,   d.to_lon,   p.to_lat,   p.to_lon)
                time_diff_min = abs((d.departure_time - p.departure_time).total_seconds()) / 60

                ok_from = df <= 3.0
                ok_to   = dt <= 3.0
                ok_time = time_diff_min <= 60

                print(f"\nВодій#{d.id} vs Пасажир#{p.id}:")
                print(f"  Відстань від старту:  {df:.2f} km  {'OK' if ok_from else 'FAIL >3km'}")
                print(f"  Відстань до фіналу:   {dt:.2f} km  {'OK' if ok_to else 'FAIL >3km'}")
                print(f"  Різниця часу:         {time_diff_min:.0f} хв  {'OK' if ok_time else 'FAIL >60min'}")
                print(f"  РЕЗУЛЬТАТ: {'ЗБІГ!' if (ok_from and ok_to and ok_time) else 'НЕ ЗБІГАЄТЬСЯ'}")

asyncio.run(check())
