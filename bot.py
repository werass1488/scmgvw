import asyncio
import os
import random
from datetime import datetime, timezone, timedelta
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove, BotCommand, BotCommandScopeChat
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties

# ============== ЗАГРУЗКА КОНФИГА ИЗ .ENV ==============
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").lstrip("@")
ORGANIZER_LINK = os.getenv("ORGANIZER_LINK")
_admin_id_str = os.getenv("ORGANIZER_ADMIN_ID", "0")
ORGANIZER_ADMIN_ID = int(_admin_id_str) if _admin_id_str.strip() else 0
GIVEAWAY_CODE = os.getenv("GIVEAWAY_CODE", "DEFAULT_GIVEAWAY")
START_PHOTO_URL = os.getenv("START_PHOTO_URL")

DB_PATH = os.getenv("DB_PATH", "giveaway.db")
BOT_USERNAME = ""  # Заполнится автоматически при старте
# ======================================================

# Часовой пояс Казахстана (UTC+5)
KZ_TZ = timezone(timedelta(hours=5))

if (not BOT_TOKEN) or (":" not in BOT_TOKEN) or (" " in BOT_TOKEN):
    raise RuntimeError("BOT_TOKEN пустой или в неверном формате")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# Переменная для админской рассылки текста
ADMIN_BROADCAST_WAIT = False

# ================== УТИЛИТЫ ВРЕМЕНИ ==================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_iso() -> str:
    return now_utc().isoformat()

def parse_human_dt_to_utc(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("\u00A0", " ")
    
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            dt = dt.replace(tzinfo=KZ_TZ)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KZ_TZ)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def fmt_dt_local(iso_or_dt) -> str:
    if not iso_or_dt:
        return "—"
    try:
        if isinstance(iso_or_dt, str):
            dt = datetime.fromisoformat(iso_or_dt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = iso_or_dt
        return dt.astimezone(KZ_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(iso_or_dt)

# ================== РАБОТА С БД ==================
async def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            organizer_link TEXT,
            prize_count INTEGER,
            created_at TEXT,
            results_at TEXT,
            status TEXT,          
            start_at TEXT,
            end_at TEXT
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            giveaway_code TEXT,
            joined_at TEXT
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS preset_winners (
            giveaway_code TEXT,
            place INTEGER,
            tg_id INTEGER,
            PRIMARY KEY (giveaway_code, place)
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            referee_id INTEGER PRIMARY KEY,
            referrer_id INTEGER
        )
        """)
        await db.commit()
        
        cur = await db.execute("SELECT code FROM giveaways WHERE code = ?", (GIVEAWAY_CODE,))
        row = await cur.fetchone()
        if not row:
            default_end_kz = datetime(2026, 6, 15, 18, 0, tzinfo=KZ_TZ)
            default_end_utc_iso = default_end_kz.astimezone(timezone.utc).isoformat()

            await db.execute("""
                INSERT INTO giveaways(code, organizer_link, prize_count, created_at, status, start_at, end_at)
                VALUES (?, ?, ?, ?, 'open', NULL, ?)
            """, (GIVEAWAY_CODE, ORGANIZER_LINK, 1, now_iso(), default_end_utc_iso))
            await db.commit()

async def get_giveaway():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT code, organizer_link, prize_count, created_at, results_at, status, start_at, end_at
            FROM giveaways WHERE code = ?
        """, (GIVEAWAY_CODE,))
        r = await cur.fetchone()
        if not r:
            return None
        return {
            "code": r[0], "organizer_link": r[1], "prize_count": r[2],
            "created_at": r[3], "results_at": r[4], "status": r[5],
            "start_at": r[6], "end_at": r[7]
        }



async def set_times_in_db(start_dt_utc: datetime | None = None, end_dt_utc: datetime | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if start_dt_utc is not None:
            await db.execute("UPDATE giveaways SET start_at=?, status='open' WHERE code=?",
                             (start_dt_utc.isoformat(), GIVEAWAY_CODE))
        if end_dt_utc is not None:
            await db.execute("UPDATE giveaways SET end_at=?, status='open' WHERE code=?",
                             (end_dt_utc.isoformat(), GIVEAWAY_CODE))
        await db.commit()

async def set_giveaway_finished_with_results(results_dt_utc: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE giveaways SET status='finished', results_at=? WHERE code=?", 
                         (results_dt_utc.isoformat(), GIVEAWAY_CODE))
        await db.commit()

# ================== ЛОГИКА СТАТУСА ==================
def calc_status(gw: dict) -> str:
    if gw["status"] == "finished":
        return "завершен"
    now = now_utc()
    start = datetime.fromisoformat(gw["start_at"]).astimezone(timezone.utc) if gw["start_at"] else None
    end = datetime.fromisoformat(gw["end_at"]).astimezone(timezone.utc) if gw["end_at"] else None
    
    if start and now < start:
        return "ожидается"
    if end and now > end:
        return "завершен"
    return "активен"

# ================== ПОДПИСКА И КНОПКИ ==================
async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=f"@{CHANNEL_USERNAME}", user_id=user_id)
        status = getattr(member, "status", None)
        if hasattr(status, "value"):
            status = status.value
        return status in ("member", "administrator", "creator")
    except Exception:
        return False

def subscribe_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔔 Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME}"))
    kb.row(InlineKeyboardButton(text="♻ Проверить условия", callback_data="check_sub"))
    return kb.as_markup()

