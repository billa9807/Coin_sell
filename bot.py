import json
import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import telebot
from telebot import types
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
MIN_COIN_QTY = 10_000

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
# DB init
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
    db_execute("""
        INSERT INTO coin_rates (coin_type, display_name, rate_per_thousand, receiving_username)
        VALUES
            ('NS',   'NS Coin',   800, ''),
            ('NIVA', 'Niva Coin', 750, ''),
            ('TOP',  'Top Coin',  900, '')
        ON CONFLICT DO NOTHING
    """)
    db_execute("""
        INSERT INTO bot_settings (support_contact)
        SELECT '' WHERE NOT EXISTS (SELECT 1 FROM bot_settings)
    """)
    logger.info("Database ready.")

# ─────────────────────────────────────────────
# Admin helpers
# ─────────────────────────────────────────────

BOOTSTRAP_ADMINS = [6664150885]

def get_admin_ids():
    rows = db_fetchall("SELECT telegram_user_id FROM admins")
    db_ids = [r["telegram_user_id"] for r in rows]
    return list(set(BOOTSTRAP_ADMINS + db_ids))

def is_admin(user_id) -> bool:
    if user_id is None:
        return False
    if user_id in BOOTSTRAP_ADMINS:
        return True
    row = db_fetchone("SELECT 1 FROM admins WHERE telegram_user_id = %s", (user_id,))
    return row is not None

def is_bootstrap_admin(user_id) -> bool:
    return user_id in BOOTSTRAP_ADMINS if user_id else False

def add_admin(telegram_user_id, added_by):
    db_execute(
        "INSERT INTO admins (telegram_user_id, added_by_telegram_user_id) "
        "VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (telegram_user_id, added_by),
    )

def remove_admin(telegram_user_id):
    db_execute("DELETE FROM admins WHERE telegram_user_id = %s", (telegram_user_id,))

# ─────────────────────────────────────────────
# Conversation state
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
# Menu constants
# ─────────────────────────────────────────────

MENU_SELL    = "💰 কয়েন সেল করুন"
MENU_RATE    = "📊 আজকের রেট"
MENU_ORDERS  = "📋 আমার অর্ডার"
MENU_SUPPORT = "☎️ সাপোর্ট"
MENU_ADMIN   = "🛠 এডমিন প্যানেল"
MENU_BACK    = "⬅️ মেইন মেনু"

ADMIN_PENDING  = "📥 পেন্ডিং অর্ডার"
ADMIN_RATES    = "💱 রেট পরিবর্তন"
ADMIN_USERNAME = "🧑 কয়েনের ইউজারনেম সেট করুন"
ADMIN_SUPPORT  = "☎️ সাপোর্ট কন্টাক্ট সেট করুন"
ADMIN_STATS    = "📈 স্ট্যাটাস"
ADMIN_ADD      = "➕ নতুন এডমিন যুক্ত করুন"
ADMIN_REMOVE   = "➖ এডমিন রিমুভ করুন"
ADMIN_LIST      = "📋 এডমিন লিস্ট"
ADMIN_BROADCAST = "📢 ব্রডকাস্ট"

# ─────────────────────────────────────────────
# Keyboard builders — raw JSON with style support
# ─────────────────────────────────────────────

def _kb(rows: list, resize: bool = True) -> str:
    return json.dumps({"keyboard": rows, "resize_keyboard": resize})

def _btn(text: str, style: str = None) -> dict:
    d = {"text": text}
    if style:
        d["style"] = style
    return d

def _inline_kb(rows: list) -> str:
    return json.dumps({"inline_keyboard": rows})

def _ibtn(text: str, style: str = None, **kwargs) -> dict:
    d = {"text": text, **kwargs}
    if style:
        d["style"] = style
    return d

# Coin styles
COIN_STYLES = {
    "NS":   "primary",   # blue
    "NIVA": "success",   # green
    "TOP":  "danger",    # red
}

def main_menu_kb(admin: bool) -> str:
    rows = [
        [_btn(MENU_SELL, "success"),   _btn(MENU_RATE, "primary")],
        [_btn(MENU_ORDERS, "primary"), _btn(MENU_SUPPORT, "primary")],
    ]
    if admin:
        rows.append([_btn(MENU_ADMIN, "danger")])
    return _kb(rows)

