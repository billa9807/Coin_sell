"""
╔══════════════════════════════════════════════════════════════╗
║         Coin Sell Telegram Bot  —  Python (telebot)          ║
╠══════════════════════════════════════════════════════════════╣
║  RAILWAY DEPLOY — মাত্র ২টি ফাইল (bot.py + requirements.txt) ║
║                                                              ║
║  ১. Railway.app → New Project → GitHub repo বেছে নিন        ║
║  ২. Add PostgreSQL service (+ New → Database → PostgreSQL)   ║
║  ③. Bot service → Settings → Start Command:                  ║
║         python bot.py                                        ║
║  ৪. Bot service → Variables → নিচেরগুলো add করুন:           ║
║       TELEGRAM_BOT_TOKEN  = BotFather থেকে পাওয়া token      ║
║       ADMIN_TELEGRAM_IDS  = আপনার Telegram ID               ║
║       DATABASE_URL        = PostgreSQL service এ auto-set    ║
║  ৫. Deploy করুন — database tables auto-create হবে           ║
╚══════════════════════════════════════════════════════════════╝
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import telebot
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

BOT_TOKEN: str    = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL: str = os.environ["DATABASE_URL"]
DHAKA_TZ          = ZoneInfo("Asia/Dhaka")

COIN_TYPES = ["NS", "NIVA", "TOP"]
COIN_DISPLAY_NAMES = {
    "NS":   "NS Coin",
    "NIVA": "Niva Coin",
    "TOP":  "Top Coin",
}

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def db_fetchall(sql, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

def db_fetchone(sql, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

def db_execute(sql, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()

def db_execute_returning(sql, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
        return row

# ─────────────────────────────────────────────
# Auto-create database tables on startup
# ─────────────────────────────────────────────

def init_db():
    logger.info("Initializing database tables...")
    db_execute("""
        CREATE TABLE IF NOT EXISTS coin_rates (
            coin_type          TEXT PRIMARY KEY,
            display_name       TEXT NOT NULL,
            rate_per_thousand  NUMERIC NOT NULL DEFAULT 0,
            receiving_username TEXT NOT NULL DEFAULT ''
        )
    """)
    db_execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id                   SERIAL PRIMARY KEY,
            telegram_user_id     BIGINT NOT NULL,
            telegram_username    TEXT,
            telegram_first_name  TEXT,
            coin_type            TEXT NOT NULL,
            quantity             BIGINT NOT NULL,
            rate_per_thousand    NUMERIC NOT NULL,
            amount_bdt           NUMERIC NOT NULL,
            bkash_number         TEXT NOT NULL,
            screenshot_file_id   TEXT,
            status               TEXT NOT NULL DEFAULT 'pending',
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    db_execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id                         SERIAL PRIMARY KEY,
            telegram_user_id           BIGINT UNIQUE NOT NULL,
            added_by_telegram_user_id  BIGINT,
            created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    db_execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            id               SERIAL PRIMARY KEY,
            support_contact  TEXT NOT NULL DEFAULT ''
        )
    """)
    # Default coin rates (only inserted if table was just created / empty)
    db_execute("""
        INSERT INTO coin_rates (coin_type, display_name, rate_per_thousand, receiving_username)
        VALUES
            ('NS',   'NS Coin',   800, ''),
            ('NIVA', 'Niva Coin', 750, ''),
            ('TOP',  'Top Coin',  900, '')
        ON CONFLICT DO NOTHING
    """)
    logger.info("Database ready.")

# ─────────────────────────────────────────────
# Admin helpers
# ─────────────────────────────────────────────

def get_bootstrap_admin_ids() -> list[int]:
    raw = os.environ.get("ADMIN_TELEGRAM_IDS", "")
    result = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            result.append(int(part))
    return result

def get_admin_ids() -> list[int]:
    rows = db_fetchall("SELECT telegram_user_id FROM admins")
    db_ids = [r["telegram_user_id"] for r in rows]
    return list(set(get_bootstrap_admin_ids() + db_ids))

def is_admin(user_id) -> bool:
    if user_id is None:
        return False
    if user_id in get_bootstrap_admin_ids():
        return True
    row = db_fetchone("SELECT 1 FROM admins WHERE telegram_user_id = %s", (user_id,))
    return row is not None

def is_bootstrap_admin(user_id) -> bool:
    if user_id is None:
        return False
    return user_id in get_bootstrap_admin_ids()

