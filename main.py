# -*- coding: utf-8 -*-
import sys
import types

# الوحدات المضمّنة داخل هذا الملف حتى يبقى المشروع ملف main.py واحدًا.
_EMBEDDED = {
    'config': '''import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
FOUNDER_ID = int(os.getenv("FOUNDER_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment variables.")
if not FOUNDER_ID:
    raise RuntimeError("FOUNDER_ID is not set in environment variables.")
''',
    'database': '''import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

log = logging.getLogger(__name__)

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

            for platform in ("telegram", "instagram", "tiktok"):
                con.execute(
                    "INSERT OR IGNORE INTO platforms(name, enabled) VALUES(?, 1)",
                    (platform,),
                )

    def log_event(self, level, event, user_id=None, request_id=None, details=None):
        try:
            with self.conn() as con:
                con.execute(
                    """INSERT INTO logs(level,event,user_id,request_id,details,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (level, event, user_id, request_id, details, now_iso()),
                )
        except Exception:
            log.exception("Could not write application log")

    def get_user(self, user_id):
        with self.conn() as con:
            return con.execute("SELECT * FROM users WHERE user_id=? AND active=1", (user_id,)).fetchone()

    def add_user(self, user_id, username, full_name, role="member"):
        with self.conn() as con:
            con.execute(
                """INSERT INTO users(user_id,username,full_name,role,active,created_at)
                   VALUES(?,?,?,?,1,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     username=excluded.username, full_name=excluded.full_name,
                     role=excluded.role, active=1""",
                (user_id, username, full_name, role, now_iso()),
            )

    def remove_user(self, user_id):
        with self.conn() as con:
            con.execute("UPDATE users SET active=0 WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM admins WHERE user_id=?", (user_id,))

    def list_users(self):
        with self.conn() as con:
            return con.execute(
                "SELECT * FROM users WHERE active=1 ORDER BY created_at DESC"
            ).fetchall()

    def add_admin(self, user_id, platform):
        with self.conn() as con:
            con.execute(
                """INSERT INTO admins(user_id,platform,active,created_at)
                   VALUES(?,?,1,?)
                   ON CONFLICT(user_id) DO UPDATE SET platform=excluded.platform, active=1""",
                (user_id, platform, now_iso()),
            )
            con.execute("UPDATE users SET role='admin', active=1 WHERE user_id=?", (user_id,))

    def remove_admin(self, user_id):
        with self.conn() as con:
            con.execute("UPDATE admins SET active=0 WHERE user_id=?", (user_id,))
            con.execute(
                """UPDATE users SET role='member'
                   WHERE user_id=? AND active=1""",
                (user_id,),
            )

    def get_admin(self, user_id, platform=None):
        with self.conn() as con:
            if platform:
                return con.execute(
                    "SELECT * FROM admins WHERE user_id=? AND platform=? AND active=1",
                    (user_id, platform),
                ).fetchone()
            return con.execute(
                "SELECT * FROM admins WHERE user_id=? AND active=1", (user_id,)
            ).fetchone()

    def list_admins(self):
        with self.conn() as con:
            return con.execute(
                """SELECT a.*, u.username, u.full_name
                   FROM admins a JOIN users u ON u.user_id=a.user_id
                   WHERE a.active=1 ORDER BY a.platform, a.created_at DESC"""
            ).fetchall()

    def set_group(self, platform, chat_id):
        with self.conn() as con:
            con.execute(
                """INSERT INTO groups_config(platform,chat_id)
                   VALUES(?,?)
                   ON CONFLICT(platform) DO UPDATE SET chat_id=excluded.chat_id""",
                (platform, chat_id),
            )

    def get_group(self, platform):
        with self.conn() as con:
            return con.execute(
                "SELECT * FROM groups_config WHERE platform=?", (platform,)
            ).fetchone()

    def set_previous_permissions(self, platform, permissions_json):
        with self.conn() as con:
            con.execute(
                "UPDATE groups_config SET previous_permissions=? WHERE platform=?",
                (permissions_json, platform),
            )

    def clear_previous_permissions(self, platform):
        with self.conn() as con:
            con.execute(
                "UPDATE groups_config SET previous_permissions=NULL WHERE platform=?",
                (platform,),
            )

    def create_request(self, platform, user_id, username, data):
        created = now_iso()
        with self.conn() as con:
            cur = con.execute(
                """INSERT INTO requests(
                    platform,user_id,username,data_json,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (platform, user_id, username, json.dumps(data, ensure_ascii=False),
                 "PENDING", created, created),
            )
            return cur.lastrowid

    def get_request(self, request_id):
        with self.conn() as con:
            return con.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()

    def transition_request(self, request_id, from_status, to_status, user_id, action):
        with self.conn() as con:
            # IMMEDIATE makes the status transition atomic between concurrent callbacks.
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                f"""UPDATE requests
                    SET status=?, {action}_by=?, {action}_at=?, updated_at=?
                    WHERE id=? AND status=?""",
                (to_status, user_id, now_iso(), now_iso(), request_id, from_status),
            )
            return cur.rowcount == 1

    def set_group_message(self, request_id, chat_id, message_id):
        with self.conn() as con:
            con.execute(
                """UPDATE requests
                   SET group_chat_id=?, group_message_id=?, updated_at=?
                   WHERE id=?""",
                (chat_id, message_id, now_iso(), request_id),
            )

    def reject_request(self, request_id, user_id):
        with self.conn() as con:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                """UPDATE requests
                   SET status='REJECTED', updated_at=?
                   WHERE id=? AND status='PENDING'""",
                (now_iso(), request_id),
            )
            if cur.rowcount == 1:
                con.execute(
                    "INSERT INTO logs(level,event,user_id,request_id,details,created_at) VALUES(?,?,?,?,?,?)",
                    ("INFO", "request_rejected", user_id, request_id, None, now_iso()),
                )
            return cur.rowcount == 1

    def set_request_status(self, request_id, status):
        with self.conn() as con:
            con.execute(
                "UPDATE requests SET status=?, updated_at=? WHERE id=?",
                (status, now_iso(), request_id),
            )

    def get_pending_for_platform(self, platform):
        with self.conn() as con:
            return con.execute(
                """SELECT * FROM requests
                   WHERE platform=? AND status='PENDING'
                   ORDER BY created_at ASC""",
                (platform,),
            ).fetchall()

    def list_requests(self, limit=50):
        with self.conn() as con:
            return con.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def stats(self):
        with self.conn() as con:
            rows = con.execute(
                "SELECT platform,status,COUNT(*) AS n FROM requests GROUP BY platform,status"
            ).fetchall()
            return rows

    def get_session(self, user_id):
        with self.conn() as con:
            row = con.execute("SELECT * FROM sessions WHERE user_id=?", (user_id,)).fetchone()
            if not row:
                return None
            return row["state"], json.loads(row["data_json"])

    def set_session(self, user_id, state, data=None):
        with self.conn() as con:
            con.execute(
                """INSERT INTO sessions(user_id,state,data_json,updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     state=excluded.state,
                     data_json=excluded.data_json,
                     updated_at=excluded.updated_at""",
                (user_id, state, json.dumps(data or {}, ensure_ascii=False), now_iso()),
            )

    def clear_session(self, user_id):
        with self.conn() as con:
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
''',
    'keyboards': '''from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Telegram", callback_data="platform:telegram")],
        [InlineKeyboardButton("Instagram", callback_data="platform:instagram")],
        [InlineKeyboardButton("TikTok", callback_data="platform:tiktok")],
    ])

def cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("إلغاء", callback_data="cancel")]
    ])

def admin_request_keyboard(request_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("قبول الشد", callback_data=f"approve:{request_id}"),
            InlineKeyboardButton("رفض الشد", callback_data=f"reject:{request_id}"),
        ]
    ])

def finish_keyboard(request_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("انتهاء الشد", callback_data=f"finish:{request_id}")]
    ])

def founder_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("إدارة الأعضاء", callback_data="founder:users")],
        [InlineKeyboardButton("إدارة المشرفين", callback_data="founder:admins")],
        [InlineKeyboardButton("إدارة المنصات", callback_data="founder:platforms")],
        [InlineKeyboardButton("إدارة الكروبات", callback_data="founder:groups")],
        [InlineKeyboardButton("الطلبات", callback_data="founder:requests")],
        [InlineKeyboardButton("الإحصائيات", callback_data="founder:stats")],
        [InlineKeyboardButton("الإعدادات", callback_data="founder:settings")],
    ])

def founder_users_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("إضافة عضو", callback_data="f:add_user")],
        [InlineKeyboardButton("حذف عضو", callback_data="f:remove_user")],
        [InlineKeyboardButton("قائمة الأعضاء", callback_data="f:list_users")],
        [InlineKeyboardButton("رجوع", callback_data="founder:home")],
    ])

def founder_admins_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("إضافة مشرف", callback_data="f:add_admin")],
        [InlineKeyboardButton("حذف مشرف", callback_data="f:remove_admin")],
        [InlineKeyboardButton("قائمة المشرفين", callback_data="f:list_admins")],
        [InlineKeyboardButton("رجوع", callback_data="founder:home")],
    ])

def founder_groups_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("تعيين كروب Telegram", callback_data="f:set_group:telegram")],
        [InlineKeyboardButton("تعيين كروب Instagram", callback_data="f:set_group:instagram")],
        [InlineKeyboardButton("تعيين كروب TikTok", callback_data="f:set_group:tiktok")],
        [InlineKeyboardButton("رجوع", callback_data="founder:home")],
    ])

def founder_platforms_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Telegram", callback_data="f:platform:telegram")],
        [InlineKeyboardButton("Instagram", callback_data="f:platform:instagram")],
        [InlineKeyboardButton("TikTok", callback_data="f:platform:tiktok")],
        [InlineKeyboardButton("رجوع", callback_data="founder:home")],
    ])
''',
    'utils': '''import json
import logging
from telegram import ChatPermissions
from telegram.error import TelegramError

log = logging.getLogger(__name__)

async def safe_answer(query, text=None):
    try:
        await query.answer(text=text)
    except TelegramError:
        pass

def row_data(row):
    return json.loads(row["data_json"])

def format_request(request):
    data = row_data(request)
    platform = request["platform"].upper()
    lines = [f"{platform} SHD", ""]
    for key, value in data.items():
        lines.append(f"{key}:")
        lines.append(str(value))
        lines.append("")
    lines.append(f"رقم الطلب: {request['id']}")
    return "\n".join(lines).strip()

def permissions_to_json(permissions):
    return json.dumps({
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
        "can_change_info": permissions.can_change_info,
        "can_invite_users": permissions.can_invite_users,
        "can_pin_messages": permissions.can_pin_messages,
        "can_manage_topics": permissions.can_manage_topics,
        "can_react_to_messages": getattr(permissions, "can_react_to_messages", None),
    }, ensure_ascii=False)

def permissions_from_json(raw):
    data = json.loads(raw)
    return ChatPermissions(**{k: v for k, v in data.items() if v is not None})

async def lock_group(bot, db, platform, chat_id):
    chat = await bot.get_chat(chat_id)
    if not chat.permissions:
        raise RuntimeError("تعذر قراءة صلاحيات الكروب.")
    db.set_previous_permissions(platform, permissions_to_json(chat.permissions))
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
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False,
    )
    await bot.set_chat_permissions(chat_id, locked, use_independent_chat_permissions=True)

async def unlock_group(bot, db, platform, chat_id):
    group = db.get_group(platform)
    if group and group["previous_permissions"]:
        permissions = permissions_from_json(group["previous_permissions"])
    else:
        permissions = ChatPermissions(
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
    await bot.set_chat_permissions(chat_id, permissions, use_independent_chat_permissions=True)
    db.clear_previous_permissions(platform)
''',
    'platforms': '''''',
    'platforms.telegram': '''from keyboards import cancel_keyboard
from database import now_iso

STATES = {
    "COPY": "WAITING_TELEGRAM_COPY",
    "TITLE": "WAITING_TELEGRAM_TITLE",
    "EMAIL": "WAITING_TELEGRAM_EMAIL",
}

async def start(bot, db, user_id):
    db.set_session(user_id, STATES["COPY"], {})
    return "أرسل الكليشة.", cancel_keyboard()

async def handle_text(db, user_id, state, text):
    session = db.get_session(user_id)
    data = session[1] if session else {}

    if state == STATES["COPY"]:
        data["الكليشة"] = text
        db.set_session(user_id, STATES["TITLE"], data)
        return "أرسل العنوان.", cancel_keyboard()

    if state == STATES["TITLE"]:
        data["العنوان"] = text
        db.set_session(user_id, STATES["EMAIL"], data)
        return "أرسل البريد الإلكتروني.", cancel_keyboard()

    if state == STATES["EMAIL"]:
        data["البريد الإلكتروني"] = text
        db.clear_session(user_id)
        return data, None

    return None, None
''',
    'platforms.instagram': '''from keyboards import cancel_keyboard

STATES = {
    "USERNAME": "WAITING_INSTAGRAM_USERNAME",
    "SHD": "WAITING_INSTAGRAM_SHD",
}

async def start(bot, db, user_id):
    db.set_session(user_id, STATES["USERNAME"], {})
    return "أرسل يوزر الحساب.", cancel_keyboard()

async def handle_text(db, user_id, state, text):
    session = db.get_session(user_id)
    data = session[1] if session else {}

    if state == STATES["USERNAME"]:
        username = text.strip().lstrip("@").strip()
        if not username or " " in username or "/" in username:
            return "أرسل اليوزر بشكل صحيح.", cancel_keyboard()
        data["Username"] = f"@{username}"
        data["Instagram"] = f"https://instagram.com/{username}"
        db.set_session(user_id, STATES["SHD"], data)
        return "أرسل شدتك.", cancel_keyboard()

    if state == STATES["SHD"]:
        data["الشدة"] = text
        db.clear_session(user_id)
        return data, None

    return None, None
''',
    'platforms.tiktok': '''from keyboards import cancel_keyboard

# TikTok intentionally has one generic field because the requested TikTok
# fields were not specified. Edit this file only when the TikTok workflow
# is defined. Telegram and Instagram are not affected.
STATE = "WAITING_TIKTOK_DATA"

async def start(bot, db, user_id):
    db.set_session(user_id, STATE, {})
    return "أرسل بيانات شد TikTok.", cancel_keyboard()

async def handle_text(db, user_id, state, text):
    if state != STATE:
        return None, None
    if not text.strip():
        return "أرسل بيانات الشد.", cancel_keyboard()
    db.clear_session(user_id)
    return {"بيانات الشد": text.strip()}, None
''',
    'handlers': '''''',
    'handlers.start': '''from telegram import Update
from telegram.ext import ContextTypes
from keyboards import main_keyboard, founder_keyboard

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("لا تملك صلاحية استخدام البوت.")
        return

    if user["user_id"] == context.application.bot_data["founder_id"]:
        await update.message.reply_text("لوحة الإدارة", reply_markup=founder_keyboard())
        return

    await update.message.reply_text("اختر المنصة.", reply_markup=main_keyboard())
''',
    'handlers.admin': '''from telegram import Update
from telegram.ext import ContextTypes
from keyboards import founder_keyboard, founder_users_keyboard, founder_admins_keyboard, founder_groups_keyboard, founder_platforms_keyboard

def is_founder(update, context):
    return update.effective_user.id == context.application.bot_data["founder_id"]

async def founder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    db = context.application.bot_data["db"]
    if not is_founder(update, context):
        await q.answer("لا تملك صلاحية الإدارة.", show_alert=True)
        return
    await q.answer()
    key = q.data

    if key == "founder:home":
        await q.edit_message_text("لوحة الإدارة", reply_markup=founder_keyboard())
    elif key == "founder:users":
        await q.edit_message_text("إدارة الأعضاء", reply_markup=founder_users_keyboard())
    elif key == "founder:admins":
        await q.edit_message_text("إدارة المشرفين", reply_markup=founder_admins_keyboard())
    elif key == "founder:groups":
        await q.edit_message_text("إدارة الكروبات", reply_markup=founder_groups_keyboard())
    elif key == "founder:platforms":
        await q.edit_message_text("إدارة المنصات", reply_markup=founder_platforms_keyboard())
    elif key == "founder:requests":
        rows = db.list_requests()
        text = "آخر الطلبات:\n\n" + "\n".join(
            f"#{r['id']} | {r['platform']} | {r['status']} | {r['created_at']}" for r in rows
        )
        await q.edit_message_text(text or "لا توجد طلبات.", reply_markup=founder_keyboard())
    elif key == "founder:stats":
        rows = db.stats()
        text = "الإحصائيات:\n\n" + "\n".join(
            f"{r['platform']} - {r['status']}: {r['n']}" for r in rows
        )
        await q.edit_message_text(text or "لا توجد بيانات.", reply_markup=founder_keyboard())
    elif key == "founder:settings":
        await q.edit_message_text(
            "الإعدادات الحالية محفوظة في Environment Variables وقاعدة البيانات.",
            reply_markup=founder_keyboard(),
        )

async def founder_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    db = context.application.bot_data["db"]
    if not is_founder(update, context):
        await q.answer("لا تملك صلاحية الإدارة.", show_alert=True)
        return
    await q.answer()
    action = q.data.split(":")[1]

    prompts = {
        "add_user": ("أرسل ID العضو ثم الاسم بهذا الشكل:\n123456789 | الاسم"),
        "remove_user": ("أرسل ID العضو المراد حذفه."),
        "add_admin": ("أرسل ID المشرف ثم المنصة بهذا الشكل:\n123456789 | telegram"),
        "remove_admin": ("أرسل ID المشرف المراد حذفه."),
    }
    if action in prompts:
        db.set_session(q.from_user.id, f"FOUNDER_{action.upper()}", {})
        await q.edit_message_text(prompts[action])
        return

    if action == "list_users":
        rows = db.list_users()
        text = "الأعضاء:\n\n" + "\n".join(
            f"{r['user_id']} | {r['full_name']} | {r['role']}" for r in rows
        )
        await q.edit_message_text(text or "لا يوجد أعضاء.", reply_markup=founder_users_keyboard())
        return

    if action == "list_admins":
        rows = db.list_admins()
        text = "المشرفون:\n\n" + "\n".join(
            f"{r['user_id']} | {r['full_name']} | {r['platform']}" for r in rows
        )
        await q.edit_message_text(text or "لا يوجد مشرفون.", reply_markup=founder_admins_keyboard())
        return

async def set_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    db = context.application.bot_data["db"]
    if not is_founder(update, context):
        await q.answer("لا تملك صلاحية الإدارة.", show_alert=True)
        return
    await q.answer()
    platform = q.data.split(":")[2]
    db.set_session(q.from_user.id, f"FOUNDER_SET_GROUP_{platform.upper()}", {})
    await q.edit_message_text(
        f"أرسل Chat ID لكروب {platform}."
    )

async def founder_platform_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    db = context.application.bot_data["db"]
    if not is_founder(update, context):
        await q.answer("لا تملك صلاحية الإدارة.", show_alert=True)
        return
    await q.answer()
    platform = q.data.split(":")[2]
    group = db.get_group(platform)
    admins = [a for a in db.list_admins() if a["platform"] == platform]
    await q.edit_message_text(
        f"المنصة: {platform}\n"
        f"الحالة: مفعلة\n"
        f"Chat ID: {group['chat_id'] if group else 'غير مضبوط'}\n"
        f"عدد المشرفين: {len(admins)}",
        reply_markup=founder_platforms_keyboard(),
    )

async def founder_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return False
    db = context.application.bot_data["db"]
    if not is_founder(update, context):
        return False
    session = db.get_session(update.effective_user.id)
    if not session or not session[0].startswith("FOUNDER_"):
        return False
    state = session[0]
    text = update.message.text.strip()

    try:
        if state == "FOUNDER_ADD_USER":
            uid_s, name = [x.strip() for x in text.split("|", 1)]
            uid = int(uid_s)
            db.add_user(uid, None, name, "member")
            db.clear_session(update.effective_user.id)
            await update.message.reply_text("تمت إضافة العضو.", reply_markup=founder_keyboard())
            return True

        if state == "FOUNDER_REMOVE_USER":
            db.remove_user(int(text))
            db.clear_session(update.effective_user.id)
            await update.message.reply_text("تم حذف العضو.", reply_markup=founder_keyboard())
            return True

        if state == "FOUNDER_ADD_ADMIN":
            uid_s, platform = [x.strip() for x in text.split("|", 1)]
            uid = int(uid_s)
            if platform not in ("telegram", "instagram", "tiktok"):
                raise ValueError
            if not db.get_user(uid):
                db.add_user(uid, None, str(uid), "member")
            db.add_admin(uid, platform)
            db.clear_session(update.effective_user.id)
            await update.message.reply_text("تمت إضافة المشرف وربطه بالمنصة.", reply_markup=founder_keyboard())
            return True

        if state == "FOUNDER_REMOVE_ADMIN":
            db.remove_admin(int(text))
            db.clear_session(update.effective_user.id)
            await update.message.reply_text("تم حذف المشرف.", reply_markup=founder_keyboard())
            return True

        if state.startswith("FOUNDER_SET_GROUP_"):
            platform = state.rsplit("_", 1)[1].lower()
            chat_id = int(text)
            db.set_group(platform, chat_id)
            db.clear_session(update.effective_user.id)
            await update.message.reply_text("تم حفظ Chat ID.", reply_markup=founder_keyboard())
            return True
    except (ValueError, IndexError):
        await update.message.reply_text("القيمة غير صحيحة. أرسلها بالشكل المطلوب.")
        return True
    return False
''',
    'handlers.requests': '''import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from keyboards import main_keyboard, admin_request_keyboard, finish_keyboard
from platforms import telegram as tg_platform
from platforms import instagram as ig_platform
from platforms import tiktok as tt_platform
from utils import format_request, lock_group, unlock_group

log = logging.getLogger(__name__)
PLATFORMS = {"telegram": tg_platform, "instagram": ig_platform, "tiktok": tt_platform}

def member_row(db, user_id):
    return db.get_user(user_id)

async def send_request_to_admins(bot, db, request_id, platform):
    request = db.get_request(request_id)
    text = format_request(request)
    admins = db.list_admins()
    sent = 0
    for admin in admins:
        if admin["platform"] != platform:
            continue
        try:
            await bot.send_message(
                admin["user_id"], text, reply_markup=admin_request_keyboard(request_id)
            )
            sent += 1
        except TelegramError as exc:
            db.log_event("ERROR", "notify_admin_failed", admin["user_id"], request_id, str(exc))
    return sent

async def platform_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = context.application.bot_data["db"]
    user = member_row(db, q.from_user.id)
    if not user:
        await q.edit_message_text("لا تملك صلاحية استخدام البوت.")
        return

    platform = q.data.split(":", 1)[1]
    module = PLATFORMS.get(platform)
    if not module:
        return

    db.clear_session(q.from_user.id)
    message, keyboard = await module.start(context.bot, db, q.from_user.id)
    await q.edit_message_text(message, reply_markup=keyboard)

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    db = context.application.bot_data["db"]
    if not db.get_user(q.from_user.id):
        await q.edit_message_text("لا تملك صلاحية استخدام البوت.")
        return
    db.clear_session(q.from_user.id)
    await q.edit_message_text("تم إلغاء العملية.", reply_markup=main_keyboard())

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    db = context.application.bot_data["db"]
    user = db.get_user(update.effective_user.id)
    if not user:
        await update.message.reply_text("لا تملك صلاحية استخدام البوت.")
        return

    session = db.get_session(update.effective_user.id)
    if not session:
        await update.message.reply_text("اختر المنصة.", reply_markup=main_keyboard())
        return

    state, _ = session
    module = next((m for m in PLATFORMS.values() if state in getattr(m, "STATES", {}).values()), None)
    if module is None and state == tt_platform.STATE:
        module = tt_platform
    if module is None:
        db.clear_session(update.effective_user.id)
        await update.message.reply_text("انتهت العملية. اختر المنصة.", reply_markup=main_keyboard())
        return

    message, keyboard = await module.handle_text(
        db, update.effective_user.id, state, update.message.text
    )
    if isinstance(message, dict):
        platform = "telegram" if module is tg_platform else "instagram" if module is ig_platform else "tiktok"
        request_id = db.create_request(
            platform,
            update.effective_user.id,
            update.effective_user.username,
            message,
        )
        sent = await send_request_to_admins(context.bot, db, request_id, platform)
        if sent == 0:
            db.set_request_status(request_id, "REJECTED")
            db.log_event("ERROR", "no_platform_admin", update.effective_user.id, request_id, platform)
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

    await update.message.reply_text(message, reply_markup=keyboard)

async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    db = context.application.bot_data["db"]
    request_id = int(q.data.split(":")[1])
    request = db.get_request(request_id)
    if not request:
        await q.answer("الطلب غير موجود.", show_alert=True)
        return
    admin = db.get_admin(q.from_user.id, request["platform"])
    if not admin:
        await q.answer("لا تملك صلاحية هذه المنصة.", show_alert=True)
        return
    if request["status"] != "PENDING":
        await q.answer("تمت معالجة الطلب بالفعل.", show_alert=True)
        return

    if not db.transition_request(request_id, "PENDING", "APPROVED", q.from_user.id, "approved"):
        await q.answer("تمت معالجة الطلب بالفعل.", show_alert=True)
        return

    group = db.get_group(request["platform"])
    if not group:
        db.set_request_status(request_id, "PENDING")
        db.log_event("ERROR", "group_not_configured", q.from_user.id, request_id, request["platform"])
        await q.answer("لم يتم تعيين كروب لهذه المنصة.", show_alert=True)
        return

    try:
        msg = await context.bot.send_message(group["chat_id"], format_request(request))
        db.set_group_message(request_id, group["chat_id"], msg.message_id)

        try:
            await context.bot.pin_chat_message(
                group["chat_id"], msg.message_id, disable_notification=True
            )
        except TelegramError as exc:
            db.log_event("ERROR", "pin_failed", q.from_user.id, request_id, str(exc))
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("تم إرسال الشدة، لكن تعذر تثبيتها. راجع صلاحيات البوت في الكروب.")
            await context.bot.send_message(
                q.from_user.id, "تمت الموافقة على شدتك وتم نزولها."
            )
            return

        try:
            await lock_group(context.bot, db, request["platform"], group["chat_id"])
        except TelegramError as exc:
            db.log_event("ERROR", "lock_group_failed", q.from_user.id, request_id, str(exc))
            await q.message.reply_text("تم نزول الشدة، لكن تعذر قفل الكتابة. تأكد أن البوت Administrator ولديه صلاحية تقييد الأعضاء.")
            await context.bot.send_message(q.from_user.id, "تمت الموافقة على شدتك وتم نزولها.")
            await q.edit_message_reply_markup(reply_markup=None)
            return

        await q.edit_message_reply_markup(reply_markup=finish_keyboard(request_id))
        await q.answer("تم قبول الشد.")
        await context.bot.send_message(q.from_user.id, "تمت الموافقة على شدتك وتم نزولها.")
    except TelegramError as exc:
        db.set_request_status(request_id, "PENDING")
        db.log_event("ERROR", "send_group_failed", q.from_user.id, request_id, str(exc))
        await q.answer("تعذر إرسال الشدة إلى الكروب. لم يتم إكمال العملية.", show_alert=True)

async def reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    db = context.application.bot_data["db"]
    request_id = int(q.data.split(":")[1])
    request = db.get_request(request_id)
    if not request:
        await q.answer("الطلب غير موجود.", show_alert=True)
        return
    if not db.get_admin(q.from_user.id, request["platform"]):
        await q.answer("لا تملك صلاحية هذه المنصة.", show_alert=True)
        return
    if not db.reject_request(request_id, q.from_user.id):
        await q.answer("تمت معالجة الطلب بالفعل.", show_alert=True)
        return
    await q.edit_message_reply_markup(reply_markup=None)
    await q.answer("تم رفض الشد.")
    await context.bot.send_message(request["user_id"], "تم رفض الشدة.")

async def finish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    db = context.application.bot_data["db"]
    request_id = int(q.data.split(":")[1])
    request = db.get_request(request_id)
    if not request:
        await q.answer("الطلب غير موجود.", show_alert=True)
        return
    if not db.get_admin(q.from_user.id, request["platform"]):
        await q.answer("لا تملك صلاحية هذه المنصة.", show_alert=True)
        return
    if request["status"] != "APPROVED":
        await q.answer("هذا الطلب لا يمكن إنهاؤه.", show_alert=True)
        return

    group = db.get_group(request["platform"])
    if not group:
        await q.answer("كروب المنصة غير مضبوط.", show_alert=True)
        return

    try:
        await unlock_group(context.bot, db, request["platform"], group["chat_id"])
    except TelegramError as exc:
        db.log_event("ERROR", "unlock_group_failed", q.from_user.id, request_id, str(exc))
        await q.answer("تعذر فتح الكتابة. تأكد من صلاحيات البوت.", show_alert=True)
        return

    if not db.transition_request(request_id, "APPROVED", "FINISHED", q.from_user.id, "finished"):
        await q.answer("تم إنهاء الطلب بالفعل.", show_alert=True)
        return

    await q.edit_message_reply_markup(reply_markup=None)
    await q.answer("انتهى الشد.")
    await context.bot.send_message(request["user_id"], "انتهى الشد.")

    if request["group_message_id"]:
        try:
            await context.bot.edit_message_text(
                chat_id=request["group_chat_id"],
                message_id=request["group_message_id"],
                text=format_request(request) + "\n\nالحالة: انتهى الشد.",
            )
        except TelegramError as exc:
            db.log_event("ERROR", "edit_finished_message_failed", q.from_user.id, request_id, str(exc))
'''
}