def admin_menu_kb() -> str:
    rows = [
        [_btn(ADMIN_PENDING, "danger"),   _btn(ADMIN_RATES, "primary")],
        [_btn(ADMIN_USERNAME, "primary"), _btn(ADMIN_SUPPORT, "primary")],
        [_btn(ADMIN_STATS, "success"),    _btn(ADMIN_BROADCAST, "success")],
        [_btn(ADMIN_ADD, "success"),      _btn(ADMIN_REMOVE, "danger")],
        [_btn(ADMIN_LIST, "primary")],
        [_btn(MENU_BACK, "primary")],
    ]
    return _kb(rows)

def coin_select_inline(rates: list) -> str:
    rows = []
    for r in rates:
        name  = COIN_DISPLAY_NAMES.get(r["coin_type"], r["coin_type"])
        style = COIN_STYLES.get(r["coin_type"], "primary")
        rows.append([_ibtn(
            f"{name} ({fmt_money(r['rate_per_thousand'])} টাকা/হাজার)",
            style=style,
            callback_data=f"sel_coin:{r['coin_type']}",
        )])
    return _inline_kb(rows)

def username_copy_inline(username: str) -> str:
    return _inline_kb([[
        _ibtn(username, style="success", copy_text={"text": username})
    ]])

def order_action_inline(order_id: int) -> str:
    return _inline_kb([[
        _ibtn("✅ Approve", style="success", callback_data=f"approve:{order_id}"),
        _ibtn("❌ Reject",  style="danger",  callback_data=f"reject:{order_id}"),
    ]])

def coin_rate_edit_inline(rates: list) -> str:
    rows = []
    for r in rates:
        name  = COIN_DISPLAY_NAMES.get(r["coin_type"], r["coin_type"])
        style = COIN_STYLES.get(r["coin_type"], "primary")
        rows.append([_ibtn(
            f"✏️ {name}  —  {fmt_money(r['rate_per_thousand'])} টাকা/হাজার",
            style=style,
            callback_data=f"edit_rate:{r['coin_type']}",
        )])
    return _inline_kb(rows)

def coin_username_edit_inline(rates: list) -> str:
    rows = []
    for r in rates:
        name  = COIN_DISPLAY_NAMES.get(r["coin_type"], r["coin_type"])
        style = COIN_STYLES.get(r["coin_type"], "primary")
        un    = r["receiving_username"] or "(সেট করা নেই)"
        rows.append([_ibtn(
            f"✏️ {name}  —  {un}",
            style=style,
            callback_data=f"edit_un:{r['coin_type']}",
        )])
    return _inline_kb(rows)

# ─────────────────────────────────────────────
# Notify admins
# ─────────────────────────────────────────────

def notify_admins(text: str, markup=None):
    for aid in get_admin_ids():
        try:
            bot.send_message(aid, text, reply_markup=markup)
        except Exception as e:
            logger.warning(f"Cannot notify admin {aid}: {e}")

def notify_admins_photo(file_id: str, caption: str, markup=None):
    for aid in get_admin_ids():
        try:
            bot.send_photo(aid, file_id, caption=caption, reply_markup=markup)
        except Exception as e:
            logger.warning(f"Cannot notify admin {aid}: {e}")

# ─────────────────────────────────────────────
# /start  &  /cancel
# ─────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(msg):
    reset_state(msg.chat.id)
    name  = msg.from_user.first_name or "বন্ধু"
    admin = is_admin(msg.from_user.id)
    bot.send_message(
        msg.chat.id,
        f"স্বাগতম {name}! 👋\n\nকোন কয়েন সেল করবেন?",
        reply_markup=main_menu_kb(admin),
    )

@bot.message_handler(commands=["cancel"])
def cmd_cancel(msg):
    state = get_state(msg.chat.id)
    if state.get("step") == "idle":
        bot.send_message(
            msg.chat.id,
            "কোনো চলমান প্রক্রিয়া নেই।",
            reply_markup=main_menu_kb(is_admin(msg.from_user.id)),
        )
        return
    reset_state(msg.chat.id)
    bot.send_message(
        msg.chat.id,
        "❌ বাতিল করা হয়েছে। মেইন মেনুতে ফিরে গেলেন।",
        reply_markup=main_menu_kb(is_admin(msg.from_user.id)),
    )

# ─────────────────────────────────────────────
# Main menu
# ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == MENU_BACK)
def menu_back(msg):
    reset_state(msg.chat.id)
    bot.send_message(
        msg.chat.id,
        "মেইন মেনুতে ফিরে গেলেন!",
        reply_markup=main_menu_kb(is_admin(msg.from_user.id)),
    )