def add_admin(telegram_user_id, added_by):
    db_execute(
        "INSERT INTO admins (telegram_user_id, added_by_telegram_user_id) "
        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (telegram_user_id, added_by),
    )

def remove_admin(telegram_user_id):
    db_execute("DELETE FROM admins WHERE telegram_user_id = %s", (telegram_user_id,))

# ─────────────────────────────────────────────
# Conversation state (in-memory)
# ─────────────────────────────────────────────

conversations: dict = {}

def get_state(chat_id) -> dict:
    if chat_id not in conversations:
        conversations[chat_id] = {"step": "idle"}
    return conversations[chat_id]

def reset_state(chat_id):
    conversations[chat_id] = {"step": "idle"}

# ─────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────

def fmt_num(v) -> str:
    return f"{int(v):,}"

def fmt_money(v) -> str:
    return f"{float(v):,.2f}"

def fmt_dhaka(dt: datetime):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    d = dt.astimezone(DHAKA_TZ)
    return d.strftime("%Y-%m-%d"), d.strftime("%I:%M %p")

def status_label(status: str) -> str:
    return {
        "pending":  "পেন্ডিং (Processing)",
        "approved": "সম্পন্ন (Approved)",
        "rejected": "বাতিল (Rejected)",
    }.get(status, status)

# ─────────────────────────────────────────────
# Menu text constants
# ─────────────────────────────────────────────

MENU_SELL    = "💰 কয়েন সেল করুন"
MENU_RATE    = "📊 আজকের রেট"
MENU_ORDERS  = "📋 আমার অর্ডার"
MENU_SUPPORT = "☎️ সাপোর্ট"
MENU_ADMIN   = "🛠 এডমিন প্যানেল"
MENU_BACK    = "⬅️ মেইন মেনু"

ADMIN_PENDING  = "📥 পেন্ডিং অর্ডার"
ADMIN_RATES    = "💱 রেট পরিবর্তন"
ADMIN_USERNAME = "🧑‍🦲 কয়েনের ইউজারনেম সেট করুন"
ADMIN_SUPPORT  = "☎️ সাপোর্ট কন্টাক্ট সেট করুন"
ADMIN_STATS    = "📈 স্ট্যাটাস"
ADMIN_ADD      = "➕ নতুন এডমিন যুক্ত করুন"
ADMIN_REMOVE   = "➖ এডমিন রিমুভ করুন"
ADMIN_LIST     = "📋 এডমিন লিস্ট"

# ─────────────────────────────────────────────
# Keyboard builders  (raw JSON → style support)
# ─────────────────────────────────────────────

def _kb(rows: list, resize=True) -> str:
    return json.dumps({"keyboard": rows, "resize_keyboard": resize})

def _btn(text: str, style: str | None = None) -> dict:
    d = {"text": text}
    if style:
        d["style"] = style
    return d

def _inline_kb(rows: list) -> str:
    return json.dumps({"inline_keyboard": rows})

def _ibtn(text: str, style: str | None = None, **kwargs) -> dict:
    d = {"text": text, **kwargs}
    if style:
        d["style"] = style
    return d

# Coin-specific styles
COIN_STYLES = {
    "NIVA": "success",   # 🟢 green
    "NS":   "primary",   # 🔵 blue
    "TOP":  "danger",    # 🔴 red
}

def main_menu_kb(admin: bool) -> str:
    rows = [
        [_btn(MENU_SELL, "success"),   _btn(MENU_RATE,    "primary")],
        [_btn(MENU_ORDERS, "primary"), _btn(MENU_SUPPORT, "primary")],
    ]
    if admin:
        rows.append([_btn(MENU_ADMIN, "danger")])
    return _kb(rows)

def admin_menu_kb() -> str:
    rows = [
        [_btn(ADMIN_PENDING,  "danger"),  _btn(ADMIN_RATES,    "primary")],
        [_btn(ADMIN_USERNAME, "primary"), _btn(ADMIN_SUPPORT,  "primary")],
        [_btn(ADMIN_STATS,    "success")],
        [_btn(ADMIN_ADD,      "success"), _btn(ADMIN_REMOVE,   "danger")],
        [_btn(ADMIN_LIST,     "primary")],
        [_btn(MENU_BACK)],
    ]
    return _kb(rows)