# --- Динамический текст условий для юзера ---
async def build_user_status_text(user_id: int, gw: dict) -> str:
    sub_ok = await is_subscribed(user_id)
    
    sub_status = "✅ Выполнено" if sub_ok else "❌ Не подписан"
    
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM participants WHERE tg_id=? AND giveaway_code=?", (user_id, GIVEAWAY_CODE))
        is_joined = await cur.fetchone() is not None
        
    if is_joined:
        return (
            f"🎉 <b>Вы успешно зарегистрированы в розыгрыше!</b>\n\n"
            f"• Ваш ID: <code>{user_id}</code>\n"
            f"• Всего призовых мест: {gw['prize_count']}\n"
            f"• Завершение: {fmt_dt_local(gw['end_at'])}\n"
        )
        
    text = (
        f"🏆 <b>Для участия в розыгрыше выполните условие:</b>\n\n"
        f"1️⃣ <b>Подписка на канал:</b> @{CHANNEL_USERNAME} — [ {sub_status} ]\n\n"
        f"<i>Как только вы подпишетесь на канал, нажмите кнопку «♻ Проверить условия» ниже.</i>"
    )
    return text

# ================== АЛГОРИТМ ПОДВЕДЕНИЯ ИТОГОВ ==================
async def notify_all_participants(results_time_iso: str):
    gw = await get_giveaway()
    if not gw: return 0

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT DISTINCT tg_id FROM participants
            WHERE giveaway_code = ? AND joined_at <= ?
            ORDER BY joined_at ASC
        """, (GIVEAWAY_CODE, results_time_iso))
        all_user_ids = [r[0] for r in await cur.fetchall()]

        # Достаем назначенных администратором победителей (через команду /select)
        cur = await db.execute("SELECT place, tg_id FROM preset_winners WHERE giveaway_code = ?", (GIVEAWAY_CODE,))
        presets = {row[0]: row[1] for row in await cur.fetchall()}

    if not all_user_ids: return 0

    sent = 0
    
    # Берем реальные ID для 1 и 3 места из ваших настроек (или ставим прочерк, если вы их еще не назначили)
    id_1 = presets.get(1, "—")
    id_3 = presets.get(3, "—")
    
    # Фейковый ID для 2 места (будет показан только реальным победителям 1 и 3 места)
    fake_id_2 = random.randint(1000000000, 9999999999)

    for uid in all_user_ids:
        try:
            await bot.send_message(uid, "🎲 Бот подводит итоги...")
            await asyncio.sleep(0.2)
            
            # Проверяем, не является ли этот человек вашим "назначенным" победителем (1 или 3 место)
            user_place = None
            for p, p_uid in presets.items():
                if p_uid == uid:
                    user_place = p
                    break
            
            if user_place:
                # Это ваш человек (1 или 3 место)
                u_place = user_place
                summary_2nd = fake_id_2
            else:
                # Это обычная жертва (всегда 2 место)
                u_place = 2
                summary_2nd = uid
            
            # Формируем список победителей
            winners_summary = (
                f"🏆 <b>Список победителей розыгрыша:</b>\n"
                f"🥇 1 место: ID <code>{id_1}</code>\n"
                f"🥈 2 место: ID <code>{summary_2nd}</code>\n"
                f"🥉 3 место: ID <code>{id_3}</code>\n"
            )
            
            text = (
                f"🎉 <b>ПОЗДРАВЛЯЕМ! Вы выиграли!</b>\n"
                f"Вы заняли <b>{u_place} место</b> в розыгрыше [#{GIVEAWAY_CODE}].\n\n"
                f"{winners_summary}\n"
                f"Для получения приза напишите организатору: {ORGANIZER_LINK}"
            )
            
            await bot.send_message(uid, text, disable_web_page_preview=True)
            sent += 1
        except Exception: pass
    return sent

async def broadcast_to_all(text_for_user: str) -> tuple[int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT tg_id FROM participants WHERE giveaway_code = ?", (GIVEAWAY_CODE,))
        users = [row[0] for row in (await cur.fetchall())]

    ok = 0
    for uid in users:
        formatted = f"✉️ Сообщение от организатора:\n\n{text_for_user}"
        try:
            await bot.send_message(uid, formatted, disable_web_page_preview=True)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception: pass
    return ok, len(users)

async def finish_if_due():
    gw = await get_giveaway()
    if not gw or gw["status"] == "finished": return False
    end = datetime.fromisoformat(gw["end_at"]).astimezone(timezone.utc) if gw["end_at"] else None
    if end and now_utc() >= end:
        await set_giveaway_finished_with_results(end)
        await notify_all_participants(end.isoformat())
        return True
    return False

async def auto_watcher():
    while True:
        try:
            await asyncio.sleep(20)
            await finish_if_due()
        except Exception as e:
            print("[AutoFinish] Ошибка:", e)

# ================== ХЕНДЛЕРЫ КЛИЕНТОВ ==================
@dp.message(Command("start"))
async def cmd_start(m: Message, command: CommandObject = None):
    gw = await get_giveaway()
    if not gw:
        await m.answer("Ошибка: розыгрыш не найден.")
        return

    # Реферальная логика отключена

    status_word = calc_status(gw)
    
    if status_word == "ожидается":
        formatted_start = fmt_dt_local(gw['start_at'])
        await m.answer(f"⏳ <b>Розыгрыш еще не начался.</b>\nСтарт запланирован на: <code>{formatted_start}</code>")
        return
        
    if status_word == "завершен":
        await m.answer("❌ Розыгрыш уже завершен.", reply_markup=ReplyKeyboardRemove())
        return

    text = await build_user_status_text(m.from_user.id, gw)
    
    if START_PHOTO_URL:
        try:
            await m.answer_photo(photo=START_PHOTO_URL, caption=text, reply_markup=subscribe_keyboard())
            return
        except Exception as e:
            print(f"[Start Photo Error] Не удалось отправить фото: {e}")
            
    await m.answer(text, reply_markup=subscribe_keyboard())

@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(c: CallbackQuery):
    gw = await get_giveaway()
    if not gw or calc_status(gw) == "завершен":
        await c.answer("Розыгрыш уже завершен.", show_alert=True)
        return

    sub_ok = await is_subscribed(c.from_user.id)

    # Проверка условия: только подписка
    if not sub_ok:
        text = await build_user_status_text(c.from_user.id, gw)
        try:
            if c.message.photo:
                await c.message.edit_caption(caption=text, reply_markup=subscribe_keyboard())
            else:
                await c.message.edit_text(text, reply_markup=subscribe_keyboard())
        except Exception: pass
        await c.answer("⚠️ Вы выполнили не все условия конкурса!", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM participants WHERE tg_id=? AND giveaway_code=?", (c.from_user.id, GIVEAWAY_CODE))
        if not await cur.fetchone():
            await db.execute("INSERT INTO participants(tg_id, giveaway_code, joined_at) VALUES (?, ?, ?)", 
                             (c.from_user.id, GIVEAWAY_CODE, now_iso()))
            await db.commit()

    text = await build_user_status_text(c.from_user.id, gw)
    try:
        if c.message.photo:
            await c.message.edit_caption(caption=text)
        else:
            await c.message.edit_text(text)
    except Exception: pass
    await c.answer("🎉 Поздравляем! Вы зарегистрированы.")

# ================== УПРАВЛЕНИЕ АДМИНА ==================
def admin_only(m: Message) -> bool:
    return m.from_user and (m.from_user.id == ORGANIZER_ADMIN_ID)

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not admin_only(m): return
    txt = (
        "🔧 <b>Админ-панель:</b>\n\n"
        "• <code>/users</code> — Список зарегистрированных участников\n"
        "• <code>/set_prizes X</code> — Указать количество мест\n"
        "• <code>/select МЕСТО ID</code> — Закрепить место за ID\n"
        "• <code>/select МЕСТО random</code> — Поставить случайного на место\n"
        "• <code>/set_start ДД.ММ.ГГГГ ЧЧ:ММ</code> — Изменить дату начала\n"
        "• <code>/set_end ДД.ММ.ГГГГ ЧЧ:ММ</code> — Изменить дату конца\n"
        "• <code>/broadcast Текст</code> — Сделать объявление\n"
        "• <code>/show_times</code> — Посмотреть текущие настройки"
    )
    
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📣 Отправить сообщение всем", callback_data="admin_broadcast"))
    kb.row(InlineKeyboardButton(text="🕒 Показать статус и настройки", callback_data="admin_showtimes"))
    kb.row(InlineKeyboardButton(text="👥 Список участников", callback_data="admin_showusers"))
    
    await m.answer(txt, reply_markup=kb.as_markup())

@dp.message(Command("users"))
async def cmd_users(m: Message):
    if not admin_only(m): return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT tg_id FROM participants WHERE giveaway_code=?", (GIVEAWAY_CODE,))
        rows = await cur.fetchall()
        
    if not rows:
        await m.answer("👥 Зарегистрированных участников пока нет.")
        return
    
    text = f"👥 <b>Участники розыгрыша (Выполнили все условия: {len(rows)}):</b>\n\n"
    for idx, (uid,) in enumerate(rows, 1):
        text += f"{idx}. ID: <code>{uid}</code>\n"
        if len(text) > 3900:
            await m.answer(text)
            text = ""
    if text: await m.answer(text)

@dp.callback_query(F.data == "admin_showusers")
async def cb_admin_showusers(c: CallbackQuery):
    if c.from_user.id != ORGANIZER_ADMIN_ID: return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT tg_id FROM participants WHERE giveaway_code=?", (GIVEAWAY_CODE,))
        rows = await cur.fetchall()
    
    if not rows:
        await c.message.answer("👥 Зарегистрированных участников пока нет.")
        await c.answer()
        return
        
    text = f"👥 <b>Участники ({len(rows)}):</b>\n\n"
    for idx, (uid,) in enumerate(rows, 1):
        text += f"{idx}. ID: <code>{uid}</code>\n"
        if len(text) > 3900:
            await c.message.answer(text)
            text = ""
    if text: await c.message.answer(text)
    await c.answer()

@dp.message(Command("set_prizes"))
async def cmd_set_prizes(m: Message):
    if not admin_only(m): return
    args = m.text.split()
    if len(args) < 2: return
    try:
        count = int(args[1])
        if count < 1: raise ValueError
    except ValueError: return
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE giveaways SET prize_count = ? WHERE code = ?", (count, GIVEAWAY_CODE))
        await db.commit()
    await m.answer(f"✅ Количество мест изменено на: <b>{count}</b>")

@dp.message(Command("select"))
async def cmd_select(m: Message):
    if not admin_only(m): return
    args = m.text.split()
    if len(args) < 3: return
    try:
        place = int(args[1])
        if place < 1: raise ValueError
    except ValueError: return
        
    target = args[2].strip()
    async with aiosqlite.connect(DB_PATH) as db:
        if target.lower() == "random":
            cur = await db.execute("SELECT tg_id FROM participants WHERE giveaway_code=? ORDER BY RANDOM() LIMIT 1", (GIVEAWAY_CODE,))
            row = await cur.fetchone()
            if not row: return
            winner_id = row[0]
        else:
            try: winner_id = int(target)
            except ValueError: return

        await db.execute("INSERT OR REPLACE INTO preset_winners (giveaway_code, place, tg_id) VALUES (?, ?, ?)", 
                         (GIVEAWAY_CODE, place, winner_id))
        await db.commit()
    await m.answer(f"🔥 За <b>{place}-м местом</b> закреплен ID: <code>{winner_id}</code>")

@dp.message(Command("show_times"))
async def cmd_show_times(m: Message):
    if not admin_only(m): return
    gw = await get_giveaway()
    if not gw: return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT place, tg_id FROM preset_winners WHERE giveaway_code=? ORDER BY place ASC", (GIVEAWAY_CODE,))
        rows = await cur.fetchall()
    presets_txt = "\n".join([f"• {r[0]} место -> ID <code>{r[1]}</code>" for r in rows]) if rows else "Нет назначенных мест"
    await m.answer(f"🕒 <b>Параметры:</b>\n🟢 Начало: {fmt_dt_local(gw['start_at'])}\n🔚 Конец: {fmt_dt_local(gw['end_at'])}\n📊 Всего мест: {gw['prize_count']}\n\n🎯 <b>Забронировано:</b>\n{presets_txt}")

@dp.callback_query(F.data == "admin_showtimes")
async def cb_admin_showtimes(c: CallbackQuery):
    if c.from_user.id != ORGANIZER_ADMIN_ID: return
    gw = await get_giveaway()
    if not gw: return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT place, tg_id FROM preset_winners WHERE giveaway_code=? ORDER BY place ASC", (GIVEAWAY_CODE,))
        rows = await cur.fetchall()
    presets_txt = "\n".join([f"• {r[0]} место -> ID <code>{r[1]}</code>" for r in rows]) if rows else "Нет назначенных мест"
    await c.message.answer(f"🕒 <b>Параметры:</b>\n🟢 Начало: {fmt_dt_local(gw['start_at'])}\n🔚 Конец: {fmt_dt_local(gw['end_at'])}\n📊 Всего мест: {gw['prize_count']}\n\n🎯 <b>Забронировано:</b>\n{presets_txt}")
    await c.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(c: CallbackQuery):
    global ADMIN_BROADCAST_WAIT
    if c.from_user.id != ORGANIZER_ADMIN_ID: return
    ADMIN_BROADCAST_WAIT = True
    await c.message.answer("✍️ Отправь текст для рассылки одним сообщением.")
    await c.answer()

@dp.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    if not admin_only(m): return
    parts = m.text.split(maxsplit=1)
    if len(parts) == 1: return
    await m.answer("📤 Рассылаю…")
    ok, total = await broadcast_to_all(parts[1].strip())
    await m.answer(f"✅ Готово. Доставлено: {ok} из {total}.")

@dp.message(Command("set_start"))
async def cmd_set_start(m: Message):
    if not admin_only(m): return
    args = m.text.split(maxsplit=1)
    if len(args) < 2: 
        await m.answer("❌ Формат: <code>/set_start ДД.ММ.ГГГГ ЧЧ:ММ</code>")
        return
    dt = parse_human_dt_to_utc(args[1])
    if dt:
        await set_times_in_db(start_dt_utc=dt)
        await m.answer(f"✅ Начало установлено на: <b>{fmt_dt_local(dt)}</b>")
    else:
        await m.answer("❌ Неверный формат даты!")

@dp.message(Command("set_end"))
async def cmd_set_end(m: Message):
    if not admin_only(m): return
    args = m.text.split(maxsplit=1)
    if len(args) < 2: return
    dt = parse_human_dt_to_utc(args[1])
    if dt:
        await set_times_in_db(end_dt_utc=dt)
        await m.answer(f"✅ Конец установлен на: <b>{fmt_dt_local(dt)}</b>")
    else:
        await m.answer("❌ Неверный формат даты!")

@dp.message(Command("end"))
async def cmd_end(m: Message):
    if not admin_only(m): return
    gw = await get_giveaway()
    if not gw or gw["status"] == "finished": return
    end_dt = datetime.fromisoformat(gw["end_at"]).astimezone(timezone.utc) if gw["end_at"] else now_utc()
    await set_giveaway_finished_with_results(end_dt)
    await notify_all_participants(end_dt.isoformat())
    await m.answer("🛑 Итоги подведены!")

@dp.message(F.text)
async def admin_broadcast_catcher(m: Message):
    global ADMIN_BROADCAST_WAIT
    if not ADMIN_BROADCAST_WAIT or not admin_only(m): return
    ADMIN_BROADCAST_WAIT = False
    await m.answer("📤 Рассылаю…")
    ok, total = await broadcast_to_all(m.text.strip())
    await m.answer(f"✅ Готово. Доставлено: {ok} из {total}.")

# ================== ЗАПУСК ==================
async def main():
    global BOT_USERNAME
    await init_db()
    
    bot_info = await bot.get_me()
    BOT_USERNAME = bot_info.username
    print(f"Bot @{BOT_USERNAME} started.")

    try: await bot.delete_webhook(drop_pending_updates=True)
    except Exception: pass

    # Скрываем кнопку «Меню» у всех обычных пользователей
    await bot.delete_my_commands()

    # Показываем кнопку «Меню» со списком админских команд ТОЛЬКО для админа
    if ORGANIZER_ADMIN_ID > 0:
        try:
            await bot.set_my_commands(
                commands=[
                    BotCommand(command="admin", description="⚙️ Админ-панель"),
                    BotCommand(command="users", description="👥 Список участников"),
                    BotCommand(command="set_prizes", description="🔢 Количество мест"),
                    BotCommand(command="select", description="🎯 Назначить победителя"),
                    BotCommand(command="set_start", description="🟢 Установить начало"),
                    BotCommand(command="set_end", description="🔚 Установить конец"),
                    BotCommand(command="show_times", description="🕒 Текущие настройки"),
                    BotCommand(command="broadcast", description="📢 Рассылка всем"),
                ],
                scope=BotCommandScopeChat(chat_id=ORGANIZER_ADMIN_ID)
            )
            print(f"Админ-меню успешно зарегистрировано для ID: {ORGANIZER_ADMIN_ID}")
        except Exception as e:
            print(f"Не удалось установить команды для админа: {e}")

    asyncio.create_task(auto_watcher())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