@bot.message_handler(func=lambda m: m.text == MENU_RATE)
def menu_rate(msg):
    rates = db_fetchall("SELECT * FROM coin_rates ORDER BY coin_type")
    if not rates:
        bot.send_message(msg.chat.id, "কোনো রেট পাওয়া যায়নি।")
        return
    lines = ["📊 আজকের কয়েন রেট:\n"]
    for r in rates:
        name = COIN_DISPLAY_NAMES.get(r["coin_type"], r["coin_type"])
        lines.append(f"🪙 {name}: {fmt_money(r['rate_per_thousand'])} টাকা/হাজার")
    bot.send_message(msg.chat.id, "\n".join(lines))

@bot.message_handler(func=lambda m: m.text == MENU_SUPPORT)
def menu_support(msg):
    row = db_fetchone("SELECT support_contact FROM bot_settings LIMIT 1")
    contact = (row["support_contact"] if row and row["support_contact"] else "এখনো সেট করা হয়নি।")
    bot.send_message(msg.chat.id, f"☎️ সাপোর্ট যোগাযোগ:\n\n{contact}")

@bot.message_handler(func=lambda m: m.text == MENU_ORDERS)
def menu_orders(msg):
    orders = db_fetchall(
        "SELECT * FROM orders WHERE telegram_user_id=%s ORDER BY created_at DESC LIMIT 10",
        (msg.from_user.id,),
    )
    if not orders:
        bot.send_message(msg.chat.id, "আপনার কোনো অর্ডার নেই।")
        return
    lines = ["📋 আপনার শেষ ১০টি অর্ডার:\n"]
    for o in orders:
        name = COIN_DISPLAY_NAMES.get(o["coin_type"], o["coin_type"])
        date, time_ = fmt_dhaka(o["created_at"])
        lines.append(
            f"🔹 অর্ডার #{o['id']}\n"
            f"   🪙 {name} — {fmt_num(o['quantity'])} কয়েন\n"
            f"   💵 {fmt_money(o['amount_bdt'])} BDT\n"
            f"   📌 {status_label(o['status'])}\n"
            f"   🕐 {date} {time_}\n"
        )
    bot.send_message(msg.chat.id, "\n".join(lines))

# ─────────────────────────────────────────────
# Sell flow — Step 1: Show coin list
# ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == MENU_SELL)
def menu_sell(msg):
    reset_state(msg.chat.id)
    rates = db_fetchall("SELECT * FROM coin_rates ORDER BY coin_type")
    if not rates:
        bot.send_message(msg.chat.id, "এই মুহূর্তে কোনো কয়েন পাওয়া যাচ্ছে না।")
        return
    get_state(msg.chat.id)["step"] = "select_coin"
    bot.send_message(
        msg.chat.id,
        "কোন কয়েন সেল করতে চান?",
        reply_markup=coin_select_inline(rates),
    )

# ─────────────────────────────────────────────
# Sell flow — Step 2: Coin selected → ask quantity
# ─────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("sel_coin:"))
def cb_select_coin(call):
    coin_type = call.data.split(":", 1)[1]
    if coin_type not in COIN_TYPES:
        bot.answer_callback_query(call.id, "অবৈধ কয়েন।")
        return
    rate_row = db_fetchone("SELECT * FROM coin_rates WHERE coin_type=%s", (coin_type,))
    if not rate_row:
        bot.answer_callback_query(call.id, "রেট পাওয়া যায়নি।")
        return

    bot.answer_callback_query(call.id)

    # Delete the coin selection message
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    state = get_state(call.message.chat.id)
    state["step"]      = "enter_qty"
    state["coin_type"] = coin_type
    state["rate"]      = float(rate_row["rate_per_thousand"])
    state["un"]        = rate_row["receiving_username"] or ""

    name = COIN_DISPLAY_NAMES.get(coin_type, coin_type)
    bot.send_message(
        call.message.chat.id,
        f"🪙 {name}\n"
        f"📊 রেট: {fmt_money(rate_row['rate_per_thousand'])} টাকা প্রতি হাজার কয়েন\n\n"
        f"👉 আপনি কত কয়েন সেল করতে চান? সংখ্যা লিখে পাঠান:\n"
        f"(সর্বনিম্ন {fmt_num(MIN_COIN_QTY)} কয়েন)",
    )

