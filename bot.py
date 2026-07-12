
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
    return [6664150885]

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
        _ibtn("❌ Reject",  style="danger",  callback_data=f