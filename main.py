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
import asyncio
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
    ChatJoinRequestHandler,
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

PLATFORM_LABELS = {
    "telegram": "تيليجرام",
    "instagram": "انستقرام",
    "tiktok": "تيك توك",
}

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
    "programming": "البرمجة",
    "band": "الباند",
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

            CREATE TABLE IF NOT EXISTS admin_platforms (
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, platform),
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                audience TEXT NOT NULL,
                department TEXT,
                platform TEXT,
                text TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                sent_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                meeting_time TEXT NOT NULL,
                details TEXT,
                audience TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'SCHEDULED'
            );

            CREATE TABLE IF NOT EXISTS templates (
                key TEXT PRIMARY KEY,
                text TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS member_invites (
                invite_link TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_platforms (
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(user_id, platform),
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            """)

            # ترقية آمنة لقاعدة البيانات القديمة دون حذف أي إعدادات.
            cols = {r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
            if "department" not in cols:
                con.execute("ALTER TABLE users ADD COLUMN department TEXT")

            for platform in PLATFORMS:
                con.execute(
                    "INSERT OR IGNORE INTO platforms(name, enabled) VALUES(?, 1)",
                    (platform,),
                )

            # نقل صلاحيات admins القديمة إلى الجدول الجديد متعدد الصلاحيات.
            old_admins = con.execute("SELECT user_id, platform, active, created_at FROM admins").fetchall()
            for a in old_admins:
                con.execute(
                    "INSERT OR IGNORE INTO admin_platforms(user_id, platform, active, created_at) VALUES(?,?,?,?)",
                    (a[0], a[1], a[2], a[3]),
                )

            defaults = {
                "template_shd_start": "نزل شد جديد على {platform}.\nتوجهوا إلى قسم {platform} وشاركوا في الشدة.",
                "template_shd_finish": "انتهى الشد وتم فتح الكروب العام.",
                "template_meeting": "اجتماع جديد\nالعنوان: {title}\nالوقت: {time}\n{details}",
                "template_alert": "تنبيه من إدارة التيم:\n{text}",
            }
            for k, v in defaults.items():
                con.execute("INSERT OR IGNORE INTO templates(key,text) VALUES(?,?)", (k,v))

    def log_event(self, level, event, user_id=None, request_id=None, details=None):
        # متعمد: لا يتم تخزين Logs لتخفيف SQLite وRailway المجاني.
        return None

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
            con.execute("UPDATE admins SET active=0 WHERE user_id=?", (user_id,))
            con.execute("UPDATE admin_platforms SET active=0 WHERE user_id=?", (user_id,))
            con.execute("UPDATE member_invites SET active=0 WHERE user_id=?", (user_id,))
            con.execute("UPDATE user_platforms SET active=0 WHERE user_id=?", (user_id,))

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
                "INSERT OR IGNORE INTO admin_platforms(user_id,platform,active,created_at) VALUES(?,?,1,?)",
                (user_id, platform, now_iso()),
            )
            con.execute(
                "UPDATE admin_platforms SET active=1 WHERE user_id=? AND platform=?",
                (user_id, platform),
            )
            con.execute(
                "UPDATE users SET role='admin', active=1 WHERE user_id=?",
                (user_id,),
            )

    def remove_admin(self, user_id, platform=None):
        with self.conn() as con:
            if platform:
                con.execute("UPDATE admin_platforms SET active=0 WHERE user_id=? AND platform=?", (user_id, platform))
            else:
                con.execute("UPDATE admin_platforms SET active=0 WHERE user_id=?", (user_id,))
            remaining = con.execute("SELECT 1 FROM admin_platforms WHERE user_id=? AND active=1 LIMIT 1", (user_id,)).fetchone()
            if not remaining:
                con.execute("UPDATE users SET role='member' WHERE user_id=? AND active=1", (user_id,))

    def get_admin(self, user_id, platform=None):
        with self.conn() as con:
            if platform:
                return con.execute(
                    "SELECT * FROM admin_platforms WHERE user_id=? AND platform=? AND active=1",
                    (user_id, platform),
                ).fetchone()
            return con.execute(
                "SELECT * FROM admin_platforms WHERE user_id=? AND active=1 LIMIT 1",
                (user_id,),
            ).fetchone()

    def list_admins(self):
        with self.conn() as con:
            return con.execute(
                """SELECT a.user_id, a.platform, a.active, a.created_at, u.username, u.full_name
                   FROM admin_platforms a JOIN users u ON u.user_id=a.user_id
                   WHERE a.active=1 ORDER BY a.user_id, a.platform"""
            ).fetchall()

    def add_member_invite(self, invite_link, user_id, platform, chat_id):
        with self.conn() as con:
            con.execute("INSERT OR REPLACE INTO member_invites(invite_link,user_id,platform,chat_id,active,created_at) VALUES(?,?,?,?,1,?)", (invite_link,user_id,platform,chat_id,now_iso()))

    def get_member_invite(self, invite_link):
        with self.conn() as con:
            return con.execute("SELECT * FROM member_invites WHERE invite_link=? AND active=1", (invite_link,)).fetchone()

    def deactivate_member_invite(self, invite_link):
        with self.conn() as con:
            con.execute("UPDATE member_invites SET active=0 WHERE invite_link=?", (invite_link,))

    def deactivate_user_invites(self, user_id):
        with self.conn() as con:
            con.execute("UPDATE member_invites SET active=0 WHERE user_id=?", (user_id,))

    def add_user_platform(self, user_id, platform):
        with self.conn() as con:
            con.execute("INSERT INTO user_platforms(user_id,platform,active) VALUES(?,?,1) ON CONFLICT(user_id,platform) DO UPDATE SET active=1", (user_id,platform))

    def get_platform_members(self, platform):
        with self.conn() as con:
            return con.execute("SELECT user_id FROM user_platforms WHERE platform=? AND active=1", (platform,)).fetchall()

    def set_department(self, user_id, department):
        with self.conn() as con:
            con.execute("UPDATE users SET department=? WHERE user_id=?", (department, user_id))

    def get_template(self, key, default=""):
        with self.conn() as con:
            row=con.execute("SELECT text FROM templates WHERE key=?", (key,)).fetchone()
            return row["text"] if row else default

    def set_template(self, key, value):
        with self.conn() as con:
            con.execute("INSERT INTO templates(key,text) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET text=excluded.text", (key,value))

    def create_announcement(self, kind, audience, text, created_by, department=None, platform=None):
        with self.conn() as con:
            cur=con.execute("INSERT INTO announcements(kind,audience,department,platform,text,created_by,created_at) VALUES(?,?,?,?,?,?,?)", (kind,audience,department,platform,text,created_by,now_iso()))
            return cur.lastrowid

    def update_announcement_count(self, ann_id, count):
        with self.conn() as con:
            con.execute("UPDATE announcements SET sent_count=? WHERE id=?", (count,ann_id))

    def list_announcements(self, limit=20):
        with self.conn() as con:
            return con.execute("SELECT * FROM announcements ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def create_meeting(self, title, meeting_time, details, audience, created_by):
        with self.conn() as con:
            cur=con.execute("INSERT INTO meetings(title,meeting_time,details,audience,created_by,created_at) VALUES(?,?,?,?,?,?)", (title,meeting_time,details,audience,created_by,now_iso()))
            return cur.lastrowid

    def get_meeting(self, meeting_id):
        with self.conn() as con:
            return con.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()

    def list_meetings(self, limit=20):
        with self.conn() as con:
            return con.execute("SELECT * FROM meetings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def get_recipients(self, audience, department=None, platform=None):
        with self.conn() as con:
            if audience == "all":
                return con.execute("SELECT user_id FROM users WHERE active=1").fetchall()
            if audience == "department":
                return con.execute("SELECT user_id FROM users WHERE active=1 AND department=?", (department,)).fetchall()
            if audience == "platform":
                return con.execute("SELECT user_id FROM user_platforms WHERE active=1 AND platform=?", (platform,)).fetchall()
            return []

    def count_active_users(self):
        with self.conn() as con:
            return con.execute("SELECT COUNT(*) n FROM users WHERE active=1").fetchone()["n"]

    def count_active_admins(self):
        with self.conn() as con:
            return con.execute("SELECT COUNT(DISTINCT user_id) n FROM admin_platforms WHERE active=1").fetchone()["n"]

    def application_stats(self):
        with self.conn() as con:
            return con.execute("SELECT status, COUNT(*) n FROM applications GROUP BY status").fetchall()

    def remove_user_everywhere_data(self, user_id):
        self.remove_user(user_id)

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
                    "SELECT user_id,platform FROM admin_platforms WHERE active=1"
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
            templates = [dict(r) for r in con.execute("SELECT key,text FROM templates")]
            announcements = [dict(r) for r in con.execute("SELECT kind,audience,department,platform,text,created_by,sent_count,created_at FROM announcements ORDER BY id DESC LIMIT 30")]
            user_platforms = [dict(r) for r in con.execute("SELECT user_id,platform,active FROM user_platforms WHERE active=1")]

        return {
            "version": 2,
            "exported_at": now_iso(),
            "users": users,
            "admins": admins,
            "groups": groups,
            "platforms": platforms,
            "settings": settings,
            "templates": templates,
            "announcements": announcements,
            "user_platforms": user_platforms,
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
                if "department" in u:
                    con.execute("UPDATE users SET department=? WHERE user_id=?", (u.get("department"), u["user_id"]))

            for a in data.get("admins", []):
                if "user_id" not in a or "platform" not in a:
                    continue
                con.execute(
                    """INSERT INTO admin_platforms(user_id,platform,active,created_at)
                       VALUES(?,?,1,?)
                       ON CONFLICT(user_id,platform) DO UPDATE SET active=1""",
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

            for up in data.get("user_platforms", []):
                if "user_id" in up and "platform" in up:
                    con.execute("INSERT INTO user_platforms(user_id,platform,active) VALUES(?,?,1) ON CONFLICT(user_id,platform) DO UPDATE SET active=1", (up["user_id"], up["platform"]))


# ============================================================
# Lightweight team management helpers
# ============================================================

ANNOUNCEMENT_KINDS = {
    "shd": "خبر شد",
    "meeting": "اجتماع",
    "alert": "تنبيه",
    "general": "إعلان عام",
    "department": "إعلان لقسم",
}
AUDIENCES = {
    "all": "جميع التيم",
    "department": "قسم",
    "platform": "منصة",
}
TEAM_STATUS = {
    "available": "متاح",
    "shd": "يوجد شد",
    "meeting": "اجتماع",
    "emergency": "طوارئ",
    "closed": "مغلق",
}

def get_team_status():
    return db.get_setting("team_status", "available")

def set_team_status(value):
    if value in TEAM_STATUS:
        db.set_setting("team_status", value)

def feature_enabled(key, default="1"):
    return db.get_setting(key, default) == "1"

async def send_announcement(bot, kind, audience, text, created_by, department=None, platform=None):
    ann_id=db.create_announcement(kind,audience,text,created_by,department,platform)
    recipients=db.get_recipients(audience,department,platform)
    sent=0
    for row in recipients:
        try:
            await bot.send_message(row["user_id"], text)
            sent += 1
        except TelegramError:
            pass
    db.update_announcement_count(ann_id,sent)
    return ann_id,sent

async def notify_shd(bot, platform, started=True):
    if not feature_enabled("shd_notifications", "1"):
        return
    label=PLATFORM_LABELS.get(platform,platform)
    if started:
        text=db.get_template("template_shd_start", "نزل شد جديد على {platform}.\nتوجهوا إلى قسم {platform} وشاركوا في الشدة.").format(platform=label)
        await send_announcement(bot,"shd","all",text,FOUNDER_ID)
    else:
        text=db.get_template("template_shd_finish", "انتهى الشد وتم فتح الكروب العام.")
        await send_announcement(bot,"shd","all",text,FOUNDER_ID)

async def schedule_meeting_reminders(bot, meeting_id):
    meeting=db.get_meeting(meeting_id)
    if not meeting:
        return
    try:
        dt=datetime.fromisoformat(meeting["meeting_time"])
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=timezone.utc)
    except Exception:
        return
    now=datetime.now(timezone.utc)
    for minutes in (60,10):
        delay=(dt-now).total_seconds()-minutes*60
        if delay <= 0:
            continue
        async def reminder(delay_seconds=delay, mins=minutes):
            await asyncio.sleep(delay_seconds)
            m=db.get_meeting(meeting_id)
            if not m or m["status"] != "SCHEDULED":
                return
            audience=m["audience"]
            text=f"تذكير باجتماع التيم\nالعنوان: {m['title']}\nالوقت: {m['meeting_time']}\nمتبقي: {mins} دقيقة"
            if m["details"]:
                text += "\n" + m["details"]
            await send_announcement(bot,"meeting",audience,text,FOUNDER_ID)
        asyncio.create_task(reminder())



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
        [InlineKeyboardButton("إدارة التيم", callback_data="founder:team"), InlineKeyboardButton("إدارة الشد", callback_data="founder:shd")],
        [InlineKeyboardButton("الإعلانات", callback_data="founder:announcements"), InlineKeyboardButton("الاجتماعات", callback_data="founder:meetings")],
        [InlineKeyboardButton("التقديم", callback_data="founder:apply_menu"), InlineKeyboardButton("الكروبات", callback_data="founder:groups")],
        [InlineKeyboardButton("الإحصائيات", callback_data="founder:stats"), InlineKeyboardButton("الإعدادات", callback_data="founder:settings")],
        [InlineKeyboardButton("النسخ الاحتياطي", callback_data="founder:backup")],
    ])

def team_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("الأعضاء", callback_data="f:team_users"), InlineKeyboardButton("المشرفون والصلاحيات", callback_data="f:team_admins")],
        [InlineKeyboardButton("حالة التيم", callback_data="f:team_status")],
        [InlineKeyboardButton("رجوع", callback_data="founder:home")],
    ])

def status_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("متاح", callback_data="f:status:available"), InlineKeyboardButton("يوجد شد", callback_data="f:status:shd")],
        [InlineKeyboardButton("اجتماع", callback_data="f:status:meeting"), InlineKeyboardButton("طوارئ", callback_data="f:status:emergency")],
        [InlineKeyboardButton("مغلق", callback_data="f:status:closed")],
        [InlineKeyboardButton("رجوع", callback_data="founder:team")],
    ])

def announcements_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("إعلان جديد", callback_data="f:ann_new")],
        [InlineKeyboardButton("الإعلانات المرسلة", callback_data="f:ann_list")],
        [InlineKeyboardButton("القوالب", callback_data="f:templates")],
        [InlineKeyboardButton("رجوع", callback_data="founder:home")],
    ])

def meetings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("اجتماع جديد", callback_data="f:meeting_new")],
        [InlineKeyboardButton("الاجتماعات", callback_data="f:meeting_list")],
        [InlineKeyboardButton("رجوع", callback_data="founder:home")],
    ])

def announcement_kind_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(v, callback_data=f"f:ann_kind:{k}")] for k,v in ANNOUNCEMENT_KINDS.items()] + [[InlineKeyboardButton("إلغاء", callback_data="founder:announcements")]])

def audience_keyboard(kind):
    rows=[[InlineKeyboardButton("جميع التيم", callback_data=f"f:ann_aud:{kind}:all")],[InlineKeyboardButton("البرمجة", callback_data=f"f:ann_aud:{kind}:dept:programming"), InlineKeyboardButton("الباند", callback_data=f"f:ann_aud:{kind}:dept:band")],[InlineKeyboardButton("Telegram", callback_data=f"f:ann_aud:{kind}:platform:telegram"), InlineKeyboardButton("Instagram", callback_data=f"f:ann_aud:{kind}:platform:instagram"), InlineKeyboardButton("TikTok", callback_data=f"f:ann_aud:{kind}:platform:tiktok")]]
    rows.append([InlineKeyboardButton("رجوع", callback_data="founder:announcements")])
    return InlineKeyboardMarkup(rows)


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
        [InlineKeyboardButton("قائمة الأعضاء", callback_data="f:list_users")],
        [InlineKeyboardButton("رابط دخول عضو", callback_data="f:member_link")],
        [InlineKeyboardButton("تعيين قسم لعضو", callback_data="f:set_department")],
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
    db.add_user_platform(user_id, platform)

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

        # نقفل الكروب العام فقط (بدون إرسال نسخة من الشدة إليه) طوال مدة الشد،
        # ونرسل تنبيهًا للأعضاء يوجههم إلى كروب المنصة المخصص.
        common = db.get_group("common")
        if common:
            try:
                await lock_common_group(
                    context.bot,
                    common["chat_id"],
                )

                platform_label = PLATFORM_LABELS.get(
                    request["platform"],
                    request["platform"],
                )

                try:
                    await context.bot.send_message(
                        common["chat_id"],
                        (
                            "تم قفل الدردشة، نزلت شدّة، الكل يتوجه لقسم "
                            f"\"{platform_label}\"، حطوا لايك على الشدة "
                            "بعدما تخلص."
                        ),
                    )
                except TelegramError as exc:
                    db.log_event(
                        "ERROR",
                        "common_group_notice_failed",
                        user_id,
                        request_id,
                        str(exc),
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
        set_team_status("shd")
        await notify_shd(context.bot, request["platform"], True)

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
    set_team_status("available")
    await notify_shd(context.bot, request["platform"], False)

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


async def make_member_invite(bot, chat_id, user_id, platform):
    try:
        invite = await bot.create_chat_invite_link(
            chat_id,
            creates_join_request=True,
            name=f"member-{user_id}-{platform}",
        )
        db.add_member_invite(invite.invite_link, user_id, platform, chat_id)
        return invite.invite_link
    except TelegramError:
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

    if department == "programming":
        db.set_department(app_row["user_id"], "programming")
        target_platforms = list(PLATFORMS)
    elif department == "band":
        db.set_department(app_row["user_id"], "band")
        target_platforms = list(PLATFORMS)
    elif department in ("instagram", "both"):
        target_platforms.append("instagram")
    if department in ("telegram", "both"):
        target_platforms.append("telegram")
    if not target_platforms:
        target_platforms = list(PLATFORMS)

    links = []

    for platform in target_platforms:
        group = db.get_group(platform)
        if not group:
            continue
        link = await make_member_invite(context.bot, group["chat_id"], app_row["user_id"], platform)
        if link:
            links.append(f"كروب {platform}: {link}")

    common = db.get_group("common")
    if common:
        link = await make_member_invite(context.bot, common["chat_id"], app_row["user_id"], COMMON_PLATFORM)
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


async def kick_member_from_all_groups(bot, user_id):
    removed=0
    for platform in (*PLATFORMS, COMMON_PLATFORM):
        group=db.get_group(platform)
        if not group:
            continue
        try:
            await bot.ban_chat_member(group["chat_id"], user_id)
            try:
                await bot.unban_chat_member(group["chat_id"], user_id, only_if_banned=True)
            except TelegramError:
                pass
            removed += 1
        except TelegramError:
            pass
    return removed

async def member_join_request(update, context):
    req=update.chat_join_request
    if not req:
        return
    invite=db.get_member_invite(req.invite_link.invite_link if req.invite_link else "")
    if not invite or not invite["active"] or invite["user_id"] != req.from_user.id or invite["chat_id"] != req.chat.id:
        try:
            await context.bot.decline_chat_join_request(req.chat.id, req.from_user.id)
        except TelegramError:
            pass
        return
    try:
        await context.bot.approve_chat_join_request(req.chat.id, req.from_user.id)
        db.deactivate_member_invite(invite["invite_link"])
        if invite["platform"] != COMMON_PLATFORM:
            db.add_user_platform(req.from_user.id, invite["platform"])
    except TelegramError:
        pass

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

    status=TEAM_STATUS.get(get_team_status(), "متاح")
    await update.message.reply_text(
        f"حالة التيم: {status}\n\nاختر المنصة.",
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

    if key == "founder:team":
        await query.edit_message_text("إدارة التيم", reply_markup=team_keyboard())
        return

    if key == "founder:shd":
        await query.edit_message_text("إدارة الشد", reply_markup=founder_platforms_keyboard())
        return

    if key == "founder:announcements":
        await query.edit_message_text("مركز الإعلانات", reply_markup=announcements_keyboard())
        return

    if key == "founder:meetings":
        await query.edit_message_text("نظام الاجتماعات", reply_markup=meetings_keyboard())
        return

    if key == "founder:apply_menu":
        await founder_apply_menu_callback_entry(query)
        return

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
        status=TEAM_STATUS.get(get_team_status(), "متاح")
        await query.edit_message_text(
            "الإعدادات\n\n"
            f"حالة التيم: {status}\n"
            f"تنبيهات الشد: {'مفعلة' if feature_enabled('shd_notifications') else 'متوقفة'}\n"
            "لا يتم حفظ Logs في قاعدة البيانات.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("تشغيل تنبيهات الشد", callback_data="f:toggle:shd:1"), InlineKeyboardButton("إيقاف تنبيهات الشد", callback_data="f:toggle:shd:0")],
                [InlineKeyboardButton("رجوع", callback_data="founder:home")],
            ]),
        )


async def feature_callback(update, context):
    query=update.callback_query
    if not is_founder(query.from_user.id):
        await query.answer("لا تملك صلاحية الإدارة.", show_alert=True); return
    await query.answer()
    parts=query.data.split(":")
    action=parts[1]
    if action=="toggle":
        if len(parts) >= 4 and parts[2]=="shd":
            db.set_setting("shd_notifications", parts[3])
        await query.edit_message_text("تم تحديث إعداد التنبيهات.", reply_markup=founder_keyboard()); return
    if action=="status":
        set_team_status(parts[2]); await query.edit_message_text(f"تم تغيير حالة التيم إلى: {TEAM_STATUS[parts[2]]}", reply_markup=status_keyboard()); return
    if action=="ann_kind":
        kind=parts[2]; await query.edit_message_text("اختر الجمهور:", reply_markup=audience_keyboard(kind)); return
    if action=="ann_aud":
        kind=parts[2]; typ=parts[3]; val=parts[4] if len(parts)>4 else None
        audience="all" if typ=="all" else ("department" if typ=="dept" else "platform")
        data={"kind":kind,"audience":audience,"department":val if typ=="dept" else None,"platform":val if typ=="platform" else None}
        db.set_session(query.from_user.id,"FOUNDER_ANN_TEXT",data)
        await query.edit_message_text("أرسل نص الإعلان الآن.", reply_markup=cancel_keyboard()); return
    if action=="template":
        key=parts[2]
        db.set_session(query.from_user.id,"FOUNDER_TEMPLATE_"+key,{})
        await query.edit_message_text("أرسل نص القالب الجديد. المتغيرات المتاحة تعتمد على القالب."); return

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
        "remove_admin": ("أرسل ID المشرف ثم المنصة بهذا الشكل:\n123456789 | telegram"),
        "member_link": ("أرسل ID العضو لإصدار روابط دخول خاصة له."),
        "set_department": ("أرسل ID العضو ثم القسم: programming أو band"),
        "meeting_new": ("أرسل بيانات الاجتماع بهذا الشكل:\nالعنوان | 2026-09-13T20:00:00+03:00 | التفاصيل | all\nالجمهور: all أو programming أو band أو telegram أو instagram أو tiktok"),
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

    if action == "team_users":
        rows=db.list_users()
        text="الأعضاء:\n\n"+"\n".join(f"{r['user_id']} | {r['full_name']} | {r['department'] or 'غير محدد'} | {r['role']}" for r in rows)
        await query.edit_message_text(text or "لا يوجد أعضاء.", reply_markup=team_keyboard())
        return

    if action == "team_admins":
        rows=db.list_admins()
        text="المشرفون والصلاحيات:\n\n"+"\n".join(f"{r['user_id']} | {r['full_name']} | {r['platform']}" for r in rows)
        await query.edit_message_text(text or "لا يوجد مشرفون.", reply_markup=team_keyboard())
        return

    if action == "team_status":
        await query.edit_message_text(f"حالة التيم الحالية: {TEAM_STATUS.get(get_team_status(),'متاح')}", reply_markup=status_keyboard())
        return

    if action == "status":
        return

    if action == "member_link":
        db.set_session(user_id,"FOUNDER_MEMBER_LINK",{})
        await query.edit_message_text("أرسل ID العضو لإصدار روابط الدخول.")
        return

    if action == "set_department":
        db.set_session(user_id,"FOUNDER_SET_DEPARTMENT",{})
        await query.edit_message_text("أرسل ID العضو ثم القسم: programming أو band")
        return

    if action == "ann_new":
        await query.edit_message_text("اختر نوع الإعلان:", reply_markup=announcement_kind_keyboard())
        return

    if action == "ann_list":
        rows=db.list_announcements()
        text="الإعلانات المرسلة:\n\n"+"\n".join(f"#{r['id']} | {ANNOUNCEMENT_KINDS.get(r['kind'],r['kind'])} | {r['sent_count']} مستلم | {r['created_at']}\n{r['text'][:120]}" for r in rows)
        await query.edit_message_text(text or "لا توجد إعلانات.", reply_markup=announcements_keyboard())
        return

    if action == "templates":
        await query.edit_message_text("قوالب الإعلانات", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("تعديل قالب نزول شد",callback_data="f:template:shd_start")],[InlineKeyboardButton("تعديل قالب انتهاء شد",callback_data="f:template:shd_finish")],[InlineKeyboardButton("تعديل قالب اجتماع",callback_data="f:template:meeting")],[InlineKeyboardButton("تعديل قالب تنبيه",callback_data="f:template:alert")],[InlineKeyboardButton("رجوع",callback_data="founder:announcements")]]))
        return

    if action == "meeting_new":
        db.set_session(user_id,"FOUNDER_MEETING_NEW",{})
        await query.edit_message_text(prompts["meeting_new"])
        return

    if action == "meeting_list":
        rows=db.list_meetings()
        text="الاجتماعات:\n\n"+"\n".join(f"#{r['id']} | {r['title']} | {r['meeting_time']} | {r['audience']}" for r in rows)
        await query.edit_message_text(text or "لا توجد اجتماعات.", reply_markup=meetings_keyboard())
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

    if state == "FOUNDER_ANN_TEXT":
        data=session[1]; await send_announcement(context.bot,data["kind"],data["audience"],text,user_id,data.get("department"),data.get("platform")); db.clear_session(user_id); await update.message.reply_text("تم إرسال الإعلان.",reply_markup=announcements_keyboard()); return True

    if state == "FOUNDER_MEMBER_LINK":
        try: uid=int(text)
        except ValueError: await update.message.reply_text("أرسل ID صحيحًا."); return True
        if not db.get_user(uid): await update.message.reply_text("العضو غير موجود."); return True
        links=[]
        targets=list(PLATFORMS)+[COMMON_PLATFORM]
        for platform in targets:
            group=db.get_group(platform)
            if group:
                link=await make_member_invite(context.bot,group["chat_id"],uid,platform)
                if link: links.append(f"{PLATFORM_LABELS.get(platform,'الكروب العام')}: {link}")
        db.clear_session(user_id)
        await context.bot.send_message(uid,"روابط الدخول الخاصة بك:\n\n"+"\n".join(links) if links else "تعذر إنشاء الروابط حاليًا.")
        await update.message.reply_text("تم إصدار الروابط وإرسالها للعضو.",reply_markup=founder_users_keyboard()); return True

    if state == "FOUNDER_SET_DEPARTMENT":
        try: uid_s,dept=text.split("|",1); uid=int(uid_s.strip()); dept=dept.strip().lower()
        except ValueError: await update.message.reply_text("الصيغة: ID | programming أو band"); return True
        if dept not in ("programming","band"): await update.message.reply_text("القسم يجب أن يكون programming أو band."); return True
        db.set_department(uid,dept); db.clear_session(user_id); await update.message.reply_text("تم تعيين القسم.",reply_markup=team_keyboard()); return True

    if state == "FOUNDER_MEETING_NEW":
        try: title,when,details,audience=[x.strip() for x in text.split("|",3)]
        except ValueError: await update.message.reply_text("الصيغة غير صحيحة."); return True
        aud=audience
        if aud in ("programming","band"): aud="department:"+aud
        elif aud in PLATFORMS: aud="platform:"+aud
        elif aud!="all": await update.message.reply_text("الجمهور غير صحيح."); return True
        try: datetime.fromisoformat(when)
        except ValueError: await update.message.reply_text("صيغة الوقت يجب أن تكون ISO مثل 2026-09-13T20:00:00+03:00"); return True
        mid=db.create_meeting(title,when,details,aud,user_id); db.clear_session(user_id)
        await send_announcement(context.bot,"meeting",aud if aud=="all" else ("department" if aud.startswith("department:") else "platform"),db.get_template("template_meeting").format(title=title,time=when,details=details),user_id, aud.split(":",1)[1] if aud.startswith("department:") else None, aud.split(":",1)[1] if aud.startswith("platform:") else None)
        await schedule_meeting_reminders(context.bot,mid)
        await update.message.reply_text("تم إنشاء الاجتماع وجدولة التذكير قبل ساعة وقبل 10 دقائق.",reply_markup=meetings_keyboard()); return True

    if state.startswith("FOUNDER_TEMPLATE_"):
        key=state[len("FOUNDER_TEMPLATE_")]; db.set_template("template_"+key,text); db.clear_session(user_id); await update.message.reply_text("تم حفظ القالب.",reply_markup=announcements_keyboard()); return True

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
            target=int(text)
            removed=await kick_member_from_all_groups(context.bot,target)
            db.remove_user(target)
            db.clear_session(user_id)

            await update.message.reply_text(
                f"تم حذف العضو وطرده من {removed} كروب.",
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
            parts=[x.strip() for x in text.split("|",1)]
            db.remove_admin(int(parts[0]), parts[1].lower() if len(parts)>1 else None)
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
        ChatJoinRequestHandler(member_join_request)
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
            feature_callback,
            pattern=r"^f:(status|toggle|ann_kind|ann_aud|template):",
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
                r"add_admin|remove_admin|list_admins|team_users|team_admins|team_status|"
                r"member_link|set_department|ann_new|ann_list|templates|meeting_new|meeting_list)$"
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
            pattern=r"^apply_dept:(programming|band|instagram|telegram|both)$",
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