# ─────────────────────────────────────────────
# Sell flow — Step 3: Quantity → show amount + ask screenshot
# ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: get_state(m.chat.id).get("step") == "enter_qty")
def step_qty(msg):
    text = msg.text.strip().replace(",", "").replace(" ", "")
    if not text.isdigit() or int(text) <= 0:
        bot.send_message(msg.chat.id, "সঠিক সংখ্যা লিখুন (যেমন: 10000):")
        return

    qty = int(text)

    if qty < MIN_COIN_QTY:
        bot.send_message(
            msg.chat.id,
            f"❌ সর্বনিম্ন {fmt_num(MIN_COIN_QTY)} কয়েন সেল করতে হবে।\n\n"
            f"আবার সংখ্যা লিখুন:",
        )
        return

    state  = get_state(msg.chat.id)
    rate   = state["rate"]
    amount = (qty / 1000) * rate
    state["qty"]    = qty
    state["amount"] = amount
    state["step"]   = "upload_ss"

    coin_type = state["coin_type"]
    name = COIN_DISPLAY_NAMES.get(coin_type, coin_type)
    un   = state["un"]

    body = (
        f"📦 পরিমাণ: {fmt_num(qty)} {name}\n"
        f"💵 পাবেন: {fmt_money(amount)} BDT\n"
    )

    if un:
        body += (
            f"\n👆 নিচের বাটনে ক্লিক করে ইউজারনেম কপি করুন, তারপর কয়েন সেন্ড করুন:\n"
            f"\n📸 কয়েন পাঠানোর পর স্ক্রিনশট আপলোড করুন:"
        )
        bot.send_message(msg.chat.id, body, reply_markup=username_copy_inline(un))
    else:
        body += "\n📸 কয়েন পাঠানোর পর স্ক্রিনশট আপলোড করুন:"
        bot.send_message(msg.chat.id, body)

# ─────────────────────────────────────────────
# Sell flow — Step 4: Screenshot → ask bkash number
# ─────────────────────────────────────────────

@bot.message_handler(
    content_types=["photo"],
    func=lambda m: get_state(m.chat.id).get("step") == "upload_ss",
)
def step_screenshot(msg):
    state = get_state(msg.chat.id)
    state["ss_file_id"] = msg.photo[-1].file_id
    state["step"]       = "enter_bkash"
    bot.send_message(
        msg.chat.id,
        "✅ স্ক্রিনশট পেয়েছি!\n\n"
        "📱 এখন আপনার bKash নম্বর লিখুন (যেখানে টাকা পাঠাবো):",
    )

# ─────────────────────────────────────────────
# Sell flow — Step 5: bkash → confirm order
# ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: get_state(m.chat.id).get("step") == "enter_bkash")
def step_bkash(msg):
    bkash = msg.text.strip()
    if not re.match(r"^01[3-9]\d{8}$", bkash):
        bot.send_message(
            msg.chat.id,
            "❌ সঠিক bKash নম্বর দিন।\nযেমন: 01XXXXXXXXX (১১ সংখ্যা):",
        )
        return

    state      = get_state(msg.chat.id)
    coin_type  = state["coin_type"]
    qty        = state["qty"]
    amount     = state["amount"]
    rate       = state["rate"]
    ss_file_id = state["ss_file_id"]

    row = db_execute_returning(
        """
        INSERT INTO orders
            (telegram_user_id, telegram_username, telegram_first_name,
             coin_type, quantity, rate_per_thousand, amount_bdt,
             bkash_number, screenshot_file_id, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
        RETURNING id
        """,
        (
            msg.from_user.id,
            msg.from_user.username,
            msg.from_user.first_name,
            coin_type, qty, rate, amount, bkash, ss_file_id,
        ),
    )
    order_id = row["id"]
    reset_state(msg.chat.id)

    name  = COIN_DISPLAY_NAMES.get(coin_type, coin_type)
    uname = f"@{msg.from_user.username}" if msg.from_user.username else str(msg.from_user.id)

    caption = (
        f"🆕 নতুন অর্ডার #{order_id}\n"
        f"👤 ইউজার: {msg.from_user.first_name} ({uname})\n"
        f"🪙 কয়েন: {name}\n"
        f"📦 পরিমাণ: {fmt_num(qty)}\n"
        f"💵 টাকা: {fmt_money(amount)} BDT\n"
        f"📱 bKash: {bkash}"
    )
    notify_admins_photo(ss_file_id, caption, markup=order_action_inline(order_id))

    bot.send_message(
        msg.chat.id,
        f"🎉 অর্ডার সফলভাবে জমা হয়েছে!\n\n"
        f"🔖 অর্ডার নম্বর: #{order_id}\n"
        f"🪙 কয়েন: {name}\n"
        f"📦 পরিমাণ: {fmt_num(qty)}\n"
        f"💵 পাবেন: {fmt_money(amount)} BDT\n"
        f"📱 bKash: {bkash}\n\n"
        f"শীঘ্রই প্রক্রিয়া করা হবে। ধন্যবাদ! 🙏",
        reply_markup=main_menu_kb(is_admin(msg.from_user.id)),
    )

