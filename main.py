# -*- coding: utf-8 -*-
"""Team SHD Bot - Railway build.
Single-file Telegram bot with platform groups and a common/public group.
All user-facing text is UTF-8 Arabic. No emoji are used.
"""

import os
import json
import logging
import sqlite3
import base64
import gzip
from io import BytesIO
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

TEAM_LINK_URL = "https://t.me/team_dark8"

# ============================================================
# Application feature configuration
# ============================================================

APPLY_STATE = "APPLYING"

# مفاتيح ونصوص أسئلة التقديم. القسم (department) له معالجة خاصة بأزرار
# وليس نصًا حرًا. المالك يستطيع تغيير ترتيب هذه الأسئلة فقط (وليس حذفها).
APPLY_QUESTIONS = {
    "name": "ما اسمك؟",
    "fields": "ما هي مجالاتك التي تخصصت بها؟",
    "department": "ما هو قسمك؟",
    "prev_teams": "ما هي التيمات التي سبق وأن دخلت بها؟",
    "self_eval": "هل ترى نفسك تستحق التقديم؟",
}

DEPARTMENT_OPTIONS = {
    "instagram": "انستقرام",
    "telegram": "تيليجرام",
    "both": "كلاهما",
}

# ============================================================
# Config backup/restore feature (نسخ احتياطي لإعدادات البوت)
# ============================================================

CONFIG_BACKUP_PREFIX = "SHDCFG1:"
# إذا كان طول الكود أطول من هذا الحد يُرسل كملف .txt بدل رسالة نصية.
CONFIG_BACKUP_INLINE_LIMIT = 3500


def encode_config_blob(data):
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(raw)
    encoded = base64.urlsafe_b64encode(compressed).decode("ascii")
    return CONFIG_BACKUP_PREFIX + encoded