def coin_select_kb(rates: list) -> str:
    rows = []
    for r in rates:
        name  = COIN_DISPLAY_NAMES.get(r["coin_type"], r["coin_type"])
        style = COIN_STYLES.get(r["coin_type"], "primary")
        rows.append([_ibtn(
            f"{name} ({r['rate_per_thousand']} টাকা/হাজার)",
            style=style,
            callback_data=f"select_coin:{r['coin_type']}",
        )])
    return _inline_kb(rows)

def copy_username_kb(username: str) -> str:
    return _inline_kb([[
        _ibtn(username, style="success", copy_text={"text": username})
    ]])

def order_action_kb(order_id: int) -> str:
    return _inline_kb([[
        _ibtn("✅ Approve", style="success", callback_data=f"order_approve:{order_id}"),
        _ibtn("❌ Reject",  style="danger",  callback_data=f"order_reject:{order_id}"),
    ]])

def rate_select_kb(rates: list) -> str:
    rows = []
    for r in rates:
        rows.append([_ibtn(
            f"{r['display_name']} — বর্তমান রেট: {r['rate_per_thousand']}",
            style="primary",
            callback_data=f"set_rate:{r['coin_type']}",
        )])
    return _inline_kb(rows)

def username_select_kb(rates: list) -> str:
    rows = []
    for r in rates:
        rows.append([_ibtn(
            f"{r['display_name']} — বর্তমান ইউজারনেম: {r['receiving_username']}",
            style="primary",
            callback_data=f"set_username:{r['coin_type']}",
        )])
    return _inline_kb(rows)

# ─────────────────────────────────────────────
# DB query helpers
# ─────────────────────────────────────────────

def get_rates():
    return db_fetchall("SELECT * FROM coin_rates ORDER BY coin_type")

def get_settings():
    return db_fetchone("SELECT * FROM bot_settings LIMIT 1")

def get_rate_for_coin(coin_type):
    return db_fetchone("SELECT * FROM coin_rates WHERE coin_type = %s", (coin_type,))

# ─────────────────────────────────────────────
# Notify admins
# ─────────────────────────────────────────────

def notify_admins(order):
    caption = (
        f"🆕 নতুন অর্ডার #{order['id']}\n"
        f"👤 {order['telegram_first_name'] or 'Unknown'}"
        + (f" (@{order['telegram_username']})" if order["telegram_username"] else "")
        + f"\n🪙 {COIN_DISPLAY_NAMES.get(order['coin_type'], order['coin_type'])}: "
        + f"{fmt_num(order['quantity'])} coins\n"
        + f"💰 {fmt_money(order['amount_bdt'])} BDT\n"
        + f"📱 বিকাশ: {order['bkash_number']}"
    )
    kb = order_action_kb(order["id"])
    for admin_id in get_admin_ids():
        try:
            if order["screenshot_file_id"]:
                bot.send_photo(admin_id, order["screenshot_file_id"],
                               caption=caption, reply_markup=kb)
            else:
                bot.send_message(admin_id, caption, reply_markup=kb)
        except Exception as e:
            logger.error(f"Admin notify failed ({admin_id}): {e}")

# ─────────────────────────────────────────────
# /start  /admin
# ─────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(msg):
    reset_state(msg.chat.id)
    bot.send_message(
        msg.chat.id,
        "👋 স্বাগতম! আপনি কী করতে চান নিচ থেকে বেছে নিন।",
        reply_markup=main_menu_kb(is_admin(msg.from_user.id)),
    )

@bot.message_handler(commands=["admin"])
def cmd_admin(msg):
    if not is_admin(msg.from_user.id):
        bot.send_message(msg.chat.id, "⛔ আপনার এই কমান্ড ব্যবহারের অনুমতি নেই।")
        return
    reset_state(msg.chat.id)
    bot.send_message(msg.chat.id,
                     "🛠 এডমিন প্যানেল — একটি অপশন বাছাই করুন:",
                     reply_markup=admin_menu_kb())

# ─────────────────────────────────────────────
# Photo handler
# ─────────────────────────────────────────────

@bot.message_handler(content_types=["photo"])
def on_photo(msg):
    state = get_state(msg.chat.id)
    if state.get("step") == "await_screenshot":
        state["screenshot_file_id"] = msg.photo[-1].file_id
        state["step"] = "await_bkash"
        bot.send_message(msg.chat.id,
                         "✅ স্ক্রিনশট পাওয়া গেছে। আপনার বিকাশ নাম্বার লিখুন:")