# ─────────────────────────────────────────────
# Admin panel
# ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == MENU_ADMIN)
def menu_admin(msg):
    if not is_admin(msg.from_user.id):
        bot.send_message(msg.chat.id, "❌ আপনার অ্যাক্সেস নেই।")
        return
    reset_state(msg.chat.id)
    bot.send_message(msg.chat.id, "🛠 এডমিন প্যানেলে স্বাগতম!", reply_markup=admin_menu_kb())

# ── Pending orders ──────────────────────────

@bot.message_handler(func=lambda m: m.text == ADMIN_PENDING and is_admin(m.from_user.id))
def admin_pending(msg):
    orders = db_fetchall("SELECT * FROM orders WHERE status='pending' ORDER BY created_at ASC")
    if not orders:
        bot.send_message(msg.chat.id, "কোনো পেন্ডিং অর্ডার নেই। ✅")
        return
    bot.send_message(msg.chat.id, f"📥 মোট পেন্ডিং অর্ডার: {len(orders)}")
    for o in orders:
        name  = COIN_DISPLAY_NAMES.get(o["coin_type"], o["coin_type"])
        uname = f"@{o['telegram_username']}" if o["telegram_username"] else str(o["telegram_user_id"])
        date, time_ = fmt_dhaka(o["created_at"])
        caption = (
            f"📋 অর্ডার #{o['id']}\n"
            f"👤 ইউজার: {o['telegram_first_name']} ({uname})\n"
            f"🪙 কয়েন: {name}\n"
            f"📦 পরিমাণ: {fmt_num(o['quantity'])}\n"
            f"💵 টাকা: {fmt_money(o['amount_bdt'])} BDT\n"
            f"📱 bKash: {o['bkash_number']}\n"
            f"🕐 {date} {time_}"
        )
        if o["screenshot_file_id"]:
            bot.send_photo(msg.chat.id, o["screenshot_file_id"], caption=caption,
                           reply_markup=order_action_inline(o["id"]))
        else:
            bot.send_message(msg.chat.id, caption, reply_markup=order_action_inline(o["id"]))

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("approve:"))
def cb_approve(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "আপনার অ্যাক্সেস নেই।")
        return
    order_id = int(call.data.split(":")[1])
    order = db_fetchone("SELECT * FROM orders WHERE id=%s", (order_id,))
    if not order:
        bot.answer_callback_query(call.id, "অর্ডার পাওয়া যায়নি।")
        return
    if order["status"] != "pending":
        bot.answer_callback_query(call.id, f"অর্ডার ইতিমধ্যে {order['status']}.")
        return
    db_execute("UPDATE orders SET status='approved' WHERE id=%s", (order_id,))
    bot.answer_callback_query(call.id, f"✅ অর্ডার #{order_id} অনুমোদিত।")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    name = COIN_DISPLAY_NAMES.get(order["coin_type"], order["coin_type"])
    try:
        bot.send_message(
            order["telegram_user_id"],
            f"✅ আপনার অর্ডার অনুমোদিত হয়েছে!\n\n"
            f"🔖 অর্ডার: #{order_id}\n"
            f"🪙 {name} — {fmt_num(order['quantity'])} কয়েন\n"
            f"💵 {fmt_money(order['amount_bdt'])} BDT আপনার bKash-এ পাঠানো হয়েছে।\n\n"
            f"ধন্যবাদ! 🙏",
        )
    except Exception:
        pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("reject:"))
