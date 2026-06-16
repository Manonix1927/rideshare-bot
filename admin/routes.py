"""
Web admin panel routes for the RideShare Telegram bot.
Mount via setup_admin(app) in main.py.
Auth: ADMIN_TOKEN env var (cookie-based session).
"""
import asyncio
import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp_jinja2
import jinja2
from aiohttp import web
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from database.database import AsyncSessionLocal
from database.models import User, Trip, Match, SupportTicket, FAQ, BotSetting

ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "changeme")
_COOKIE = "adm_s"

DEFAULT_SETTINGS: dict[str, str] = {
    # Banner
    "banner_active":        "0",
    "banner_text":          "",
    # Main menu
    "btn_driver":           "🚗 Я водій",
    "btn_passenger":        "🙋 Я пасажир",
    "btn_search":           "🔍 Поїздки поруч",
    "btn_mytrips":          "📋 Мої поїздки",
    "btn_rating":           "⭐ Мій рейтинг",
    "btn_support":          "🛟 Підтримка",
    "btn_faq":              "❓ Часті питання",
    # Match offer
    "btn_confirm":          "✅ Підтвердити",
    "btn_reject":           "❌ Відмовитися",
    # Confirmed — driver
    "btn_departed":         "🚀 Виїхав до попутника",
    "btn_map_driver":       "🗺 Відкрити карту поїздки",
    "btn_cancel_driver":    "❌ Відмінити поїздку",
    # Confirmed — passenger
    "btn_map_passenger":    "🗺 Відстежити водія на карті",
    "btn_cancel_passenger": "❌ Відмінити поїздку",
    # Passenger alert (driver departed)
    "btn_ready":            "✅ Я на місці!",
    "btn_map_pax":          "🗺 Відстежити водія",
    "btn_cancel_pax":       "❌ Відмінити поїздку",
    # Reminder
    "btn_map_rem":          "🗺 Відкрити карту поїздки",
    # Messages
    "msg_welcome":          "Вітаю! Я допоможу знайти попутника або пасажира. Оберіть дію 👇",
    "msg_confirmed_driver": "🎉 Поїздку підтверджено. Натисніть «Виїхав» коли вирушите до пасажира.",
    "msg_confirmed_pax":    "🎉 Поїздку підтверджено. Очікуйте сповіщення від водія.",
    "msg_reminder":         "⏰ Ваша поїздка через 10 хвилин!\n\n🗺 {маршрут}\n🕒 {час}",
    "msg_departed_pax":     "🚗 Водій вже їде до вас! Натисніть «Я на місці» як тільки прийдете.",
    "msg_ready_driver":     "✅ Пасажир вже на місці! Він чекає на вас 🤝",
}


# ── Auth ──────────────────────────────────────────────────────────────────────

def _token_hash() -> str:
    return hashlib.sha256(ADMIN_TOKEN.encode()).hexdigest()


def _is_authed(request: web.Request) -> bool:
    if not ADMIN_TOKEN:
        return True
    return request.cookies.get(_COOKIE, "") == _token_hash()


def _require_auth(handler):
    async def wrapper(request: web.Request):
        if not _is_authed(request):
            raise web.HTTPFound("/admin/login")
        return await handler(request)
    return wrapper


# ── Login / Logout ────────────────────────────────────────────────────────────

async def admin_login_get(request: web.Request) -> web.Response:
    if _is_authed(request):
        raise web.HTTPFound("/admin/")
    return aiohttp_jinja2.render_template("login.html", request, {"error": None})


async def admin_login_post(request: web.Request) -> web.Response:
    data = await request.post()
    if data.get("token", "") == ADMIN_TOKEN:
        resp = web.HTTPFound("/admin/")
        resp.set_cookie(_COOKIE, _token_hash(), httponly=True, max_age=86400 * 30)
        raise resp
    return aiohttp_jinja2.render_template("login.html", request, {"error": "Невірний токен"})


async def admin_logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/admin/login")
    resp.del_cookie(_COOKIE)
    raise resp