def decode_config_blob(text):
    cleaned = "".join(text.split())

    if not cleaned.startswith(CONFIG_BACKUP_PREFIX):
        raise ValueError("invalid backup code prefix")

    encoded = cleaned[len(CONFIG_BACKUP_PREFIX):]
    compressed = base64.urlsafe_b64decode(encoded.encode("ascii"))
    raw = gzip.decompress(compressed)
    return json.loads(raw.decode("utf-8"))

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

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                department TEXT,
                answers_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                decided_by INTEGER,
                decided_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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

    # -------------------- Settings (key/value) --------------------

    def get_setting(self, key, default=None):
        with self.conn() as con:
            row = con.execute(
                "SELECT value FROM settings WHERE key=?",
                (key,),
            ).fetchone()
            return row["value"] if row else default

    def set_setting(self, key, value):
        with self.conn() as con:
            con.execute(
                """INSERT INTO settings(key, value)
                   VALUES(?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )

    # -------------------- Applications (التقديم على التيم) --------------------

    def create_application(self, user_id, username, full_name, department, answers):
        created = now_iso()

        with self.conn() as con:
            cur = con.execute(
                """INSERT INTO applications(
                    user_id,username,full_name,department,answers_json,
                    status,created_at,updated_at
                )
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    username,
                    full_name,
                    department,
                    json.dumps(answers, ensure_ascii=False),
                    "PENDING",
                    created,
                    created,
                ),
            )
            return cur.lastrowid

    def get_application(self, app_id):
        with self.conn() as con:
            return con.execute(
                "SELECT * FROM applications WHERE id=?",
                (app_id,),
            ).fetchone()

    def get_pending_application(self, user_id):
        with self.conn() as con:
            return con.execute(
                """SELECT * FROM applications
                   WHERE user_id=? AND status='PENDING'
                   ORDER BY id DESC
                   LIMIT 1""",
                (user_id,),
            ).fetchone()

    def set_application_status(self, app_id, status, decided_by):
        with self.conn() as con:
            con.execute(
                """UPDATE applications
                   SET status=?, decided_by=?, decided_at=?, updated_at=?
                   WHERE id=?""",
                (
                    status,
                    decided_by,
                    now_iso(),
                    now_iso(),
                    app_id,
                ),
            )

    def list_applications(self, limit=50):
        with self.conn() as con:
            return con.execute(
                """SELECT * FROM applications
                   ORDER BY id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()

    # -------------------- Config backup/restore --------------------
    # يشمل النسخ الاحتياطي: الأعضاء، المشرفين، الكروبات، المنصات،
    # وإعدادات ميزة التقديم (رسالة الترحيب وترتيب الأسئلة).
    # لا يشمل: الطلبات (requests)، طلبات التقديم (applications)،
    # الجلسات المؤقتة (sessions)، أو السجلات (logs).

    def export_config(self):
        with self.conn() as con:
            users = [
                dict(r) for r in con.execute(
                    """SELECT user_id,username,full_name,role
                       FROM users WHERE active=1"""
                )
            ]
            admins = [
                dict(r) for r in con.execute(
                    "SELECT user_id,platform FROM admins WHERE active=1"
                )
            ]
            groups = [
                dict(r) for r in con.execute(
                    "SELECT platform,chat_id FROM groups_config"
                )
            ]
            platforms = [
                dict(r) for r in con.execute(
                    "SELECT name,enabled FROM platforms"
                )
            ]
            settings = [
                dict(r) for r in con.execute(
                    "SELECT key,value FROM settings"
                )
            ]

        return {
            "version": 1,
            "exported_at": now_iso(),
            "users": users,
            "admins": admins,
            "groups": groups,
            "platforms": platforms,
            "settings": settings,
        }

    def import_config(self, data):
        with self.conn() as con:
            con.execute("BEGIN IMMEDIATE")

            for u in data.get("users", []):
                if "user_id" not in u:
                    continue
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
                        u["user_id"],
                        u.get("username"),
                        u.get("full_name") or str(u["user_id"]),
                        u.get("role", "member"),
                        now_iso(),
                    ),
                )

            for a in data.get("admins", []):
                if "user_id" not in a or "platform" not in a:
                    continue
                con.execute(
                    """INSERT INTO admins(
                        user_id,platform,active,created_at
                    )
                    VALUES(?,?,1,?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        platform=excluded.platform,
                        active=1""",
                    (
                        a["user_id"],
                        a["platform"],
                        now_iso(),
                    ),
                )

            for g in data.get("groups", []):
                if "platform" not in g or "chat_id" not in g:
                    continue
                con.execute(
                    """INSERT INTO groups_config(
                        platform,chat_id,previous_permissions
                    )
                    VALUES(?,?,NULL)
                    ON CONFLICT(platform) DO UPDATE SET
                        chat_id=excluded.chat_id""",
                    (
                        g["platform"],
                        g["chat_id"],
                    ),
                )

            for p in data.get("platforms", []):
                if "name" not in p:
                    continue
                con.execute(
                    """INSERT INTO platforms(name, enabled)
                       VALUES(?, ?)
                       ON CONFLICT(name) DO UPDATE SET
                           enabled=excluded.enabled""",
                    (
                        p["name"],
                        p.get("enabled", 1),
                    ),
                )

            for s in data.get("settings", []):
                if "key" not in s:
                    continue
                con.execute(
                    """INSERT INTO settings(key, value)
                       VALUES(?, ?)
                       ON CONFLICT(key) DO UPDATE SET
                           value=excluded.value""",
                    (
                        s["key"],
                        s.get("value"),
                    ),
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
            "إعدادات التقديم",
            callback_data="founder:apply",
        )],
        [InlineKeyboardButton(
            "نسخ احتياطي للإعدادات",
            callback_data="founder:backup",
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


def founder_apply_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "تعيين رسالة الترحيب",
            callback_data="f:apply_set_welcome",
        )],
        [InlineKeyboardButton(
            "ترتيب أسئلة التقديم",
            callback_data="f:apply_order",
        )],
        [InlineKeyboardButton(
            "معاينة رسالة الترحيب",
            callback_data="f:apply_preview",
        )],
        [InlineKeyboardButton(
            "رجوع",
            callback_data="founder:home",
        )],
    ])


def founder_backup_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "تصدير الإعدادات (نسخ احتياطي)",
            callback_data="f:backup_export",
        )],
        [InlineKeyboardButton(
            "استيراد الإعدادات (استرجاع)",
            callback_data="f:backup_import",
        )],
        [InlineKeyboardButton(
            "رجوع",
            callback_data="founder:home",
        )],
    ])


def apply_order_keyboard():
    order = apply_question_order()
    rows = []

    for i, key in enumerate(order):
        label = APPLY_QUESTIONS[key]
        rows.append([InlineKeyboardButton(
            f"{i + 1}. {label}",
            callback_data="noop",
        )])

        arrows = []
        if i > 0:
            arrows.append(InlineKeyboardButton(
                "أعلى",
                callback_data=f"f:apply_order:up:{key}",
            ))
        if i < len(order) - 1:
            arrows.append(InlineKeyboardButton(
                "أسفل",
                callback_data=f"f:apply_order:down:{key}",
            ))
        if arrows:
            rows.append(arrows)

    rows.append([InlineKeyboardButton(
        "رجوع",
        callback_data="founder:apply",
    )])

    return InlineKeyboardMarkup(rows)


def welcome_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "رابط التيم",
            url=TEAM_LINK_URL,
        )],
        [InlineKeyboardButton(
            "التقديم",
            callback_data="apply:start",
        )],
    ])


def department_keyboard():
    rows = [
        [InlineKeyboardButton(label, callback_data=f"apply_dept:{key}")]
        for key, label in DEPARTMENT_OPTIONS.items()
    ]
    rows.append([InlineKeyboardButton("إلغاء", callback_data="apply_cancel")])
    return InlineKeyboardMarkup(rows)


def cancel_apply_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("إلغاء", callback_data="apply_cancel")]
    ])


def application_keyboard(app_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "قبول",
            callback_data=f"app_approve:{app_id}",
        ),
        InlineKeyboardButton(
            "رفض",
            callback_data=f"app_reject:{app_id}",
        ),
    ]])

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


def format_application(app_row):
    answers = json.loads(app_row["answers_json"])

    lines = ["طلب تقديم جديد", ""]
    lines.append(f"الاسم: {app_row['full_name'] or 'غير متوفر'}")
    lines.append(
        f"اليوزر: @{app_row['username']}"
        if app_row["username"]
        else "اليوزر: غير متوفر"
    )
    lines.append(f"الايدي: {app_row['user_id']}")
    lines.append(
        "القسم: "
        + DEPARTMENT_OPTIONS.get(app_row["department"], app_row["department"] or "غير محدد")
    )
    lines.append("")

    for key, label in APPLY_QUESTIONS.items():
        if key == "department":
            continue
        if key in answers:
            lines.append(f"{label}")
            lines.append(str(answers[key]))
            lines.append("")

    lines.append(f"رقم الطلب: {app_row['id']}")
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
        # المستخدم ليس عضوًا: نعرض له رسالة الترحيب وخيار التقديم
        # بدل رسالة "لا تملك صلاحية" فقط.
        await send_welcome_content(context.bot, update.effective_chat.id)
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
        # الشدة تُرسل إلى كروب المنصة المخصص فقط، وليس إلى الكروب العام.
        message = await context.bot.send_message(
            group["chat_id"],
            format_request(request),
        )

        db.set_group_message(
            request_id,
            group["chat_id"],
            message.message_id,
        )

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

        # نقفل الكروب العام فقط (بدون إرسال نسخة من الشدة إليه) طوال مدة الشد.
        common = db.get_group("common")
        if common:
            try:
                await lock_common_group(
                    context.bot,
                    common["chat_id"],
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
                    "تم نزول الشدة، لكن تعذر قفل الكتابة في الكروب العام. "
                    "تأكد أن البوت Administrator ولديه صلاحية تقييد الأعضاء."
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
# Apply-to-team feature (التقديم على التيم)
# ============================================================

def apply_question_order():
    raw = db.get_setting("apply_question_order")

    if raw:
        try:
            order = json.loads(raw)
            if (
                isinstance(order, list)
                and set(order) == set(APPLY_QUESTIONS.keys())
            ):
                return order
        except Exception:
            pass

    return list(APPLY_QUESTIONS.keys())


async def send_welcome_content(bot, chat_id):
    media_type = db.get_setting("welcome_media_type", "text")
    caption = db.get_setting("welcome_caption", "") or (
        "مرحبًا بك، اضغط على التقديم للانضمام إلى التيم."
    )
    file_id = db.get_setting("welcome_media_file_id", "")

    try:
        if media_type == "photo" and file_id:
            await bot.send_photo(
                chat_id,
                file_id,
                caption=caption,
                reply_markup=welcome_keyboard(),
            )
        elif media_type == "video" and file_id:
            await bot.send_video(
                chat_id,
                file_id,
                caption=caption,
                reply_markup=welcome_keyboard(),
            )
        else:
            await bot.send_message(
                chat_id,
                caption,
                reply_markup=welcome_keyboard(),
            )
    except TelegramError as exc:
        db.log_event(
            "ERROR",
            "send_welcome_failed",
            None,
            None,
            str(exc),
        )


async def send_next_apply_question(bot, chat_id, user_id):
    session = db.get_session(user_id)

    if not session or session[0] != APPLY_STATE:
        return

    data = session[1]
    order = data.get("order", [])
    idx = data.get("idx", 0)

    if idx >= len(order):
        await finalize_application(bot, user_id, data)
        return

    key = order[idx]

    if key == "department":
        await bot.send_message(
            chat_id,
            APPLY_QUESTIONS[key],
            reply_markup=department_keyboard(),
        )
    else:
        await bot.send_message(
            chat_id,
            APPLY_QUESTIONS[key],
            reply_markup=cancel_apply_keyboard(),
        )


async def finalize_application(bot, user_id, data):
    db.clear_session(user_id)

    answers = data.get("answers", {})
    department = answers.get("department", "both")
    username = data.get("username")
    full_name = data.get("full_name") or str(user_id)

    app_id = db.create_application(
        user_id,
        username,
        full_name,
        department,
        answers,
    )

    app_row = db.get_application(app_id)

    try:
        await bot.send_message(
            FOUNDER_ID,
            format_application(app_row),
            reply_markup=application_keyboard(app_id),
        )
    except TelegramError as exc:
        db.log_event(
            "ERROR",
            "notify_founder_application_failed",
            user_id,
            app_id,
            str(exc),
        )

    await bot.send_message(
        user_id,
        "تم إرسال طلب التقديم، الرجاء انتظار الرد من الإدارة.",
    )


async def make_invite_link(bot, chat_id):
    try:
        invite = await bot.create_chat_invite_link(
            chat_id,
            member_limit=1,
        )
        return invite.invite_link
    except TelegramError as exc:
        db.log_event(
            "ERROR",
            "invite_link_failed",
            None,
            None,
            str(exc),
        )
        return None


async def apply_start_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    if db.get_user(user_id):
        await query.answer(
            "أنت عضو بالفعل.",
            show_alert=True,
        )
        return

    if db.get_pending_application(user_id):
        await query.answer(
            "لديك طلب تقديم قيد المراجعة بالفعل.",
            show_alert=True,
        )
        return

    await query.answer()

    order = apply_question_order()

    db.set_session(
        user_id,
        APPLY_STATE,
        {
            "order": order,
            "idx": 0,
            "answers": {},
            "username": query.from_user.username,
            "full_name": query.from_user.full_name,
        },
    )

    await query.message.reply_text(
        "سيتم الآن سؤالك عدة أسئلة للتقديم على التيم."
    )

    await send_next_apply_question(
        context.bot,
        query.message.chat_id,
        user_id,
    )


async def apply_text_router(update, context):
    if not update.message or not update.message.text:
        return False

    user_id = update.effective_user.id
    session = db.get_session(user_id)

    if not session or session[0] != APPLY_STATE:
        return False

    data = session[1]
    order = data.get("order", [])
    idx = data.get("idx", 0)

    if idx >= len(order):
        return False

    key = order[idx]

    if key == "department":
        await update.message.reply_text(
            "الرجاء اختيار القسم من الأزرار."
        )
        return True

    text = update.message.text.strip()

    if not text:
        await update.message.reply_text(
            "الرجاء إرسال إجابة نصية."
        )
        return True

    data.setdefault("answers", {})[key] = text
    data["idx"] = idx + 1

    db.set_session(user_id, APPLY_STATE, data)

    await send_next_apply_question(
        context.bot,
        update.effective_chat.id,
        user_id,
    )
    return True


async def apply_department_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    session = db.get_session(user_id)

    if not session or session[0] != APPLY_STATE:
        await query.answer()
        return

    data = session[1]
    order = data.get("order", [])
    idx = data.get("idx", 0)

    if idx >= len(order) or order[idx] != "department":
        await query.answer()
        return

    value = query.data.split(":", 1)[1]

    if value not in DEPARTMENT_OPTIONS:
        await query.answer()
        return

    await query.answer()

    data.setdefault("answers", {})["department"] = value
    data["idx"] = idx + 1

    db.set_session(user_id, APPLY_STATE, data)

    await query.edit_message_reply_markup(reply_markup=None)

    await send_next_apply_question(
        context.bot,
        query.message.chat_id,
        user_id,
    )


async def apply_cancel_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    db.clear_session(user_id)

    await query.answer("تم إلغاء التقديم.")

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass


async def application_approve_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    if not is_founder(user_id):
        await query.answer(
            "لا تملك صلاحية الإدارة.",
            show_alert=True,
        )
        return

    try:
        app_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer(
            "رقم الطلب غير صحيح.",
            show_alert=True,
        )
        return

    app_row = db.get_application(app_id)

    if not app_row:
        await query.answer(
            "الطلب غير موجود.",
            show_alert=True,
        )
        return

    if app_row["status"] != "PENDING":
        await query.answer(
            "تمت معالجة الطلب بالفعل.",
            show_alert=True,
        )
        return

    db.set_application_status(app_id, "APPROVED", user_id)

    db.add_user(
        app_row["user_id"],
        app_row["username"],
        app_row["full_name"] or str(app_row["user_id"]),
        "member",
    )

    department = app_row["department"]
    target_platforms = []

    if department in ("instagram", "both"):
        target_platforms.append("instagram")
    if department in ("telegram", "both"):
        target_platforms.append("telegram")
    if department not in ("instagram", "telegram", "both"):
        target_platforms = list(PLATFORMS)

    links = []

    for platform in target_platforms:
        group = db.get_group(platform)
        if not group:
            continue
        link = await make_invite_link(context.bot, group["chat_id"])
        if link:
            links.append(f"كروب {platform}: {link}")

    common = db.get_group("common")
    if common:
        link = await make_invite_link(context.bot, common["chat_id"])
        if link:
            links.append(f"الكروب العام: {link}")

    message_lines = ["تهانينا، تم قبول طلبك في التيم."]

    if links:
        message_lines.append("")
        message_lines.extend(links)
    else:
        message_lines.append(
            "سيتم تزويدك بروابط الكروبات قريبًا."
        )

    try:
        await context.bot.send_message(
            app_row["user_id"],
            "\n".join(message_lines),
        )
    except TelegramError as exc:
        db.log_event(
            "ERROR",
            "notify_applicant_approved_failed",
            user_id,
            app_id,
            str(exc),
        )

    await query.edit_message_reply_markup(reply_markup=None)
    await query.answer("تم قبول الطلب.")


async def application_reject_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    if not is_founder(user_id):
        await query.answer(
            "لا تملك صلاحية الإدارة.",
            show_alert=True,
        )
        return

    try:
        app_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer(
            "رقم الطلب غير صحيح.",
            show_alert=True,
        )
        return

    app_row = db.get_application(app_id)

    if not app_row:
        await query.answer(
            "الطلب غير موجود.",
            show_alert=True,
        )
        return

    if app_row["status"] != "PENDING":
        await query.answer(
            "تمت معالجة الطلب بالفعل.",
            show_alert=True,
        )
        return

    db.set_application_status(app_id, "REJECTED", user_id)

    try:
        await context.bot.send_message(
            app_row["user_id"],
            "نأسف، تم رفض طلب التقديم الخاص بك.",
        )
    except TelegramError as exc:
        db.log_event(
            "ERROR",
            "notify_applicant_rejected_failed",
            user_id,
            app_id,
            str(exc),
        )

    await query.edit_message_reply_markup(reply_markup=None)
    await query.answer("تم رفض الطلب.")


async def noop_callback(update, context):
    await update.callback_query.answer()


async def apply_order_move_callback(update, context):
    query = update.callback_query

    if not is_founder(query.from_user.id):
        await query.answer(
            "لا تملك صلاحية الإدارة.",
            show_alert=True,
        )
        return

    await query.answer()

    _, _, action, key = query.data.split(":", 3)
    order = apply_question_order()

    if key not in order:
        return

    idx = order.index(key)
    swap_idx = idx - 1 if action == "up" else idx + 1

    if 0 <= swap_idx < len(order):
        order[idx], order[swap_idx] = order[swap_idx], order[idx]
        db.set_setting(
            "apply_question_order",
            json.dumps(order, ensure_ascii=False),
        )

    await query.edit_message_reply_markup(
        reply_markup=apply_order_keyboard()
    )


async def founder_apply_menu_callback_entry(query):
    """Shared body used from founder_callback for the 'founder:apply' key."""
    await query.edit_message_text(
        "إعدادات ميزة التقديم",
        reply_markup=founder_apply_keyboard(),
    )


async def founder_apply_action_callback(update, context):
    query = update.callback_query

    if not is_founder(query.from_user.id):
        await query.answer(
            "لا تملك صلاحية الإدارة.",
            show_alert=True,
        )
        return

    await query.answer()

    action = query.data.split(":", 1)[1]

    if action == "set_welcome":
        db.set_session(query.from_user.id, "FOUNDER_SET_WELCOME", {})
        await query.edit_message_text(
            "أرسل رسالة الترحيب الآن.\n"
            "يمكنك إرسال صورة أو فيديو مع نص (Caption)، أو إرسال نص فقط."
        )
        return

    if action == "order":
        await query.edit_message_text(
            "رتب أسئلة التقديم كما تريد:",
            reply_markup=apply_order_keyboard(),
        )
        return

    if action == "preview":
        await send_welcome_content(context.bot, query.message.chat_id)
        return


async def founder_welcome_media_router(update, context):
    if not update.message:
        return

    user_id = update.effective_user.id

    if not is_founder(user_id):
        return

    session = db.get_session(user_id)

    if not session or session[0] != "FOUNDER_SET_WELCOME":
        return

    caption = update.message.caption or ""

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        db.set_setting("welcome_media_type", "photo")
        db.set_setting("welcome_media_file_id", file_id)
        db.set_setting("welcome_caption", caption)

    elif update.message.video:
        file_id = update.message.video.file_id
        db.set_setting("welcome_media_type", "video")
        db.set_setting("welcome_media_file_id", file_id)
        db.set_setting("welcome_caption", caption)

    else:
        return

    db.clear_session(user_id)

    await update.message.reply_text(
        "تم حفظ رسالة الترحيب.",
        reply_markup=founder_apply_keyboard(),
    )


async def founder_backup_action_callback(update, context):
    query = update.callback_query

    if not is_founder(query.from_user.id):
        await query.answer(
            "لا تملك صلاحية الإدارة.",
            show_alert=True,
        )
        return

    await query.answer()

    action = query.data.split(":", 1)[1]

    if action == "backup_export":
        data = db.export_config()
        blob = encode_config_blob(data)

        if len(blob) <= CONFIG_BACKUP_INLINE_LIMIT:
            await context.bot.send_message(
                query.from_user.id,
                "احفظ هذا الكود في مكان آمن. استخدمه لاسترجاع إعدادات "
                "البوت في حال تغيير الاستضافة:\n\n"
                f"<code>{blob}</code>",
                parse_mode="HTML",
            )
        else:
            buffer = BytesIO(blob.encode("ascii"))
            buffer.name = "shd_bot_backup.txt"
            await context.bot.send_document(
                query.from_user.id,
                buffer,
                caption=(
                    "احفظ هذا الملف في مكان آمن. استخدمه لاسترجاع "
                    "إعدادات البوت في حال تغيير الاستضافة."
                ),
            )
        return

    if action == "backup_import":
        db.set_session(query.from_user.id, "FOUNDER_IMPORT_CONFIG", {})
        await query.edit_message_text(
            "أرسل كود الاسترجاع الآن، إما كنص أو كملف .txt."
        )
        return


async def apply_config_code(bot, user_id, raw_text):
    try:
        data = decode_config_blob(raw_text)
        db.import_config(data)
    except Exception as exc:
        db.log_event(
            "ERROR",
            "import_config_failed",
            user_id,
            None,
            str(exc),
        )
        await bot.send_message(
            user_id,
            "الكود غير صالح أو تالف. تأكد من نسخه أو رفعه كاملًا.",
        )
        return False

    await bot.send_message(
        user_id,
        "تم استرجاع إعدادات البوت بنجاح.",
    )
    return True


async def founder_import_document_router(update, context):
    if not update.message or not update.message.document:
        return

    user_id = update.effective_user.id

    if not is_founder(user_id):
        return

    session = db.get_session(user_id)

    if not session or session[0] != "FOUNDER_IMPORT_CONFIG":
        return

    try:
        file = await update.message.document.get_file()
        raw_bytes = await file.download_as_bytearray()
        text = bytes(raw_bytes).decode("utf-8", errors="strict")
    except Exception as exc:
        db.log_event(
            "ERROR",
            "import_config_file_failed",
            user_id,
            None,
            str(exc),
        )
        await update.message.reply_text("تعذر قراءة الملف.")
        return

    db.clear_session(user_id)
    await apply_config_code(context.bot, user_id, text)

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
        await send_welcome_content(context.bot, update.effective_chat.id)
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

    elif key == "founder:apply":
        await founder_apply_menu_callback_entry(query)

    elif key == "founder:backup":
        await query.edit_message_text(
            "نسخ احتياطي للإعدادات\n\n"
            "التصدير يعطيك كودًا يحفظ الأعضاء والمشرفين والكروبات "
            "وإعدادات ميزة التقديم. لا يشمل سجل الطلبات القديمة.",
            reply_markup=founder_backup_keyboard(),
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

    if state == "FOUNDER_SET_WELCOME":
        db.set_setting("welcome_media_type", "text")
        db.set_setting("welcome_media_file_id", "")
        db.set_setting("welcome_caption", text)

        db.clear_session(user_id)

        await update.message.reply_text(
            "تم حفظ رسالة الترحيب.",
            reply_markup=founder_apply_keyboard(),
        )

        return True

    if state == "FOUNDER_IMPORT_CONFIG":
        db.clear_session(user_id)
        await apply_config_code(context.bot, user_id, text)
        return True

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

    if await apply_text_router(update, context):
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
        CallbackQueryHandler(
            founder_apply_action_callback,
            pattern=r"^f:apply_(set_welcome|order|preview)$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            founder_backup_action_callback,
            pattern=r"^f:backup_(export|import)$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            apply_order_move_callback,
            pattern=r"^f:apply_order:(up|down):.+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            noop_callback,
            pattern=r"^noop$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            apply_start_callback,
            pattern=r"^apply:start$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            apply_department_callback,
            pattern=r"^apply_dept:(instagram|telegram|both)$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            apply_cancel_callback,
            pattern=r"^apply_cancel$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            application_approve_callback,
            pattern=r"^app_approve:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            application_reject_callback,
            pattern=r"^app_reject:\d+$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & (filters.PHOTO | filters.VIDEO)
            & ~filters.COMMAND,
            founder_welcome_media_router,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.Document.ALL & ~filters.COMMAND,
            founder_import_document_router,
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