def cb_reject(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "আপনার অ্যাক্সেস নেই।")
        return
    order_id = int(call.data.split(":")[1])
    order = db_fetchone("SELECT * FROM orders WHERE id=%s", (order_id,))
    if not order:
        bot.answer_callback_query(call.id, "অর্ডার পাওয়া যায়নি।")
        return
    if order["status"] != "pending":
        bot.answer_callback_query(call.id, f"অর্ডার ইতিমধ্যে {order['status']}.")
        return
    db_execute("UPDATE orders SET status='rejected' WHERE id=%s", (order_id,))
    bot.answer_callback_query(call.id, f"❌ অর্ডার #{order_id} বাতিল।")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    name = COIN_DISPLAY_NAMES.get(order["coin_type"], order["coin_type"])
    try:
        bot.send_message(
            order["telegram_user_id"],
            f"❌ আপনার অর্ডার বাতিল হয়েছে।\n\n"
            f"🔖 অর্ডার: #{order_id}\n"
            f"🪙 {name} — {fmt_num(order['quantity'])} কয়েন\n\n"
            f"সমস্যা হলে সাপোর্টে যোগাযোগ করুন।",
        )
    except Exception:
        pass

# ── Rate edit ───────────────────────────────

@bot.message_handler(func=lambda m: m.text == ADMIN_RATES and is_admin(m.from_user.id))
def admin_rates(msg):
    rates = db_fetchall("SELECT * FROM coin_rates ORDER BY coin_type")
    bot.send_message(
        msg.chat.id,
        "কোন কয়েনের রেট পরিবর্তন করবেন?",
        reply_markup=coin_rate_edit_inline(rates),
    )

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("edit_rate:"))
def cb_edit_rate(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "আপনার অ্যাক্সেস নেই।")
        return
    coin_type = call.data.split(":")[1]
    state = get_state(call.message.chat.id)
    state["step"]      = "admin_rate"
    state["edit_coin"] = coin_type
    bot.answer_callback_query(call.id)
    name = COIN_DISPLAY_NAMES.get(coin_type, coin_type)
    bot.send_message(call.message.chat.id, f"✏️ {name} এর নতুন রেট লিখুন (টাকা/হাজার):")

@bot.message_handler(func=lambda m: get_state(m.chat.id).get("step") == "admin_rate")
def step_admin_rate(msg):
    if not is_admin(msg.from_user.id):
        return
    text = msg.text.strip().replace(",", "")
    try:
        new_rate = float(text)
        if new_rate <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(msg.chat.id, "সঠিক সংখ্যা লিখুন:")
        return
    state     = get_state(msg.chat.id)
    coin_type = state["edit_coin"]
    db_execute("UPDATE coin_rates SET rate_per_thousand=%s WHERE coin_type=%s", (new_rate, coin_type))
    reset_state(msg.chat.id)
    name = COIN_DISPLAY_NAMES.get(coin_type, coin_type)
    bot.send_message(
        msg.chat.id,
        f"✅ {name} এর রেট আপডেট: {fmt_money(new_rate)} টাকা/হাজার",
        reply_markup=admin_menu_kb(),
    )

# ── Username edit ────────────────────────────

@bot.message_handler(func=lambda m: m.text == ADMIN_USERNAME and is_admin(m.from_user.id))
def admin_username(msg):
    rates = db_fetchall("SELECT * FROM coin_rates ORDER BY coin_type")
    bot.send_message(
        msg.chat.id,
        "কোন কয়েনের receiving ইউজারনেম সেট করবেন?",
        reply_markup=coin_username_edit_inline(rates),
    )

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("edit_un:"))
def cb_edit_un(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "আপনার অ্যাক্সেস নেই।")
        return
    coin_type = call.data.split(":")[1]
    state = get_state(call.message.chat.id)
    state["step"]      = "admin_un"
    state["edit_coin"] = coin_type
    bot.answer_callback_query(call.id)
    name = COIN_DISPLAY_NAMES.get(coin_type, coin_type)
    bot.send_message(call.message.chat.id, f"✏️ {name} এর নতুন ইউজারনেম লিখুন:")

@bot.message_handler(func=lambda m: get_state(m.chat.id).get("step") == "admin_un")
def step_admin_un(msg):
    if not is_admin(msg.from_user.id):
        return
    username  = msg.text.strip()
    state     = get_state(msg.chat.id)
    coin_type = state["edit_coin"]
    db_execute("UPDATE coin_rates SET receiving_username=%s WHERE coin_type=%s", (username, coin_type))
    reset_state(msg.chat.id)
    name = COIN_DISPLAY_NAMES.get(coin_type, coin_type)
    bot.send_message(
        msg.chat.id,
        f"✅ {name} এর ইউজারনেম সেট: {username}",
        reply_markup=admin_menu_kb(),
    )