# ── Dashboard ─────────────────────────────────────────────────────────────────

@_require_auth
async def admin_dashboard(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        users_total     = (await s.execute(select(func.count(User.id)))).scalar() or 0
        users_blocked   = (await s.execute(select(func.count(User.id)).where(User.is_blocked == True))).scalar() or 0
        trips_active    = (await s.execute(select(func.count(Trip.id)).where(Trip.status == "ACTIVE"))).scalar() or 0
        trips_confirmed = (await s.execute(select(func.count(Trip.id)).where(Trip.status == "CONFIRMED"))).scalar() or 0
        trips_closed    = (await s.execute(select(func.count(Trip.id)).where(Trip.status == "CLOSED"))).scalar() or 0
        match_confirmed = (await s.execute(select(func.count(Match.id)).where(Match.status == "CONFIRMED"))).scalar() or 0
        match_cancelled = (await s.execute(select(func.count(Match.id)).where(Match.status == "CANCELLED"))).scalar() or 0
        match_closed    = (await s.execute(select(func.count(Match.id)).where(Match.status == "CLOSED"))).scalar() or 0
        unread_tickets  = (await s.execute(select(func.count(SupportTicket.id)).where(SupportTicket.is_read == False))).scalar() or 0

        recent_trips = (await s.execute(
            select(Trip).options(selectinload(Trip.user))
            .where(Trip.status.in_(["ACTIVE", "CONFIRMED", "MATCHING"]))
            .order_by(Trip.created_at.desc()).limit(5)
        )).scalars().all()

    ctx = {
        "active": "dashboard",
        "users_total": users_total,
        "users_blocked": users_blocked,
        "trips_active": trips_active,
        "trips_confirmed": trips_confirmed,
        "trips_closed": trips_closed,
        "match_confirmed": match_confirmed,
        "match_cancelled": match_cancelled,
        "match_closed": match_closed,
        "unread_tickets": unread_tickets,
        "recent_trips": recent_trips,
    }
    return aiohttp_jinja2.render_template("dashboard.html", request, ctx)


# ── Trips ─────────────────────────────────────────────────────────────────────

@_require_auth
async def admin_trips(request: web.Request) -> web.Response:
    status_filter = request.rel_url.query.get("status", "ACTIVE")
    async with AsyncSessionLocal() as s:
        q = select(Trip).options(selectinload(Trip.user)).order_by(Trip.departure_time.desc()).limit(200)
        if status_filter != "ALL":
            q = q.where(Trip.status == status_filter)
        trips = (await s.execute(q)).scalars().all()
    return aiohttp_jinja2.render_template("trips.html", request, {
        "active": "trips", "trips": trips, "status_filter": status_filter,
    })


# ── Users ─────────────────────────────────────────────────────────────────────

@_require_auth
async def admin_users(request: web.Request) -> web.Response:
    search = request.rel_url.query.get("q", "").strip()
    blocked = request.rel_url.query.get("blocked", "")
    async with AsyncSessionLocal() as s:
        q = select(User).order_by(User.created_at.desc()).limit(300)
        if blocked == "1":
            q = q.where(User.is_blocked == True)
        elif blocked == "0":
            q = q.where(User.is_blocked == False)
        users = (await s.execute(q)).scalars().all()
    if search:
        sl = search.lower()
        users = [u for u in users if sl in (u.username or "").lower()
                 or sl in str(u.id) or sl in u.first_name.lower()]
    return aiohttp_jinja2.render_template("users.html", request, {
        "active": "users", "users": users, "search": search, "blocked": blocked,
    })


@_require_auth
async def admin_block_user(request: web.Request) -> web.Response:
    uid = int(request.match_info["user_id"])
    async with AsyncSessionLocal() as s:
        user = await s.get(User, uid)
        if user:
            user.is_blocked = True
            await s.commit()
    raise web.HTTPFound("/admin/users")


@_require_auth
async def admin_unblock_user(request: web.Request) -> web.Response:
    uid = int(request.match_info["user_id"])
    async with AsyncSessionLocal() as s:
        user = await s.get(User, uid)
        if user:
            user.is_blocked = False
            await s.commit()
    raise web.HTTPFound("/admin/users")


# ── Matches ───────────────────────────────────────────────────────────────────

@_require_auth
async def admin_matches(request: web.Request) -> web.Response:
    status_filter = request.rel_url.query.get("status", "CONFIRMED")
    async with AsyncSessionLocal() as s:
        q = (select(Match)
             .options(
                 selectinload(Match.driver_trip).selectinload(Trip.user),
                 selectinload(Match.passenger_trip).selectinload(Trip.user),
             )
             .order_by(Match.created_at.desc()).limit(100))
        if status_filter != "ALL":
            q = q.where(Match.status == status_filter)
        matches = (await s.execute(q)).scalars().all()
    return aiohttp_jinja2.render_template("matches.html", request, {
        "active": "matches", "matches": matches, "status_filter": status_filter,
    })


# ── Tickets ───────────────────────────────────────────────────────────────────

@_require_auth
async def admin_tickets(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        tickets = (await s.execute(
            select(SupportTicket).options(selectinload(SupportTicket.user))
            .order_by(SupportTicket.is_read, SupportTicket.created_at.desc()).limit(200)
        )).scalars().all()
    return aiohttp_jinja2.render_template("tickets.html", request, {
        "active": "tickets", "tickets": tickets,
    })


@_require_auth
async def admin_ticket_read(request: web.Request) -> web.Response:
    tid = int(request.match_info["ticket_id"])
    async with AsyncSessionLocal() as s:
        ticket = await s.get(SupportTicket, tid)
        if ticket:
            ticket.is_read = True
            await s.commit()
    raise web.HTTPFound("/admin/tickets")


# ── FAQ ───────────────────────────────────────────────────────────────────────

@_require_auth
async def admin_faq(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        faqs = (await s.execute(select(FAQ).order_by(FAQ.order_idx, FAQ.id))).scalars().all()
    return aiohttp_jinja2.render_template("faq.html", request, {"active": "faq", "faqs": faqs})


@_require_auth
async def admin_faq_add(request: web.Request) -> web.Response:
    data = await request.post()
    q, a = data.get("question", "").strip(), data.get("answer", "").strip()
    if q and a:
        async with AsyncSessionLocal() as s:
            s.add(FAQ(question=q, answer=a))
            await s.commit()
    raise web.HTTPFound("/admin/faq")


@_require_auth
async def admin_faq_edit(request: web.Request) -> web.Response:
    fid = int(request.match_info["faq_id"])
    data = await request.post()
    q, a = data.get("question", "").strip(), data.get("answer", "").strip()
    async with AsyncSessionLocal() as s:
        faq = await s.get(FAQ, fid)
        if faq and q and a:
            faq.question, faq.answer = q, a
            await s.commit()
    raise web.HTTPFound("/admin/faq")


@_require_auth
async def admin_faq_delete(request: web.Request) -> web.Response:
    fid = int(request.match_info["faq_id"])
    async with AsyncSessionLocal() as s:
        faq = await s.get(FAQ, fid)
        if faq:
            await s.delete(faq)
            await s.commit()
    raise web.HTTPFound("/admin/faq")


# ── Workflow schema ────────────────────────────────────────────────────────────

@_require_auth
async def admin_workflow(request: web.Request) -> web.Response:
    return aiohttp_jinja2.render_template("workflow.html", request, {"active": "workflow"})


# ── Settings ──────────────────────────────────────────────────────────────────

@_require_auth
async def admin_settings(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(BotSetting))).scalars().all()
    db_map = {r.key: r.value for r in rows}
    settings = [{
        "key": k,
        "value": db_map.get(k, v),
        "default": v,
    } for k, v in DEFAULT_SETTINGS.items()]
    return aiohttp_jinja2.render_template("settings.html", request, {
        "active": "settings", "settings": settings,
    })


@_require_auth
async def admin_settings_save(request: web.Request) -> web.Response:
    data = await request.post()
    async with AsyncSessionLocal() as s:
        for key, default in DEFAULT_SETTINGS.items():
            value = data.get(key, "").strip()
            if not value:
                value = default
            row = await s.get(BotSetting, key)
            if row:
                row.value = value
            else:
                s.add(BotSetting(key=key, value=value))
        await s.commit()
    raise web.HTTPFound("/admin/settings")


# ── JSON API ──────────────────────────────────────────────────────────────────

async def api_settings_get(request: web.Request) -> web.Response:
    if not _is_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(BotSetting))).scalars().all()
    db_map = {r.key: r.value for r in rows}
    merged = {k: db_map.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    return web.json_response(merged)


async def api_settings_set(request: web.Request) -> web.Response:
    if not _is_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    key = request.match_info["key"]
    if key not in DEFAULT_SETTINGS:
        return web.json_response({"error": "unknown key"}, status=400)
    try:
        data = await request.json()
        value = str(data.get("value", "")).strip()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    if not value:
        return web.json_response({"error": "empty value"}, status=400)
    async with AsyncSessionLocal() as s:
        row = await s.get(BotSetting, key)
        if row:
            row.value = value
        else:
            s.add(BotSetting(key=key, value=value))
        await s.commit()
    return web.json_response({"ok": True, "key": key, "value": value})


async def api_trips_map(request: web.Request) -> web.Response:
    if not _is_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    async with AsyncSessionLocal() as s:
        trips = (await s.execute(
            select(Trip).options(selectinload(Trip.user))
            .where(Trip.status.in_(["ACTIVE", "CONFIRMED", "MATCHING"]))
        )).scalars().all()
    features = []
    for t in trips:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [t.from_lon, t.from_lat]},
            "properties": {
                "id": t.id, "role": t.role, "status": t.status,
                "from_addr": t.from_address, "to_addr": t.to_address,
                "to_lat": t.to_lat, "to_lon": t.to_lon,
                "time": t.departure_time.strftime("%d.%m %H:%M"),
                "price": t.price, "seats": t.seats,
                "user": t.user.first_name if t.user else "?",
                "username": t.user.username if t.user else None,
            }
        })
    return web.json_response({"type": "FeatureCollection", "features": features})