# ─────────────────────────────────────────────
# Text handler
# ─────────────────────────────────────────────

@bot.message_handler(content_types=["text"])
def on_text(msg):
    chat_id = msg.chat.id
    user_id = msg.from_user.id if msg.from_user else None
    text    = (msg.text or "").strip()
    admin   = is_admin(user_id)

    try:
        # ── Main menu ────────────────────────────────────────────
        if text == MENU_SELL:
            rates = get_rates()
            if not rates:
                bot.send_message(chat_id, "⚠️ এই মুহূর্তে কোনো কয়েনের রেট সেট করা নেই।")
            else:
                bot.send_message(chat_id, "🪙 কোন কয়েন সেল করতে চান?",
                                 reply_markup=coin_select_kb(rates))
            return

        if text == MENU_RATE:
            rates = get_rates()
            if not rates:
                bot.send_message(chat_id, "⚠️ এই মুহূর্তে কোনো রেট সেট করা নেই।")
            else:
                lines = "\n".join(
                    f"🪙 {r['display_name']}: {r['rate_per_thousand']} টাকা / হাজার কয়েন"
                    for r in rates
                )
                bot.send_message(chat_id, f"📊 আজকের রেট\n\n{lines}")
            return

        if text == MENU_ORDERS:
            orders = db_fetchall(
                "SELECT * FROM orders WHERE telegram_user_id = %s "
                "ORDER BY created_at DESC LIMIT 10", (user_id,)
            )
            if not orders:
                bot.send_message(chat_id, "📋 আপনার কোনো অর্ডার নেই।")
            else:
                lines = "\n".join(
                    f"🗂 #{o['id']} — {fmt_num(o['quantity'])} "
                    f"{COIN_DISPLAY_NAMES.get(o['coin_type'], o['coin_type'])} — "
                    f"{fmt_money(o['amount_bdt'])} BDT — {status_label(o['status'])}"
                    for o in orders
                )
                bot.send_message(chat_id, f"📋 আপনার সাম্প্রতিক অর্ডার\n\n{lines}")
            return

        if text == MENU_SUPPORT:
            settings = get_settings()
            contact = settings["support_contact"] if settings else "সেট করা নেই"
            bot.send_message(chat_id, f"☎️ সাপোর্ট প্রয়োজন হলে যোগাযোগ করুন:\n{contact}")
            return

        if text == MENU_ADMIN:
            if not admin:
                bot.send_message(chat_id, "⛔ আপনার এই মেনু ব্যবহারের অনুমতি নেই।")
            else:
                reset_state(chat_id)
                bot.send_message(chat_id,
                                 "🛠 এডমিন প্যানেল — একটি অপশন বাছাই করুন:",
                                 reply_markup=admin_menu_kb())
            return

        if text == MENU_BACK:
            reset_state(chat_id)
            bot.send_message(chat_id, "⬅️ মেইন মেনুতে ফিরে গেলেন।",
                             reply_markup=main_menu_kb(admin))
            return

        # ── Admin menu ───────────────────────────────────────────
        if admin:
            if text == ADMIN_PENDING:
                orders = db_fetchall(
                    "SELECT * FROM orders WHERE status='pending' "
                    "ORDER BY created_at DESC LIMIT 20"
                )
                if not orders:
                    bot.send_message(chat_id, "✅ কোনো পেন্ডিং অর্ডার নেই।")
                else:
                    for o in orders:
                        cap = (
                            f"🗂 অর্ডার #{o['id']}\n"
                            f"👤 {o['telegram_first_name'] or 'Unknown'}"
                            + (f" (@{o['telegram_username']})" if o["telegram_username"] else "")
                            + f"\n🪙 {COIN_DISPLAY_NAMES.get(o['coin_type'], o['coin_type'])}: "
                            + f"{fmt_num(o['quantity'])} coins"
                            + f"\n💰 {fmt_money(o['amount_bdt'])} BDT"
                            + f"\n📱 বিকাশ: {o['bkash_number']}"
                        )
                        kb = order_action_kb(o["id"])
                        if o["screenshot_file_id"]:
                            bot.send_photo(chat_id, o["screenshot_file_id"],
                                           caption=cap, reply_markup=kb)
                        else:
                            bot.send_message(chat_id, cap + "\n\n(কোনো স্ক্রিনশট নেই)",
                                             reply_markup=kb)
                return

            if text == ADMIN_RATES:
                bot.send_message(chat_id, "💱 কোন কয়েনের রেট পরিবর্তন করতে চান?",
                                 reply_markup=rate_select_kb(get_rates()))
                return

            if text == ADMIN_USERNAME:
                bot.send_message(chat_id, "🧑‍🦲 কোন কয়েনের ইউজারনেম পরিবর্তন করতে চান?",
                                 reply_markup=username_select_kb(get_rates()))
                return

            if text == ADMIN_SUPPORT:
                get_state(chat_id)["step"] = "admin_await_support"
                bot.send_message(chat_id, "☎️ ইউজারদের জন্য সাপোর্ট কন্টাক্ট তথ্য লিখুন:")
                return

            if text == ADMIN_STATS:
                totals = db_fetchone(
                    "SELECT COUNT(*) AS total_orders, "
                    "SUM(CASE WHEN status='approved' THEN amount_bdt::numeric ELSE 0 END) AS total_paid "
                    "FROM orders"
                )
                status_rows = db_fetchall(
                    "SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status"
                )
                coin_rows = db_fetchall(
                    "SELECT coin_type, SUM(quantity) AS qty FROM orders "
                    "WHERE status='approved' GROUP BY coin_type"
                )
                counts = {r["status"]: r["cnt"] for r in status_rows}
                coin_lines = "\n".join(
                    f"🪙 {COIN_DISPLAY_NAMES.get(c['coin_type'], c['coin_type'])}: "
                    f"{fmt_num(c['qty'] or 0)} coins"
                    for c in coin_rows
                ) or "কোনো এপ্রুভড অর্ডার নেই।"
                bot.send_message(
                    chat_id,
                    f"📈 স্ট্যাটাস\n\n"
                    f"📦 মোট অর্ডার: {fmt_num(totals['total_orders'] or 0)}\n"
                    f"⏳ পেন্ডিং: {fmt_num(counts.get('pending', 0))}\n"
                    f"✅ এপ্রুভড: {fmt_num(counts.get('approved', 0))}\n"
                    f"❌ রিজেক্টেড: {fmt_num(counts.get('rejected', 0))}\n"
                    f"💰 মোট পরিশোধিত: {fmt_money(totals['total_paid'] or 0)} BDT\n\n"
                    f"{coin_lines}",
                )
                return

            if text == ADMIN_ADD:
                get_state(chat_id)["step"] = "admin_await_add_admin"
                bot.send_message(
                    chat_id,
                    "➕ নতুন এডমিনের টেলিগ্রাম ইউজার আইডি (সংখ্যা) লিখুন।\n"
                    "ℹ️ ইউজার আইডি জানতে তাকে বলুন @userinfobot কে মেসেজ পাঠাতে।",
                )
                return

            if text == ADMIN_REMOVE:
                ids = get_admin_ids()
                id_list = "\n".join(str(i) for i in ids) if ids else "কোনো এডমিন নেই।"
                get_state(chat_id)["step"] = "admin_await_remove_admin"
                bot.send_message(
                    chat_id,
                    f"➖ কোন এডমিনকে রিমুভ করতে চান? আইডি লিখে পাঠান:\n\n{id_list}"
                )
                return

            if text == ADMIN_LIST:
                ids = get_admin_ids()
                lines = "\n".join(
                    f"👤 {uid}" + (" (মূল এডমিন)" if is_bootstrap_admin(uid) else "")
                    for uid in ids
                ) if ids else "কোনো এডমিন নেই।"
                bot.send_message(chat_id, f"📋 এডমিন লিস্ট\n\n{lines}")
                return

        # ── Conversation steps ───────────────────────────────────
        state = get_state(chat_id)
        step  = state.get("step")

        if step == "await_quantity":
            _handle_quantity(chat_id, text, state)
        elif step == "await_bkash":
            _handle_bkash(chat_id, text, state, msg.from_user)
        elif step == "admin_await_rate" and admin:
            _handle_rate_input(chat_id, text, state)
        elif step == "admin_await_coin_username" and admin:
            _handle_username_input(chat_id, text, state)
        elif step == "admin_await_support" and admin:
            _handle_support_input(chat_id, text)
        elif step == "admin_await_add_admin" and admin:
            _handle_add_admin(chat_id, text, user_id)
        elif step == "admin_await_remove_admin" and admin:
            _handle_remove_admin(chat_id, text)

    except Exception as e:
        logger.exception(f"Error in chat {chat_id}: {e}")
        try:
            bot.send_message(chat_id, "⚠️ কিছু একটা সমস্যা হয়েছে। আবার চেষ্টা করুন।")
        except Exception:
            pass

