#!/usr/bin/env python3
"""
Outlook OTP Telegram Bot — Inline User Selection + Full Export
==============================================================
Requirements:
    pip install "python-telegram-bot==20.*" requests
"""

import asyncio
import base64
import datetime
import io
import logging
import re
import sqlite3
import time

import requests
from telegram import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─── CONFIG — FILL THESE IN ──────────────────────────────────────────────────
BOT_TOKEN = "8576724259:AAHHr4_3wpacmBlL6sJwa3C6KNHZXCmF_As"

# One admin:   ADMIN_IDS = {123456789}
# Multiple:    ADMIN_IDS = {123456789, 987654321}
ADMIN_IDS = {7113000547}

DB_PATH = "accounts.db"
# ─────────────────────────────────────────────────────────────────────────────

MS_TOKEN_URL  = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MS_SCOPE      = "https://graph.microsoft.com/Mail.ReadWrite offline_access"
MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
OTP_PATTERN   = re.compile(r'\b(\d{4,8})\b')

# States
AWAIT_ASSIGN_COUNT  = "assign_count"
AWAIT_REMOVE_EMAIL  = "remove_email"
AWAIT_REMOVEALL_CONFIRM = "removeall_confirm"
AWAIT_ADD_ADMIN_ID  = "add_admin_id"
AWAIT_DEL_ADMIN_ID  = "del_admin_id"

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


