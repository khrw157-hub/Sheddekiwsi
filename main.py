# -*- coding: utf-8 -*-
"""Team SHD Bot - Railway build.
Single-file Telegram bot with platform groups and a common/public group.
All user-facing text is UTF-8 Arabic. No emoji are used.
"""

import os
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger(__name__)

# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

try:
    FOUNDER_ID = int(os.getenv("FOUNDER_ID", "0") or 0)
except ValueError:
    FOUNDER_ID = 0

DB_PATH = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment variables.")

if not FOUNDER_ID:
    raise RuntimeError("FOUNDER_ID is not set in environment variables.")

PLATFORMS = ("telegram", "instagram", "tiktok")
COMMON_PLATFORM = "common"

# ============================================================
# Database
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path):
        self.path = path
        self.init()

    @contextmanager
    def conn(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def init(self):
        with self.conn() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS platforms (
                name TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS groups_config (
                platform TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                previous_permissions TEXT
            );

            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                data_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                approved_by INTEGER,
                approved_at TEXT,
                finished_by INTEGER,
                finished_at TEXT,
                group_chat_id INTEGER,
                group_message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                data_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                event TEXT NOT NULL,
                user_id INTEGER,
                request_id INTEGER,
                details TEXT,
                created_at TEXT NOT NULL
            );
            """)

            for platform in PLATFORMS:
                con.execute(
                    "INSERT OR IGNORE INTO platforms(name, enabled) VALUES(?, 1)",
                    (platform,),
                )

    def log_event(self, level, event, user_id=None, request_id=None, details=None):
        try:
            with self.conn() as con:
                con.execute(
                    """INSERT INTO logs(
                        level,event,user_id,request_id,details,created_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (
                        level,
                        event,
                        user_id,
                        request_id,
                        details,
                        now_iso(),
                    ),
                )
        except Exception:
            log.exception("Could not write application log")

    def get_user(self, user_id):
        with self.conn() as con:
            return con.execute(
                "SELECT * FROM users WHERE user_id=? AND active=1",
                (user_id,),
            ).fetchone()

    def add_user(self, user_id, username, full_name, role="member"):
        with self.conn() as con:
            con.execute(
                """INSERT INTO users(
                    user_id,username,full_name,role,active,created_at
                )
                VALUES(?,?,?,?,1,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    full_name=excluded.full_name,
                    role=excluded.role,
                    active=1""",
                (
                    user_id,
                    username,
                    full_name,
                    role,
                    now_iso(),
                ),
            )

    def remove_user(self, user_id):
        with self.conn() as con:
            con.execute(
                "UPDATE users SET active=0 WHERE user_id=?",
                (user_id,),
            )
            con.execute(
                "UPDATE admins SET active=0 WHERE user_id=?",
                (user_id,),
            )

    def list_users(self):
        with self.conn() as con:
            return con.execute(
                """SELECT * FROM users
                   WHERE active=1
                   ORDER BY created_at DESC"""
            ).fetchall()

    def add_admin(self, user_id, platform):
        with self.conn() as con:
            con.execute(
                """INSERT INTO admins(
                    user_id,platform,active,created_at
                )
                VALUES(?,?,1,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    platform=excluded.platform,
                    active=1""",
                (
                    user_id,
                    platform,
                    now_iso(),
                ),
            )
            con.execute(
                "UPDATE users SET role='admin', active=1 WHERE user_id=?",
                (user_id,),
            )

    def remove_admin(self, user_id):
        with self.conn() as con:
            con.execute(
                "UPDATE admins SET active=0 WHERE user_id=?",
                (user_id,),
            )
            con.execute(
                "UPDATE users SET role='member' WHERE user_id=? AND active=1",
                (user_id,),
            )

    def get_admin(self, user_id, platform=None):
        with self.conn() as con:
            if platform:
                return con.execute(
                    """SELECT * FROM admins
                       WHERE user_id=? AND platform=? AND active=1""",
                    (
                        user_id,
                        platform,
                    ),
                ).fetchone()

            return con.execute(
                """SELECT * FROM admins
                   WHERE user_id=? AND active=1""",
                (user_id,),
            ).fetchone()

    def list_admins(self):
        with self.conn() as con:
            return con.execute(
                """SELECT a.*, u.username, u.full_name
                   FROM admins a
                   JOIN users u ON u.user_id=a.user_id
                   WHERE a.active=1
                   ORDER BY a.platform, a.created_at DESC"""
            ).fetchall()

    def set_group(self, platform, chat_id):
        with self.conn() as con:
            con.execute(
                """INSERT INTO groups_config(
                    platform,chat_id,previous_permissions
                )
                VALUES(?,?,NULL)
                ON CONFLICT(platform) DO UPDATE SET
                    chat_id=excluded.chat_id""",
                (
                    platform,
                    chat_id,
                ),
            )

    def get_group(self, platform):
        with self.conn() as con:
            return con.execute(
                "SELECT * FROM groups_config WHERE platform=?",
                (platform,),
            ).fetchone()

    def set_previous_permissions(self, platform, permissions_json):
        with self.conn() as con:
            con.execute(
                """UPDATE groups_config
                   SET previous_permissions=?
                   WHERE platform=?""",
                (
                    permissions_json,
                    platform,
                ),
            )

    def get_common_lock(self):
        with self.conn() as con:
            row = con.execute(
                "SELECT previous_permissions FROM groups_config WHERE platform='common'"
            ).fetchone()
            return row["previous_permissions"] if row else None

    def set_common_lock(self, permissions_json):
        self.set_previous_permissions("common", permissions_json)

    def clear_common_lock(self):
        self.clear_previous_permissions("common")

    def clear_previous_permissions(self, platform):
        with self.conn() as con:
            con.execute(
                """UPDATE groups_config
                   SET previous_permissions=NULL
                   WHERE platform=?""",
                (platform,),
            )

    def create_request(self, platform, user_id, username, data):
        created = now_iso()

        with self.conn() as con:
            cur = con.execute(
                """INSERT INTO requests(
                    platform,user_id,username,data_json,status,
                    created_at,updated_at
                )
                VALUES(?,?,?,?,?,?,?)""",
                (
                    platform,
                    user_id,
                    username,
                    json.dumps(data, ensure_ascii=False),
                    "PENDING",
                    created,
                    created,
                ),
            )
            return cur.lastrowid

    def get_request(self, request_id):
        with self.conn() as con:
            return con.execute(
                "SELECT * FROM requests WHERE id=?",
                (request_id,),
            ).fetchone()

    def transition_request(
        self,
        request_id,
        from_status,
        to_status,
        user_id,
        action,
    ):
        with self.conn() as con:
            con.execute("BEGIN IMMEDIATE")

            cur = con.execute(
                f"""UPDATE requests
                    SET status=?,
                        {action}_by=?,
                        {action}_at=?,
                        updated_at=?
                    WHERE id=? AND status=?""",
                (
                    to_status,
                    user_id,
                    now_iso(),
                    now_iso(),
                    request_id,
                    from_status,
                ),
            )

            return cur.rowcount == 1

    def reject_request(self, request_id, user_id):
        with self.conn() as con:
            con.execute("BEGIN IMMEDIATE")

            cur = con.execute(
                """UPDATE requests
                   SET status='REJECTED',
                       approved_by=?,
                       approved_at=?,
                       updated_at=?
                   WHERE id=? AND status='PENDING'""",
                (
                    user_id,
                    now_iso(),
                    now_iso(),
                    request_id,
                ),
            )

            if cur.rowcount == 1:
                con.execute(
                    """INSERT INTO logs(
                        level,event,user_id,request_id,details,created_at
                    )
                    VALUES(?,?,?,?,?,?)""",
                    (
                        "INFO",
                        "request_rejected",
                        user_id,
                        request_id,
                        None,
                        now_iso(),
                    ),
                )

            return cur.rowcount == 1

    def set_request_status(self, request_id, status):
        with self.conn() as con:
            con.execute(
                """UPDATE requests
                   SET status=?, updated_at=?
                   WHERE id=?""",
                (
                    status,
                    now_iso(),
                    request_id,
                ),
            )

    def set_group_message(self, request_id, chat_id, message_id):
        with self.conn() as con:
            con.execute(
                """UPDATE requests
                   SET group_chat_id=?,
                       group_message_id=?,
                       updated_at=?
                   WHERE id=?""",
                (
                    chat_id,
                    message_id,
                    now_iso(),
                    request_id,
                ),
            )

    def list_requests(self, limit=50):
        with self.conn() as con:
            return con.execute(
                """SELECT * FROM requests
                   ORDER BY id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()

    def stats(self):
        with self.conn() as con:
            return con.execute(
                """SELECT platform,status,COUNT(*) AS n
                   FROM requests
                   GROUP BY platform,status"""
            ).fetchall()

    def get_session(self, user_id):
        with self.conn() as con:
            row = con.execute(
                "SELECT * FROM sessions WHERE user_id=?",
                (user_id,),
            ).fetchone()

            if not row:
                return None

            return row["state"], json.loads(row["data_json"])

    def set_session(self, user_id, state, data=None):
        with self.conn() as con:
            con.execute(
                """INSERT INTO sessions(
                    user_id,state,data_json,updated_at
                )
                VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    state=excluded.state,
                    data_json=excluded.data_json,
                    updated_at=excluded.updated_at""",
                (
                    user_id,
                    state,
                    json.dumps(data or {}, ensure_ascii=False),
                    now_iso(),
                ),
            )

    def clear_session(self, user_id):
        with self.conn() as con:
            con.execute(
                "DELETE FROM sessions WHERE user_id=?",
                (user_id,),
            )


db = Database(DB_PATH)

# ============================================================
# Keyboards
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "Telegram",
            callback_data="platform:telegram",
        )],
        [InlineKeyboardButton(
            "Instagram",
            callback_data="platform:instagram",
        )],
        [InlineKeyboardButton(
            "TikTok",
            callback_data="platform:tiktok",
        )],
    ])


def cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "إلغاء",
            callback_data="cancel",
        )]
    ])


def admin_request_keyboard(request_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "قبول الشد",
            callback_data=f"approve:{request_id}",
        ),
        InlineKeyboardButton(
            "رفض الشد",
            callback_data=f"reject:{request_id}",
        ),
    ]])


def finish_keyboard(request_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "انتهاء الشد",
            callback_data=f"finish:{request_id}",
        )
    ]])


def founder_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "إدارة الأعضاء",
            callback_data="founder:users",
        )],
        [InlineKeyboardButton(
            "إدارة المشرفين",
            callback_data="founder:admins",
        )],
        [InlineKeyboardButton(
            "إدارة المنصات",
            callback_data="founder:platforms",
        )],
        [InlineKeyboardButton(
            "إدارة الكروبات",
            callback_data="founder:groups",
        )],
        [InlineKeyboardButton(
            "الطلبات",
            callback_data="founder:requests",
        )],
        [InlineKeyboardButton(
            "الإحصائيات",
            callback_data="founder:stats",
        )],
        [InlineKeyboardButton(
            "الإعدادات",
            callback_data="founder:settings",
        )],
    ])


def founder_users_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "إضافة عضو",
            callback_data="f:add_user",
        )],
        [InlineKeyboardButton(
            "حذف عضو",
            callback_data="f:remove_user",
        )],
        [InlineKeyboardButton(
            "قائمة الأعضاء",
            callback_data="f:list_users",
        )],
        [InlineKeyboardButton(
            "رجوع",
            callback_data="founder:home",
        )],
    ])


def founder_admins_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "إضافة مشرف",
            callback_data="f:add_admin",
        )],
        [InlineKeyboardButton(
            "حذف مشرف",
            callback_data="f:remove_admin",
        )],
        [InlineKeyboardButton(
            "قائمة المشرفين",
            callback_data="f:list_admins",
        )],
        [InlineKeyboardButton(
            "رجوع",
            callback_data="founder:home",
        )],
    ])


def founder_groups_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "تعيين كروب Telegram",
            callback_data="f:set_group:telegram",
        )],
        [InlineKeyboardButton(
            "تعيين كروب Instagram",
            callback_data="f:set_group:instagram",
        )],
        [InlineKeyboardButton(
            "تعيين كروب TikTok",
            callback_data="f:set_group:tiktok",
        )],
        [InlineKeyboardButton(
            "تعيين الكروب العام",
            callback_data="f:set_group:common",
        )],
        [InlineKeyboardButton(
            "عرض الكروبات",
            callback_data="f:list_groups",
        )],
        [InlineKeyboardButton(
            "رجوع",
            callback_data="founder:home",
        )],
    ])


def founder_platforms_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "Telegram",
            callback_data="f:platform:telegram",
        )],
        [InlineKeyboardButton(
            "Instagram",
            callback_data="f:platform:instagram",
        )],
        [InlineKeyboardButton(
            "TikTok",
            callback_data="f:platform:tiktok",
        )],
        [InlineKeyboardButton(
            "رجوع",
            callback_data="founder:home",
        )],
    ])

# ============================================================
# Utilities
# ============================================================

def row_data(row):
    return json.loads(row["data_json"])


def format_request(request):
    data = row_data(request)
    lines = [f"{request['platform'].upper()} SHD", ""]
    for key, value in data.items():
        lines.append(f"{key}:")
        lines.append(str(value))
        lines.append("")
    lines.append(f"رقم الطلب: {request['id']}")
    return "\n".join(lines).strip()

# ============================================================
# Common group permissions
# ============================================================

def common_permissions_to_json(permissions):
    values = {
        "can_send_messages": permissions.can_send_messages,
        "can_send_audios": permissions.can_send_audios,
        "can_send_documents": permissions.can_send_documents,
        "can_send_photos": permissions.can_send_photos,
        "can_send_videos": permissions.can_send_videos,
        "can_send_video_notes": permissions.can_send_video_notes,
        "can_send_voice_notes": permissions.can_send_voice_notes,
        "can_send_polls": permissions.can_send_polls,
        "can_send_other_messages": permissions.can_send_other_messages,
        "can_add_web_page_previews": permissions.can_add_web_page_previews,
    }
    return json.dumps(values, ensure_ascii=False)


def common_permissions_from_json(raw):
    return ChatPermissions(**json.loads(raw))


async def lock_common_group(bot, chat_id):
    chat = await bot.get_chat(chat_id)
    if not chat.permissions:
        raise RuntimeError("تعذر قراءة صلاحيات الكروب العام.")
    db.set_common_lock(common_permissions_to_json(chat.permissions))
    locked = ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )
    await bot.set_chat_permissions(
        chat_id,
        locked,
        use_independent_chat_permissions=True,
    )


async def unlock_common_group(bot, chat_id):
    raw = db.get_common_lock()
    permissions = (
        common_permissions_from_json(raw)
        if raw
        else ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        )
    )
    await bot.set_chat_permissions(
        chat_id,
        permissions,
        use_independent_chat_permissions=True,
    )
    db.clear_common_lock()


# ============================================================
# Platform workflows
# ============================================================

TELEGRAM_COPY = "WAITING_TELEGRAM_COPY"
TELEGRAM_TITLE = "WAITING_TELEGRAM_TITLE"
TELEGRAM_EMAIL = "WAITING_TELEGRAM_EMAIL"

INSTAGRAM_USERNAME = "WAITING_INSTAGRAM_USERNAME"
INSTAGRAM_SHD = "WAITING_INSTAGRAM_SHD"

TIKTOK_DATA = "WAITING_TIKTOK_DATA"


async def start_telegram(user_id):
    db.set_session(
        user_id,
        TELEGRAM_COPY,
        {},
    )
    return "أرسل الكليشة.", cancel_keyboard()


async def telegram_text(user_id, state, text):
    session = db.get_session(user_id)
    data = session[1] if session else {}

    if state == TELEGRAM_COPY:
        data["الكليشة"] = text
        db.set_session(
            user_id,
            TELEGRAM_TITLE,
            data,
        )
        return "أرسل العنوان.", cancel_keyboard()

    if state == TELEGRAM_TITLE:
        data["العنوان"] = text
        db.set_session(
            user_id,
            TELEGRAM_EMAIL,
            data,
        )
        return "أرسل البريد الإلكتروني.", cancel_keyboard()

    if state == TELEGRAM_EMAIL:
        data["البريد الإلكتروني"] = text
        db.clear_session(user_id)
        return data, None

    return None, None


async def start_instagram(user_id):
    db.set_session(
        user_id,
        INSTAGRAM_USERNAME,
        {},
    )
    return "أرسل يوزر الحساب.", cancel_keyboard()


async def instagram_text(user_id, state, text):
    session = db.get_session(user_id)
    data = session[1] if session else {}

    if state == INSTAGRAM_USERNAME:
        username = text.strip().lstrip("@").strip()

        if (
            not username
            or " " in username
            or "/" in username
        ):
            return "أرسل اليوزر بشكل صحيح.", cancel_keyboard()

        data["Username"] = f"@{username}"
        data["Instagram"] = f"https://instagram.com/{username}"

        db.set_session(
            user_id,
            INSTAGRAM_SHD,
            data,
        )

        return "أرسل شدتك.", cancel_keyboard()

    if state == INSTAGRAM_SHD:
        data["الشدة"] = text
        db.clear_session(user_id)
        return data, None

    return None, None


async def start_tiktok(user_id):
    db.set_session(
        user_id,
        TIKTOK_DATA,
        {},
    )
    return "أرسل بيانات شد TikTok.", cancel_keyboard()


async def tiktok_text(user_id, state, text):
    if state != TIKTOK_DATA:
        return None, None

    if not text.strip():
        return "أرسل بيانات الشد.", cancel_keyboard()

    db.clear_session(user_id)

    return {
        "بيانات الشد": text.strip()
    }, None

# ============================================================
# Common request flow
# ============================================================

async def send_request_to_admins(bot, request_id, platform):
    request = db.get_request(request_id)

    if not request:
        return 0

    text = format_request(request)
    sent = 0

    for admin in db.list_admins():
        if admin["platform"] != platform:
            continue

        try:
            await bot.send_message(
                admin["user_id"],
                text,
                reply_markup=admin_request_keyboard(request_id),
            )
            sent += 1

        except TelegramError as exc:
            db.log_event(
                "ERROR",
                "notify_admin_failed",
                admin["user_id"],
                request_id,
                str(exc),
            )

    return sent


async def platform_callback(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if not db.get_user(user_id):
        await query.edit_message_text(
            "لا تملك صلاحية استخدام البوت."
        )
        return

    platform = query.data.split(":", 1)[1]

    db.clear_session(user_id)

    if platform == "telegram":
        message, keyboard = await start_telegram(user_id)
    elif platform == "instagram":
        message, keyboard = await start_instagram(user_id)
    else:
        message, keyboard = await start_tiktok(user_id)

    await query.edit_message_text(
        message,
        reply_markup=keyboard,
    )


async def cancel_callback(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if not db.get_user(user_id):
        await query.edit_message_text(
            "لا تملك صلاحية استخدام البوت."
        )
        return

    db.clear_session(user_id)

    await query.edit_message_text(
        "تم إلغاء العملية.",
        reply_markup=main_keyboard(),
    )


async def text_router(update, context):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id

    if not db.get_user(user_id):
        await update.message.reply_text(
            "لا تملك صلاحية استخدام البوت."
        )
        return

    session = db.get_session(user_id)

    if not session:
        await update.message.reply_text(
            "اختر المنصة.",
            reply_markup=main_keyboard(),
        )
        return

    state, _ = session
    text = update.message.text

    if state in (
        TELEGRAM_COPY,
        TELEGRAM_TITLE,
        TELEGRAM_EMAIL,
    ):
        message, keyboard = await telegram_text(
            user_id,
            state,
            text,
        )
        platform = "telegram"

    elif state in (
        INSTAGRAM_USERNAME,
        INSTAGRAM_SHD,
    ):
        message, keyboard = await instagram_text(
            user_id,
            state,
            text,
        )
        platform = "instagram"

    elif state == TIKTOK_DATA:
        message, keyboard = await tiktok_text(
            user_id,
            state,
            text,
        )
        platform = "tiktok"

    else:
        db.clear_session(user_id)

        await update.message.reply_text(
            "انتهت العملية. اختر المنصة.",
            reply_markup=main_keyboard(),
        )
        return

    if isinstance(message, dict):
        request_id = db.create_request(
            platform,
            user_id,
            update.effective_user.username,
            message,
        )

        sent = await send_request_to_admins(
            context.bot,
            request_id,
            platform,
        )

        if sent == 0:
            db.set_request_status(
                request_id,
                "REJECTED",
            )

            db.log_event(
                "ERROR",
                "no_platform_admin",
                user_id,
                request_id,
                platform,
            )

            await update.message.reply_text(
                "تعذر إرسال الشدة للمشرفين حاليًا. حاول لاحقًا.",
                reply_markup=main_keyboard(),
            )
            return

        await update.message.reply_text(
            "تم إرسال شدتك إلى المشرف، انتظر المراجعة.",
            reply_markup=main_keyboard(),
        )
        return

    await update.message.reply_text(
        message,
        reply_markup=keyboard,
    )

# ============================================================
# Admin request actions
# ============================================================

async def approve_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    try:
        request_id = int(
            query.data.split(":", 1)[1]
        )
    except (ValueError, IndexError):
        await query.answer(
            "رقم الطلب غير صحيح.",
            show_alert=True,
        )
        return

    request = db.get_request(request_id)

    if not request:
        await query.answer(
            "الطلب غير موجود.",
            show_alert=True,
        )
        return

    if not db.get_admin(
        user_id,
        request["platform"],
    ):
        await query.answer(
            "لا تملك صلاحية هذه المنصة.",
            show_alert=True,
        )
        return

    if request["status"] != "PENDING":
        await query.answer(
            "تمت معالجة الطلب بالفعل.",
            show_alert=True,
        )
        return

    if not db.transition_request(
        request_id,
        "PENDING",
        "APPROVED",
        user_id,
        "approved",
    ):
        await query.answer(
            "تمت معالجة الطلب بالفعل.",
            show_alert=True,
        )
        return

    group = db.get_group(
        request["platform"]
    )

    if not group:
        db.set_request_status(
            request_id,
            "PENDING",
        )

        db.log_event(
            "ERROR",
            "group_not_configured",
            user_id,
            request_id,
            request["platform"],
        )

        await query.answer(
            "لم يتم تعيين كروب لهذه المنصة.",
            show_alert=True,
        )
        return

    try:
        message = await context.bot.send_message(
            group["chat_id"],
            format_request(request),
        )

        db.set_group_message(
            request_id,
            group["chat_id"],
            message.message_id,
        )

        # أرسل نسخة إلى الكروب العام، ثم اقفل الكروب العام فقط.
        common = db.get_group("common")
        if common:
            try:
                await context.bot.send_message(
                    common["chat_id"],
                    format_request(request),
                )
                await lock_common_group(context.bot, common["chat_id"])
            except (TelegramError, RuntimeError) as exc:
                db.log_event(
                    "ERROR",
                    "common_group_failed",
                    user_id,
                    request_id,
                    str(exc),
                )
                # فشل الكروب العام لا يلغي نزول الشدة في كروب المنصة.

        try:
            await context.bot.pin_chat_message(
                group["chat_id"],
                message.message_id,
                disable_notification=True,
            )

        except TelegramError as exc:
            db.log_event(
                "ERROR",
                "pin_failed",
                user_id,
                request_id,
                str(exc),
            )

            await query.edit_message_reply_markup(
                reply_markup=None
            )

            await query.message.reply_text(
                "تم إرسال الشدة، لكن تعذر تثبيتها. راجع صلاحيات البوت في الكروب."
            )

            await context.bot.send_message(
                request["user_id"],
                "تمت الموافقة على شدتك وتم نزولها.",
            )
            return

        # إرسال نسخة إلى الكروب العام.
        common_message = await send_to_common_group(
            context.bot,
            request,
        )

        if common_message:
            db.log_event(
                "INFO",
                "sent_to_common_group",
                user_id,
                request_id,
                str(common_message.message_id),
            )

        try:
            await lock_common_group(
                context.bot,
                request["platform"],
                group["chat_id"],
            )

        except (TelegramError, RuntimeError) as exc:
            db.log_event(
                "ERROR",
                "lock_group_failed",
                user_id,
                request_id,
                str(exc),
            )

            await query.message.reply_text(
                "تم نزول الشدة، لكن تعذر قفل الكتابة. تأكد أن البوت Administrator ولديه صلاحية تقييد الأعضاء."
            )

            await context.bot.send_message(
                request["user_id"],
                "تمت الموافقة على شدتك وتم نزولها.",
            )

            await query.edit_message_reply_markup(
                reply_markup=None
            )
            return

        await query.edit_message_reply_markup(
            reply_markup=finish_keyboard(request_id)
        )

        await query.answer(
            "تم قبول الشد."
        )

        await context.bot.send_message(
            request["user_id"],
            "تمت الموافقة على شدتك وتم نزولها.",
        )

    except TelegramError as exc:
        db.set_request_status(
            request_id,
            "PENDING",
        )

        db.log_event(
            "ERROR",
            "send_group_failed",
            user_id,
            request_id,
            str(exc),
        )

        await query.answer(
            "تعذر إرسال الشدة إلى الكروب. لم يتم إكمال العملية.",
            show_alert=True,
        )


async def reject_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    try:
        request_id = int(
            query.data.split(":", 1)[1]
        )
    except (ValueError, IndexError):
        await query.answer(
            "رقم الطلب غير صحيح.",
            show_alert=True,
        )
        return

    request = db.get_request(request_id)

    if not request:
        await query.answer(
            "الطلب غير موجود.",
            show_alert=True,
        )
        return

    if not db.get_admin(
        user_id,
        request["platform"],
    ):
        await query.answer(
            "لا تملك صلاحية هذه المنصة.",
            show_alert=True,
        )
        return

    if not db.reject_request(
        request_id,
        user_id,
    ):
        await query.answer(
            "تمت معالجة الطلب بالفعل.",
            show_alert=True,
        )
        return

    await query.edit_message_reply_markup(
        reply_markup=None
    )

    await query.answer(
        "تم رفض الشد."
    )

    await context.bot.send_message(
        request["user_id"],
        "تم رفض الشدة.",
    )


async def finish_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    try:
        request_id = int(
            query.data.split(":", 1)[1]
        )
    except (ValueError, IndexError):
        await query.answer(
            "رقم الطلب غير صحيح.",
            show_alert=True,
        )
        return

    request = db.get_request(request_id)

    if not request:
        await query.answer(
            "الطلب غير موجود.",
            show_alert=True,
        )
        return

    if not db.get_admin(
        user_id,
        request["platform"],
    ):
        await query.answer(
            "لا تملك صلاحية هذه المنصة.",
            show_alert=True,
        )
        return

    if request["status"] != "APPROVED":
        await query.answer(
            "هذا الطلب لا يمكن إنهاؤه.",
            show_alert=True,
        )
        return

    group = db.get_group(
        request["platform"]
    )

    if not group:
        await query.answer(
            "كروب المنصة غير مضبوط.",
            show_alert=True,
        )
        return

    common = db.get_group("common")
    if common:
        try:
            await unlock_common_group(context.bot, common["chat_id"])
        except (TelegramError, RuntimeError) as exc:
            db.log_event(
                "ERROR",
                "common_group_unlock_failed",
                user_id,
                request_id,
                str(exc),
            )
            await query.answer(
                "تعذر فتح الكروب العام. تأكد من صلاحيات البوت.",
                show_alert=True,
            )
            return

    if not db.transition_request(
        request_id,
        "APPROVED",
        "FINISHED",
        user_id,
        "finished",
    ):
        await query.answer(
            "تم إنهاء الطلب بالفعل.",
            show_alert=True,
        )
        return

    await query.edit_message_reply_markup(
        reply_markup=None
    )

    await query.answer(
        "انتهى الشد."
    )

    await context.bot.send_message(
        request["user_id"],
        "انتهى الشد.",
    )

    if request["group_message_id"]:
        try:
            await context.bot.edit_message_text(
                chat_id=request["group_chat_id"],
                message_id=request["group_message_id"],
                text=(
                    format_request(request)
                    + "\n\nالحالة: انتهى الشد."
                ),
            )

        except TelegramError as exc:
            db.log_event(
                "ERROR",
                "edit_finished_message_failed",
                user_id,
                request_id,
                str(exc),
            )

# ============================================================
# Founder panel
# ============================================================

def is_founder(user_id):
    return user_id == FOUNDER_ID


async def start(update, context):
    if update.effective_chat and update.effective_chat.type != "private":
        return

    user_id = update.effective_user.id

    if not db.get_user(user_id):
        await update.message.reply_text(
            "لا تملك صلاحية استخدام البوت."
        )
        return

    if is_founder(user_id):
        await update.message.reply_text(
            "لوحة الإدارة",
            reply_markup=founder_keyboard(),
        )
        return

    await update.message.reply_text(
        "اختر المنصة.",
        reply_markup=main_keyboard(),
    )


async def founder_callback(update, context):
    query = update.callback_query

    if not is_founder(query.from_user.id):
        await query.answer(
            "لا تملك صلاحية الإدارة.",
            show_alert=True,
        )
        return

    await query.answer()

    key = query.data

    if key == "founder:home":
        await query.edit_message_text(
            "لوحة الإدارة",
            reply_markup=founder_keyboard(),
        )

    elif key == "founder:users":
        await query.edit_message_text(
            "إدارة الأعضاء",
            reply_markup=founder_users_keyboard(),
        )

    elif key == "founder:admins":
        await query.edit_message_text(
            "إدارة المشرفين",
            reply_markup=founder_admins_keyboard(),
        )

    elif key == "founder:groups":
        await query.edit_message_text(
            "إدارة الكروبات",
            reply_markup=founder_groups_keyboard(),
        )

    elif key == "founder:platforms":
        await query.edit_message_text(
            "إدارة المنصات",
            reply_markup=founder_platforms_keyboard(),
        )

    elif key == "founder:requests":
        rows = db.list_requests()

        text = "آخر الطلبات:\n\n" + "\n".join(
            (
                f"#{r['id']} | {r['platform']} | "
                f"{r['status']} | {r['created_at']}"
            )
            for r in rows
        )

        await query.edit_message_text(
            text or "لا توجد طلبات.",
            reply_markup=founder_keyboard(),
        )

    elif key == "founder:stats":
        rows = db.stats()

        text = "الإحصائيات:\n\n" + "\n".join(
            f"{r['platform']} - {r['status']}: {r['n']}"
            for r in rows
        )

        await query.edit_message_text(
            text or "لا توجد بيانات.",
            reply_markup=founder_keyboard(),
        )

    elif key == "founder:settings":
        await query.edit_message_text(
            "الإعدادات الحالية محفوظة في Environment Variables وقاعدة البيانات.",
            reply_markup=founder_keyboard(),
        )


async def founder_action_callback(update, context):
    query = update.callback_query

    if not is_founder(query.from_user.id):
        await query.answer(
            "لا تملك صلاحية الإدارة.",
            show_alert=True,
        )
        return

    await query.answer()

    action = query.data.split(":")[1]

    prompts = {
        "add_user": (
            "أرسل ID العضو ثم الاسم بهذا الشكل:\n"
            "123456789 | الاسم"
        ),
        "remove_user": (
            "أرسل ID العضو المراد حذفه."
        ),
        "add_admin": (
            "أرسل ID المشرف ثم المنصة بهذا الشكل:\n"
            "123456789 | telegram"
        ),
        "remove_admin": (
            "أرسل ID المشرف المراد حذفه."
        ),
    }

    if action in prompts:
        db.set_session(
            query.from_user.id,
            f"FOUNDER_{action.upper()}",
            {},
        )

        await query.edit_message_text(
            prompts[action]
        )
        return

    if action == "list_users":
        rows = db.list_users()

        text = "الأعضاء:\n\n" + "\n".join(
            f"{r['user_id']} | {r['full_name']} | {r['role']}"
            for r in rows
        )

        await query.edit_message_text(
            text or "لا يوجد أعضاء.",
            reply_markup=founder_users_keyboard(),
        )
        return

    if action == "list_admins":
        rows = db.list_admins()

        text = "المشرفون:\n\n" + "\n".join(
            (
                f"{r['user_id']} | "
                f"{r['full_name']} | "
                f"{r['platform']}"
            )
            for r in rows
        )

        await query.edit_message_text(
            text or "لا يوجد مشرفون.",
            reply_markup=founder_admins_keyboard(),
        )


async def list_groups_callback(update, context):
    query = update.callback_query

    if not is_founder(query.from_user.id):
        await query.answer(
            "لا تملك صلاحية الإدارة.",
            show_alert=True,
        )
        return

    await query.answer()

    lines = ["الكروبات الحالية:", ""]

    labels = {
        "telegram": "Telegram",
        "instagram": "Instagram",
        "tiktok": "TikTok",
        "common": "الكروب العام",
    }

    for platform in (
        "telegram",
        "instagram",
        "tiktok",
        "common",
    ):
        group = db.get_group(platform)

        lines.append(
            f"{labels[platform]}: "
            f"{group['chat_id'] if group else 'غير مضبوط'}"
        )

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=founder_groups_keyboard(),
    )


async def set_group_callback(update, context):
    query = update.callback_query

    if not is_founder(query.from_user.id):
        await query.answer(
            "لا تملك صلاحية الإدارة.",
            show_alert=True,
        )
        return

    await query.answer()

    platform = query.data.split(":")[2]

    db.set_session(
        query.from_user.id,
        f"FOUNDER_SET_GROUP_{platform.upper()}",
        {},
    )

    if platform == COMMON_PLATFORM:
        label = "الكروب العام"
    else:
        label = f"كروب {platform}"

    await query.edit_message_text(
        f"أرسل Chat ID لـ {label}."
    )


async def founder_platform_callback(update, context):
    query = update.callback_query

    if not is_founder(query.from_user.id):
        await query.answer(
            "لا تملك صلاحية الإدارة.",
            show_alert=True,
        )
        return

    await query.answer()

    platform = query.data.split(":")[2]

    group = db.get_group(platform)

    admins = [
        admin
        for admin in db.list_admins()
        if admin["platform"] == platform
    ]

    await query.edit_message_text(
        f"المنصة: {platform}\n"
        f"الحالة: مفعلة\n"
        f"Chat ID: "
        f"{group['chat_id'] if group else 'غير مضبوط'}\n"
        f"عدد المشرفين: {len(admins)}",
        reply_markup=founder_platforms_keyboard(),
    )


async def founder_text_router(update, context):
    if not update.message or not update.message.text:
        return False

    user_id = update.effective_user.id

    if not is_founder(user_id):
        return False

    session = db.get_session(user_id)

    if not session or not session[0].startswith("FOUNDER_"):
        return False

    state = session[0]
    text = update.message.text.strip()

    try:
        if state == "FOUNDER_ADD_USER":
            uid_s, name = [
                x.strip()
                for x in text.split("|", 1)
            ]

            uid = int(uid_s)

            db.add_user(
                uid,
                None,
                name,
                "member",
            )

            db.clear_session(user_id)

            await update.message.reply_text(
                "تمت إضافة العضو.",
                reply_markup=founder_keyboard(),
            )

            return True

        if state == "FOUNDER_REMOVE_USER":
            db.remove_user(int(text))
            db.clear_session(user_id)

            await update.message.reply_text(
                "تم حذف العضو.",
                reply_markup=founder_keyboard(),
            )

            return True

        if state == "FOUNDER_ADD_ADMIN":
            uid_s, platform = [
                x.strip().lower()
                for x in text.split("|", 1)
            ]

            uid = int(uid_s)

            if platform not in PLATFORMS:
                raise ValueError

            if not db.get_user(uid):
                db.add_user(
                    uid,
                    None,
                    str(uid),
                    "member",
                )

            db.add_admin(
                uid,
                platform,
            )

            db.clear_session(user_id)

            await update.message.reply_text(
                "تمت إضافة المشرف وربطه بالمنصة.",
                reply_markup=founder_keyboard(),
            )

            return True

        if state == "FOUNDER_REMOVE_ADMIN":
            db.remove_admin(int(text))
            db.clear_session(user_id)

            await update.message.reply_text(
                "تم حذف المشرف.",
                reply_markup=founder_keyboard(),
            )

            return True

        if state.startswith("FOUNDER_SET_GROUP_"):
            platform = state.rsplit("_", 1)[1].lower()

            chat_id = int(text)

            if platform not in (
                "telegram",
                "instagram",
                "tiktok",
                "common",
            ):
                raise ValueError

            db.set_group(
                platform,
                chat_id,
            )

            db.clear_session(user_id)

            await update.message.reply_text(
                "تم حفظ Chat ID.",
                reply_markup=founder_keyboard(),
            )

            return True

    except (ValueError, IndexError):
        await update.message.reply_text(
            "القيمة غير صحيحة. أرسلها بالشكل المطلوب."
        )
        return True

    return False

# ============================================================
# Main application
# ============================================================

async def route_text(update, context):
    if await founder_text_router(update, context):
        return

    await text_router(update, context)


async def error_handler(update, context):
    error = context.error

    log.exception(
        "Unhandled Telegram error",
        exc_info=error,
    )

    user_id = getattr(
        getattr(update, "effective_user", None),
        "id",
        None,
    )

    db.log_event(
        "ERROR",
        "unhandled_exception",
        user_id,
        None,
        repr(error),
    )

    if user_id:
        try:
            await context.bot.send_message(
                user_id,
                "حدث خطأ غير متوقع. تم تسجيل الخطأ، حاول مرة أخرى.",
            )
        except Exception:
            pass


def build_app():
    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.bot_data["db"] = db
    app.bot_data["founder_id"] = FOUNDER_ID

    if not db.get_user(FOUNDER_ID):
        db.add_user(
            FOUNDER_ID,
            None,
            "Founder",
            "founder",
        )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            platform_callback,
            pattern=r"^platform:(telegram|instagram|tiktok)$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            cancel_callback,
            pattern=r"^cancel$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            approve_callback,
            pattern=r"^approve:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            reject_callback,
            pattern=r"^reject:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            finish_callback,
            pattern=r"^finish:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            founder_callback,
            pattern=r"^founder:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            founder_action_callback,
            pattern=(
                r"^f:(add_user|remove_user|list_users|"
                r"add_admin|remove_admin|list_admins)$"
            ),
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            set_group_callback,
            pattern=(
                r"^f:set_group:"
                r"(telegram|instagram|tiktok|common)$"
            ),
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            list_groups_callback,
            pattern=r"^f:list_groups$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            founder_platform_callback,
            pattern=r"^f:platform:(telegram|instagram|tiktok)$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            route_text,
        )
    )

    app.add_error_handler(
        error_handler
    )

    return app


if __name__ == "__main__":
    build_app().run_polling(
        allowed_updates=Update.ALL_TYPES
    )
