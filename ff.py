#!/usr/bin/env python3
# rose_full_manager.py
# Rose-like Telegram Group Manager Bot (full assembled)
# Requirements:
#   pip install python-telegram-bot==20.4
# Run:
#   python rose_full_manager.py

import logging
import sqlite3
import re
import time
from functools import wraps
from typing import Callable, Dict, Any, Optional

from telegram import (
    Update,
    ChatPermissions,
    ChatMember,
    User,
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ------------- CONFIG -------------
BOT_TOKEN = "8361474378:AAEP-cPvtVuCcRwlCLPGjngQtFqSft96CH8"
DB_FILE = "group_mgr.db"
DEFAULT_WARN_LIMIT = 3
DEFAULT_WELCOME = "👋 <b>Welcome {mention}!</b>"
DEFAULT_GOODBYE = "👋 <b>Goodbye {mention}!</b>"
SPAM_THRESHOLD = 5
SPAM_WINDOW = 6  # seconds
# ----------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# in-memory trackers
_msg_times: Dict[tuple, list] = {}  # (chat_id, user_id) -> [timestamps]

# ----------------- DB helpers -----------------
def init_db() -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"""CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            rules TEXT DEFAULT 'No rules set.',
            anti_link INTEGER DEFAULT 0,
            slow_mode INTEGER DEFAULT 0,
            warn_limit INTEGER DEFAULT {DEFAULT_WARN_LIMIT},
            welcome TEXT DEFAULT '{DEFAULT_WELCOME}',
            goodbye TEXT DEFAULT '{DEFAULT_GOODBYE}'
        );""")
    c.execute("""CREATE TABLE IF NOT EXISTS warns (
            chat_id INTEGER,
            user_id INTEGER,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );""")
    conn.commit()
    conn.close()

def ensure_chat(chat_id: int) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,))
    if not c.fetchone():
        c.execute("INSERT INTO chats (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
    conn.close()

def get_chat(chat_id: int) -> Dict[str, Any]:
    ensure_chat(chat_id)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT rules, anti_link, slow_mode, warn_limit, welcome, goodbye FROM chats WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return {
        "rules": row[0],
        "anti_link": bool(row[1]),
        "slow_mode": int(row[2]),
        "warn_limit": int(row[3]),
        "welcome": row[4],
        "goodbye": row[5],
    }

def set_chat_field(chat_id: int, field: str, value: Any) -> None:
    if field not in ("rules", "anti_link", "slow_mode", "warn_limit", "welcome", "goodbye"):
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"UPDATE chats SET {field}=? WHERE chat_id=?", (value, chat_id))
    conn.commit()
    conn.close()