# ─────────────────────────────────────────────
# Conversation step handlers
# ─────────────────────────────────────────────

def _handle_quantity(chat_id, text, state):
    cleaned = text.replace(",", "").replace(" ", "")
    try:
        quantity = int(cleaned)
        if quantity <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "⚠️ সঠিক সংখ্যা লিখুন (যেমন: 100000)।")
        return

    coin_type = state.get("coin_type")
    rate      = state.get("rate_per_thousand", 0)
    amount    = (quantity / 1000) * rate

    rate_row = get_rate_for_coin(coin_type) if coin_type else None
    username = rate_row["receiving_username"] if rate_row else "সেট করা নেই"

    state["quantity"]   = quantity
    state["amount_bdt"] = amount
    state["step"]       = "await_screenshot"

    bot.send_message(
        chat_id,
        f"🔘 পরিমাণ: {fmt_num(quantity)} {COIN_DISPLAY_NAMES.get(coin_type, coin_type)}\n"
        f"💵 পাবেন: {fmt_money(amount)} BDT\n\n"
        f"👇 নিচের বাটনে ক্লিক করে ইউজারনেম কপি করুন, তারপর কয়েন সেন্ড করুন:\n\n"
        f"📤 কয়েন পাঠানোর পর স্ক্রিনশট আপলোড করুন:",
        reply_markup=copy_username_kb(username),
    )