# ─── DATABASE ────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            email     TEXT UNIQUE NOT NULL,
            password  TEXT NOT NULL,
            token     TEXT NOT NULL,
            client_id TEXT NOT NULL,
            recovery  TEXT,
            added_at  INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS assigned_accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            email       TEXT NOT NULL,
            password    TEXT NOT NULL,
            token       TEXT NOT NULL,
            client_id   TEXT NOT NULL,
            recovery    TEXT,
            used        INTEGER DEFAULT 0,
            assigned_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            first_name TEXT,
            last_name  TEXT,
            first_seen INTEGER DEFAULT (strftime('%s','now')),
            last_seen  INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS user_last_account (
            user_id   INTEGER PRIMARY KEY,
            email     TEXT,
            password  TEXT,
            token     TEXT,
            client_id TEXT
        );
    """)
    for uid in ADMIN_IDS:
        con.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
    con.commit()
    con.close()


def db():
    return sqlite3.connect(DB_PATH)

def is_admin(user_id: int) -> bool:
    with db() as c:
        return bool(c.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone())

def get_all_admins():
    with db() as c:
        return [r[0] for r in c.execute("SELECT user_id FROM admins").fetchall()]

def register_user(user):
    with db() as c:
        c.execute("""
            INSERT INTO users(user_id, username, first_name, last_name)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                last_seen=strftime('%s','now')
        """, (user.id, user.username, user.first_name, user.last_name))
        c.commit()

def get_all_users():
    with db() as c:
        return c.execute("""
            SELECT u.user_id, u.username, u.first_name, u.last_name, u.last_seen,
                   (SELECT COUNT(*) FROM assigned_accounts a
                    WHERE a.user_id=u.user_id AND a.used=0) as remaining,
                   (SELECT COUNT(*) FROM assigned_accounts a
                    WHERE a.user_id=u.user_id) as total_assigned
            FROM users u ORDER BY u.last_seen DESC
        """).fetchall()

def get_user_info(user_id: int):
    with db() as c:
        return c.execute(
            "SELECT user_id,username,first_name,last_name FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

def account_count() -> int:
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

def assigned_count_for_user(user_id: int) -> int:
    with db() as c:
        return c.execute(
            "SELECT COUNT(*) FROM assigned_accounts WHERE user_id=? AND used=0",
            (user_id,)
        ).fetchone()[0]

def total_assigned_count_for_user(user_id: int) -> int:
    with db() as c:
        return c.execute(
            "SELECT COUNT(*) FROM assigned_accounts WHERE user_id=?",
            (user_id,)
        ).fetchone()[0]

def pop_assigned_account(user_id: int):
    with db() as c:
        row = c.execute("""
            SELECT id, email, password, token, client_id, recovery
            FROM assigned_accounts WHERE user_id=? AND used=0
            ORDER BY id LIMIT 1
        """, (user_id,)).fetchone()
        if row:
            c.execute("UPDATE assigned_accounts SET used=1 WHERE id=?", (row[0],))
            c.commit()
            return row[1:]
    return None

def pop_account():
    with db() as c:
        row = c.execute(
            "SELECT email,password,token,client_id,recovery FROM accounts ORDER BY id LIMIT 1"
        ).fetchone()
        if row:
            c.execute("DELETE FROM accounts WHERE email=?", (row[0],))
            c.commit()
    return row

def assign_accounts_to_user(user_id: int, count: int) -> int:
    assigned = 0
    with db() as c:
        rows = c.execute(
            "SELECT email,password,token,client_id,recovery FROM accounts ORDER BY id LIMIT ?",
            (count,)
        ).fetchall()
        for row in rows:
            email, password, token, client_id, recovery = row
            c.execute(
                "INSERT OR IGNORE INTO assigned_accounts(user_id,email,password,token,client_id,recovery) VALUES(?,?,?,?,?,?)",
                (user_id, email, password, token, client_id, recovery)
            )
            c.execute("DELETE FROM accounts WHERE email=?", (email,))
            assigned += 1
        c.commit()
    return assigned

def get_all_user_accounts(user_id: int):
    """Get ALL assigned accounts (used + unused) for export."""
    with db() as c:
        return c.execute("""
            SELECT email, password, token, client_id, recovery, used
            FROM assigned_accounts WHERE user_id=? ORDER BY id
        """, (user_id,)).fetchall()

def delete_all_user_accounts(user_id: int):
    """Delete ALL assigned accounts for a user (after export)."""
    with db() as c:
        c.execute("DELETE FROM assigned_accounts WHERE user_id=?", (user_id,))
        c.commit()

def remove_account(email: str) -> bool:
    with db() as c:
        cur = c.execute("DELETE FROM accounts WHERE email=?", (email,))
        c.commit()
        return cur.rowcount > 0

def remove_all_accounts():
    with db() as c:
        c.execute("DELETE FROM accounts")
        c.commit()

def list_all_emails():
    with db() as c:
        return c.execute("SELECT email FROM accounts ORDER BY id").fetchall()

def insert_accounts(lines: list) -> tuple:
    added = skipped = 0
    with db() as c:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 4:
                skipped += 1
                continue
            email, password, token, client_id = [p.strip() for p in parts[:4]]
            recovery = parts[4].strip() if len(parts) >= 5 else None
            try:
                c.execute(
                    "INSERT OR IGNORE INTO accounts(email,password,token,client_id,recovery) VALUES(?,?,?,?,?)",
                    (email, password, token, client_id, recovery),
                )
                if c.execute("SELECT changes()").fetchone()[0]:
                    added += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
        c.commit()
    return added, skipped

def save_last_account(user_id, email, password, token, client_id):
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO user_last_account(user_id,email,password,token,client_id) VALUES(?,?,?,?,?)",
            (user_id, email, password, token, client_id),
        )
        c.commit()

def get_last_account(user_id):
    with db() as c:
        return c.execute(
            "SELECT email,password,token,client_id FROM user_last_account WHERE user_id=?",
            (user_id,),
        ).fetchone()

def add_admin(user_id: int):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (user_id,))
        c.commit()

def del_admin(user_id: int):
    with db() as c:
        c.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        c.commit()

def fmt_time(ts):
    if not ts:
        return "?"
    return datetime.datetime.utcfromtimestamp(int(ts)).strftime("%d %b %Y %H:%M")


# ─── OUTLOOK HELPERS ─────────────────────────────────────────────────────────
def refresh_token(acc: dict) -> bool:
    try:
        r = requests.post(MS_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": acc["token"],
            "client_id":     acc["client_id"],
            "scope":         MS_SCOPE,
        }, timeout=20)
        if r.status_code == 200:
            data = r.json()
            acc["access_token"]   = data["access_token"]
            acc["access_expires"] = time.time() + data.get("expires_in", 3600) - 60
            if "refresh_token" in data:
                acc["token"] = data["refresh_token"]
            return True
    except Exception as e:
        log.error("Token refresh: %s", e)
    return False

def _fetch_messages_from_folder(acc: dict, folder: str):
    """
    Fetch the 10 most recent messages from a given well-known folder name
    (e.g. 'inbox', 'junkemail').  Returns list of message dicts or [].
    Handles a single 401 retry with a fresh access token.
    """
    headers = {"Authorization": f"Bearer {acc['access_token']}", "Content-Type": "application/json"}
    params  = {
        "$top":     "10",
        "$orderby": "receivedDateTime desc",
        "$select":  "id,subject,from,receivedDateTime,bodyPreview,body",
    }
    url = f"{MS_GRAPH_BASE}/me/mailFolders/{folder}/messages"
    try:
        r = requests.get(url, headers=headers, params=params, timeout=20)
        if r.status_code == 401:
            if not refresh_token(acc):
                return []
            headers["Authorization"] = f"Bearer {acc['access_token']}"
            r = requests.get(url, headers=headers, params=params, timeout=20)
        if r.status_code == 200:
            return r.json().get("value", [])
    except Exception as e:
        log.error("Folder '%s' fetch: %s", folder, e)
    return []


def get_latest_email(acc: dict):
    """
    Fetch the most recent 10 messages from every well-known mail folder,
    pool them all together, and return the single newest email across all
    folders as a (msg, folder_label) tuple.  Returns (None, None) when
    every folder is empty or unreachable.
    """
    # Well-known folder ids supported by Microsoft Graph
    FOLDERS = [
        ("inbox",          "📥 Inbox"),
        ("junkemail",      "🗑 Junk"),
        ("deleteditems",   "🗑 Deleted"),
        ("sentitems",      "📤 Sent"),
        ("drafts",         "📝 Drafts"),
        ("archive",        "📦 Archive"),
        ("clutter",        "📌 Clutter"),
    ]

    all_candidates = []  # list of (receivedDateTime_str, msg, folder_label)

    for folder_id, folder_label in FOLDERS:
        msgs = _fetch_messages_from_folder(acc, folder_id)
        for msg in msgs:
            ts = msg.get("receivedDateTime", "")
            all_candidates.append((ts, msg, folder_label))

    if not all_candidates:
        return None, None

    # Sort all messages newest-first across every folder
    all_candidates.sort(key=lambda x: x[0], reverse=True)

    # Return the absolute newest message regardless of folder
    _, newest_msg, newest_folder = all_candidates[0]
    return newest_msg, newest_folder

def extract_otp(msg: dict):
    body = ""
    try:
        body = msg.get("body", {}).get("content", "")
    except Exception:
        pass
    if not body:
        body = msg.get("bodyPreview", "")
    plain = re.sub(r'<[^>]+>', ' ', body)
    matches = OTP_PATTERN.findall(plain)
    if matches:
        six = [m for m in matches if len(m) == 6]
        return six[0] if six else matches[0]
    return None

def fetch_otp_for_account(email, password, token, client_id):
    acc = {"email": email, "password": password, "token": token,
           "client_id": client_id, "access_token": None, "access_expires": 0}
    if not refresh_token(acc):
        return None, "❌ Could not authenticate — token may be expired."
    msg, folder_label = get_latest_email(acc)
    if not msg:
        return None, "📭 Inbox and Junk are empty or could not be read."
    subject  = msg.get("subject", "(no subject)")
    sender   = msg.get("from", {}).get("emailAddress", {}).get("address", "?")
    received = msg.get("receivedDateTime", "?")
    otp = extract_otp(msg)
    detail = (
        f"📧 *From:* `{sender}`\n"
        f"📌 *Subject:* `{subject}`\n"
        f"🕐 *Time:* `{received}`\n"
        f"📂 *Folder:* {folder_label}"
    )
    return otp, detail


# ─── KEYBOARDS ───────────────────────────────────────────────────────────────
USER_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📥 Get Account"), KeyboardButton("📨 Get Code")]],
    resize_keyboard=True,
)

ADMIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📥 Get Account"),        KeyboardButton("📨 Get Code")],
        [KeyboardButton("📤 Upload Accounts"),    KeyboardButton("📊 Status")],
        [KeyboardButton("📋 List Accounts"),      KeyboardButton("🗑 Remove Account")],
        [KeyboardButton("👥 View Users"),          KeyboardButton("🎯 Assign Accounts")],
        [KeyboardButton("📦 Export User Accounts"),KeyboardButton("🧹 Remove All")],
        [KeyboardButton("👑 Add Admin"),           KeyboardButton("❌ Remove Admin")],
    ],
    resize_keyboard=True,
)

def kb_for(user_id: int):
    return ADMIN_KB if is_admin(user_id) else USER_KB


# ─── INLINE USER PICKER ──────────────────────────────────────────────────────
def user_picker_keyboard(action: str):
    """Build inline keyboard of all users for a given action prefix."""
    users = get_all_users()
    if not users:
        return None, []
    buttons = []
    for u in users:
        user_id, username, first_name, last_name, last_seen, remaining, total = u
        name = first_name or ""
        if last_name:
            name += f" {last_name}"
        uname = f" (@{username})" if username else ""
        label = f"👤 {name}{uname} — 🎯{remaining} left / {total} total"
        buttons.append([InlineKeyboardButton(label, callback_data=f"{action}:{user_id}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"{action}:cancel")])
    return InlineKeyboardMarkup(buttons), users


# ─── /start ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    register_user(update.effective_user)
    name = update.effective_user.first_name or "there"
    assigned = assigned_count_for_user(uid)
    role = "🔑 *Admin Panel*" if is_admin(uid) else "👤 *User Panel*"
    info = (
        f"📦 Pool: *{account_count()}*  |  🎯 Your assigned: *{assigned}*"
    ) if is_admin(uid) else (
        f"🎯 Accounts assigned to you: *{assigned}*"
    )
    await update.message.reply_text(
        f"👋 Welcome, *{name}*!\n\n{role}\n\n{info}\n\nUse the keyboard below 👇",
        parse_mode="Markdown",
        reply_markup=kb_for(uid),
    )


# ─── CALLBACK HANDLER (inline button presses) ────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query: CallbackQuery = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data  # format: "action:user_id" or "action:cancel"

    parts = data.split(":", 1)
    action = parts[0]
    arg    = parts[1] if len(parts) > 1 else ""

    if arg == "cancel":
        await query.message.edit_text("✅ Cancelled.")
        await query.message.reply_text("Back to menu.", reply_markup=kb_for(uid))
        return

    # ── assign_pick — user selected, now ask count ──
    if action == "assign_pick":
        target = int(arg)
        pool = account_count()
        user_info = get_user_info(target)
        name = user_info[2] if user_info else str(target)
        ctx.user_data["state"]         = AWAIT_ASSIGN_COUNT
        ctx.user_data["assign_target"] = target
        await query.message.edit_text(
            f"🎯 Assigning to *{name}*\n\n"
            f"📦 Pool available: *{pool}*\n\n"
            f"How many accounts to assign? Reply with a number.\n"
            f"_(or /cancel to abort)_",
            parse_mode="Markdown",
        )

    # ── export_pick — user selected, do export ──
    elif action == "export_pick":
        target = int(arg)
        rows = get_all_user_accounts(target)
        user_info = get_user_info(target)
        name = user_info[2] if user_info else str(target)

        if not rows:
            await query.message.edit_text(
                f"📭 No accounts found for *{name}* (`{target}`).",
                parse_mode="Markdown",
            )
            await query.message.reply_text("Back to menu.", reply_markup=kb_for(uid))
            return

        used_count   = sum(1 for r in rows if r[5] == 1)
        unused_count = sum(1 for r in rows if r[5] == 0)

        lines = []
        for row in rows:
            email, password, token, client_id, recovery, used = row
            lines.append(email)

        content  = "\n".join(lines)
        file_obj = io.BytesIO(content.encode("utf-8"))
        file_obj.name = f"accounts_{target}.txt"

        await query.message.edit_text(
            f"📦 Exporting *{len(rows)}* accounts for *{name}*...",
            parse_mode="Markdown",
        )
        await query.message.reply_document(
            document=file_obj,
            filename=f"accounts_{target}.txt",
            caption=(
                f"📦 *Export for {name}* (`{target}`)\n\n"
                f"✅ Used accounts:   *{used_count}*\n"
                f"🎯 Unused accounts: *{unused_count}*\n"
                f"📊 Total exported:  *{len(rows)}*\n\n"
                f"🗑 All accounts removed from database."
            ),
            parse_mode="Markdown",
            reply_markup=kb_for(uid),
        )
        delete_all_user_accounts(target)

    # ── copy_acc — user tapped "Copy Account" ──
    elif action == "copy_acc":
        # Look up the last account from DB to avoid callback_data size limits
        acc = get_last_account(uid)
        if acc:
            email = acc[0]
            copy_text = email
        else:
            copy_text = "⚠️ Could not retrieve account. Press 📥 Get Account again."
        await query.answer("Tap the message below to copy ✅", show_alert=False)
        await query.message.reply_text(
            f"`{copy_text}`",
            parse_mode="Markdown",
        )
        return

    # ── copy_otp — user tapped "Copy OTP" ──
    elif action == "copy_otp":
        try:
            otp_text = base64.b64decode(arg.encode()).decode()
        except Exception:
            otp_text = arg
        await query.answer("Tap the message below to copy ✅", show_alert=False)
        await query.message.reply_text(
            f"`{otp_text}`",
            parse_mode="Markdown",
        )
        return

    # ── add_admin_pick ──
    elif action == "addadmin_pick":
        target = int(arg)
        add_admin(target)
        user_info = get_user_info(target)
        name = user_info[2] if user_info else str(target)
        await query.message.edit_text(f"✅ *{name}* is now an admin.", parse_mode="Markdown")
        await query.message.reply_text("Back to menu.", reply_markup=kb_for(uid))

    # ── remove_admin_pick ──
    elif action == "deladmin_pick":
        target = int(arg)
        if target == uid:
            await query.message.edit_text("⚠️ You can't remove yourself.")
            await query.message.reply_text("Back to menu.", reply_markup=kb_for(uid))
            return
        del_admin(target)
        user_info = get_user_info(target)
        name = user_info[2] if user_info else str(target)
        await query.message.edit_text(f"✅ Admin rights revoked for *{name}*.", parse_mode="Markdown")
        await query.message.reply_text("Back to menu.", reply_markup=kb_for(uid))


# ─── MAIN MESSAGE ROUTER ─────────────────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = update.effective_user
    register_user(user)
    text = update.message.text.strip()

    # ── Conversation: assign count reply ──
    if ctx.user_data.get("state") == AWAIT_ASSIGN_COUNT:
        try:
            count = int(text)
            if count <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("⚠️ Send a valid number greater than 0. Or /cancel")
            return
        target = ctx.user_data.pop("assign_target")
        ctx.user_data.pop("state")
        pool    = account_count()
        count   = min(count, pool)
        assigned = assign_accounts_to_user(target, count)
        user_info = get_user_info(target)
        name = user_info[2] if user_info else str(target)
        total_now = assigned_count_for_user(target)
        await update.message.reply_text(
            f"✅ *{assigned} accounts assigned* to *{name}*\n\n"
            f"🎯 Their total unused now: *{total_now}*\n"
            f"📦 Pool remaining: *{account_count()}*",
            parse_mode="Markdown",
            reply_markup=kb_for(uid),
        )
        try:
            await ctx.bot.send_message(
                target,
                f"🎉 *{assigned} account(s) assigned to you!*\n\nPress *📥 Get Account* to use them.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    # ── Conversation: remove email ──
    if ctx.user_data.get("state") == AWAIT_REMOVE_EMAIL:
        ctx.user_data.pop("state")
        ok = remove_account(text)
        msg = f"✅ Removed `{text}` from pool." if ok else f"⚠️ `{text}` not found in pool."
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb_for(uid))
        return

    # ── Conversation: removeall confirm ──
    if ctx.user_data.get("state") == AWAIT_REMOVEALL_CONFIRM:
        ctx.user_data.pop("state")
        if text == "✅ Yes, delete all":
            remove_all_accounts()
            await update.message.reply_text("🧹 All pool accounts deleted.", reply_markup=kb_for(uid))
        else:
            await update.message.reply_text("✅ Cancelled.", reply_markup=kb_for(uid))
        return

    # ── Conversation: add admin ID ──
    if ctx.user_data.get("state") == AWAIT_ADD_ADMIN_ID:
        ctx.user_data.pop("state")
        try:
            target = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Not a valid user ID.", reply_markup=kb_for(uid))
            return
        add_admin(target)
        await update.message.reply_text(f"✅ User `{target}` is now an admin.", parse_mode="Markdown", reply_markup=kb_for(uid))
        return

    # ── Conversation: remove admin ID ──
    if ctx.user_data.get("state") == AWAIT_DEL_ADMIN_ID:
        ctx.user_data.pop("state")
        try:
            target = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Not a valid user ID.", reply_markup=kb_for(uid))
            return
        if target == uid:
            await update.message.reply_text("⚠️ You can't remove yourself.", reply_markup=kb_for(uid))
            return
        del_admin(target)
        await update.message.reply_text(f"✅ User `{target}` admin rights revoked.", parse_mode="Markdown", reply_markup=kb_for(uid))
        return

    # ── 📥 Get Account ──
    if text == "📥 Get Account":
        row = pop_assigned_account(uid)
        if not row:
            if is_admin(uid):
                row = pop_account()
                if not row:
                    await update.message.reply_text("📭 No accounts available in pool.", reply_markup=kb_for(uid))
                    return
            else:
                await update.message.reply_text(
                    "📭 No accounts assigned to you.\nPlease contact the admin.",
                    reply_markup=kb_for(uid),
                )
                uname = f"@{user.username}" if user.username else user.first_name
                for admin_id in get_all_admins():
                    try:
                        await ctx.bot.send_message(
                            admin_id,
                            f"⚠️ *No Accounts Left!*\n\n"
                            f"User {uname} (`{uid}`) has no assigned accounts.\nPlease assign more.",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                return

        email, password, token, client_id, recovery = row
        save_last_account(uid, email, password, token, client_id)
        remaining = assigned_count_for_user(uid)

        copy_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Copy Account", copy_text=CopyTextButton(text=email))]
        ])
        await update.message.reply_text(
            f"✅ *Your account:*\n\n`{email}`\n\n🎯 Remaining for you: *{remaining}*",
            parse_mode="Markdown",
            reply_markup=copy_kb,
        )

        if remaining == 0 and not is_admin(uid):
            uname = f"@{user.username}" if user.username else user.first_name
            for admin_id in get_all_admins():
                try:
                    await ctx.bot.send_message(
                        admin_id,
                        f"🚨 *Account Pool Empty!*\n\n"
                        f"User {uname} (`{uid}`) just used their *last* assigned account.\n"
                        f"Assign more accounts to keep them going.",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

    # ── 📨 Get Code ──
    elif text == "📨 Get Code":
        acc = get_last_account(uid)
        if not acc:
            await update.message.reply_text(
                "⚠️ No account linked yet. Press *📥 Get Account* first.",
                parse_mode="Markdown", reply_markup=kb_for(uid),
            )
            return
        email, password, token, client_id = acc
        wait = await update.message.reply_text(f"🔄 Checking inbox for `{email}`...", parse_mode="Markdown")
        otp, detail = await asyncio.to_thread(fetch_otp_for_account, email, password, token, client_id)
        await wait.delete()
        if otp:
            otp_payload = base64.b64encode(otp.encode()).decode()
            otp_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Copy OTP Code", copy_text=CopyTextButton(text=otp))]
            ])
            await update.message.reply_text(
                f"📨 *Latest email:*\n{detail}\n\n🔑 *Code:* `{otp}`\n\n"
                f"_If this isn't the code you're looking for, tap 📨 Get Code again in a few seconds._",
                parse_mode="Markdown", reply_markup=otp_kb,
            )
        else:
            await update.message.reply_text(
                f"📨 *Latest email:*\n{detail}\n\n⚠️ No OTP found.\n_Try again in a few seconds._",
                parse_mode="Markdown", reply_markup=kb_for(uid),
            )

    # ── 📤 Upload Accounts ──
    elif text == "📤 Upload Accounts":
        if not is_admin(uid):
            await update.message.reply_text("⛔ Admins only.", reply_markup=kb_for(uid))
            return
        await update.message.reply_text(
            "📂 *Send your accounts file* (`.txt` or `.csv`)\nFormat: `email|password|token|client_id`",
            parse_mode="Markdown", reply_markup=kb_for(uid),
        )

    # ── 📊 Status ──
    elif text == "📊 Status":
        if not is_admin(uid):
            await update.message.reply_text("⛔ Admins only.", reply_markup=kb_for(uid))
            return
        pool  = account_count()
        users = get_all_users()
        total_unused  = sum(r[5] for r in users)
        total_assigned = sum(r[6] for r in users)
        await update.message.reply_text(
            f"📊 *Bot Status*\n\n"
            f"📦 Pool (unassigned): *{pool}*\n"
            f"🎯 Total assigned:    *{total_assigned}*\n"
            f"✅ Still unused:      *{total_unused}*\n"
            f"👥 Total users:       *{len(users)}*",
            parse_mode="Markdown", reply_markup=kb_for(uid),
        )

    # ── 📋 List Accounts ──
    elif text == "📋 List Accounts":
        if not is_admin(uid):
            await update.message.reply_text("⛔ Admins only.", reply_markup=kb_for(uid))
            return
        rows = list_all_emails()
        if not rows:
            await update.message.reply_text("📭 Pool is empty.", reply_markup=kb_for(uid))
            return
        emails = [f"`{r[0]}`" for r in rows]
        for i in range(0, len(emails), 50):
            chunk = emails[i:i+50]
            await update.message.reply_text(
                f"📋 *Pool {i+1}–{i+len(chunk)} of {len(emails)}:*\n\n" + "\n".join(chunk),
                parse_mode="Markdown",
            )
        await update.message.reply_text("✅ Done.", reply_markup=kb_for(uid))

    # ── 👥 View Users ──
    elif text == "👥 View Users":
        if not is_admin(uid):
            await update.message.reply_text("⛔ Admins only.", reply_markup=kb_for(uid))
            return
        users = get_all_users()
        if not users:
            await update.message.reply_text("👥 No users yet.", reply_markup=kb_for(uid))
            return
        lines = []
        for u in users:
            user_id, username, first_name, last_name, last_seen, remaining, total = u
            name = (first_name or "") + (f" {last_name}" if last_name else "")
            uname = f"@{username}" if username else "no username"
            lines.append(
                f"👤 *{name}* ({uname})\n"
                f"   🆔 `{user_id}`\n"
                f"   🎯 Unused: *{remaining}* / Total assigned: *{total}*\n"
                f"   🕐 Last seen: {fmt_time(last_seen)}"
            )
        for i in range(0, len(lines), 15):
            chunk = lines[i:i+15]
            await update.message.reply_text(
                f"👥 *Users {i+1}–{i+len(chunk)} of {len(lines)}:*\n\n" + "\n\n".join(chunk),
                parse_mode="Markdown",
            )
        await update.message.reply_text("✅ Done.", reply_markup=kb_for(uid))

    # ── 🎯 Assign Accounts ──
    elif text == "🎯 Assign Accounts":
        if not is_admin(uid):
            await update.message.reply_text("⛔ Admins only.", reply_markup=kb_for(uid))
            return
        if account_count() == 0:
            await update.message.reply_text("📭 Pool is empty. Upload accounts first.", reply_markup=kb_for(uid))
            return
        kb, users = user_picker_keyboard("assign_pick")
        if not kb:
            await update.message.reply_text("👥 No users found yet.", reply_markup=kb_for(uid))
            return
        await update.message.reply_text(
            f"🎯 *Select a user to assign accounts to:*\n📦 Pool: *{account_count()}*",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    # ── 📦 Export User Accounts ──
    elif text == "📦 Export User Accounts":
        if not is_admin(uid):
            await update.message.reply_text("⛔ Admins only.", reply_markup=kb_for(uid))
            return
        kb, users = user_picker_keyboard("export_pick")
        if not kb:
            await update.message.reply_text("👥 No users found yet.", reply_markup=kb_for(uid))
            return
        await update.message.reply_text(
            "📦 *Select a user to export accounts from:*\n_(All accounts — used & unused — will be exported then deleted)_",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    # ── 🗑 Remove Account ──
    elif text == "🗑 Remove Account":
        if not is_admin(uid):
            await update.message.reply_text("⛔ Admins only.", reply_markup=kb_for(uid))
            return
        ctx.user_data["state"] = AWAIT_REMOVE_EMAIL
        await update.message.reply_text(
            "✏️ Type the *email* to remove from pool, or /cancel",
            parse_mode="Markdown", reply_markup=ReplyKeyboardRemove(),
        )

    # ── 🧹 Remove All ──
    elif text == "🧹 Remove All":
        if not is_admin(uid):
            await update.message.reply_text("⛔ Admins only.", reply_markup=kb_for(uid))
            return
        ctx.user_data["state"] = AWAIT_REMOVEALL_CONFIRM
        confirm_kb = ReplyKeyboardMarkup(
            [[KeyboardButton("✅ Yes, delete all"), KeyboardButton("❌ Cancel")]],
            resize_keyboard=True, one_time_keyboard=True,
        )
        await update.message.reply_text(
            "⚠️ Delete *ALL pool accounts*? This cannot be undone.",
            parse_mode="Markdown", reply_markup=confirm_kb,
        )

    # ── 👑 Add Admin ──
    elif text == "👑 Add Admin":
        if not is_admin(uid):
            await update.message.reply_text("⛔ Admins only.", reply_markup=kb_for(uid))
            return
        kb, users = user_picker_keyboard("addadmin_pick")
        if kb:
            await update.message.reply_text(
                "👑 *Select user to make admin:*",
                parse_mode="Markdown", reply_markup=kb,
            )
        else:
            # Fallback: no users in DB yet, type ID
            ctx.user_data["state"] = AWAIT_ADD_ADMIN_ID
            await update.message.reply_text(
                "✏️ No users found yet. Send the *Telegram user ID* to promote, or /cancel",
                parse_mode="Markdown", reply_markup=ReplyKeyboardRemove(),
            )

    # ── ❌ Remove Admin ──
    elif text == "❌ Remove Admin":
        if not is_admin(uid):
            await update.message.reply_text("⛔ Admins only.", reply_markup=kb_for(uid))
            return
        kb, users = user_picker_keyboard("deladmin_pick")
        if kb:
            await update.message.reply_text(
                "❌ *Select admin to demote:*",
                parse_mode="Markdown", reply_markup=kb,
            )
        else:
            ctx.user_data["state"] = AWAIT_DEL_ADMIN_ID
            await update.message.reply_text(
                "✏️ No users found. Send the *Telegram user ID* to demote, or /cancel",
                parse_mode="Markdown", reply_markup=ReplyKeyboardRemove(),
            )

    else:
        await update.message.reply_text("Use the keyboard buttons below 👇", reply_markup=kb_for(uid))


# ─── /cancel ─────────────────────────────────────────────────────────────────
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ctx.user_data.clear()
    await update.message.reply_text("✅ Cancelled.", reply_markup=kb_for(uid))


# ─── FILE UPLOAD ─────────────────────────────────────────────────────────────
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Admins only.", reply_markup=kb_for(uid))
        return
    doc = update.message.document
    if not doc:
        return
    if not (doc.file_name or "").lower().endswith((".txt", ".csv")):
        await update.message.reply_text("⚠️ Please send a .txt or .csv file.", reply_markup=kb_for(uid))
        return
    msg = await update.message.reply_text("⏳ Processing file...")
    tg_file = await ctx.bot.get_file(doc.file_id)
    raw  = await tg_file.download_as_bytearray()
    text = raw.decode("utf-8", errors="replace")
    added, skipped = insert_accounts(text.splitlines())
    total = account_count()
    await msg.edit_text(
        f"✅ *Upload complete!*\n\n"
        f"➕ Added:   *{added}*\n"
        f"⏭ Skipped: *{skipped}*\n"
        f"📦 Pool total: *{total}*",
        parse_mode="Markdown",
    )
    await update.message.reply_text("Ready.", reply_markup=kb_for(uid))


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