# ── Support contact ──────────────────────────

@bot.message_handler(func=lambda m: m.text == ADMIN_SUPPORT and is_admin(m.from_user.id))
def admin_support_set(msg):
    get_state(msg.chat.id)["step"] = "admin_support"
    bot.send_message(msg.chat.id, "☎️ নতুন সাপোর্ট কন্টাক্ট লিখুন:")

@bot.message_handler(func=lambda m: get_state(m.chat.id).get("step") == "admin_support")
def step_admin_support(msg):
    if not is_admin(msg.from_user.id):
        return
    contact  = msg.text.strip()
    existing = db_fetchone("SELECT id FROM bot_settings LIMIT 1")
    if existing:
        db_execute("UPDATE bot_settings SET support_contact=%s WHERE id=%s", (contact, existing["id"]))
    else:
        db_execute("INSERT INTO bot_settings (support_contact) VALUES (%s)", (contact,))
    reset_state(msg.chat.id)
    bot.send_message(
        msg.chat.id,
        f"✅ সাপোর্ট কন্টাক্ট আপডেট:\n{contact}",
        reply_markup=admin_menu_kb(),
    )

# ── Stats ────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == ADMIN_STATS and is_admin(m.from_user.id))
def admin_stats(msg):
    total     = db_fetchone("SELECT COUNT(*) AS c FROM orders")["c"]
    pending   = db_fetchone("SELECT COUNT(*) AS c FROM orders WHERE status='pending'")["c"]
    approved  = db_fetchone("SELECT COUNT(*) AS c FROM orders WHERE status='approved'")["c"]
    rejected  = db_fetchone("SELECT COUNT(*) AS c FROM orders WHERE status='rejected'")["c"]
    total_bdt = db_fetchone(
        "SELECT COALESCE(SUM(amount_bdt),0) AS s FROM orders WHERE status='approved'"
    )["s"]
    users = db_fetchone("SELECT COUNT(DISTINCT telegram_user_id) AS c FROM orders")["c"]
    bot.send_message(
        msg.chat.id,
        f"📈 স্ট্যাটিস্টিক্স:\n\n"
        f"📦 মোট অর্ডার: {total}\n"
        f"⏳ পেন্ডিং: {pending}\n"
        f"✅ অনুমোদিত: {approved}\n"
        f"❌ বাতিল: {rejected}\n"
        f"💵 মোট পরিশোধ: {fmt_money(total_bdt)} BDT\n"
        f"👥 মোট ইউজার: {users}",
    )

# ── Admin management ─────────────────────────

@bot.message_handler(func=lambda m: m.text == ADMIN_ADD and is_admin(m.from_user.id))
def admin_add(msg):
    if not is_bootstrap_admin(msg.from_user.id):
        bot.send_message(msg.chat.id, "শুধুমাত্র সুপার এডমিন এটি করতে পারবে।")
        return
    get_state(msg.chat.id)["step"] = "admin_add_id"
    bot.send_message(msg.chat.id, "নতুন এডমিনের Telegram User ID লিখুন:")

@bot.message_handler(func=lambda m: get_state(m.chat.id).get("step") == "admin_add_id")
def step_admin_add(msg):
    if not is_bootstrap_admin(msg.from_user.id):
        return
    if not msg.text.strip().isdigit():
        bot.send_message(msg.chat.id, "সঠিক User ID দিন (শুধু সংখ্যা):")
        return
    new_id = int(msg.text.strip())
    add_admin(new_id, msg.from_user.id)
    reset_state(msg.chat.id)
    bot.send_message(
        msg.chat.id,
        f"✅ {new_id} কে এডমিন হিসেবে যুক্ত করা হয়েছে।",
        reply_markup=admin_menu_kb(),
    )

@bot.message_handler(func=lambda m: m.text == ADMIN_REMOVE and is_admin(m.from_user.id))
def admin_remove(msg):
    if not is_bootstrap_admin(msg.from_user.id):
        bot.send_message(msg.chat.id, "শুধুমাত্র সুপার এডমিন এটি করতে পারবে।")
        return
    get_state(msg.chat.id)["step"] = "admin_rm_id"
    bot.send_message(msg.chat.id, "রিমুভ করতে চান কোন এডমিনের User ID লিখুন:")