def _handle_bkash(chat_id, text, state, from_user):
    bkash = text.strip()
    if not re.match(r"^01[0-9]{9}$", bkash):
        bot.send_message(chat_id, "⚠️ সঠিক বিকাশ নাম্বার লিখুন (যেমন: 01712345678)।")
        return

    coin_type  = state.get("coin_type")
    quantity   = state.get("quantity")
    amount_bdt = state.get("amount_bdt")
    rate       = state.get("rate_per_thousand", 0)
    screenshot = state.get("screenshot_file_id")

    if not coin_type or not quantity or amount_bdt is None:
        bot.send_message(chat_id, "⚠️ কিছু ভুল হয়েছে। আবার শুরু করুন।")
        reset_state(chat_id)
        return

    order = db_execute_returning(
        """
        INSERT INTO orders
          (telegram_user_id, telegram_username, telegram_first_name,
           coin_type, quantity, rate_per_thousand, amount_bdt,
           bkash_number, screenshot_file_id, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending') RETURNING *
        """,
        (
            from_user.id,
            getattr(from_user, "username", None),
            getattr(from_user, "first_name", None),
            coin_type, quantity, str(rate), str(amount_bdt),
            bkash, screenshot,
        ),
    )

    reset_state(chat_id)

    if not order:
        bot.send_message(chat_id, "⚠️ অর্ডার তৈরি করতে সমস্যা হয়েছে।")
        return

    date_str, time_str = fmt_dhaka(order["created_at"])
    bot.send_message(
        chat_id,
        f"⏳ রিকোয়েস্ট সফলভাবে সাবমিট হয়েছে!\n\n"
        f"🧾 পেমেন্ট রিসিট\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🗂 অর্ডার আইডি: #{order['id']}\n"
        f"🪙 টোটাল কয়েন: {fmt_num(order['quantity'])} Coins\n"
        f"💰 প্রাপ্য অ্যামাউন্ট: {fmt_money(order['amount_bdt'])} BDT\n"
        f"📱 বিকাশ অ্যাকাউন্ট: {order['bkash_number']}\n"
        f"📅 তারিখ: {date_str} ({time_str})\n"
        f"⏱ অবস্তা: {status_label(order['status'])}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✨ এডমিন ভেরিফাই করে কিছুক্ষণের মধ্যে টাকা পাঠিয়ে দেবে।",
    )
    notify_admins(order)