def _load_embedded(name, source):
    mod = types.ModuleType(name)
    mod.__file__ = name
    mod.__package__ = name.rsplit(".", 1)[0] if "." in name else ""
    sys.modules[name] = mod
    exec(compile(source, name, "exec"), mod.__dict__)
    return mod

# الحزم أولًا ثم الملفات التابعة لها.
for _name in ("platforms", "handlers"):
    _load_embedded(_name, _EMBEDDED[_name])

for _name in (
    "config", "database", "keyboards", "utils",
    "platforms.telegram", "platforms.instagram", "platforms.tiktok",
    "handlers.start", "handlers.admin", "handlers.requests"
):
    _load_embedded(_name, _EMBEDDED[_name])

import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

from config import BOT_TOKEN, FOUNDER_ID, DB_PATH
from database import Database
from handlers.start import start
from handlers.admin import (
    founder_callback, founder_action_callback, set_group_callback,
    founder_platform_callback, founder_text_router
)
from handlers.requests import (
    platform_callback, cancel_callback, text_router,
    approve_callback, reject_callback, finish_callback
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger(__name__)

async def route_text(update, context):
    if await founder_text_router(update, context):
        return
    await text_router(update, context)

async def error_handler(update, context):
    db = context.application.bot_data["db"]
    error = context.error
    log.exception("Unhandled Telegram error", exc_info=error)
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    db.log_event("ERROR", "unhandled_exception", user_id, None, repr(error))
    if user_id:
        try:
            await context.bot.send_message(
                user_id, "حدث خطأ غير متوقع. تم تسجيل الخطأ، حاول مرة أخرى."
            )
        except Exception:
            pass

def build_app():
    db = Database(DB_PATH)
    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["db"] = db
    app.bot_data["founder_id"] = FOUNDER_ID

    if not db.get_user(FOUNDER_ID):
        db.add_user(FOUNDER_ID, None, "Founder", "founder")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(platform_callback, pattern=r"^platform:(telegram|instagram|tiktok)$"))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^cancel$"))
    app.add_handler(CallbackQueryHandler(approve_callback, pattern=r"^approve:\d+$"))
    app.add_handler(CallbackQueryHandler(reject_callback, pattern=r"^reject:\d+$"))
    app.add_handler(CallbackQueryHandler(finish_callback, pattern=r"^finish:\d+$"))
    app.add_handler(CallbackQueryHandler(founder_callback, pattern=r"^founder:"))
    app.add_handler(CallbackQueryHandler(
        founder_action_callback,
        pattern=r"^f:(add_user|remove_user|list_users|add_admin|remove_admin|list_admins)$"
    ))
    app.add_handler(CallbackQueryHandler(
        set_group_callback,
        pattern=r"^f:set_group:(telegram|instagram|tiktok)$"
    ))
    app.add_handler(CallbackQueryHandler(
        founder_platform_callback,
        pattern=r"^f:platform:(telegram|instagram|tiktok)$"
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))
    app.add_error_handler(error_handler)
    return app

if __name__ == "__main__":
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)