@bot.message_handler(func=lambda m: get_state(m.chat.id).get("step") == "admin_rm_id")
def step_admin_rm(msg):
    if not is_bootstrap_admin(msg.from_user.id):
        return
    if not msg.text.strip().isdigit():
        bot.send_message(msg.chat.id, "সঠিক User ID দিন:")
        return
    rm_id = int(msg.text.strip())
    if rm_id in BOOTSTRAP_ADMINS:
        bot.send_message(msg.chat.id, "সুপার এডমিনকে রিমুভ করা যাবে না।")
        return
    remove_admin(rm_id)
    reset_state(msg.chat.id)
    bot.send_message(
        msg.chat.id,
        f"✅ {rm_id} কে এডমিন লিস্ট থেকে সরানো হয়েছে।",
        reply_markup=admin_menu_kb(),
    )

@bot.message_handler(func=lambda m: m.text == ADMIN_LIST and is_admin(m.from_user.id))
def admin_list(msg):
    admins = db_fetchall("SELECT telegram_user_id, created_at FROM admins ORDER BY created_at")
    lines  = ["📋 এডমিন লিস্ট:\n"]
    for aid in BOOTSTRAP_ADMINS:
        lines.append(f"⭐ {aid} (সুপার এডমিন)")
    for a in admins:
        if a["telegram_user_id"] not in BOOTSTRAP_ADMINS:
            date, _ = fmt_dhaka(a["created_at"])
            lines.append(f"• {a['telegram_user_id']} (যুক্ত: {date})")
    bot.send_message(msg.chat.id, "\n".join(lines))

# ── Broadcast ────────────────────────────────

@bot.message_handler(func=lambda m: m.text == ADMIN_BROADCAST and is_admin(m.from_user.id))
def admin_broadcast(msg):
    get_state(msg.chat.id)["step"] = "admin_broadcast"
    users = db_fetchall("SELECT COUNT(DISTINCT telegram_user_id) AS c FROM orders")
    count = users[0]["c"] if users else 0
    bot.send_message(
        msg.chat.id,
        f"📢 ব্রডকাস্ট মেসেজ লিখুন:\n\n"
        f"(মোট {count} জন ইউজার পাবে)\n\n"
        f"বাতিল করতে /cancel লিখুন।",
    )

@bot.message_handler(func=lambda m: get_state(m.chat.id).get("step") == "admin_broadcast")
def step_broadcast(msg):
    if not is_admin(msg.from_user.id):
        return
    text = msg.text.strip()
    reset_state(msg.chat.id)

    user_ids = db_fetchall("SELECT DISTINCT telegram_user_id FROM orders")
    total    = len(user_ids)
    success  = 0
    failed   = 0

    status_msg = bot.send_message(msg.chat.id, f"📤 পাঠানো হচ্ছে... (0/{total})")

    for i, row in enumerate(user_ids, 1):
        try:
            bot.send_message(row["telegram_user_id"], f"📢 বিজ্ঞপ্তি:\n\n{text}")
            success += 1
        except Exception:
            failed += 1
        # Update progress every 5 users
        if i % 5 == 0 or i == total:
            try:
                bot.edit_message_text(
                    f"📤 পাঠানো হচ্ছে... ({i}/{total})",
                    msg.chat.id,
                    status_msg.message_id,
                )
            except Exception:
                pass

    bot.edit_message_text(
        f"✅ ব্রডকাস্ট সম্পন্ন!\n\n"
        f"📨 সফল: {success}\n"
        f"❌ ব্যর্থ: {failed}\n"
        f"📊 মোট: {total}",
        msg.chat.id,
        status_msg.message_id,
    )
    bot.send_message(msg.chat.id, "এডমিন মেনু:", reply_markup=admin_menu_kb())

# ─────────────────────────────────────────────
# Fallback
# ─────────────────────────────────────────────

@bot.message_handler(content_types=["photo"],
                     func=lambda m: get_state(m.chat.id).get("step") != "upload_ss")
def unexpected_photo(msg):
    bot.send_message(
        msg.chat.id,
        "মেনু থেকে বেছে নিন:",
        reply_markup=main_menu_kb(is_admin(msg.from_user.id)),
    )

@bot.message_handler(func=lambda m: True)
def fallback(msg):
    if get_state(msg.chat.id).get("step") == "idle":
        bot.send_message(
            msg.chat.id,
            "মেনু থেকে বেছে নিন:",
            reply_markup=main_menu_kb(is_admin(msg.from_user.id)),
        )

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    logger.info("Bot polling started...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