def _handle_rate_input(chat_id, text, state):
    try:
        rate = float(text.replace(",", "").replace(" ", ""))
        if rate <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "⚠️ সঠিক সংখ্যা লিখুন (যেমন: 800)।")
        return

    coin_type = state.get("pending_rate_coin_type")
    if not coin_type:
        bot.send_message(chat_id, "⚠️ কিছু ভুল হয়েছে। আবার শুরু করুন।")
        reset_state(chat_id)
        return

    db_execute("UPDATE coin_rates SET rate_per_thousand = %s WHERE coin_type = %s",
               (str(rate), coin_type))
    reset_state(chat_id)
    bot.send_message(
        chat_id,
        f"✅ {COIN_DISPLAY_NAMES.get(coin_type, coin_type)} এর রেট "
        f"{rate} টাকা/হাজার কয়েনে সেট করা হয়েছে।",
        reply_markup=admin_menu_kb(),
    )


def _handle_username_input(chat_id, text, state):
    username = text.strip().lstrip("@")
    if not username:
        bot.send_message(chat_id, "⚠️ একটি সঠিক ইউজারনেম লিখুন।")
        return
    coin_type = state.get("pending_username_coin_type")
    if not coin_type:
        bot.send_message(chat_id, "⚠️ কিছু ভুল হয়েছে। আবার শুরু করুন।")
        reset_state(chat_id)
        return
    db_execute("UPDATE coin_rates SET receiving_username = %s WHERE coin_type = %s",
               (username, coin_type))
    reset_state(chat_id)
    bot.send_message(
        chat_id,
        f"✅ {COIN_DISPLAY_NAMES.get(coin_type, coin_type)} এর ইউজারনেম "
        f"{username} সেট করা হয়েছে।",
        reply_markup=admin_menu_kb(),
    )


def _handle_support_input(chat_id, text):
    contact = text.strip()
    if not contact:
        bot.send_message(chat_id, "⚠️ সঠিক তথ্য লিখুন।")
        return
    existing = get_settings()
    if existing:
        db_execute("UPDATE bot_settings SET support_contact = %s WHERE id = %s",
                   (contact, existing["id"]))
    else:
        db_execute("INSERT INTO bot_settings (support_contact) VALUES (%s)", (contact,))
    reset_state(chat_id)
    bot.send_message(chat_id, "✅ সাপোর্ট কন্টাক্ট সেট করা হয়েছে।",
                     reply_markup=admin_menu_kb())


def _handle_add_admin(chat_id, text, from_id):
    try:
        new_id = int(text.strip())
        if new_id <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(chat_id, "⚠️ সঠিক টেলিগ্রাম ইউজার আইডি (শুধু সংখ্যা) লিখুন।")
        return
    add_admin(new_id, from_id)
    reset_state(chat_id)
    bot.send_message(chat_id,
                     f"✅ ইউজার আইডি {new_id} কে এডমিন হিসেবে যুক্ত করা হয়েছে।",
                     reply_markup=admin_menu_kb())
    try:
        bot.send_message(new_id,
                         "🎉 আপনাকে এই বটের এডমিন হিসেবে যুক্ত করা হয়েছে। "
                         "/admin লিখে দেখুন।")
    except Exception:
        pass


def _handle_remove_admin(chat_id, text):
    try:
        target_id = int(text.strip())
    except ValueError:
        bot.send_message(chat_id, "⚠️ সঠিক টেলিগ্রাম ইউজার আইডি লিখুন।")
        return
    if is_bootstrap_admin(target_id):
        bot.send_message(
            chat_id,
            "⚠️ এই আইডিটি সিস্টেমের মূল এডমিন (ENV এ সেট করা), এটি রিমুভ করা যাবে না।",
        )
        reset_state(chat_id)
        return
    remove_admin(target_id)
    reset_state(chat_id)
    bot.send_message(chat_id,
                     f"✅ ইউজার আইডি {target_id} কে এডমিন তালিকা থেকে রিমুভ করা হয়েছে।",
                     reply_markup=admin_menu_kb())

