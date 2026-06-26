import os
import re
import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, CallbackQuery, ChatJoinRequest, LabeledPrice
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError

# --- CONFIGURATION ---
def load_env_file(path: str = ".env") -> None:
    """Simple .env loader without external dependency."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key and value and key not in os.environ:
                os.environ[key] = value


load_env_file()

API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable must be set with your bot token.")

PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN")
PAYMENT_CURRENCY = os.getenv("PAYMENT_CURRENCY", "UZS")

def parse_id_list(raw: str | None, default: str = "") -> list[int]:
    """Parse comma-separated numeric IDs into a list of ints."""
    ids: list[int] = []
    data = raw if raw not in (None, "") else default
    for part in data.replace(" ", "").split(","):
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids

SUPERADMIN_IDS = parse_id_list(os.getenv("SUPERADMIN_ID"), "7706048424")
if not SUPERADMIN_IDS:
    raise RuntimeError("SUPERADMIN_ID must contain at least one numeric ID (comma-separated for multiple).")
SUPERADMIN_ID = SUPERADMIN_IDS[0]  # backward-compat alias
DATABASE = os.getenv("DATABASE_PATH", "kino_bot.db")
MOVIE_CHANNEL_ID = os.getenv("MOVIE_CHANNEL_ID", "-1003736304208")  # Bu yerga kinolar yuklangan kanal ID sini yozing

# --- LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger(__name__)

# --- STATES ---
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_channel = State()
    waiting_for_movie_channel = State()
    waiting_for_invite_link = State()
    waiting_for_premium_price = State()
    waiting_for_payment_link = State()
    waiting_for_premium_info = State()

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, joined_date TEXT, is_premium INTEGER DEFAULT 0, premium_until TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, added_by INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        user_id INTEGER,
        duration TEXT,
        amount INTEGER,
        provider TEXT,
        paid INTEGER DEFAULT 0,
        created_at TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS join_requests (
        user_id INTEGER,
        channel_id TEXT,
        requested_at TEXT,
        PRIMARY KEY (user_id, channel_id)
    )''')
    
    # Schema migration: add request_required column if missing
    cursor.execute("PRAGMA table_info(channels)")
    cols = [row[1] for row in cursor.fetchall()]
    if "request_required" not in cols:
        cursor.execute("ALTER TABLE channels ADD COLUMN request_required INTEGER DEFAULT 0")
    if "invite_link" not in cols:
        cursor.execute("ALTER TABLE channels ADD COLUMN invite_link TEXT")
    
    cursor.execute("PRAGMA table_info(users)")
    user_cols = [row[1] for row in cursor.fetchall()]
    if "is_premium" not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0")
    if "premium_until" not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN premium_until TEXT")
    if "viewed_info" not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN viewed_info INTEGER DEFAULT 0")
    
    # Default settings
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('mandatory_enabled', '1'))
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('movie_channel', MOVIE_CHANNEL_ID))
    # premium prices per duration
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('premium_price_1kun', '0'))
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('premium_price_1hafta', '0'))
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('premium_price_15kun', '0'))
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('premium_price_30kun', '0'))
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('premium_info_text', ''))
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('click_payment_url', ''))
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ('paynet_payment_url', ''))
    
    # Add superadmin(s) to admins table
    for sa_id in SUPERADMIN_IDS:
        cursor.execute("INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)", (sa_id, 0))
    
    conn.commit()
    conn.close()

# DB Helper Functions
def db_query(query, params=(), fetchone=False, fetchall=False):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute(query, params)
    res = None
    if fetchone: res = cursor.fetchone()
    elif fetchall: res = cursor.fetchall()
    if not query.lstrip().upper().startswith("SELECT"):
        conn.commit()
    conn.close()
    return res

def prune_join_requests(days: int = 7):
    """Cleanup old join request records to prevent table growth."""
    db_query(
        "DELETE FROM join_requests WHERE requested_at < datetime('now', ?)",
        (f'-{days} day',)
    )

def ensure_user(user_id: int, username: str | None, full_name: str | None):
    """Insert user if missing; keep first join date."""
    db_query(
        "INSERT OR IGNORE INTO users (user_id, username, joined_date, is_premium, premium_until) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 0, None)
    )

def is_admin(user_id):
    res = db_query("SELECT user_id FROM admins WHERE user_id = ?", (user_id,), fetchone=True)
    return res is not None

def parse_premium_duration(raw: str) -> timedelta | None:
    text = (raw or "").strip().lower()
    if not text:
        return None
    text = re.sub(r"https?://", "", text)
    if text.startswith("t.me/"):
        text = text[5:]
    text = text.split("?")[0].split("/")[0].strip()
    text = text.replace(" ", "")

    direct_map = {
        "soat": timedelta(hours=1),
        "1soat": timedelta(hours=1),
        "1h": timedelta(hours=1),
        "1kun": timedelta(days=1),
        "1d": timedelta(days=1),
        "1hafta": timedelta(days=7),
        "15kun": timedelta(days=15),
        "30kun": timedelta(days=30),
        "hafta": timedelta(days=7),
        "kun": timedelta(days=1),
    }
    if text in direct_map:
        return direct_map[text]

    m = re.match(r"^(\d+)(soat|h|hour|hours|kun|d|day|days|hafta|week|weeks)$", text)
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2)
    if unit in {"soat", "h", "hour", "hours"}:
        return timedelta(hours=value)
    if unit in {"kun", "d", "day", "days"}:
        return timedelta(days=value)
    if unit in {"hafta", "week", "weeks"}:
        return timedelta(days=value * 7)
    return None


def parse_price_to_int(raw: str | None) -> int | None:
    """Normalize a price string to an integer (so'm). Returns None if not parseable."""
    if not raw:
        return None
    # Remove common currency words and separators, keep digits only
    s = re.sub(r"[^0-9]", "", raw)
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        return None


def amount_for_currency_units(price_int: int, currency: str) -> int:
    """Return amount in the smallest units required by Telegram Payments.
    For currencies with two decimal places (USD, EUR, etc.) multiply by 100.
    For currencies without subunits like UZS, return as-is.
    """
    zero_decimal_currencies = {"JPY", "VND", "KRW", "UZS"}
    currency = (currency or "").upper()
    if currency in zero_decimal_currencies:
        return int(price_int)
    # default to two-decimal currencies
    return int(price_int * 100)

def is_premium(user_id):
    res = db_query("SELECT premium_until FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not res or not res[0]:
        return False
    try:
        expires = datetime.strptime(res[0], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return False
    return expires > datetime.now()

def set_premium(user_id, duration: timedelta | None):
    ensure_user(user_id, None, None)
    if duration is None:
        db_query("UPDATE users SET is_premium = 0, premium_until = NULL WHERE user_id = ?", (user_id,))
        return
    until = datetime.now() + duration
    db_query(
        "UPDATE users SET is_premium = 1, premium_until = ? WHERE user_id = ?",
        (until.strftime('%Y-%m-%d %H:%M:%S'), user_id)
    )

# --- BOT INITIALIZATION ---
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- UTILS ---
async def check_subscriptions(user_id):
    enabled = db_query("SELECT value FROM settings WHERE key = 'mandatory_enabled'", fetchone=True)[0]
    if enabled == '0': return []
    
    prune_join_requests()  # avoid table swelling; drop entries older than 7 days

    channels = db_query("SELECT channel_id, COALESCE(request_required,0) FROM channels", fetchall=True)
    not_subscribed = []
    for channel, req in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                # If user has already sent a join request, treat as temporarily allowed
                jr = db_query(
                    "SELECT 1 FROM join_requests WHERE user_id = ? AND channel_id = ?",
                    (user_id, str(channel)),
                    fetchone=True
                )
                if jr:
                    continue
                not_subscribed.append((channel, req))
            else:
                # User is fully in; clean up stale join_request record
                db_query("DELETE FROM join_requests WHERE user_id = ? AND channel_id = ?", (user_id, str(channel)))
        except Exception as exc:
            logger.warning("Subscription check failed for user %s in %s: %s", user_id, channel, exc)
            continue
    return not_subscribed

async def build_join_button(channel_id: str, request_required: bool = False) -> InlineKeyboardButton | None:
    """
    Build a join button that works for both @username channels and numeric -100 IDs.
    If invite link creation fails, returns None so the caller can skip it.
    """
    channel_id = channel_id.strip()
    url = None
    if request_required:
        # Use admin-supplied join-request link if available (most reliable for private/zayavka kanallar)
        stored = db_query(
            "SELECT invite_link FROM channels WHERE channel_id = ?",
            (channel_id,),
            fetchone=True
        )
        if stored and stored[0]:
            url = stored[0]
        else:
            # If admin link yo'q, try to create fresh join-request link (requires bot to be admin with invite rights)
            try:
                invite = await bot.create_chat_invite_link(
                    chat_id=channel_id,
                    creates_join_request=True
                )
                url = invite.invite_link
                db_query(
                    "UPDATE channels SET invite_link = ? WHERE channel_id = ?",
                    (url, channel_id)
                )
            except Exception as exc:
                logger.warning("Join-request invite generation failed for %s: %s", channel_id, exc)
                return None
    else:
        if channel_id.startswith("@"):
            url = f"https://t.me/{channel_id[1:]}"
        else:
            try:
                invite = await bot.create_chat_invite_link(chat_id=channel_id, creates_join_request=False)
                url = invite.invite_link
            except Exception as exc:
                logger.warning("Invite link generation failed for %s: %s", channel_id, exc)
                return None
    return InlineKeyboardButton(text="A'zo bo'lish", url=url)

# --- HANDLERS ---

@dp.chat_join_request()
async def on_chat_join_request(request: ChatJoinRequest) -> None:
    """
    Record join-requests for mandatory channels so bot knows user bosgan (clicked) join.
    No auto-approve – approval remains channel admins' responsibility.
    """
    channel_row = db_query(
        "SELECT request_required FROM channels WHERE channel_id = ?",
        (str(request.chat.id),),
        fetchone=True
    )
    # Fallback: match by @username if admin saved channel that way
    if not channel_row and request.chat.username:
        channel_row = db_query(
            "SELECT request_required FROM channels WHERE channel_id = ?",
            (f"@{request.chat.username}",),
            fetchone=True
        )
    if not channel_row:
        return  # Bot only manages known mandatory channels

    user = request.from_user
    if not user:
        return

    ensure_user(user.id, user.username, user.full_name)
    # Mark that user sent join request (can be used to suppress repeated prompts)
    db_query(
        "INSERT OR REPLACE INTO join_requests (user_id, channel_id, requested_at) VALUES (?, ?, ?)",
        (user.id, str(request.chat.id), datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    )

    try:
        await request.bot.send_message(
            user.id,
            "✅ Zayavka yuborildi. Admin tasdiqlagach botdan foydalanishingiz mumkin."
        )
    except Exception as exc:
        logger.error("Join request approval failed for %s in %s: %s", user.id, request.chat.id, exc)
        # quietly ignore so handler doesn't crash
        return

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    db_query("INSERT OR IGNORE INTO users (user_id, username, joined_date) VALUES (?, ?, ?)", 
             (message.from_user.id, message.from_user.username, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

    # If admin is currently setting premium prices, don't proceed with normal movie flow
    cur_state = await state.get_state()
    if cur_state and 'waiting_for_premium_price' in cur_state:
        await message.answer("Premium narxni sozlayapsiz — avval narxni yuboring yoki /cancel bilan chiqib keting.")
        return

    not_subscribed = await check_subscriptions(message.from_user.id)
    builder = InlineKeyboardBuilder()
    if not_subscribed:
        if is_premium(message.from_user.id):
            not_subscribed.sort(key=lambda item: item[1])
        for ch, req in not_subscribed:
            btn = await build_join_button(ch, bool(req))
            if btn:
                builder.row(btn)
        builder.row(InlineKeyboardButton(text="Qoidalarni o'qidim", callback_data="info_clicked"))
        builder.row(InlineKeyboardButton(text="Tekshirish", callback_data="check_sub"))

    # Always show VIP menu button
    builder.row(InlineKeyboardButton(text="VIP paketlar", callback_data="vip_menu"))

    # Reply depending on subscription state; include VIP and any join buttons
    if not_subscribed:
        await message.answer(
            "Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling.\nA'zo bo'lgach 'Tekshirish' tugmasini bosing.",
            reply_markup=builder.as_markup()
        )
        return

    # Check if user sent a movie code
    args = message.text.split()
    if len(args) > 1:
        payload = args[1]
        # deep-link back from external payment providers: paid|<order_id>
        if payload.startswith('paid|'):
            order_id = payload.split('|', 1)[1]
            ord_row = db_query("SELECT user_id, duration, amount, paid FROM orders WHERE order_id = ?", (order_id,), fetchone=True)
            if ord_row and ord_row[3] == 0 and ord_row[0] == message.from_user.id:
                db_query("UPDATE orders SET paid = 1 WHERE order_id = ?", (order_id,))
                delta = parse_premium_duration(ord_row[1])
                set_premium(message.from_user.id, delta)
                await message.answer(f"To'lov qabul qilindi. Sizga {ord_row[1]} uchun premium berildi.")
                try:
                    await bot.send_message(SUPERADMIN_ID, f"Avtomatik to'lov bajarildi: {message.from_user.full_name} (id:{message.from_user.id}) order:{order_id} dur:{ord_row[1]}")
                except Exception:
                    pass
                return
        code = payload
        await send_movie(message, code)
    else:
        await message.answer("Assalomu alaykum! Kino kodini yuboring.", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(callback: CallbackQuery):
    not_subscribed = await check_subscriptions(callback.from_user.id)
    if not not_subscribed:
        await callback.message.edit_text(
            "Rahmat! Endi kino kodini yuborishingiz mumkin.\n"
            "Kod misoli: 123"
        )
    else:
        await callback.answer("Hamma kanallarga a'zo bo'lmadingiz!", show_alert=True)


@dp.callback_query(F.data == "info_clicked")
async def info_clicked_cb(callback: CallbackQuery):
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    db_query("UPDATE users SET viewed_info = 1 WHERE user_id = ?", (callback.from_user.id,))
    await callback.answer("Rahmat! Qoidalarni o'qidingiz deb belgilandi.")
    try:
        await callback.message.reply("Siz qoidalarni o'qidingiz — endi Tekshirish tugmasini bosing.")
    except Exception:
        pass

async def send_movie(message, code):
    movie_channel = db_query("SELECT value FROM settings WHERE key = 'movie_channel'", fetchone=True)[0]
    try:
        # Copy the message from the channel using the ID (code)
        await bot.copy_message(chat_id=message.chat.id, from_chat_id=movie_channel, message_id=int(code))
    except Exception as e:
        logger.error("Failed to send movie code %s from channel %s: %s", code, movie_channel, e)
        await message.answer(
            "Kino topilmadi yoki kod xato.\n"
            "Kod to'g'riligini tekshirib, qayta yuboring."
        )

@dp.message(F.text.isdigit())
async def handle_movie_code(message: types.Message, state: FSMContext):
    # Only accept movie codes in DMs, not in groups/channels
    if message.chat.type != 'private':
        return

    # Ignore movie-code messages if admin is entering premium prices
    cur_state = await state.get_state()
    if cur_state and 'waiting_for_premium_price' in cur_state:
        await message.answer("Premium narxni sozlayapsiz — avval narxni yuboring yoki /cancel bilan chiqib keting.")
        return

    not_subscribed = await check_subscriptions(message.from_user.id)
    if not_subscribed:
        await start_cmd(message, state)
        return
    await send_movie(message, message.text)

# --- ADMIN PANEL ---

@dp.message(Command("admin"))
async def admin_cmd(message: types.Message):
    if not is_admin(message.from_user.id): return
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Statistika", callback_data="adm_stats"))
    builder.row(InlineKeyboardButton(text="Kanallar (majburiy)", callback_data="adm_channels"))
    builder.row(InlineKeyboardButton(text="Reklama (yuborish)", callback_data="adm_broadcast"))
    builder.row(InlineKeyboardButton(text="Premium narx", callback_data="adm_premium"))
    builder.row(InlineKeyboardButton(text="Sozlamalar", callback_data="adm_settings"))
    
    await message.answer(
        "Admin panel:\n"
        "Statistika, majburiy kanallar, reklama va boshqa sozlamalarni shu yerda boshqaring.",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "adm_stats")
async def adm_stats_cb(callback: CallbackQuery):
    u_count = db_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
    a_count = db_query("SELECT COUNT(*) FROM admins", fetchone=True)[0]
    c_count = db_query("SELECT COUNT(*) FROM channels", fetchone=True)[0]
    premium_count = db_query("SELECT COUNT(*) FROM users WHERE is_premium = 1", fetchone=True)[0]
    
    # Count users who have joined mandatory channels (via join_requests table)
    subscribed_count = db_query(
        "SELECT COUNT(DISTINCT user_id) FROM join_requests",
        fetchone=True
    )[0]
    non_subscribed = u_count - subscribed_count
    
    text = (
        "📊 Bot Statistikasi:\n\n"
        f"👥 Hammasi: {u_count}\n"
        f"👑 Adminlar: {a_count}\n"
        f"📢 Majburiy kanallar: {c_count}\n"
        f"✅ Kanallarga azo bolganlar: {subscribed_count}\n"
        f"❌ Kanallarga azo bolmaganlar: {non_subscribed}\n"
        f"💎 Premium foydalanuvchilar: {premium_count}"
    )
    await callback.message.answer(text)
    
    # Show premium users list if any
    if premium_count > 0:
        premium_users = db_query(
            "SELECT user_id, username, premium_until FROM users WHERE is_premium = 1 ORDER BY premium_until DESC",
            fetchall=True
        )
        if premium_users:
            premium_text = "💎 Premium Foydalanuvchilar:\n"
            for user_id, username, until_date in premium_users:
                user_mention = f"@{username}" if username else f"ID: {user_id}"
                premium_text += f"• {user_mention} ({until_date})\n"
            await callback.message.answer(premium_text)
    
    await callback.answer()

@dp.callback_query(F.data == "adm_channels")
async def adm_channels_cb(callback: CallbackQuery):
    channels = db_query("SELECT channel_id, COALESCE(request_required,0) FROM channels", fetchall=True)
    builder = InlineKeyboardBuilder()
    for ch, req in channels:
        label = f"O'chirish: {ch} ({'zayavka' if req else 'oddiy'})"
        builder.row(InlineKeyboardButton(text=label, callback_data=f"del_ch|{ch}"))
    builder.row(InlineKeyboardButton(text="Kanal qo'shish (oddiy)", callback_data="add_ch|0"))
    builder.row(InlineKeyboardButton(text="Kanal qo'shish (zayavka)", callback_data="add_ch|1"))
    
    enabled = db_query("SELECT value FROM settings WHERE key = 'mandatory_enabled'", fetchone=True)[0]
    status_text = "Yoqilgan" if enabled == '1' else "O'chirilgan"
    builder.row(InlineKeyboardButton(text=f"Holat: {status_text}", callback_data="toggle_mandatory"))
    builder.row(InlineKeyboardButton(text="Orqaga", callback_data="adm_back"))
    
    await callback.message.edit_text(
        "Majburiy kanallarni boshqarish:\n"
        "• Oddiy: darhol qo'shadi\n"
        "• Zayavka: join-request yuboradi",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "toggle_mandatory")
async def toggle_mandatory_cb(callback: CallbackQuery):
    current = db_query("SELECT value FROM settings WHERE key = 'mandatory_enabled'", fetchone=True)[0]
    new_val = '0' if current == '1' else '1'
    db_query("UPDATE settings SET value = ? WHERE key = 'mandatory_enabled'", (new_val,))
    await adm_channels_cb(callback)

@dp.callback_query(F.data.startswith("add_ch|"))
async def add_ch_cb(callback: CallbackQuery, state: FSMContext):
    req_flag = callback.data.split("|")[1]
    await state.update_data(request_required=int(req_flag))
    await callback.message.answer(
        "Kanal username yoki ID sini yuboring (masalan: @kanal_nomi yoki -100...), "
        "yoki kanaldan bir dona xabarni forward qiling."
    )
    await state.set_state(AdminStates.waiting_for_channel)
    await callback.answer()

@dp.callback_query(F.data.startswith("del_ch|"))
async def del_ch_cb(callback: CallbackQuery):
    ch_id = callback.data.split("|")[1]
    db_query("DELETE FROM channels WHERE channel_id = ?", (ch_id,))
    await adm_channels_cb(callback)

@dp.callback_query(F.data == "adm_premium")
async def adm_premium_cb(callback: CallbackQuery, state: FSMContext):
    p1 = db_query("SELECT value FROM settings WHERE key = 'premium_price_1kun'", fetchone=True)[0]
    p7 = db_query("SELECT value FROM settings WHERE key = 'premium_price_1hafta'", fetchone=True)[0]
    p15 = db_query("SELECT value FROM settings WHERE key = 'premium_price_15kun'", fetchone=True)[0]
    p30 = db_query("SELECT value FROM settings WHERE key = 'premium_price_30kun'", fetchone=True)[0]
    info = db_query("SELECT value FROM settings WHERE key = 'premium_info_text'", fetchone=True)[0] or ""
    def show_price_admin(p):
        return ("Bepul" if (not p or p == '0') else f"{p} so'm")
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=f"1 kun — {show_price_admin(p1)}", callback_data="set_price|1kun"))
    builder.row(InlineKeyboardButton(text=f"1 hafta — {show_price_admin(p7)}", callback_data="set_price|1hafta"))
    builder.row(InlineKeyboardButton(text=f"15 kun — {show_price_admin(p15)}", callback_data="set_price|15kun"))
    builder.row(InlineKeyboardButton(text=f"30 kun — {show_price_admin(p30)}", callback_data="set_price|30kun"))
    builder.row(InlineKeyboardButton(text="Premium ma'lumotini o'zgartirish", callback_data="set_premium_info"))
    builder.row(InlineKeyboardButton(text="Orqaga", callback_data="adm_back"))
    text = "Premium paketlar va narxlari:"
    if info:
        text = f"{info}\n\n{text}"
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_cb(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Reklama xabarini yuboring. Forward qilingan yoki to‘g‘ridan-to‘g‘ri matn, rasm va video xabarlar ham o‘tadi.")
    await callback.answer()


@dp.callback_query(F.data == "vip_menu")
async def vip_menu_cb(callback: CallbackQuery):
    p1 = db_query("SELECT value FROM settings WHERE key = 'premium_price_1kun'", fetchone=True)[0]
    p7 = db_query("SELECT value FROM settings WHERE key = 'premium_price_1hafta'", fetchone=True)[0]
    p15 = db_query("SELECT value FROM settings WHERE key = 'premium_price_15kun'", fetchone=True)[0]
    p30 = db_query("SELECT value FROM settings WHERE key = 'premium_price_30kun'", fetchone=True)[0]
    info = db_query("SELECT value FROM settings WHERE key = 'premium_info_text'", fetchone=True)[0] or ""
    def show_price(p):
        return ("Bepul" if (not p or p == '0') else f"{p} so'm")
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=f"1 kun — {show_price(p1)}", callback_data="buy_premium|1kun"))
    builder.row(InlineKeyboardButton(text=f"1 hafta — {show_price(p7)}", callback_data="buy_premium|1hafta"))
    builder.row(InlineKeyboardButton(text=f"15 kun — {show_price(p15)}", callback_data="buy_premium|15kun"))
    builder.row(InlineKeyboardButton(text=f"30 kun — {show_price(p30)}", callback_data="buy_premium|30kun"))
    builder.row(InlineKeyboardButton(text="Orqaga", callback_data="vip_back"))
    text = "VIP paketlar:"
    if info:
        text = f"{info}\n\n{text}"
    await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "vip_back")
async def vip_back_cb(callback: CallbackQuery):
    await check_sub_cb(callback)
    await callback.answer()

@dp.callback_query(F.data.startswith("set_price|"))
async def set_price_cb(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Faqat adminlar mumkin", show_alert=True)
        return
    duration = callback.data.split("|")[1]
    await state.update_data(price_duration=duration)
    await callback.message.answer(f"{duration} uchun yangi narxni yuboring (faqat raqam yoki valyuta bilan):")
    await state.set_state(AdminStates.waiting_for_premium_price)
    await callback.answer()

@dp.callback_query(F.data == "set_premium_info")
async def set_premium_info_cb(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Faqat adminlar mumkin", show_alert=True)
        return
    await callback.message.answer(
        "Iltimos, premium bo'limidagi matnni yuboring. Bu foydalanuvchilarga VIP menyuda ko'rsatiladi."
    )
    await state.set_state(AdminStates.waiting_for_premium_info)
    await callback.answer()

@dp.message(AdminStates.waiting_for_premium_info)
async def premium_info_message(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = message.text or ""
    db_query("UPDATE settings SET value = ? WHERE key = ?", (text, 'premium_info_text'))
    await message.answer("Premium ma'lumotlari saqlandi.")
    await state.clear()

@dp.message(Command("broadcast"))
@dp.message(Command("reklama"))
async def broadcast_cmd(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Reklama xabarini yuboring. Forward qilingan yoki to‘g‘ridan-to‘g‘ridan matn, rasm va video xabarlar ham o‘tadi.")
    await state.set_state(AdminStates.waiting_for_broadcast)

@dp.message(Command("premium"))
async def premium_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Iltimos, premium beriladigan foydalanuvchi va muddatni yuboring. Masalan: /premium @user 1kun"
        )
        return
    target = parts[1].strip()
    duration_text = parts[2].strip()
    duration = parse_premium_duration(duration_text)
    if not duration:
        await message.answer(
            "Muddat topilmadi. Quyidagilardan birini yuboring: 1soat, 1kun, 1hafta, 15kun, 30kun."
        )
        return
    try:
        if target.startswith("@") or target.startswith("t.me/") or target.startswith("https://t.me/"):
            if not target.startswith("@"):
                target = target.replace("https://", "").replace("http://", "")
                if target.startswith("t.me/"):
                    target = "@" + target.split("/")[1].split("?")[0]
            chat = await bot.get_chat(target)
            target_id = chat.id
        else:
            target_id = int(target)
    except Exception:
        await message.answer("Foydalanuvchi topilmadi. To'g'ri @username yoki ID yuboring.")
        return
    set_premium(target_id, duration)
    expires = datetime.now() + duration
    await message.answer(
        f"{target} foydalanuvchisiga {duration_text} uchun premium berildi. Amal qilish muddati: {expires.strftime('%Y-%m-%d %H:%M:%S')}"
    )

@dp.message(Command("unpremium"))
async def unpremium_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Iltimos, premium olib qo‘yiladigan foydalanuvchi @username yoki ID ni yuboring. Masalan: /unpremium @user")
        return
    target = parts[1].strip()
    try:
        if target.startswith("@"):
            chat = await bot.get_chat(target)
            target_id = chat.id
        else:
            target_id = int(target)
    except Exception:
        await message.answer("Foydalanuvchi topilmadi. To'g'ri @username yoki ID yuboring.")
        return
    set_premium(target_id, False)
    await message.answer(f"{target} foydalanuvchisidan premium olingan.")

@dp.callback_query(F.data == "adm_settings")
async def adm_settings_cb(callback: CallbackQuery):
    current = db_query("SELECT value FROM settings WHERE key = 'movie_channel'", fetchone=True)[0]
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Kino kanalini o'zgartirish", callback_data="set_movie_ch"))
    builder.row(InlineKeyboardButton(text="To'lov linklarini sozlash", callback_data="adm_paylinks"))
    builder.row(InlineKeyboardButton(text="Orqaga", callback_data="adm_back"))
    await callback.message.edit_text(
        f"Sozlamalar:\\n\\nKino kanali: {current}",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "set_movie_ch")
async def set_movie_ch_cb(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Kino kanali ID yoki username'ini yuboring:")
    await state.set_state(AdminStates.waiting_for_movie_channel)
    await callback.answer()


@dp.callback_query(F.data == "adm_paylinks")
async def adm_paylinks_cb(callback: CallbackQuery):
    click_t = db_query("SELECT value FROM settings WHERE key = 'click_payment_url'", fetchone=True)[0]
    paynet_t = db_query("SELECT value FROM settings WHERE key = 'paynet_payment_url'", fetchone=True)[0]
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Set Click link template", callback_data="set_paylink|click"))
    builder.row(InlineKeyboardButton(text="Set Paynet link template", callback_data="set_paylink|paynet"))
    builder.row(InlineKeyboardButton(text="Orqaga", callback_data="adm_back"))
    await callback.message.edit_text(
        f"To'lov linklari sozlamalari:\n\nClick: {click_t or '(not set)'}\nPaynet: {paynet_t or '(not set)'}",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("set_paylink|"))
async def set_paylink_cb(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Faqat adminlar mumkin", show_alert=True)
        return
    provider = callback.data.split("|")[1]
    await state.update_data(pay_provider=provider)
    await callback.message.answer(
        "Iltimos, link shablonini yuboring. Foydalanish uchun {user_id}, {amount}, {duration}, {order_id}, {return_url} joylarini ishlatishingiz mumkin.\n" \
        "Namuna: https://click.example/pay?user={user_id}&amount={amount}&order={order_id}&return={return_url}"
    )
    await state.set_state(AdminStates.waiting_for_payment_link)
    await callback.answer()

@dp.callback_query(F.data == "adm_back")
async def adm_back_cb(callback: CallbackQuery):
    await admin_cmd(callback.message)
    await callback.message.delete()


@dp.callback_query(F.data.startswith("buy_premium|"))
async def buy_premium_cb(callback: CallbackQuery):
    duration = callback.data.split("|")[1]
    key = f"premium_price_{duration}"
    price = db_query("SELECT value FROM settings WHERE key = ?", (key,), fetchone=True)[0]
    if not price or price == '0':
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="Tasdiqlash (bepul)", callback_data=f"confirm_buy|{duration}"))
        kb.row(InlineKeyboardButton(text="Bekor qilish", callback_data="cancel"))
        await callback.message.answer(f"{duration} paket bepul. Tasdiqlaysizmi?", reply_markup=kb.as_markup())
        await callback.answer()
        return

    if PAYMENT_PROVIDER_TOKEN:
        price_int = int(price)
        invoice_title = f"VIP {duration} paketi"
        invoice_description = f"{duration} VIP paket uchun to'lov: {price_int} so'm"
        amount = amount_for_currency_units(price_int, PAYMENT_CURRENCY)
        prices = [LabeledPrice(label=invoice_title, amount=amount)]
        try:
            await bot.send_invoice(
                chat_id=callback.from_user.id,
                title=invoice_title,
                description=invoice_description,
                payload=f"premium|{duration}|{callback.from_user.id}",
                provider_token=PAYMENT_PROVIDER_TOKEN,
                currency=PAYMENT_CURRENCY,
                prices=prices,
                start_parameter=f"vip_{duration}",
                need_name=False,
                need_phone_number=False,
                need_email=False,
                need_shipping_address=False,
            )
            await callback.answer()
            return
        except Exception as exc:
            logger.error("Invoice send failed: %s", exc)
            await callback.message.answer("To'lovni yaratishda xatolik yuz berdi. Iltimos, keyinroq urinib ko'ring.")
            await callback.answer()
            return
    # External payment templates (Click / Paynet)
    click_tpl = db_query("SELECT value FROM settings WHERE key = 'click_payment_url'", fetchone=True)[0]
    paynet_tpl = db_query("SELECT value FROM settings WHERE key = 'paynet_payment_url'", fetchone=True)[0]
    me = await bot.get_me()
    bot_username = getattr(me, 'username', None)
    return_url_base = f"https://t.me/{bot_username}?start=paid|{{order_id}}" if bot_username else ""
    any_provider = False
    kb = InlineKeyboardBuilder()
    # For each provider, create an order and a payment url
    if click_tpl:
        order_id = f"click{int(time.time())}{callback.from_user.id}"
        db_query(
            "INSERT OR REPLACE INTO orders (order_id, user_id, duration, amount, provider, paid, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (order_id, callback.from_user.id, duration, int(price), 'click', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        try:
            url = click_tpl.format(user_id=callback.from_user.id, amount=price, duration=duration, order_id=order_id, return_url=return_url_base.format(order_id=order_id))
        except Exception:
            url = click_tpl
        kb.row(InlineKeyboardButton(text="Click to'lov", url=url))
        kb.row(InlineKeyboardButton(text="Men to'lov qildim (Click)", callback_data=f"confirm_buy|{order_id}"))
        any_provider = True
    if paynet_tpl:
        order_id2 = f"paynet{int(time.time())}{callback.from_user.id}"
        db_query(
            "INSERT OR REPLACE INTO orders (order_id, user_id, duration, amount, provider, paid, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (order_id2, callback.from_user.id, duration, int(price), 'paynet', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        try:
            url2 = paynet_tpl.format(user_id=callback.from_user.id, amount=price, duration=duration, order_id=order_id2, return_url=return_url_base.format(order_id=order_id2))
        except Exception:
            url2 = paynet_tpl
        kb.row(InlineKeyboardButton(text="Paynet to'lov", url=url2))
        kb.row(InlineKeyboardButton(text="Men to'lov qildim (Paynet)", callback_data=f"confirm_buy|{order_id2}"))
        any_provider = True

    if any_provider:
        kb.row(InlineKeyboardButton(text="Bekor qilish", callback_data="cancel"))
        await callback.message.answer(
            f"{duration} paket narxi: {price} so'm.\nQuyidagi to'lov usullaridan birini tanlang:",
            reply_markup=kb.as_markup()
        )
        await callback.answer()
        return

    # Fallback: manual confirmation
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="Men to'lov qildim — xabar yuborish", callback_data=f"confirm_buy|{duration}"))
    kb.row(InlineKeyboardButton(text="Bekor qilish", callback_data="cancel"))
    await callback.message.answer(
        f"{duration} paket narxi: {price} so'm.\n\nIltimos, to'lovni amalga oshiring va keyin 'Men to'lov qildim' tugmasini bosing.\nAdmin to'lovni tekshirib, premiumni faollashtiradi.",
        reply_markup=kb.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data == "cancel")
async def cancel_cb(callback: CallbackQuery):
    await callback.message.answer("Amal bekor qilindi.")
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_buy|"))
async def confirm_buy_cb(callback: CallbackQuery):
    token = callback.data.split("|")[1]
    # Check if token is an order_id
    ord_row = db_query("SELECT order_id, user_id, duration, amount, provider, paid FROM orders WHERE order_id = ?", (token,), fetchone=True)
    if ord_row:
        order_id, user_id, duration, amount, provider, paid = ord_row
        if paid:
            await callback.message.answer("Bu buyurtma uchun to'lov allaqachon qayd etilgan.")
            await callback.answer()
            return
        # notify admin about manual payment for this order
        try:
            await bot.send_message(SUPERADMIN_ID, f"To'lov bildirildi (manual):\nFoydalanuvchi: {callback.from_user.full_name} (id:{callback.from_user.id})\nOrder: {order_id}\nMuddat: {duration}\nNarx: {amount} so'm\nProvider: {provider}")
            await callback.message.answer("To'lov haqida adminga xabar yuborildi. Admin tekshirgach premiumni faollashtiradi.")
        except Exception as exc:
            logger.error("Failed to notify admin about manual payment: %s", exc)
            await callback.message.answer("Xatolik yuz berdi, iltimos keyinroq urinib ko'ring.")
        await callback.answer()
        return

    # Fallback: token treated as duration (legacy flows)
    duration = token
    key = f"premium_price_{duration}"
    price = db_query("SELECT value FROM settings WHERE key = ?", (key,), fetchone=True)[0]
    user = callback.from_user
    # If free, grant immediately
    if not price or price == '0':
        delta = parse_premium_duration(duration)
        set_premium(user.id, delta)
        await callback.message.answer(f"Sizga {duration} uchun premium berildi. Tabriklaymiz!")
        await callback.answer()
        return

    if PAYMENT_PROVIDER_TOKEN:
        await callback.message.answer(
            "Avto-to'lov tizimi yoqilgan. To'lovni tugatganingizdan so'ng, bu yerga qaytib "
            "to'lovni yakunlang. Agar siz to'lov qildingiz, to'lov xabarini kuting."
        )
        await callback.answer()
        return

    # Paid: notify superadmin for verification
    try:
        admin_chat = SUPERADMIN_ID
        user_display = f"{user.full_name} (id:{user.id})"
        await bot.send_message(admin_chat, f"To'lov so'rovi:\nFoydalanuvchi: {user_display}\nMuddat: {duration}\nNarx: {price} so'm\nTekshirib, /premium <{user.id}> {duration} bilan premium bering.")
        await callback.message.answer("To'lov haqida adminga xabar yuborildi. Admin tasdiqlagach premium faollashadi.")
    except Exception as exc:
        logger.error("Failed to notify admin about purchase: %s", exc)
        await callback.message.answer("To'lovni yuborishda xatolik yuz berdi, iltimos keyinroq urinib ko'ring.")
    await callback.answer()

# --- PAYMENT HANDLERS ---

@dp.pre_checkout_query()
async def pre_checkout_query(pre_checkout: types.PreCheckoutQuery):
    await pre_checkout.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    payment = message.successful_payment
    if not payment or not payment.invoice_payload:
        return
    payload = payment.invoice_payload
    if not payload.startswith("premium|"):
        return
    parts = payload.split("|")
    if len(parts) < 3:
        return
    duration = parts[1]
    delta = parse_premium_duration(duration)
    if not delta:
        return
    set_premium(message.from_user.id, delta)
    await message.answer(f"To'lov qabul qilindi. Sizga {duration} uchun premium berildi.")
    try:
        await bot.send_message(
            SUPERADMIN_ID,
            f"Premium avtomatik faollashtirildi:\nFoydalanuvchi: {message.from_user.full_name} (id:{message.from_user.id})\nMuddat: {duration}."
        )
    except Exception as exc:
        logger.error("Failed to notify admin about auto premium: %s", exc)

# --- FSM PROCESSORS ---

@dp.message(AdminStates.waiting_for_channel)
async def proc_add_ch(message: types.Message, state: FSMContext):
    data = await state.get_data()
    req_flag = int(data.get("request_required", 0))

    # Try to extract channel from forwarded message first
    source_chat = None
    if getattr(message, "forward_from_chat", None):
        source_chat = message.forward_from_chat
    elif getattr(message, "forward_origin", None):
        origin = message.forward_origin
        chat = getattr(origin, "chat", None)
        if chat:
            source_chat = chat

    channel_id = None
    if source_chat:
        # Prefer numeric ID for reliability
        channel_id = str(source_chat.id)
    else:
        text_val = (message.text or "").strip()
        if not text_val:
            await message.answer("Kanal qo'shish uchun username/ID yuboring yoki kanal xabarini forward qiling.")
            return
        channel_id = text_val

    await state.update_data(channel_id=channel_id)

    if req_flag == 1:
        await message.answer(
            "Zayavka kanali uchun join link yuboring (masalan: https://t.me/+ilUQlM-PNQQxZDli)."
        )
        await state.set_state(AdminStates.waiting_for_invite_link)
        return

    db_query(
        "INSERT OR IGNORE INTO channels (channel_id, request_required, invite_link) VALUES (?, ?, ?)",
        (channel_id, req_flag, None)
    )
    await message.answer(f"{channel_id} qo'shildi. Tur: {'zayavka' if req_flag else 'oddiy'}.")
    await state.clear()

@dp.message(AdminStates.waiting_for_invite_link)
async def proc_add_invite_link(message: types.Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("channel_id")
    req_flag = int(data.get("request_required", 1))

    invite_link = (message.text or "").strip()
    if not invite_link:
        await message.answer("Join link yuboring (https://t.me/+...).")
        return
    # Basic validation for zayavka links
    if not (
        invite_link.startswith("https://t.me/+")
        or invite_link.startswith("https://t.me/joinchat/")
        or "join_request=1" in invite_link
    ):
        await message.answer("Zayavka uchun t.me/+ yoki joinchat link yuboring.")
        return

    db_query(
        "INSERT OR REPLACE INTO channels (channel_id, request_required, invite_link) VALUES (?, ?, ?)",
        (channel_id, req_flag, invite_link)
    )
    await message.answer(f"{channel_id} qo'shildi. Join link saqlandi.")
    await state.clear()

@dp.message(AdminStates.waiting_for_movie_channel)
async def proc_set_movie_ch(message: types.Message, state: FSMContext):
    db_query("UPDATE settings SET value = ? WHERE key = 'movie_channel'", (message.text,))
    await message.answer(f"Kino kanali {message.text} ga o'zgartirildi.")
    await state.clear()

@dp.message(AdminStates.waiting_for_premium_price)
async def proc_set_premium_price(message: types.Message, state: FSMContext):
    new_price_raw = (message.text or "").strip()
    if not new_price_raw:
        await message.answer("Iltimos, premium narxni yuboring.")
        return
    data = await state.get_data()
    duration = data.get("price_duration")
    if not duration:
        await message.answer("Muddat tanlanmadi. Iltimos, qayta urinib ko'ring.")
        await state.clear()
        return
    key_map = {
        '1kun': 'premium_price_1kun',
        '1hafta': 'premium_price_1hafta',
        '15kun': 'premium_price_15kun',
        '30kun': 'premium_price_30kun'
    }
    key = key_map.get(duration)
    if not key:
        await message.answer("Noma'lum muddat. Iltimos, menyudan tanlang.")
        await state.clear()
        return
    price_val = parse_price_to_int(new_price_raw)
    if price_val is None:
        await message.answer("Iltimos, narxni faqat raqam bilan yuboring (masalan: 10000).")
        return
    db_query("UPDATE settings SET value = ? WHERE key = ?", (str(price_val), key))
    await message.answer(f"{duration} uchun premium narx {price_val} so'm ga o'rnatildi.")
    await state.clear()


@dp.message(AdminStates.waiting_for_payment_link)
async def proc_set_payment_link(message: types.Message, state: FSMContext):
    data = await state.get_data()
    provider = data.get("pay_provider")
    tpl = (message.text or "").strip()
    if not provider or not tpl:
        await message.answer("Noto'g'ri so'rov. Iltimos, qayta urinib ko'ring.")
        await state.clear()
        return
    key = 'click_payment_url' if provider == 'click' else 'paynet_payment_url'
    db_query("UPDATE settings SET value = ? WHERE key = ?", (tpl, key))
    await message.answer(f"{provider} payment template saqlandi.")
    await state.clear()

@dp.message(AdminStates.waiting_for_broadcast)
async def proc_broadcast(message: types.Message, state: FSMContext):
    users = [row[0] for row in db_query("SELECT user_id FROM users", fetchall=True)]
    count = 0
    msg = await message.answer(f"Yuborilmoqda: 0/{len(users)}")
    for i, u_id in enumerate(users):
        try:
            await bot.copy_message(chat_id=u_id, from_chat_id=message.chat.id, message_id=message.message_id)
            count += 1
        except TelegramRetryAfter as exc:
            logger.warning("Flood wait %.2fs when sending to %s", exc.retry_after, u_id)
            await asyncio.sleep(exc.retry_after + 1)
            try:
                await bot.copy_message(chat_id=u_id, from_chat_id=message.chat.id, message_id=message.message_id)
                count += 1
            except Exception as retry_exc:
                logger.error("Second attempt failed for %s: %s", u_id, retry_exc)
        except TelegramForbiddenError:
            logger.info("User %s blocked the bot; skipping.", u_id)
        except Exception as exc:
            logger.error("Broadcast failed for %s: %s", u_id, exc)
        if count % 20 == 0:
            await msg.edit_text(f"Yuborilmoqda: {count}/{len(users)}")
        await asyncio.sleep(0.05)
    await msg.edit_text(f"Tugatildi. {count} ta foydalanuvchiga yuborildi.")
    await state.clear()

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())