# ── Broadcast ────────────────────────────────────────────────────────────────

@_require_auth
async def admin_broadcast_get(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        bt = await s.get(BotSetting, "banner_text")
        ba = await s.get(BotSetting, "banner_active")
    return aiohttp_jinja2.render_template("broadcast.html", request, {
        "active": "broadcast", "sent": None, "failed": 0, "error": None,
        "banner_text":   bt.value if bt else "",
        "banner_active": ba.value if ba else "0",
    })


@_require_auth
async def admin_broadcast_post(request: web.Request) -> web.Response:
    data = await request.post()
    text = data.get("text", "").strip()
    parse_mode = data.get("parse_mode", "HTML")

    async with AsyncSessionLocal() as s:
        bt = await s.get(BotSetting, "banner_text")
        ba = await s.get(BotSetting, "banner_active")
    ctx_banner = {"banner_text": bt.value if bt else "", "banner_active": ba.value if ba else "0"}

    if not text:
        return aiohttp_jinja2.render_template("broadcast.html", request, {
            "active": "broadcast", "sent": None, "failed": 0,
            "error": "Порожнє повідомлення", **ctx_banner,
        })

    bot = request.app.get("bot")
    if not bot:
        return aiohttp_jinja2.render_template("broadcast.html", request, {
            "active": "broadcast", "sent": None, "failed": 0,
            "error": "Bot не ініціалізовано — перезапустіть сервер", **ctx_banner,
        })

    async with AsyncSessionLocal() as s:
        users = (await s.execute(
            select(User).where(User.is_blocked == False)
        )).scalars().all()

    sent, failed = 0, 0
    for user in users:
        try:
            await bot.send_message(user.id, text, parse_mode=parse_mode)
            sent += 1
            await asyncio.sleep(0.04)          # ≤25 msg/sec — Telegram limit is 30
        except Exception:
            failed += 1

    return aiohttp_jinja2.render_template("broadcast.html", request, {
        "active": "broadcast", "sent": sent, "failed": failed, "error": None,
        **ctx_banner,
    })


# ── Banner save ───────────────────────────────────────────────────────────────

@_require_auth
async def admin_banner_save(request: web.Request) -> web.Response:
    data = await request.post()
    banner_text   = data.get("banner_text",   "").strip()
    banner_active = "1" if data.get("banner_active", "0") == "1" else "0"
    async with AsyncSessionLocal() as s:
        for key, value in (("banner_text", banner_text), ("banner_active", banner_active)):
            row = await s.get(BotSetting, key)
            if row:
                row.value = value
            else:
                s.add(BotSetting(key=key, value=value))
        await s.commit()
    raise web.HTTPFound("/admin/broadcast")


# ── Stats ─────────────────────────────────────────────────────────────────────

@_require_auth
async def admin_stats(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        matches = (await s.execute(
            select(Match).where(Match.status.in_(["CANCELLED", "REJECTED"]))
        )).scalars().all()

    driver_reasons: dict[str, int] = {}
    passenger_reasons: dict[str, int] = {}
    rejection_counts: dict[str, int] = {}

    for m in matches:
        cr = getattr(m, "cancel_reason", None)
        if cr:
            cb = getattr(m, "cancelled_by", None)
            if cb == "driver":
                driver_reasons[cr] = driver_reasons.get(cr, 0) + 1
            elif cb == "passenger":
                passenger_reasons[cr] = passenger_reasons.get(cr, 0) + 1
        rr = getattr(m, "rejection_reason", None)
        if rr:
            rejection_counts[rr] = rejection_counts.get(rr, 0) + 1

    return aiohttp_jinja2.render_template("stats.html", request, {
        "active": "stats",
        "driver_reasons":    sorted(driver_reasons.items(),    key=lambda x: -x[1]),
        "passenger_reasons": sorted(passenger_reasons.items(), key=lambda x: -x[1]),
        "rejections":        sorted(rejection_counts.items(),  key=lambda x: -x[1]),
        "total_cancelled":   sum(1 for m in matches if m.status == "CANCELLED"),
        "total_rejected":    sum(1 for m in matches if m.status == "REJECTED"),
    })


# ── Manual Match ──────────────────────────────────────────────────────────────

@_require_auth
async def admin_manual_match_get(request: web.Request) -> web.Response:
    async with AsyncSessionLocal() as s:
        driver_trips = (await s.execute(
            select(Trip).options(selectinload(Trip.user))
            .where(Trip.role == "driver", Trip.status == "ACTIVE")
            .order_by(Trip.departure_time)
        )).scalars().all()
        passenger_trips = (await s.execute(
            select(Trip).options(selectinload(Trip.user))
            .where(Trip.role == "passenger", Trip.status == "ACTIVE")
            .order_by(Trip.departure_time)
        )).scalars().all()
    return aiohttp_jinja2.render_template("manual_match.html", request, {
        "active": "matches",
        "driver_trips":    driver_trips,
        "passenger_trips": passenger_trips,
        "result": None, "error": None, "match_id": None,
    })


@_require_auth
async def admin_manual_match_post(request: web.Request) -> web.Response:
    data = await request.post()
    try:
        dtid = int(data.get("driver_trip_id", 0))
        ptid = int(data.get("passenger_trip_id", 0))
    except (ValueError, TypeError):
        dtid = ptid = 0

    if not dtid or not ptid:
        return aiohttp_jinja2.render_template("manual_match.html", request, {
            "active": "matches", "driver_trips": [], "passenger_trips": [],
            "result": "error", "error": "Оберіть обидві поїздки", "match_id": None,
        })

    async with AsyncSessionLocal() as s:
        driver_trip    = await s.get(Trip, dtid)
        passenger_trip = await s.get(Trip, ptid)

        if not driver_trip or not passenger_trip:
            return aiohttp_jinja2.render_template("manual_match.html", request, {
                "active": "matches", "driver_trips": [], "passenger_trips": [],
                "result": "error", "error": "Поїздку не знайдено", "match_id": None,
            })

        existing = (await s.execute(
            select(Match).where(
                Match.driver_trip_id == dtid,
                Match.passenger_trip_id == ptid,
                Match.status.in_(["PENDING", "CONFIRMED"]),
            )
        )).scalars().first()

        if existing:
            return aiohttp_jinja2.render_template("manual_match.html", request, {
                "active": "matches", "driver_trips": [], "passenger_trips": [],
                "result": "error", "error": f"Матч вже існує (#{existing.id})", "match_id": None,
            })

        match = Match(
            driver_trip_id=dtid,
            passenger_trip_id=ptid,
            status="PENDING",
        )
        s.add(match)
        driver_trip.status    = "MATCHING"
        passenger_trip.status = "MATCHING"
        await s.commit()
        await s.refresh(match)
        match_id = match.id

        d_user_id = driver_trip.user_id
        p_user_id = passenger_trip.user_id
        d_from = driver_trip.from_address.split(",")[0]
        d_to   = driver_trip.to_address.split(",")[0]
        p_from = passenger_trip.from_address.split(",")[0]
        p_to   = passenger_trip.to_address.split(",")[0]

    bot = request.app.get("bot")
    if bot:
        try:
            await bot.send_message(
                d_user_id,
                f"🤝 <b>Новий матч (підібраний адміном)</b>\n\n"
                f"📍 Маршрут пасажира: {p_from} → {p_to}\n\n"
                f"Перевірте деталі та підтвердіть поїздку.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        try:
            await bot.send_message(
                p_user_id,
                f"🤝 <b>Новий матч (підібраний адміном)</b>\n\n"
                f"🚗 Маршрут водія: {d_from} → {d_to}\n\n"
                f"Перевірте деталі та підтвердіть поїздку.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    return aiohttp_jinja2.render_template("manual_match.html", request, {
        "active": "matches", "driver_trips": [], "passenger_trips": [],
        "result": "ok", "match_id": match_id, "error": None,
    })


# ── CSV Export ────────────────────────────────────────────────────────────────

@_require_auth
async def admin_export_users(request: web.Request) -> web.Response:
    import csv, io
    async with AsyncSessionLocal() as s:
        users = (await s.execute(
            select(User).order_by(User.created_at.desc())
        )).scalars().all()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ID", "Ім'я", "Username", "Рейтинг", "Поїздок", "Успішних", "Заблоковано", "Зареєстрований"])
    for u in users:
        w.writerow([
            u.id, u.first_name, u.username or "",
            f"{u.rating:.1f}" if u.rating is not None else "",
            u.trips_count or 0,
            u.successful_trips or 0,
            "Так" if u.is_blocked else "Ні",
            u.created_at.strftime("%d.%m.%Y %H:%M") if u.created_at else "",
        ])
    return web.Response(
        body=out.getvalue().encode("utf-8-sig"),
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=users.csv"},
    )


@_require_auth
async def admin_export_trips(request: web.Request) -> web.Response:
    import csv, io
    async with AsyncSessionLocal() as s:
        trips = (await s.execute(
            select(Trip).options(selectinload(Trip.user)).order_by(Trip.created_at.desc())
        )).scalars().all()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ID", "Роль", "Користувач", "Звідки", "Куди", "Час виїзду", "Ціна", "Місця", "Статус", "Створено"])
    for t in trips:
        w.writerow([
            t.id,
            "Водій" if t.role == "driver" else "Пасажир",
            t.user.first_name if t.user else "",
            t.from_address, t.to_address,
            t.departure_time.strftime("%d.%m.%Y %H:%M") if t.departure_time else "",
            t.price or "", t.seats or "", t.status,
            t.created_at.strftime("%d.%m.%Y %H:%M") if t.created_at else "",
        ])
    return web.Response(
        body=out.getvalue().encode("utf-8-sig"),
        content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trips.csv"},
    )


# ── API: daily stats ──────────────────────────────────────────────────────────

async def api_stats_daily(request: web.Request) -> web.Response:
    if not _is_authed(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    now = datetime.utcnow()
    since = now - timedelta(days=29)

    async with AsyncSessionLocal() as s:
        user_dts = (await s.execute(
            select(User.created_at).where(User.created_at >= since)
        )).scalars().all()
        trip_dts = (await s.execute(
            select(Trip.created_at).where(Trip.created_at >= since)
        )).scalars().all()

    labels = [(now - timedelta(days=i)).strftime("%m-%d") for i in range(29, -1, -1)]
    uc: dict[str, int] = {}
    for dt in user_dts:
        if dt:
            uc[dt.strftime("%m-%d")] = uc.get(dt.strftime("%m-%d"), 0) + 1
    tc: dict[str, int] = {}
    for dt in trip_dts:
        if dt:
            tc[dt.strftime("%m-%d")] = tc.get(dt.strftime("%m-%d"), 0) + 1

    return web.json_response({
        "labels": labels,
        "users":  [uc.get(l, 0) for l in labels],
        "trips":  [tc.get(l, 0) for l in labels],
    })


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup_admin(app: web.Application) -> None:
    templates_path = str(Path(__file__).parent / "templates")
    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(templates_path))

    # Auth
    app.router.add_get("/admin/login",   admin_login_get)
    app.router.add_post("/admin/login",  admin_login_post)
    app.router.add_get("/admin/logout",  admin_logout)

    # Pages
    app.router.add_get("/admin",         lambda r: web.HTTPFound("/admin/"))
    app.router.add_get("/admin/",        admin_dashboard)
    app.router.add_get("/admin/trips",   admin_trips)
    app.router.add_get("/admin/users",   admin_users)
    app.router.add_post("/admin/users/{user_id}/block",   admin_block_user)
    app.router.add_post("/admin/users/{user_id}/unblock", admin_unblock_user)
    app.router.add_get("/admin/matches", admin_matches)
    app.router.add_get("/admin/tickets", admin_tickets)
    app.router.add_post("/admin/tickets/{ticket_id}/read", admin_ticket_read)
    app.router.add_get("/admin/faq",     admin_faq)
    app.router.add_post("/admin/faq",    admin_faq_add)
    app.router.add_post("/admin/faq/{faq_id}/edit",   admin_faq_edit)
    app.router.add_post("/admin/faq/{faq_id}/delete", admin_faq_delete)
    app.router.add_get("/admin/workflow",  admin_workflow)
    app.router.add_get("/admin/settings",  admin_settings)
    app.router.add_post("/admin/settings", admin_settings_save)

    # Broadcast
    app.router.add_get ("/admin/broadcast",          admin_broadcast_get)
    app.router.add_post("/admin/broadcast",          admin_broadcast_post)
    app.router.add_post("/admin/broadcast/banner",   admin_banner_save)
    # Stats
    app.router.add_get ("/admin/stats",              admin_stats)
    # Manual match
    app.router.add_get ("/admin/manual-match",       admin_manual_match_get)
    app.router.add_post("/admin/manual-match",       admin_manual_match_post)
    # CSV export
    app.router.add_get ("/admin/users/export.csv",   admin_export_users)
    app.router.add_get ("/admin/trips/export.csv",   admin_export_trips)
    # JSON API
    app.router.add_get ("/admin/api/settings",       api_settings_get)
    app.router.add_post("/admin/api/settings/{key}", api_settings_set)
    app.router.add_get ("/admin/api/trips/map",      api_trips_map)
    app.router.add_get ("/admin/api/stats/daily",    api_stats_daily)