def get_warns(chat_id: int, user_id: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def set_warns(chat_id: int, user_id: int, count: int) -> None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if count <= 0:
        c.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    else:
        c.execute(
            "INSERT INTO warns (chat_id, user_id, count) VALUES (?,?,?) "
            "ON CONFLICT(chat_id,user_id) DO UPDATE SET count=excluded.count",
            (chat_id, user_id, count),
        )
    conn.commit()
    conn.close()
    # ----------------- Helpers -----------------

def admin_only(func: Callable):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type == ChatType.PRIVATE:
            await update.message.reply_text("❌ This command only works in groups.")
            return

        try:
            chat_id = update.effective_chat.id
            user_id = update.effective_user.id
            member = await context.bot.get_chat_member(chat_id, user_id)

            if member.status not in ("administrator", "creator"):
                user_name = update.effective_user.mention_html()
                await update.message.reply_text(
                    f"❌ {user_name}, you are not an admin.\n\n"
                    f"Your status: <b>{member.status}</b>",
                    parse_mode=ParseMode.HTML
                )
                return
        except Exception as e:
            await update.message.reply_text(f"❌ Admin check failed: {e}")
            return

        return await func(update, context)
    return wrapped

duration_re = re.compile(r"^(\d+)([smhd])$")
def parse_duration(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    text = text.strip().lower()
    if text.isdigit():
        return int(text)
    m = duration_re.match(text)
    if not m:
        return None
    val = int(m.group(1)); unit = m.group(2)
    if unit == "s": return val
    if unit == "m": return val * 60
    if unit == "h": return val * 3600
    if unit == "d": return val * 86400
    return None

def format_user(u: User) -> str:
    return f"{u.mention_html()} (ID: <code>{u.id}</code>)"

def format_admin_info(update: Update) -> str:
    a = update.effective_user
    return f"{a.mention_html()} (ID: <code>{a.id}</code>)"

def format_template(text: str, user: User) -> str:
    try:
        return text.format(
            first=user.first_name or "",
            last=user.last_name or "",
            mention=user.mention_html(),
            id=user.id,
        )
    except Exception:
        return text

# ----------------- Commands & Handlers -----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Rose-like group manager running. Use /help or /cmds.")

async def cmd_cmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "📋 <b>Command List</b>\n\n"
        "👮 Moderation:\n"
        "/warn, /warnings, /resetwarns, /mute, /unmute, /ban, /unban, /kick, /promote, /demote, /purge\n\n"
        "⚙️ Group Settings:\n"
        "/rules, /setrules, /setwarnlimit, /antilink, /slowmode, /settings\n\n"
        "👋 Welcome/Goodbye:\n"
        "/setwelcome, /resetwelcome, /testwelcome, /setgoodbye, /resetgoodbye, /testgoodbye\n\n"
        "🔧 Utilities:\n"
        "/id, /userinfo, /echo, /help, /cmds"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "📖 <b>Help</b>\n\n"
        "Use /cmds for a compact command list.\n\n"
        "Most moderation commands must be used by admins and by replying to the target user's message."
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

# ----------------- Utilities -----------------

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    chat = update.effective_chat
    await update.message.reply_text(
        f"👤 You: <code>{u.id}</code>\n💬 Chat: <code>{chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )

async def cmd_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    txt = (
        f"👤 <b>{target.full_name}</b>\n"
        f"ID: <code>{target.id}</code>\n"
        f"Username: @{target.username or 'N/A'}\n"
        f"Is bot: {target.is_bot}"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def cmd_echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = " ".join(context.args)
    if not txt:
        await update.message.reply_text("Usage: /echo <text>")
        return
    await update.message.reply_text(txt)
    # ----------------- Group Settings -----------------

@admin_only
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_chat(update.effective_chat.id)
    await update.message.reply_text(f"📜 <b>Rules</b>:\n{s['rules']}", parse_mode=ParseMode.HTML)

@admin_only
async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /setrules <text>")
        return
    set_chat_field(update.effective_chat.id, "rules", text)
    await update.message.reply_text("✅ Rules updated.")

@admin_only
async def cmd_setwarnlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /setwarnlimit <n>")
        return
    try:
        n = int(context.args[0])
        set_chat_field(update.effective_chat.id, "warn_limit", n)
        await update.message.reply_text(f"✅ Warn limit set to {n}.")
    except Exception:
        await update.message.reply_text("❌ Invalid number.")

@admin_only
async def cmd_antilink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /antilink on|off")
        return
    val = 1 if context.args[0].lower() == "on" else 0
    set_chat_field(update.effective_chat.id, "anti_link", val)
    await update.message.reply_text(f"✅ Anti-link {'enabled' if val else 'disabled'}.")

@admin_only
async def cmd_slowmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /slowmode <seconds>")
        return
    try:
        sec = int(context.args[0])
        set_chat_field(update.effective_chat.id, "slow_mode", sec)
        await update.message.reply_text(f"✅ Slowmode set to {sec} seconds.")
    except Exception:
        await update.message.reply_text("❌ Invalid number.")

@admin_only
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_chat(update.effective_chat.id)
    txt = (
        f"⚙️ <b>Group Settings</b>\n\n"
        f"Rules: {s['rules']}\n"
        f"Warn limit: {s['warn_limit']}\n"
        f"Anti-link: {'ON' if s['anti_link'] else 'OFF'}\n"
        f"Slowmode: {s['slow_mode']} sec\n"
        f"Welcome: {s['welcome']}\n"
        f"Goodbye: {s['goodbye']}"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)
    # ----------------- Moderation -----------------

@admin_only
async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user to warn.")
        return
    user = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id
    count = get_warns(chat_id, user.id) + 1
    set_warns(chat_id, user.id, count)

    limit = get_chat(chat_id)["warn_limit"]
    if count >= limit:
        try:
            await update.effective_chat.ban_member(user.id)
            set_warns(chat_id, user.id, 0)
            await update.message.reply_text(
                f"🚫 {format_user(user)} banned (warn limit {limit} reached).",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Could not ban: {e}")
    else:
        await update.message.reply_text(
            f"⚠️ {format_user(user)} warned ({count}/{limit}).",
            parse_mode=ParseMode.HTML,
        )

@admin_only
async def cmd_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user to check warnings.")
        return
    user = update.message.reply_to_message.from_user
    count = get_warns(update.effective_chat.id, user.id)
    await update.message.reply_text(
        f"⚠️ {format_user(user)} has {count} warnings.",
        parse_mode=ParseMode.HTML,
    )

@admin_only
async def cmd_resetwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user to reset warnings.")
        return
    user = update.message.reply_to_message.from_user
    set_warns(update.effective_chat.id, user.id, 0)
    await update.message.reply_text(
        f"✅ Warnings reset for {format_user(user)}.",
        parse_mode=ParseMode.HTML,
    )

@admin_only
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user to mute.")
        return
    user = update.message.reply_to_message.from_user
    duration = parse_duration(context.args[0]) if context.args else None
    until = int(time.time()) + duration if duration else None
    try:
        await update.effective_chat.restrict_member(
            user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        msg = f"🔇 {format_user(user)} muted"
        if duration:
            msg += f" for {context.args[0]}"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Could not mute: {e}")

@admin_only
async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user to unmute.")
        return
    user = update.message.reply_to_message.from_user
    try:
        await update.effective_chat.restrict_member(
            user.id, permissions=ChatPermissions(can_send_messages=True)
        )
        await update.message.reply_text(
            f"🔊 {format_user(user)} unmuted.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Could not unmute: {e}")

@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user to ban.")
        return
    user = update.message.reply_to_message.from_user
    try:
        await update.effective_chat.ban_member(user.id)
        await update.message.reply_text(
            f"🚫 {format_user(user)} banned.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Could not ban: {e}")

@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        user_id = int(context.args[0])
        await update.effective_chat.unban_member(user_id)
        await update.message.reply_text(f"✅ Unbanned <code>{user_id}</code>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Could not unban: {e}")

@admin_only
async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user to kick.")
        return
    user = update.message.reply_to_message.from_user
    try:
        await update.effective_chat.ban_member(user.id)
        await update.effective_chat.unban_member(user.id)
        await update.message.reply_text(
            f"👢 {format_user(user)} kicked.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Could not kick: {e}")

@admin_only
async def cmd_promote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user to promote.")
        return
    user = update.message.reply_to_message.from_user
    try:
        await update.effective_chat.promote_member(
            user.id,
            can_manage_chat=True,
            can_delete_messages=True,
            can_restrict_members=True,
            can_promote_members=False,
        )
        await update.message.reply_text(
            f"⬆️ {format_user(user)} promoted to admin.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Could not promote: {e}")

@admin_only
async def cmd_demote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user to demote.")
        return
    user = update.message.reply_to_message.from_user
    try:
        await update.effective_chat.promote_member(
            user.id,
            can_manage_chat=False,
            can_delete_messages=False,
            can_restrict_members=False,
            can_promote_members=False,
        )
        await update.message.reply_text(
            f"⬇️ {format_user(user)} demoted.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Could not demote: {e}")

@admin_only
async def cmd_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message to start purging from.")
        return
    try:
        start = update.message.reply_to_message.message_id
        end = update.message.message_id
        await context.bot.delete_messages(update.effective_chat.id, list(range(start, end + 1)))
    except Exception as e:
        await update.message.reply_text(f"❌ Could not purge: {e}")
    # ----------------- Welcome & Goodbye -----------------

@admin_only
async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /setwelcome <text>")
        return
    set_chat_field(update.effective_chat.id, "welcome", text)
    await update.message.reply_text("✅ Welcome message updated.")

@admin_only
async def cmd_resetwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_chat_field(update.effective_chat.id, "welcome", DEFAULT_WELCOME)
    await update.message.reply_text("✅ Welcome message reset.")

@admin_only
async def cmd_testwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_chat(update.effective_chat.id)
    msg = format_template(s["welcome"], update.effective_user)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

@admin_only
async def cmd_setgoodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /setgoodbye <text>")
        return
    set_chat_field(update.effective_chat.id, "goodbye", text)
    await update.message.reply_text("✅ Goodbye message updated.")

@admin_only
async def cmd_resetgoodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_chat_field(update.effective_chat.id, "goodbye", DEFAULT_GOODBYE)
    await update.message.reply_text("✅ Goodbye message reset.")

@admin_only
async def cmd_testgoodbye(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_chat(update.effective_chat.id)
    msg = format_template(s["goodbye"], update.effective_user)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.new_chat_members:
        s = get_chat(update.effective_chat.id)
        for u in update.message.new_chat_members:
            msg = format_template(s["welcome"], u)
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    elif update.message.left_chat_member:
        s = get_chat(update.effective_chat.id)
        u = update.message.left_chat_member
        msg = format_template(s["goodbye"], u)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        # ----------------- Protections -----------------

async def protect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    s = get_chat(chat_id)

    # --- Anti-link ---
    if s["anti_link"]:
        text = update.message.text or ""
        if "t.me/" in text or "telegram.me/" in text:
            try:
                await update.message.delete()
                await update.effective_chat.restrict_member(
                    user_id, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + 60
                )
                await update.effective_chat.send_message(
                    f"🚫 {update.effective_user.mention_html()} muted for sending links.",
                    parse_mode=ParseMode.HTML,
                )
                return
            except Exception:
                pass

    # --- Slowmode ---
    if s["slow_mode"] > 0:
        key = (chat_id, user_id)
        last = _msg_times.get(key, [0])[-1]
        now = time.time()
        if now - last < s["slow_mode"]:
            try:
                await update.message.delete()
            except Exception:
                pass
            return
        _msg_times.setdefault(key, []).append(now)

    # --- Spam Check ---
    key = (chat_id, user_id)
    now = time.time()
    history = _msg_times.get(key, [])
    history = [t for t in history if now - t < SPAM_WINDOW]
    history.append(now)
    _msg_times[key] = history
    if len(history) > SPAM_THRESHOLD:
        try:
            await update.message.delete()
            await update.effective_chat.restrict_member(
                user_id, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + 300
            )
            await update.effective_chat.send_message(
                f"🤖 {update.effective_user.mention_html()} auto-muted for spamming.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
            # ----------------- Main -----------------

def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Core
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cmds", cmd_cmds))

    # Utilities
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("userinfo", cmd_userinfo))
    app.add_handler(CommandHandler("echo", cmd_echo))

    # Group settings
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("setrules", cmd_setrules))
    app.add_handler(CommandHandler("setwarnlimit", cmd_setwarnlimit))
    app.add_handler(CommandHandler("antilink", cmd_antilink))
    app.add_handler(CommandHandler("slowmode", cmd_slowmode))
    app.add_handler(CommandHandler("settings", cmd_settings))

    # Moderation
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("warnings", cmd_warnings))
    app.add_handler(CommandHandler("resetwarns", cmd_resetwarns))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("promote", cmd_promote))
    app.add_handler(CommandHandler("demote", cmd_demote))
    app.add_handler(CommandHandler("purge", cmd_purge))

    # Welcome/Goodbye
    app.add_handler(CommandHandler("setwelcome", cmd_setwelcome))
    app.add_handler(CommandHandler("resetwelcome", cmd_resetwelcome))
    app.add_handler(CommandHandler("testwelcome", cmd_testwelcome))
    app.add_handler(CommandHandler("setgoodbye", cmd_setgoodbye))
    app.add_handler(CommandHandler("resetgoodbye", cmd_resetgoodbye))
    app.add_handler(CommandHandler("testgoodbye", cmd_testgoodbye))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER, welcome_handler))

    # Protections
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), protect_handler))

    log.info("✅ Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
            
            
    