# ─────────────────────────────────────────────
# Callback query handler
# ─────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    chat_id = call.message.chat.id
    data    = call.data or ""
    user_id = call.from_user.id

    try:
        # ── Coin selection ───────────────────────────────────────
        if data.startswith("select_coin:"):
            coin_type = data.split(":", 1)[1]
            bot.answer_callback_query(call.id)
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except Exception:
                pass
            rate_row = get_rate_for_coin(coin_type)
            if not rate_row:
                bot.send_message(chat_id, "⚠️ এই কয়েনের রেট পাওয়া যায়নি।")
                return
            state = get_state(chat_id)
            state["step"]              = "await_quantity"
            state["coin_type"]         = coin_type
            state["rate_per_thousand"] = float(rate_row["rate_per_thousand"])
            bot.send_message(
                chat_id,
                f"🪙 {COIN_DISPLAY_NAMES.get(coin_type, coin_type)}\n"
                f"💵 রেট: {rate_row['rate_per_thousand']} টাকা প্রতি হাজার কয়েনে\n\n"
                f"👉 আপনি কত কয়েন সেল করতে চান? সংখ্যা লিখে পাঠান:",
            )
            return

        # ── Order approve / reject ───────────────────────────────
        if data.startswith("order_approve:") or data.startswith("order_reject:"):
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "অনুমতি নেই।")
                return
            action, order_id_str = data.split(":", 1)
            order_id = int(order_id_str)
            approve  = action == "order_approve"

            order = db_fetchone("SELECT * FROM orders WHERE id = %s", (order_id,))
            if not order:
                bot.answer_callback_query(call.id, "অর্ডার পাওয়া যায়নি।")
                return
            if order["status"] != "pending":
                bot.answer_callback_query(
                    call.id,
                    f"এই অর্ডার আগেই {status_label(order['status'])} করা হয়েছে।"
                )
                return

            new_status = "approved" if approve else "rejected"
            db_execute("UPDATE orders SET status = %s WHERE id = %s",
                       (new_status, order_id))
            bot.answer_callback_query(
                call.id,
                "✅ এপ্রুভ করা হয়েছে।" if approve else "❌ রিজেক্ট করা হয়েছে।",
            )
            status_text = (
                f"✅ অর্ডার #{order_id} এপ্রুভ করা হয়েছে।" if approve
                else f"❌ অর্ডার #{order_id} রিজেক্ট করা হয়েছে।"
            )
            try:
                if call.message.photo:
                    bot.edit_message_caption(
                        f"{call.message.caption or ''}\n\n{status_text}",
                        chat_id=chat_id, message_id=call.message.message_id,
                    )
                elif call.message.text:
                    bot.edit_message_text(
                        f"{call.message.text}\n\n{status_text}",
                        chat_id=chat_id, message_id=call.message.message_id,
                    )
            except Exception:
                pass
            try:
                msg_user = (
                    f"✅ আপনার অর্ডার #{order_id} এপ্রুভ হয়েছে! টাকা পাঠানো হয়েছে।"
                    if approve
                    else f"❌ আপনার অর্ডার #{order_id} বাতিল করা হয়েছে। সাপোর্টে যোগাযোগ করুন।"
                )
                bot.send_message(order["telegram_user_id"], msg_user)
            except Exception:
                pass
            return

        # ── Rate coin selection ──────────────────────────────────
        if data.startswith("set_rate:"):
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "অনুমতি নেই।")
                return
            coin_type = data.split(":", 1)[1]
            bot.answer_callback_query(call.id)
            state = get_state(chat_id)
            state["step"]                   = "admin_await_rate"
            state["pending_rate_coin_type"] = coin_type
            bot.send_message(
                chat_id,
                f"💱 {COIN_DISPLAY_NAMES.get(coin_type, coin_type)} এর নতুন রেট লিখুন "
                f"(প্রতি হাজার কয়েনে কত টাকা):",
            )
            return

        # ── Username coin selection ──────────────────────────────
        if data.startswith("set_username:"):
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "অনুমতি নেই।")
                return
            coin_type = data.split(":", 1)[1]
            bot.answer_callback_query(call.id)
            state = get_state(chat_id)
            state["step"]                       = "admin_await_coin_username"
            state["pending_username_coin_type"] = coin_type
            bot.send_message(
                chat_id,
                f"🧑‍🦲 {COIN_DISPLAY_NAMES.get(coin_type, coin_type)} এর নতুন ইউজারনেম লিখুন "
                f"(@ ছাড়া বা সহ):",
            )
            return

        bot.answer_callback_query(call.id)

    except Exception as e:
        logger.exception(f"Callback error: {e}")
        try:
            bot.answer_callback_query(call.id, "⚠️ কিছু সমস্যা হয়েছে।")
        except Exception:
            pass

# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    init_db()          # auto-create tables on first run
    logger.info("Bot starting (polling)…")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
