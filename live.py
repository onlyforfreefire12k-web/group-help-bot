# live.py — GroupManagerBot | Flask + python-telegram-bot v20 (Webhook mode for Render)
import os, re, json, html, time, copy, sqlite3, asyncio, logging, threading
from collections import deque, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ParseMode, ChatType, ChatMemberStatus
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          ConversationHandler, ContextTypes, filters)

# ============================== CONFIG ==============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("GroupManagerBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN          = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL    = os.environ.get("WEBHOOK_URL") or (os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/") + "/webhook")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "render-secret")
DB_PATH        = os.environ.get("DB_PATH", "bot.db")

if not TOKEN:
    raise SystemExit("❌ BOT_TOKEN env var missing!")

GROUPS = (ChatType.GROUP, ChatType.SUPERGROUP)

def esc(s): return html.escape(str(s or ""))
def btn(t, d=None, url=None): return InlineKeyboardButton(t, url=url) if url else InlineKeyboardButton(t, callback_data=d)

# ============================== i18n ==============================
STRINGS = {
    "en": {"only_admins": "⛔ Admins only.",
           "added": "🤖 Thanks for adding me! Open <b>/settings</b> to configure me.\n⚠️ Make me an <b>admin</b> for full functionality."},
    "hi": {"only_admins": "⛔ केवल एडमिन के लिए।",
           "added": "🤖 मुझे जोड़ने के लिए धन्यवाद! <b>/settings</b> खोलें।\n⚠️ पूरी सुविधा के लिए मुझे <b>एडमिन</b> बनाएं।"},
}
def t(gid, key):
    lang = get_settings(gid).get("language", "en") if gid else "en"
    return STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))

# ============================== DATABASE ==============================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS groups(
        group_id INTEGER PRIMARY KEY, group_name TEXT, language TEXT DEFAULT 'en',
        timezone TEXT DEFAULT 'UTC', settings_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, language_code TEXT);
    CREATE TABLE IF NOT EXISTS group_members(
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, user_id INTEGER,
        role TEXT DEFAULT 'member', is_free INTEGER DEFAULT 0, warn_count INTEGER DEFAULT 0,
        joined_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(group_id, user_id));
    CREATE TABLE IF NOT EXISTS warnings(
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, user_id INTEGER,
        warned_by INTEGER, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, action TEXT, user_id INTEGER,
        admin_id INTEGER, details TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS recurring_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, message_text TEXT,
        interval_minutes INTEGER, last_sent TEXT, is_active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS banned_words(
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, word TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_bw ON banned_words(group_id, word);
    """)
    conn.commit(); conn.close()

def db_upsert_user(u):
    try:
        conn = db()
        conn.execute("""INSERT INTO users(user_id,username,first_name,last_name,language_code) VALUES(?,?,?,?,?)
                        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                        first_name=excluded.first_name, last_name=excluded.last_name""",
                     (u.id, u.username, u.first_name, u.last_name, u.language_code))
        conn.commit(); conn.close()
    except Exception as e:
        log.warning(f"db_upsert_user: {e}")

def db_find_user_by_username(username):
    conn = db()
    r = conn.execute("SELECT user_id FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    conn.close(); return r["user_id"] if r else None

def db_add_member(gid, uid):
    conn = db(); conn.execute("INSERT OR IGNORE INTO group_members(group_id,user_id) VALUES(?,?)", (gid, uid)); conn.commit(); conn.close()
def db_del_member(gid, uid):
    conn = db(); conn.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (gid, uid)); conn.commit(); conn.close()
def db_role(gid, uid):
    conn = db(); r = conn.execute("SELECT role FROM group_members WHERE group_id=? AND user_id=?", (gid, uid)).fetchone(); conn.close()
    return r["role"] if r else "member"
def db_set_role(gid, uid, role):
    conn = db()
    conn.execute("""INSERT INTO group_members(group_id,user_id,role) VALUES(?,?,?)
                    ON CONFLICT(group_id,user_id) DO UPDATE SET role=excluded.role""", (gid, uid, role))
    conn.commit(); conn.close()
def db_is_free(gid, uid):
    conn = db(); r = conn.execute("SELECT is_free FROM group_members WHERE group_id=? AND user_id=?", (gid, uid)).fetchone(); conn.close()
    return bool(r and r["is_free"])
def db_set_free(gid, uid, val):
    conn = db()
    conn.execute("""INSERT INTO group_members(group_id,user_id,is_free) VALUES(?,?,?)
                    ON CONFLICT(group_id,user_id) DO UPDATE SET is_free=excluded.is_free""", (gid, uid, 1 if val else 0))
    conn.commit(); conn.close()
def db_add_warning(gid, uid, by, reason):
    conn = db(); conn.execute("INSERT INTO warnings(group_id,user_id,warned_by,reason) VALUES(?,?,?,?)", (gid, uid, by, reason)); conn.commit(); conn.close()
def db_warn_rows(gid, uid):
    conn = db(); rows = conn.execute("SELECT reason,created_at FROM warnings WHERE group_id=? AND user_id=? ORDER BY id DESC", (gid, uid)).fetchall(); conn.close(); return rows
def db_del_last_warning(gid, uid):
    conn = db(); cur = conn.execute("DELETE FROM warnings WHERE id=(SELECT id FROM warnings WHERE group_id=? AND user_id=? ORDER BY id DESC LIMIT 1)", (gid, uid))
    conn.commit(); n = cur.rowcount; conn.close(); return n
def db_clear_warnings(gid, uid):
    conn = db(); conn.execute("DELETE FROM warnings WHERE group_id=? AND user_id=?", (gid, uid)); conn.commit(); conn.close()
def db_log(gid, action, user_id, admin_id, details=""):
    conn = db(); conn.execute("INSERT INTO logs(group_id,action,user_id,admin_id,details) VALUES(?,?,?,?,?)", (gid, action, user_id, admin_id, details)); conn.commit(); conn.close()
def db_words(gid):
    conn = db(); rows = conn.execute("SELECT word FROM banned_words WHERE group_id=?", (gid,)).fetchall(); conn.close(); return [r["word"] for r in rows]
def db_word_rows(gid):
    conn = db(); rows = conn.execute("SELECT id,word FROM banned_words WHERE group_id=? ORDER BY id", (gid,)).fetchall(); conn.close(); return rows
def db_add_word(gid, w):
    conn = db(); conn.execute("INSERT OR IGNORE INTO banned_words(group_id,word) VALUES(?,?)", (gid, w)); conn.commit(); conn.close()
def db_del_word(wid):
    conn = db(); conn.execute("DELETE FROM banned_words WHERE id=?", (wid,)); conn.commit(); conn.close()
def db_clear_words(gid):
    conn = db(); conn.execute("DELETE FROM banned_words WHERE group_id=?", (gid,)); conn.commit(); conn.close()
def db_add_recurring(gid, text, minutes):
    conn = db(); conn.execute("INSERT INTO recurring_messages(group_id,message_text,interval_minutes,is_active) VALUES(?,?,?,1)", (gid, text, minutes)); conn.commit(); conn.close()
def db_list_recurring(gid):
    conn = db(); rows = conn.execute("SELECT id,message_text,interval_minutes FROM recurring_messages WHERE group_id=? AND is_active=1", (gid,)).fetchall(); conn.close(); return rows
def db_del_recurring(rid):
    conn = db(); conn.execute("DELETE FROM recurring_messages WHERE id=?", (rid,)); conn.commit(); conn.close()

# ============================== SETTINGS (JSON per group) ==============================
DEFAULT_SETTINGS = {
    "welcome":  {"enabled": True,  "text": "👋 Welcome {mention} to {groupname}!", "media": None, "buttons": []},
    "goodbye":  {"enabled": True,  "text": "👋 {name} has left {groupname}.", "media": None, "buttons": []},
    "rules":    {"text": "📜 No rules have been set yet.", "media": None, "buttons": []},
    "antispam": {"enabled": False, "action": "mute", "duration": 1800},
    "antiflood":{"enabled": False, "window": 5, "max": 8, "action": "mute", "duration": 600},
    "warns":    {"enabled": True,  "max": 3, "action": "ban", "duration": 0},
    "link":     {"enabled": False, "action": "delete", "duration": 0, "whitelist": []},
    "media":    {"photos": True, "videos": True, "files": True, "voice": True, "audio": True, "gifs": True, "action": "delete", "duration": 600},
    "night":    {"enabled": False, "start": "23:00", "end": "07:00", "action": "mute", "message": ""},
    "blocks":   {"forwards": False, "channel": False, "commands": False, "service": False},
    "atadmin":  {"cooldown": 60, "message": ""},
    "approval": {"enabled": False},
    "deleting": {"commands": {"enabled": False, "delay": 10},
                 "service":  {"join": False, "leave": False, "pin": False},
                 "sched":    {"enabled": False, "hours": 6},
                 "selfd":    {"enabled": False, "minutes": 30, "scope": "users"}},
    "bw":       {"enabled": False, "action": "delete", "duration": 3600},
    "msglen":   {"enabled": False, "max": 1500, "action": "delete", "duration": 600},
    "logchannel": None, "language": "en",
}

def _merge(d, dflt):
    for k, v in dflt.items():
        if k not in d: d[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(d[k], dict): _merge(d[k], v)
    return d

SET_CACHE = {}
def get_settings(gid):
    ent = SET_CACHE.get(gid)
    if ent and time.time() - ent[1] < 5: return ent[0]
    conn = db()
    row = conn.execute("SELECT settings_json FROM groups WHERE group_id=?", (gid,)).fetchone()
    if not row:
        conn.execute("INSERT OR IGNORE INTO groups(group_id,group_name,settings_json) VALUES(?,?,?)", (gid, "", json.dumps(DEFAULT_SETTINGS)))
        conn.commit()
        row = conn.execute("SELECT settings_json FROM groups WHERE group_id=?", (gid,)).fetchone()
    conn.close()
    s = _merge(json.loads(row["settings_json"] or "{}"), DEFAULT_SETTINGS)
    SET_CACHE[gid] = (s, time.time())
    return s

def save_settings(gid, s):
    conn = db(); conn.execute("UPDATE groups SET settings_json=? WHERE group_id=?", (json.dumps(s), gid)); conn.commit(); conn.close()
    SET_CACHE[gid] = (s, time.time())

# ============================== HELPERS ==============================
def parse_time(s):
    if not s: return 0
    m = re.fullmatch(r"(\d+)\s*([smhd])", str(s).strip().lower())
    if not m: return 0
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]

def fmt_time(sec):
    if not sec: return "permanent"
    if sec % 86400 == 0: return f"{sec//86400}d"
    if sec % 3600 == 0: return f"{sec//3600}h"
    if sec % 60 == 0: return f"{sec//60}m"
    return f"{sec}s"

def next_in(lst, cur):
    try: return lst[(lst.index(cur) + 1) % len(lst)]
    except ValueError: return lst[0]

def host_of(u):
    if not u.startswith("http"): u = "http://" + u
    return urlparse(u).netloc.lower().removeprefix("www.")

def hhmm_to_min(v):
    try:
        h, m = v.split(":"); return int(h) * 60 + int(m)
    except Exception: return None

def in_window(start, end, now=None):
    now = now or datetime.now(timezone.utc)
    s, e = hhmm_to_min(start), hhmm_to_min(end)
    if s is None or e is None: return False
    cur = now.hour * 60 + now.minute
    return (s <= cur < e) if s <= e else (cur >= s or cur < e)

def mention_html(u): return f'<a href="tg://user?id={u.id}">{esc(u.first_name or "User")}</a>'
def st(b): return "✅" if b else "❌"

ROLES = {"founder": 6, "cofounder": 5, "admin": 4, "moderator": 3, "cleaner": 2, "muter": 2, "helper": 1, "free": 1, "member": 0}
ROLE_LABEL = {"founder": "👑 Founder", "cofounder": "⚜️ Co-Founder", "admin": "👮 Admin", "moderator": "👷 Moderator",
              "cleaner": "🛃 Chat Cleaner", "muter": "🙊 Muter", "helper": "⛑ Helper", "free": "🔓 Free", "member": "👤 Member"}

ADMIN_CACHE, ATCOOL = {}, {}
FLOOD = defaultdict(lambda: deque(maxlen=120))
SPAM  = defaultdict(lambda: deque(maxlen=40))
TRACK = defaultdict(lambda: deque(maxlen=800))
USERS_SEEN = set()

async def tg_admin_ids(context, chat_id, force=False):
    ent = ADMIN_CACHE.get(chat_id); now = time.time()
    if force or ent is None or now - ent[1] > 300:
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            ADMIN_CACHE[chat_id] = ({a.user.id for a in admins}, now)
        except TelegramError as e:
            log.warning(f"get_chat_administrators: {e}")
            if ent is None: ADMIN_CACHE[chat_id] = (set(), now)
    return ADMIN_CACHE.get(chat_id, (set(), now))[0]

async def user_level(context, chat_id, user_id):
    if user_id in await tg_admin_ids(context, chat_id):
        return 4  # Telegram admin
    return ROLES.get(db_role(chat_id, user_id), 0)

async def reply(update, text, kb=None):
    try:
        return await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError as e:
        log.warning(f"reply: {e}")

async def try_delete(msg):
    try: await msg.delete()
    except TelegramError: pass

async def get_target(update, context):
    msg = update.effective_message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user
    args = context.args or []
    if not args: return None
    a = args[0]
    if a.startswith("@"):
        uid = db_find_user_by_username(a[1:])
        if not uid: return None
        try: return (await context.bot.get_chat_member(update.effective_chat.id, uid)).user
        except TelegramError: return None
    if a.lstrip("-").isdigit():
        try: return (await context.bot.get_chat_member(update.effective_chat.id, int(a))).user
        except TelegramError: return None
    return None

async def apply_action(context, gid, user, action, duration=0, msg=None, reason=""):
    try:
        if action == "delete":
            if msg: await try_delete(msg)
        elif action == "mute":
            until = datetime.now(timezone.utc) + timedelta(seconds=duration) if duration else None
            await context.bot.restrict_chat_member(gid, user.id, permissions=ChatPermissions.no_permissions(), until_date=until)
        elif action == "kick":
            await context.bot.ban_chat_member(gid, user.id, until_date=datetime.now(timezone.utc) + timedelta(seconds=45))
        elif action == "ban":
            until = datetime.now(timezone.utc) + timedelta(seconds=duration) if duration else None
            await context.bot.ban_chat_member(gid, user.id, until_date=until)
        elif action == "warn":
            db_add_warning(gid, user.id, 0, reason)
        label = {"mute": "🔇 muted", "kick": "🚪 kicked", "ban": "🔨 banned", "warn": "❗ warned", "delete": "🗑️"}.get(action)
        if label and action != "delete":
            txt = f"{label}: {mention_html(user)}" + (f" ({fmt_time(duration)})" if duration and action != 'warn' else "") + (f"\n📝 {esc(reason)}" if reason else "")
            try: await context.bot.send_message(gid, txt, parse_mode=ParseMode.HTML)
            except TelegramError: pass
        if msg and action != "delete": await try_delete(msg)
        await log_action(context, gid, f"auto_{action}", user.id, 0, reason)
    except (BadRequest, Forbidden) as e:
        log.warning(f"apply_action: {e.message}")

async def log_action(context, gid, action, user_id, admin_id, details=""):
    db_log(gid, action, user_id, admin_id, details)
    lc = get_settings(gid).get("logchannel")
    if not lc: return
    try:
        who = f"<a href='tg://user?id={user_id}'>{user_id}</a>"
        by = f"<a href='tg://user?id={admin_id}'>{admin_id}</a>" if admin_id else "system"
        await context.bot.send_message(lc, f"📋 <b>{esc(action.title())}</b>\n👤 {who}\n🛡 By: {by}" + (f"\n📝 {esc(details)}" if details else ""), parse_mode=ParseMode.HTML)
    except TelegramError: pass

def render_vars(text, user, chat):
    uname = user.username or user.first_name or ""
    ment = f'<a href="tg://user?id={user.id}">{esc(user.first_name or "User")}</a>'
    return (text.replace("{name}", esc(user.first_name or "User")).replace("{mention}", ment)
                .replace("{username}", esc(uname)).replace("{groupname}", esc(chat.title or "")).replace("{id}", str(user.id)))

async def send_composed(context, chat_id, cfg, user, chat):
    text = render_vars(cfg.get("text") or "", user, chat)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(tt, url=uu)] for tt, uu in (cfg.get("buttons") or [])]) or None
    media = cfg.get("media")
    try:
        if media:
            mtype, fid = media
            fn = {"photo": context.bot.send_photo, "video": context.bot.send_video, "animation": context.bot.send_animation,
                  "document": context.bot.send_document, "audio": context.bot.send_audio, "voice": context.bot.send_voice}.get(mtype)
            if fn: await fn(chat_id, fid, caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
            else: await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError as e:
        log.warning(f"send_composed: {e}")

# ============================== PANELS / KEYBOARDS ==============================
TMB_TITLES = {"welcome": "💬 Welcome", "goodbye": "👋 Goodbye", "rules": "📜 Regulation"}
ACT_ALL, ACT_MKB, ACT_DM, ACT_MN = ["delete","warn","mute","kick","ban"], ["mute","kick","ban"], ["delete","mute"], ["mute","nothing"]
DURS = [60, 300, 1800, 3600, 21600, 86400]
WINS, MAXS, MAXW, COOLS = [3,5,10,15,30,60], [3,5,8,10,15,20], [2,3,4,5,6], [30,60,120,300,600]

def render_panel(pid, s, gid):
    if pid == "main":
        kb = [
            [btn("📜 Regulation","p_rules"), btn("✉️ Anti-Spam","p_antispam")],
            [btn("💬 Welcome","p_welcome"), btn("🔇 Anti-Flood","p_antiflood")],
            [btn("👋 Goodbye","p_goodbye"), btn("🆘 @Admin","p_atadmin")],
            [btn("🔒 Blocks","p_blocks"), btn("📸 Media","p_media")],
            [btn("❗ Warns","p_warns"), btn("🌑 Night","p_night")],
            [btn("🔗 Link","p_link"), btn("📫 Approval","p_approval")],
            [btn("🗑️ Deleting Messages","p_deleting")],
            [btn("🇬🇧 Lang","p_lang"), btn("▶️ Other","p_other")],
            [btn("🕉 Alphabets","soon"), btn("🧠 Captcha","soon")],
            [btn("🔦 Checks","soon"), btn("🔔 Tag","soon")],
            [btn("👮 Guardian Bot","soon")],
            [btn("✅ Close","close")]]
        return "⚙️ <b>Group Settings</b>\n\nSelect a feature to configure:", InlineKeyboardMarkup(kb)

    if pid in TMB_TITLES:
        cfg = s[pid]; title = TMB_TITLES[pid]
        txt = (f"{title}\n\n📄 Text {st(bool(cfg['text']))}\n📸 Media {st(bool(cfg['media']))}\n🔘 Buttons {st(bool(cfg['buttons']))}\n\n"
               f"👉 Use the buttons below to configure the {title.split()[1].lower()}.")
        kb = [[btn("📄 Text", f"inp_{pid}_text"), btn("👀 See", f"tmb_{pid}_see_text")],
              [btn("📸 Media", f"inp_{pid}_media"), btn("👀 See", f"tmb_{pid}_see_media")],
              [btn("🔘 Buttons", f"inp_{pid}_buttons"), btn("👀 See", f"tmb_{pid}_see_buttons")],
              [btn("👀 Full preview", f"tmb_{pid}_preview")],
              [btn("🔙 Back", "p_main")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "antispam":
        a = s["antispam"]
        txt = f"✉️ <b>Anti-Spam</b>\n\n✅ Status: {st(a['enabled'])}\n⚡ Punishment: {a['action'].title()}\n⏱ Duration: {fmt_time(a['duration'])}\n\nDetects users repeating the same message."
        kb = [[btn(f"{st(a['enabled'])} Enabled","as_toggle"), btn(f"⚡ {a['action'].title()}","as_action")],
              [btn(f"⏱ {fmt_time(a['duration'])}","as_dur")], [btn("🔙 Back","p_main")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "antiflood":
        a = s["antiflood"]
        txt = (f"🔇 <b>Anti-Flood</b>\n\n✅ Status: {st(a['enabled'])}\n⏱ Time window: {a['window']}s\n📊 Max messages: {a['max']}\n"
               f"⚡ Punishment: {a['action'].title()}\n⏱ Duration: {fmt_time(a['duration'])}")
        kb = [[btn(f"{st(a['enabled'])} Enabled","af_toggle"), btn(f"⏱ {a['window']}s","af_window")],
              [btn(f"📊 {a['max']}","af_max"), btn(f"⚡ {a['action'].title()}","af_action")],
              [btn(f"⏱ {fmt_time(a['duration'])}","af_dur")], [btn("🔙 Back","p_main")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "warns":
        w = s["warns"]
        txt = f"❗ <b>Warns</b>\n\n✅ Status: {st(w['enabled'])}\n🔢 Max warnings: {w['max']}\n⚡ Action: {w['action'].title()}\n⏱ Duration: {fmt_time(w['duration'])}"
        kb = [[btn(f"{st(w['enabled'])} Enabled","wa_toggle"), btn(f"🔢 {w['max']}","wa_max")],
              [btn(f"⚡ {w['action'].title()}","wa_action"), btn(f"⏱ {fmt_time(w['duration'])}","wa_dur")],
              [btn("🔙 Back","p_main")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "atadmin":
        a = s["atadmin"]
        txt = f"🆘 <b>@Admin</b>\n\nMembers can type <code>@admin</code> to alert staff.\n\n⏱ Cooldown: {a['cooldown']}s\n📝 Custom message: {'set' if a['message'] else 'none'}"
        kb = [[btn(f"⏱ {a['cooldown']}s","at_cool"), btn("📝 Message","inp_at_msg")], [btn("🔙 Back","p_main")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "blocks":
        b = s["blocks"]
        txt = (f"🔒 <b>Blocks</b>\n\n📨 Forwarded messages {st(b['forwards'])}\n📢 Channel posts {st(b['channel'])}\n"
               f"🤖 Bot commands {st(b['commands'])}\n👤 Service messages {st(b['service'])}")
        kb = [[btn(f"📨 Forwards {st(b['forwards'])}","bl_fwd"), btn(f"📢 Channels {st(b['channel'])}","bl_chan")],
              [btn(f"🤖 Commands {st(b['commands'])}","bl_cmd"), btn(f"👤 Service {st(b['service'])}","bl_svc")],
              [btn("🔙 Back","p_main")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "media":
        m = s["media"]
        txt = (f"📸 <b>Media</b>\n\nToggle which media types members may send:\n"
               f"📷 {st(m['photos'])} 🎥 {st(m['videos'])} 📁 {st(m['files'])}\n🎤 {st(m['voice'])} 🎵 {st(m['audio'])} 🖼 {st(m['gifs'])}\n\n⚡ Punishment: {m['action'].title()}")
        kb = [[btn(f"📷 Photos {st(m['photos'])}","md_photos"), btn(f"🎥 Videos {st(m['videos'])}","md_videos")],
              [btn(f"📁 Files {st(m['files'])}","md_files"), btn(f"🎤 Voice {st(m['voice'])}","md_voice")],
              [btn(f"🎵 Audio {st(m['audio'])}","md_audio"), btn(f"🖼 GIFs {st(m['gifs'])}","md_gifs")],
              [btn(f"⚡ {m['action'].title()}","md_action"), btn(f"⏱ {fmt_time(m['duration'])}","md_dur")],
              [btn("🔙 Back","p_main")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "night":
        n = s["night"]
        txt = (f"🌑 <b>Night Mode</b>\n\n✅ Status: {st(n['enabled'])}\n🕐 Start: {n['start']}\n🕐 End: {n['end']}\n"
               f"⚡ Action: {n['action'].title()}\n📝 Message: {'set' if n['message'] else 'none'}\n\n⚠️ Times are in UTC.")
        kb = [[btn(f"{st(n['enabled'])} Enabled","nm_toggle"), btn(f"⚡ {n['action'].title()}","nm_action")],
              [btn(f"🕐 Start {n['start']}","inp_nm_start"), btn(f"🕐 End {n['end']}","inp_nm_end")],
              [btn("📝 Message","inp_nm_msg")], [btn("🔙 Back","p_main")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "link":
        l = s["link"]
        txt = f"🔗 <b>Link Filter</b>\n\n✅ Status: {st(l['enabled'])}\n⚡ Punishment: {l['action'].title()}\n📋 Whitelist: {len(l['whitelist'])} domains"
        kb = [[btn(f"{st(l['enabled'])} Enabled","lf_toggle"), btn(f"⚡ {l['action'].title()}","lf_action")],
              [btn("➕ Add domain","inp_lf_add"), btn("📋 Whitelist","lf_view")],
              [btn("🗑 Clear whitelist","lf_clear")], [btn("🔙 Back","p_main")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "approval":
        a = s["approval"]
        txt = f"📫 <b>Approval Mode</b>\n\nNew members must be approved by an admin before they can chat.\n\n✅ Status: {st(a['enabled'])}"
        kb = [[btn(f"{st(a['enabled'])} Enabled","ap_toggle")], [btn("🔙 Back","p_main")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "deleting":
        d = s["deleting"]
        txt = (f"🗑️ <b>Deleting Messages</b>\n\n🤖 Commands: {st(d['commands']['enabled'])} (delay {d['commands']['delay']}s)\n"
               f"💭 Service: join {st(d['service']['join'])} • leave {st(d['service']['leave'])} • pin {st(d['service']['pin'])}\n"
               f"🕐 Scheduled: {st(d['sched']['enabled'])} (every {d['sched']['hours']}h)\n💥 Delete all: manual\n"
               f"♻️ Self-destruction: {st(d['selfd']['enabled'])} ({d['selfd']['minutes']}m, {d['selfd']['scope']})")
        kb = [[btn(f"🤖 Commands {st(d['commands']['enabled'])}","dc_toggle"), btn(f"⏱ {d['commands']['delay']}s","inp_dc_delay")],
              [btn("💭 Service Messages","p_service")],
              [btn(f"🕐 Scheduled {st(d['sched']['enabled'])}","sc_toggle"), btn(f"{d['sched']['hours']}h","inp_sc_hours")],
              [btn("💥 Delete all messages","da_ask")],
              [btn(f"♻️ Self-dest {st(d['selfd']['enabled'])}","sdx_toggle"), btn(f"{d['selfd']['minutes']}m","inp_sdx_time"), btn(f"🔀 {d['selfd']['scope']}","sdx_scope")],
              [btn("✍️ Edit Checks","soon"), btn("📓 Block cancellation","soon")],
              [btn("🔙 Back","p_main")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "service":
        d = s["deleting"]["service"]
        txt = f"💭 <b>Service Messages</b>\n\n👤 Join messages {st(d['join'])}\n👋 Leave messages {st(d['leave'])}\n📌 Pin messages {st(d['pin'])}"
        kb = [[btn(f"👤 Join {st(d['join'])}","sv_join"), btn(f"👋 Leave {st(d['leave'])}","sv_leave")],
              [btn(f"📌 Pin {st(d['pin'])}","sv_pin")], [btn("🔙 Back","p_deleting")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "lang":
        cur = s["language"]
        txt = "🇬🇧 <b>Language</b>\n\nSelect the bot language for this group."
        kb = [[btn(f"English {st(cur=='en')}","lang_en")], [btn(f"हिंदी {st(cur=='hi')}","lang_hi")], [btn("🔙 Back","p_main")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "other":
        kb = [[btn("🔤 Banned Words","p_bw"), btn("🕐 Recurring","p_re")],
              [btn("📏 Message Length","p_msglen"), btn("🔍 Log Channel","p_logchannel")],
              [btn("🗂️ Topic","soon"), btn("👥 Members Mgmt","soon")],
              [btn("🎭 Masked Users","soon"), btn("📱 Personal Cmds","soon")],
              [btn("📢 Channels Mgmt","soon"), btn("📝 Permissions","soon")],
              [btn("🔙 Back","p_main")]]
        return "▶️ <b>Other</b>\n\nExtra features:", InlineKeyboardMarkup(kb)

    if pid == "bw":
        b = s["bw"]; n = len(db_words(gid))
        txt = (f"🔤 <b>Banned Words</b>\n\n✅ Status: {st(b['enabled'])}\n📋 Words: {n}\n"
               f"⚡ Punishment: {b['action'].title()}\n⏱ Duration: {fmt_time(b['duration'])}")
        kb = [[btn("➕ Add","inp_bw_add"), btn("📋 View","bw_view")],
              [btn("🗑 Clear all","bw_clear"), btn(f"⚡ {b['action'].title()}","bw_action")],
              [btn(f"⏱ {fmt_time(b['duration'])}","bw_dur"), btn(f"{st(b['enabled'])} Enabled","bw_toggle")],
              [btn("🔙 Back","p_other")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "re":
        rows = db_list_recurring(gid)
        kb = [[btn("➕ Add message","inp_re_text"), btn("📋 View","re_view")], [btn("🔙 Back","p_other")]]
        return f"🕐 <b>Recurring Messages</b>\n\n📋 Active: {len(rows)}", InlineKeyboardMarkup(kb)

    if pid == "msglen":
        m = s["msglen"]
        txt = f"📏 <b>Message Length</b>\n\n✅ Status: {st(m['enabled'])}\n🔢 Max characters: {m['max']}\n⚡ Punishment: {m['action'].title()}\n⏱ Duration: {fmt_time(m['duration'])}"
        kb = [[btn(f"{st(m['enabled'])} Enabled","ml_toggle"), btn(f"🔢 {m['max']}","inp_ml_max")],
              [btn(f"⚡ {m['action'].title()}","ml_action")], [btn("🔙 Back","p_other")]]
        return txt, InlineKeyboardMarkup(kb)

    if pid == "logchannel":
        lc = s["logchannel"]
        txt = f"🔍 <b>Log Channel</b>\n\nCurrent: {lc or 'None'}\n\n📋 Logged: bans, kicks, warns, settings changes."
        kb = [[btn("➕ Set channel","inp_lc_set")], [btn("❌ Remove","lc_off")], [btn("🔙 Back","p_other")]]
        return txt, InlineKeyboardMarkup(kb)
    return "?", None

def bw_view_page(gid):
    rows = db_word_rows(gid)
    txt = f"📋 <b>Banned words</b> ({len(rows)}):\n\n" + ("\n".join("• " + esc(w) for _, w in rows) or "(empty)")
    kb = [[btn(f"❌ {w}", f"bw_del_{i}")] for i, w in rows]
    kb.append([btn("🔙 Back", "p_bw")])
    return txt, InlineKeyboardMarkup(kb)

def re_view_page(gid):
    rows = db_list_recurring(gid)
    txt = f"📋 <b>Recurring messages</b> ({len(rows)}):\n\n" + ("\n".join(f"#{r['id']} • every {r['interval_minutes']}m — {esc(r['message_text'][:40])}" for r in rows) or "(empty)")
    kb = [[btn(f"🗑 Delete #{r['id']}", f"re_del_{r['id']}")] for r in rows]
    kb.append([btn("🔙 Back", "p_re")])
    return txt, InlineKeyboardMarkup(kb)

def lf_view_page(s):
    wl = s["link"]["whitelist"]
    txt = "📋 <b>Whitelist:</b>\n" + ("\n".join("• " + esc(d) for d in wl) or "(empty)")
    return txt, InlineKeyboardMarkup([[btn("🔙 Back", "p_link")]])

async def edit_q(q, text, kb):
    try: await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower(): log.warning(f"edit_q: {e.message}")

async def send_panel(context, chat_id, pid):
    s = get_settings(chat_id)
    txt, kb = render_panel(pid, s, chat_id)
    try: await context.bot.send_message(chat_id, txt, parse_mode=ParseMode.HTML, reply_markup=kb)
    except TelegramError: pass

# ============================== COMMANDS ==============================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add me to a Group", url=f"https://t.me/{context.bot.username}?startgroup=true")]])
        await update.effective_message.reply_html(
            "🤖 <b>GroupManagerBot</b>\n\nAdvanced group management: moderation, anti-spam, anti-flood, welcome messages, filters & much more!\n\n➕ Add me to your group and make me <b>admin</b>.", reply_markup=kb)
    else:
        await reply(update, "👋 Hi! Open /settings to configure me.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, "📖 <b>Commands</b>\n\n"
        "🛠 <b>General</b>\n/settings — settings menu\n/rules — show rules\n/lang — change language\n/reload — refresh admin list\n\n"
        "🛡 <b>Moderation</b>\n/ban @user [time] [reason]\n/unban @user\n/kick @user\n/mute @user [time] [reason]\n/unmute @user\n"
        "/warn @user [reason]\n/unwarn @user\n/warns [user]\n/del — delete replied message\n\n"
        "👥 <b>Roles</b>\n/mod @user — make moderator\n/unmod @user\n/admin @user — make admin\n/free @user — punish immunity\n/staff — staff list")

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in GROUPS:
        return await reply(update, "➕ Add me to a group first, then use /settings there.")
    if await user_level(context, chat.id, update.effective_user.id) < 4:
        return await reply(update, t(chat.id, "only_admins"))
    conn = db(); conn.execute("UPDATE groups SET group_name=? WHERE group_id=?", (chat.title or "", chat.id)); conn.commit(); conn.close()
    await send_panel(context, chat.id, "main")
    await try_delete(update.effective_message)

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in GROUPS: return
    if await user_level(context, chat.id, update.effective_user.id) < 4: return await reply(update, t(chat.id, "only_admins"))
    ids = await tg_admin_ids(context, chat.id, force=True)
    await reply(update, f"🔄 Admin list reloaded — <b>{len(ids)}</b> admins found.")

async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in GROUPS: return
    if await user_level(context, chat.id, update.effective_user.id) < 4: return await reply(update, t(chat.id, "only_admins"))
    txt, kb = render_panel("lang", get_settings(chat.id), chat.id)
    await reply(update, txt, kb)

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in GROUPS: return
    s = get_settings(chat.id)
    await send_composed(context, chat.id, s["rules"], update.effective_user, chat)

async def _moderate(update, context, action):
    chat = update.effective_chat
    if chat.type not in GROUPS: return
    actor = update.effective_user.id
    my = await user_level(context, chat.id, actor)
    if my < 3: return await reply(update, t(chat.id, "only_admins"))
    target = await get_target(update, context)
    if not target: return await reply(update, f"👉 Reply to a message or use /{action} @user [time] [reason]")
    if target.id == context.bot.id: return await reply(update, "😅 That's me!")
    if target.id != actor and await user_level(context, chat.id, target.id) >= my:
        return await reply(update, "⛔ You can't moderate this user.")
    args = list(context.args or [])
    reply_mode = bool(update.effective_message.reply_to_message)
    if reply_mode: tidx = 0 if (args and parse_time(args[0])) else None
    else: tidx = 1 if (len(args) > 1 and parse_time(args[1])) else None
    dur = parse_time(args[tidx]) if tidx is not None else 0
    reason = " ".join(args[tidx + 1:]) if tidx is not None else " ".join(args[0 if reply_mode else 1:])
    try:
        now = datetime.now(timezone.utc)
        if action == "ban":
            await context.bot.ban_chat_member(chat.id, target.id, until_date=now + timedelta(seconds=dur) if dur else None)
            txt = f"🔨 {mention_html(target)} <b>banned</b> {'for ' + fmt_time(dur) if dur else 'permanently'}"
        elif action == "unban":
            await context.bot.unban_chat_member(chat.id, target.id, only_if_banned=True)
            txt = f"✅ {mention_html(target)} <b>unbanned</b>"
        elif action == "kick":
            await context.bot.ban_chat_member(chat.id, target.id, until_date=now + timedelta(seconds=45))
            txt = f"🚪 {mention_html(target)} <b>kicked</b> (can rejoin)"
        elif action == "mute":
            await context.bot.restrict_chat_member(chat.id, target.id, permissions=ChatPermissions.no_permissions(), until_date=now + timedelta(seconds=dur) if dur else None)
            txt = f"🔇 {mention_html(target)} <b>muted</b> {'for ' + fmt_time(dur) if dur else 'permanently'}"
        elif action == "unmute":
            await context.bot.restrict_chat_member(chat.id, target.id, permissions=ChatPermissions.all_permissions())
            txt = f"🔊 {mention_html(target)} <b>unmuted</b>"
        if reason: txt += f"\n📝 {esc(reason)}"
        await reply(update, txt)
        await log_action(context, chat.id, action, target.id, actor, reason)
    except (BadRequest, Forbidden) as e:
        await reply(update, f"❌ Failed: {esc(e.message)}\n⚠️ Make sure I have admin rights with ban/restrict permission.")

async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in GROUPS: return
    if await user_level(context, chat.id, update.effective_user.id) < 3: return await reply(update, t(chat.id, "only_admins"))
    target = await get_target(update, context)
    if not target: return await reply(update, "👉 Reply to a message or use /warn @user [reason]")
    reply_mode = bool(update.effective_message.reply_to_message)
    args = list(context.args or [])
    reason = " ".join(args if reply_mode else args[1:])
    db_add_warning(chat.id, target.id, update.effective_user.id, reason)
    cnt = len(db_warn_rows(chat.id, target.id))
    cfg = get_settings(chat.id)["warns"]
    if cfg["enabled"] and cnt >= cfg["max"]:
        db_clear_warnings(chat.id, target.id)
        await apply_action(context, chat.id, target, cfg["action"], cfg["duration"], reason=f"reached {cnt} warnings")
        await reply(update, f"❗ {mention_html(target)} reached <b>{cnt}</b> warnings → {cfg['action'].title()}!")
    else:
        await reply(update, f"❗ {mention_html(target)} warned (<b>{cnt}</b>/{cfg['max']})" + (f"\n📝 {esc(reason)}" if reason else ""))
    await log_action(context, chat.id, "warn", target.id, update.effective_user.id, reason)

async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in GROUPS: return
    if await user_level(context, chat.id, update.effective_user.id) < 3: return await reply(update, t(chat.id, "only_admins"))
    target = await get_target(update, context)
    if not target: return await reply(update, "👉 Reply or /unwarn @user")
    if db_del_last_warning(chat.id, target.id):
        await reply(update, f"✅ Removed one warning from {mention_html(target)} (<b>{len(db_warn_rows(chat.id, target.id))}</b> left)")
    else:
        await reply(update, "ℹ️ No warnings found.")

async def cmd_warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in GROUPS: return
    target = await get_target(update, context) or update.effective_user
    rows = db_warn_rows(chat.id, target.id)
    lines = [f"❗ <b>Warnings for {mention_html(target)}: {len(rows)}</b>"] + [f"• {esc(r['reason'] or 'no reason')}" for r in rows[:10]]
    await reply(update, "\n".join(lines))

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat; msg = update.effective_message
    if chat.type not in GROUPS: return
    if await user_level(context, chat.id, update.effective_user.id) < 2: return await reply(update, t(chat.id, "only_admins"))
    if not msg.reply_to_message: return await reply(update, "👉 Reply to a message with /del")
    try: await msg.reply_to_message.delete()
    except TelegramError as e: return await reply(update, f"❌ {esc(e.message)}")
    await try_delete(msg)

async def _setrole(update, context, role, need):
    chat = update.effective_chat
    if chat.type not in GROUPS: return
    actor = update.effective_user.id
    my = await user_level(context, chat.id, actor)
    if my < need: return await reply(update, t(chat.id, "only_admins"))
    target = await get_target(update, context)
    if not target: return await reply(update, "👉 Reply to a message or use the command with @user")
    if role == "admin":
        try:
            m = await context.bot.get_chat_member(chat.id, actor)
            if m.status != ChatMemberStatus.OWNER and my < 5:
                return await reply(update, "⛔ Only 👑 Founder / ⚜️ Co-Founder can assign Admins.")
        except TelegramError: pass
    db_set_role(chat.id, target.id, role)
    labels = {"moderator": f"👷 {mention_html(target)} is now a <b>Moderator</b>.",
              "admin": f"👮 {mention_html(target)} is now an <b>Admin</b>.",
              "member": f"👤 {mention_html(target)} role removed (now Member)."}
    await reply(update, labels[role])
    await log_action(context, chat.id, "set_role", target.id, actor, role)

async def cmd_mod(update, context): await _setrole(update, context, "moderator", 4)
async def cmd_unmod(update, context): await _setrole(update, context, "member", 4)
async def cmd_admin(update, context): await _setrole(update, context, "admin", 4)

async def cmd_free(update, context):
    chat = update.effective_chat
    if chat.type not in GROUPS: return
    if await user_level(context, chat.id, update.effective_user.id) < 4: return await reply(update, t(chat.id, "only_admins"))
    target = await get_target(update, context)
    if not target: return await reply(update, "👉 Reply or /free @user")
    cur = db_is_free(chat.id, target.id)
    db_set_free(chat.id, target.id, not cur)
    await reply(update, (f"🔒 {mention_html(target)} immunity removed." if cur else f"🔓 {mention_html(target)} is now immune to auto-punishments."))

async def name_of(context, chat_id, uid):
    try: return esc((await context.bot.get_chat_member(chat_id, uid)).user.first_name or str(uid))
    except TelegramError: return str(uid)

async def cmd_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in GROUPS: return
    conn = db()
    rows = conn.execute("SELECT user_id, role FROM group_members WHERE group_id=? AND role NOT IN ('member')", (chat.id,)).fetchall()
    conn.close()
    lines = ["👥 <b>Staff</b>\n"]
    for role in ("founder", "cofounder", "admin", "moderator", "cleaner", "muter", "helper", "free"):
        for r in rows:
            if r["role"] == role:
                lines.append(f"{ROLE_LABEL[role]} — {await name_of(context, chat.id, r['user_id'])}")
    known = {r["user_id"] for r in rows}
    extra = [i for i in await tg_admin_ids(context, chat.id) if i not in known]
    if extra:
        lines.append("\n👮 <b>Telegram Admins</b>: " + " ".join(f"<a href='tg://user?id={i}'>•</a>" for i in extra))
    await reply(update, "\n".join(lines))

# ============================== INPUT CONVERSATION ==============================
ASK = 0
PROMPTS = {
    "welcome_text": "✍️ Send the <b>welcome text</b>.\nVariables: <code>{name} {mention} {username} {groupname} {id}</code>\n/cancel to abort.",
    "goodbye_text": "✍️ Send the <b>goodbye text</b>.\n/cancel to abort.",
    "rules_text": "✍️ Send the <b>rules text</b>.\n/cancel to abort.",
    "welcome_media": "📸 Send a <b>photo/video/GIF/document</b> (with optional caption).\nSend <code>clear</code> to remove. /cancel to abort.",
    "goodbye_media": "📸 Send a <b>photo/video/GIF/document</b>.\nSend <code>clear</code> to remove. /cancel to abort.",
    "rules_media": "📸 Send a <b>photo/video/GIF/document</b>.\nSend <code>clear</code> to remove. /cancel to abort.",
    "welcome_buttons": "🔘 Send buttons, one per line:\n<code>Button Text - https://example.com</code>\nSend <code>clear</code> to remove all. /cancel to abort.",
    "goodbye_buttons": "🔘 Send buttons, one per line:\n<code>Button Text - https://example.com</code>\nSend <code>clear</code> to remove all. /cancel to abort.",
    "rules_buttons": "🔘 Send buttons, one per line:\n<code>Button Text - https://example.com</code>\nSend <code>clear</code> to remove all. /cancel to abort.",
    "lf_add": "➕ Send domain(s) to whitelist (space separated).\ne.g. <code>example.com t.me</code>",
    "nm_start": "🕐 Send <b>start time</b> in HH:MM (24h).\ne.g. <code>23:00</code>",
    "nm_end": "🕐 Send <b>end time</b> in HH:MM (24h).\ne.g. <code>07:00</code>",
    "nm_msg": "📝 Send the custom night-mode message (or <code>clear</code>).",
    "at_msg": "📝 Send the custom @admin alert message (or <code>clear</code>).",
    "dc_delay": "⏱ Send the delay in <b>seconds</b> (0-3600) for deleting commands.",
    "sc_hours": "🕐 Send the interval in <b>hours</b> (1-48) for scheduled deletion.",
    "sdx_time": "⏱ Send self-destruction time in <b>minutes</b> (1-1440).",
    "ml_max": "🔢 Send the maximum <b>message length</b> in characters (50-4096).",
    "bw_add": "🔤 Send banned word(s), separated by commas.",
    "re_text": "✍️ Send the text of the recurring message.",
    "lc_set": "🔍 <b>Forward a message</b> from your log channel here, or send the channel ID (e.g. <code>-1001234567890</code>).\nI must be admin in that channel.",
}

async def ask_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    target = q.data[4:]
    context.user_data["inp"] = target
    await q.answer()
    m = await q.message.reply_text(PROMPTS.get(target, "✍️ Send your input now:"), parse_mode=ParseMode.HTML,
                                   reply_markup=InlineKeyboardMarkup([[btn("❌ Cancel", "cancel_input")]]))
    context.user_data["inp_prompt"] = m.message_id
    return ASK

async def cancel_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("inp", None); context.user_data.pop("re_text", None)
    if update.callback_query:
        await update.callback_query.answer("❌ Cancelled")
    else:
        await reply(update, "❌ Cancelled.")
    return ConversationHandler.END

async def finish_input(context, chat_id, panel, msg):
    prompt_id = context.user_data.pop("inp_prompt", None)
    context.user_data.pop("inp", None)
    for mid in (prompt_id, msg.message_id):
        if mid:
            try: await context.bot.delete_message(chat_id, mid)
            except TelegramError: pass
    await send_panel(context, chat_id, panel)

async def receive_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; chat = update.effective_chat
    target = context.user_data.get("inp")
    if not target: return ConversationHandler.END
    gid = chat.id
    if await user_level(context, gid, update.effective_user.id) < 4:
        context.user_data.pop("inp", None)
        await msg.reply_text("⛔ Admins only."); return ConversationHandler.END
    s = get_settings(gid)
    text = (msg.text or "").strip() if msg.text else None

    async def bad(m): await msg.reply_text(f"⚠️ {m}", parse_mode=ParseMode.HTML); return ASK

    if target in ("welcome_text", "goodbye_text", "rules_text"):
        if text is None: return await bad("Please send text.")
        feat = target.split("_")[0]; s[feat]["text"] = msg.text; save_settings(gid, s)
        await finish_input(context, gid, feat, msg); return ConversationHandler.END

    if target in ("welcome_media", "goodbye_media", "rules_media"):
        feat = target.split("_")[0]
        if text and text.lower() == "clear":
            s[feat]["media"] = None; save_settings(gid, s); await finish_input(context, gid, feat, msg); return ConversationHandler.END
        got = None
        if msg.photo: got = ["photo", msg.photo[-1].file_id]
        elif msg.video: got = ["video", msg.video.file_id]
        elif msg.animation: got = ["animation", msg.animation.file_id]
        elif msg.document: got = ["document", msg.document.file_id]
        elif msg.audio: got = ["audio", msg.audio.file_id]
        elif msg.voice: got = ["voice", msg.voice.file_id]
        if not got: return await bad("Send a photo/video/GIF/document (or <code>clear</code>).")
        s[feat]["media"] = got; save_settings(gid, s)
        await finish_input(context, gid, feat, msg); return ConversationHandler.END

    if target in ("welcome_buttons", "goodbye_buttons", "rules_buttons"):
        feat = target.split("_")[0]
        if text and text.lower() == "clear":
            s[feat]["buttons"] = []; save_settings(gid, s); await finish_input(context, gid, feat, msg); return ConversationHandler.END
        if text is None: return await bad("Send lines like: <code>Google - https://google.com</code>")
        added = 0
        for line in msg.text.splitlines():
            if " - " not in line: continue
            tt, uu = line.split(" - ", 1); uu = uu.strip()
            if not uu.startswith("http"): continue
            s[feat]["buttons"].append([tt.strip(), uu]); added += 1
        if not added: return await bad("No valid lines. Format: <code>Text - https://url</code>")
        save_settings(gid, s); await finish_input(context, gid, feat, msg); return ConversationHandler.END

    if target == "lf_add":
        if text is None: return await bad("Send domain(s).")
        doms = [d.lower().strip().removeprefix("www.") for d in re.split(r"[\s,]+", text) if d.strip()]
        wl = s["link"]["whitelist"]
        wl.extend([d for d in doms if d and d not in wl])
        save_settings(gid, s); await finish_input(context, gid, "link", msg); return ConversationHandler.END

    if target in ("nm_start", "nm_end"):
        if text is None or not re.fullmatch(r"\d{1,2}:\d{2}", text): return await bad("Format: HH:MM e.g. <code>23:00</code>")
        h, m = map(int, text.split(":"))
        if h > 23 or m > 59: return await bad("Invalid time.")
        s["night"]["start" if target == "nm_start" else "end"] = f"{h:02d}:{m:02d}"
        save_settings(gid, s); await finish_input(context, gid, "night", msg); return ConversationHandler.END

    if target == "nm_msg":
        s["night"]["message"] = "" if (text and text.lower() == "clear") else (msg.text or "")
        save_settings(gid, s); await finish_input(context, gid, "night", msg); return ConversationHandler.END

    if target == "at_msg":
        s["atadmin"]["message"] = "" if (text and text.lower() == "clear") else (msg.text or "")
        save_settings(gid, s); await finish_input(context, gid, "atadmin", msg); return ConversationHandler.END

    if target == "dc_delay":
        if text is None or not text.isdigit(): return await bad("Send a number of seconds.")
        s["deleting"]["commands"]["delay"] = max(0, min(3600, int(text)))
        save_settings(gid, s); await finish_input(context, gid, "deleting", msg); return ConversationHandler.END

    if target == "sc_hours":
        if text is None or not text.isdigit(): return await bad("Send a number of hours.")
        s["deleting"]["sched"]["hours"] = max(1, min(48, int(text)))
        save_settings(gid, s); _reschedule_sched(context, gid, s["deleting"]["sched"])
        await finish_input(context, gid, "deleting", msg); return ConversationHandler.END

    if target == "sdx_time":
        if text is None or not text.isdigit(): return await bad("Send a number of minutes.")
        s["deleting"]["selfd"]["minutes"] = max(1, min(1440, int(text)))
        save_settings(gid, s); await finish_input(context, gid, "deleting", msg); return ConversationHandler.END

    if target == "ml_max":
        if text is None or not text.isdigit(): return await bad("Send a number of characters.")
        s["msglen"]["max"] = max(50, min(4096, int(text)))
        save_settings(gid, s); await finish_input(context, gid, "msglen", msg); return ConversationHandler.END

    if target == "bw_add":
        if text is None: return await bad("Send word(s).")
        for w in [w.lower().strip() for w in re.split(r"[,\n]+", text) if w.strip()]:
            db_add_word(gid, w)
        await finish_input(context, gid, "bw", msg); return ConversationHandler.END

    if target == "lc_set":
        ch = None
        if msg.sender_chat and msg.sender_chat.type == ChatType.CHANNEL: ch = msg.sender_chat.id
        elif getattr(msg, "forward_from_chat", None) and msg.forward_from_chat.type == ChatType.CHANNEL: ch = msg.forward_from_chat.id
        elif text and re.fullmatch(r"-100\d+", text): ch = int(text)
        if ch is None: return await bad("Forward a channel message or send an ID like <code>-100...</code>")
        try:
            m = await context.bot.get_chat_member(ch, context.bot.id)
            if m.status != ChatMemberStatus.ADMINISTRATOR: return await bad("Make me admin in that channel first.")
        except TelegramError:
            return await bad("Can't access that channel. Add me as admin there.")
        s["logchannel"] = ch; save_settings(gid, s)
        await finish_input(context, gid, "logchannel", msg); return ConversationHandler.END

    if target == "re_text":
        if text is None: return await bad("Send message text.")
        context.user_data["re_text"] = msg.text
        context.user_data["inp"] = "re_interval"
        await msg.reply_text("⏱ Now send the interval in <b>minutes</b>:", parse_mode=ParseMode.HTML)
        return ASK

    if target == "re_interval":
        if text is None or not text.isdigit(): return await bad("Send minutes (number).")
        rtext = context.user_data.pop("re_text", None)
        if not rtext: return ConversationHandler.END
        db_add_recurring(gid, rtext, max(1, int(text)))
        await finish_input(context, gid, "re", msg); return ConversationHandler.END

    return await bad("Unknown input. Use /cancel.")

# ============================== CALLBACK ROUTER ==============================
def _reschedule_sched(context, gid, sc):
    if not application.job_queue: return
    for j in application.job_queue.get_jobs_by_name(f"sched_{gid}"): j.schedule_removal()
    if sc["enabled"]:
        application.job_queue.run_repeating(sched_del_job, interval=sc["hours"] * 3600, first=sc["hours"] * 3600, name=f"sched_{gid}", chat_id=gid)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    if not q.message: return await q.answer()
    chat = q.message.chat; gid = chat.id

    if data == "close":
        await q.answer("Closed ✅"); return await try_delete(q.message)
    if data == "soon":
        return await q.answer("🚧 This feature is coming soon!\n\nStay tuned for updates.", show_alert=True)
    if data.startswith("apok_"):
        await q.answer()
        if await user_level(context, gid, q.from_user.id) < 3:
            return await q.answer("⛔ Admins only.", show_alert=True)
        uid = int(data.split("_")[1])
        try:
            await context.bot.restrict_chat_member(gid, uid, permissions=ChatPermissions.all_permissions())
            await q.edit_message_text("✅ User approved — they can chat now.")
        except TelegramError as e:
            await q.answer(f"❌ {e.message}", show_alert=True)
        return

    if await user_level(context, gid, q.from_user.id) < 4:
        return await q.answer("⛔ Admins only.", show_alert=True)
    try: await q.answer()
    except TelegramError: pass

    s = get_settings(gid)
    changed = None

    # ---- open panels ----
    if data in ("p_main", "settings_main"):
        return await edit_q(q, *render_panel("main", s, gid))
    if data.startswith("p_"):
        pid = data[2:]
        if pid in ("main","welcome","goodbye","rules","antispam","antiflood","warns","atadmin","blocks","media","night",
                   "link","approval","deleting","service","lang","other","bw","re","msglen","logchannel"):
            return await edit_q(q, *render_panel(pid, s, gid))

    # ---- TMB (Text/Media/Buttons) features ----
    if data.startswith("tmb_"):
        _, feat, act = data.split("_", 2)
        cfg = s[feat]; emoji, title = TMB_TITLES[feat].split()[0], TMB_TITLES[feat].split()[1]
        if act == "see_text":
            txt = f"{emoji} <b>{title}</b>\n\n📄 <b>Current text:</b>\n\n{esc(cfg['text']) or '(empty)'}"
            return await edit_q(q, txt, InlineKeyboardMarkup([[btn("🗑 Clear text", f"tmb_{feat}_clear_text")], [btn("🔙 Back", f"p_{feat}")]]))
        if act == "clear_text":
            cfg["text"] = ""; save_settings(gid, s); return await edit_q(q, *render_panel(feat, s, gid))
        if act == "see_media":
            if cfg["media"]:
                mtype, fid = cfg["media"]
                try:
                    fn = {"photo": context.bot.send_photo, "video": context.bot.send_video, "animation": context.bot.send_animation,
                          "document": context.bot.send_document, "audio": context.bot.send_audio, "voice": context.bot.send_voice}.get(mtype)
                    if fn: await fn(gid, fid, caption=f"📸 Current {title.lower()} media:")
                except TelegramError: pass
                txt = f"{emoji} <b>{title}</b>\n\n📸 Media is set. (Preview sent above)"
            else:
                txt = f"{emoji} <b>{title}</b>\n\n📸 No media set."
            return await edit_q(q, txt, InlineKeyboardMarkup([[btn("🗑 Clear media", f"tmb_{feat}_clear_media")], [btn("🔙 Back", f"p_{feat}")]]))
        if act == "clear_media":
            cfg["media"] = None; save_settings(gid, s); return await edit_q(q, *render_panel(feat, s, gid))
        if act == "see_buttons":
            rows = cfg["buttons"]
            txt = "🔘 <b>Buttons:</b>\n" + ("\n".join(f"• {esc(tt)} → {esc(uu)}" for tt, uu in rows) or "(none)")
            return await edit_q(q, txt, InlineKeyboardMarkup([[btn("🗑 Clear all", f"tmb_{feat}_clear_buttons")], [btn("🔙 Back", f"p_{feat}")]]))
        if act == "clear_buttons":
            cfg["buttons"] = []; save_settings(gid, s); return await edit_q(q, *render_panel(feat, s, gid))
        if act == "preview":
            await send_composed(context, gid, cfg, q.from_user, chat)
            return await q.answer("👀 Preview sent below", show_alert=False)

    # ---- views ----
    if data == "bw_view": return await edit_q(q, *bw_view_page(gid))
    if data == "re_view": return await edit_q(q, *re_view_page(gid))
    if data == "lf_view": return await edit_q(q, *lf_view_page(s))
    if data.startswith("bw_del_"):
        db_del_word(int(data.split("_")[2])); return await edit_q(q, *bw_view_page(gid))
    if data.startswith("re_del_"):
        db_del_recurring(int(data.split("_")[2])); return await edit_q(q, *re_view_page(gid))

    # ---- delete all confirm ----
    if data == "da_ask":
        return await edit_q(q, "⚠️ <b>Delete all messages?</b>\n\nThis deletes every message I've seen in this group. Cannot be undone!",
                            InlineKeyboardMarkup([[btn("💥 Yes, delete all", "da_yes")], [btn("❌ Cancel", "p_deleting")]]))
    if data == "da_yes":
        ids = list(TRACK.get(gid, [])); TRACK[gid].clear(); n = 0
        for mid in ids:
            try: await context.bot.delete_message(gid, mid); n += 1
            except TelegramError: pass
        return await edit_q(q, f"🧹 Deleted <b>{n}</b> messages.", InlineKeyboardMarkup([[btn("🔙 Back", "p_deleting")]]))

    # ---- toggles & cycles ----
    if data == "as_toggle": s["antispam"]["enabled"] = not s["antispam"]["enabled"]; changed = "antispam"
    elif data == "as_action": s["antispam"]["action"] = next_in(ACT_ALL, s["antispam"]["action"]); changed = "antispam"
    elif data == "as_dur": s["antispam"]["duration"] = next_in(DURS, s["antispam"]["duration"]); changed = "antispam"
    elif data == "af_toggle": s["antiflood"]["enabled"] = not s["antiflood"]["enabled"]; changed = "antiflood"
    elif data == "af_window": s["antiflood"]["window"] = next_in(WINS, s["antiflood"]["window"]); changed = "antiflood"
    elif data == "af_max": s["antiflood"]["max"] = next_in(MAXS, s["antiflood"]["max"]); changed = "antiflood"
    elif data == "af_action": s["antiflood"]["action"] = next_in(ACT_ALL, s["antiflood"]["action"]); changed = "antiflood"
    elif data == "af_dur": s["antiflood"]["duration"] = next_in(DURS, s["antiflood"]["duration"]); changed = "antiflood"
    elif data == "wa_toggle": s["warns"]["enabled"] = not s["warns"]["enabled"]; changed = "warns"
    elif data == "wa_max": s["warns"]["max"] = next_in(MAXW, s["warns"]["max"]); changed = "warns"
    elif data == "wa_action": s["warns"]["action"] = next_in(ACT_MKB, s["warns"]["action"]); changed = "warns"
    elif data == "wa_dur": s["warns"]["duration"] = next_in(DURS, s["warns"]["duration"]); changed = "warns"
    elif data == "at_cool": s["atadmin"]["cooldown"] = next_in(COOLS, s["atadmin"]["cooldown"]); changed = "atadmin"
    elif data == "bl_fwd": s["blocks"]["forwards"] = not s["blocks"]["forwards"]; changed = "blocks"
    elif data == "bl_chan": s["blocks"]["channel"] = not s["blocks"]["channel"]; changed = "blocks"
    elif data == "bl_cmd": s["blocks"]["commands"] = not s["blocks"]["commands"]; changed = "blocks"
    elif data == "bl_svc": s["blocks"]["service"] = not s["blocks"]["service"]; changed = "blocks"
    elif data == "md_photos": s["media"]["photos"] = not s["media"]["photos"]; changed = "media"
    elif data == "md_videos": s["media"]["videos"] = not s["media"]["videos"]; changed = "media"
    elif data == "md_files": s["media"]["files"] = not s["media"]["files"]; changed = "media"
    elif data == "md_voice": s["media"]["voice"] = not s["media"]["voice"]; changed = "media"
    elif data == "md_audio": s["media"]["audio"] = not s["media"]["audio"]; changed = "media"
    elif data == "md_gifs": s["media"]["gifs"] = not s["media"]["gifs"]; changed = "media"
    elif data == "md_action": s["media"]["action"] = next_in(ACT_DM, s["media"]["action"]); changed = "media"
    elif data == "md_dur": s["media"]["duration"] = next_in(DURS, s["media"]["duration"]); changed = "media"
    elif data == "nm_toggle": s["night"]["enabled"] = not s["night"]["enabled"]; changed = "night"
    elif data == "nm_action": s["night"]["action"] = next_in(ACT_MN, s["night"]["action"]); changed = "night"
    elif data == "lf_toggle": s["link"]["enabled"] = not s["link"]["enabled"]; changed = "link"
    elif data == "lf_action": s["link"]["action"] = next_in(ACT_ALL, s["link"]["action"]); changed = "link"
    elif data == "lf_clear": s["link"]["whitelist"] = []; changed = "link"
    elif data == "ap_toggle": s["approval"]["enabled"] = not s["approval"]["enabled"]; changed = "approval"
    elif data == "dc_toggle": s["deleting"]["commands"]["enabled"] = not s["deleting"]["commands"]["enabled"]; changed = "deleting"
    elif data == "sv_join": s["deleting"]["service"]["join"] = not s["deleting"]["service"]["join"]; changed = "service"
    elif data == "sv_leave": s["deleting"]["service"]["leave"] = not s["deleting"]["service"]["leave"]; changed = "service"
    elif data == "sv_pin": s["deleting"]["service"]["pin"] = not s["deleting"]["service"]["pin"]; changed = "service"
    elif data == "sc_toggle":
        s["deleting"]["sched"]["enabled"] = not s["deleting"]["sched"]["enabled"]; changed = "deleting"
        _reschedule_sched(context, gid, s["deleting"]["sched"])
    elif data == "sdx_toggle": s["deleting"]["selfd"]["enabled"] = not s["deleting"]["selfd"]["enabled"]; changed = "deleting"
    elif data == "sdx_scope": s["deleting"]["selfd"]["scope"] = next_in(["users", "all"], s["deleting"]["selfd"]["scope"]); changed = "deleting"
    elif data == "lang_en": s["language"] = "en"; changed = "lang"
    elif data == "lang_hi": s["language"] = "hi"; changed = "lang"
    elif data == "bw_toggle": s["bw"]["enabled"] = not s["bw"]["enabled"]; changed = "bw"
    elif data == "bw_action": s["bw"]["action"] = next_in(ACT_ALL, s["bw"]["action"]); changed = "bw"
    elif data == "bw_dur": s["bw"]["duration"] = next_in(DURS, s["bw"]["duration"]); changed = "bw"
    elif data == "bw_clear": db_clear_words(gid); changed = "bw"
    elif data == "ml_toggle": s["msglen"]["enabled"] = not s["msglen"]["enabled"]; changed = "msglen"
    elif data == "ml_action": s["msglen"]["action"] = next_in(["delete", "warn", "mute"], s["msglen"]["action"]); changed = "msglen"
    elif data == "lc_off": s["logchannel"] = None; changed = "logchannel"

    if changed:
        save_settings(gid, s)
        return await edit_q(q, *render_panel(changed, s, gid))

# ============================== AUTO-MODERATION (on message) ==============================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; chat = update.effective_chat
    if chat.type not in GROUPS: return
    gid = chat.id
    s = get_settings(gid)

    # ---- joins / leaves ----
    if msg.new_chat_members:
        for m in msg.new_chat_members:
            db_upsert_user(m); db_add_member(gid, m.id)
            if m.id == context.bot.id:
                try: await msg.reply_text(t(gid, "added"), parse_mode=ParseMode.HTML,
                                          reply_markup=InlineKeyboardMarkup([[btn("⚙️ Settings", "settings_main")]]))
                except TelegramError: pass
                continue
            if s["approval"]["enabled"]:
                try: await context.bot.restrict_chat_member(gid, m.id, permissions=ChatPermissions.no_permissions())
                except TelegramError: pass
                try:
                    await msg.reply_html(f"🛡 {esc(m.first_name)} joined.\n\nAn admin must approve before they can chat.",
                                         reply_markup=InlineKeyboardMarkup([[btn("✅ Approve", f"apok_{m.id}")]]))
                except TelegramError: pass
            elif s["welcome"]["enabled"]:
                await send_composed(context, gid, s["welcome"], m, chat)
        if s["deleting"]["service"]["join"] or s["blocks"]["service"]:
            await try_delete(msg)
        return

    if msg.left_chat_member:
        lm = msg.left_chat_member
        if lm.id != context.bot.id:
            db_del_member(gid, lm.id)
            if s["deleting"]["service"]["leave"] or s["blocks"]["service"]:
                await try_delete(msg)
            elif s["goodbye"]["enabled"]:
                await send_composed(context, gid, s["goodbye"], lm, chat)
        return

    # ---- channel posts / anonymous admins ----
    if msg.sender_chat:
        if msg.sender_chat.id == gid: return  # anonymous group admin
        if s["blocks"]["channel"]: await try_delete(msg)
        return

    user = msg.from_user
    if not user: return
    if user.id not in USERS_SEEN:
        USERS_SEEN.add(user.id); db_upsert_user(user)

    TRACK[gid].append(msg.message_id)

    lvl = await user_level(context, gid, user.id)
    free = db_is_free(gid, user.id) or db_role(gid, user.id) == "free"
    staff = lvl >= 3 or free

    b = s["blocks"]
    if lvl < 4:
        if b["forwards"] and (msg.forward_from or msg.forward_from_chat or getattr(msg, "forward_origin", None)):
            await try_delete(msg); return
        if b["commands"] and msg.text and msg.text.startswith("/"):
            await try_delete(msg); return
    if msg.pinned_message and (s["deleting"]["service"]["pin"] or s["blocks"]["service"]):
        await try_delete(msg); return

    content = msg.text or msg.caption or ""

    # @admin alert
    if content and "@admin" in content.lower():
        if s["atadmin"]["cooldown"] and time.time() - ATCOOL.get(gid, 0) > s["atadmin"]["cooldown"]:
            ATCOOL[gid] = time.time()
            admins = await tg_admin_ids(context, gid)
            if admins:
                tags = " ".join(f'<a href="tg://user?id={i}">🛡</a>' for i in list(admins)[:15])
                base = s["atadmin"]["message"] or "🆘 <b>Admins</b> have been summoned!"
                try: await msg.reply_html(base + "\n" + tags)
                except TelegramError: pass

    # self-destruct
    sd = s["deleting"]["selfd"]
    if sd["enabled"] and application.job_queue and (sd["scope"] == "all" or lvl < 3):
        context.job_queue.run_once(del_msg_job, sd["minutes"] * 60, chat_id=gid,
                                   data={"chat_id": gid, "message_id": msg.message_id})

    if staff: return  # staff & free users bypass auto-punishments

    # link filter
    lf = s["link"]
    if lf["enabled"] and content:
        urls = re.findall(r"(?:https?://\S+|www\.\S+|(?:t|telegram)\.me/\S+)", content, re.I)
        if urls:
            wl = lf["whitelist"]; bad = False
            for u in urls:
                d = host_of(u)
                if not any(d == w or d.endswith("." + w) for w in wl): bad = True; break
            if bad:
                await apply_action(context, gid, user, lf["action"], lf["duration"], msg=msg, reason="link not allowed"); return

    # banned words
    if s["bw"]["enabled"] and content:
        low = content.lower(); hit = None
        for w in db_words(gid):
            if w in low: hit = w; break
        if hit:
            await apply_action(context, gid, user, s["bw"]["action"], s["bw"]["duration"], msg=msg, reason=f"banned word: {hit}"); return

    # media control
    mkey = {"photo": "photos", "video": "videos", "document": "files", "voice": "voice",
            "audio": "audio", "animation": "gifs", "sticker": "gifs"}.get(msg.content_type)
    if mkey and not s["media"][mkey]:
        await apply_action(context, gid, user, s["media"]["action"], s["media"]["duration"], msg=msg, reason=f"{msg.content_type} is blocked"); return

    # message length
    ml = s["msglen"]
    if ml["enabled"] and content and len(content) > ml["max"]:
        await apply_action(context, gid, user, ml["action"], ml["duration"], msg=msg, reason="message too long"); return

    # anti-flood
    af = s["antiflood"]
    if af["enabled"]:
        dq = FLOOD[(gid, user.id)]; now = time.time(); dq.append(now)
        if len([x for x in dq if now - x <= af["window"]]) > af["max"]:
            dq.clear()
            await apply_action(context, gid, user, af["action"], af["duration"], msg=msg, reason="flooding"); return

    # anti-spam
    asp = s["antispam"]
    if asp["enabled"] and msg.text:
        dq = SPAM[(gid, user.id)]; now = time.time(); dq.append((msg.text, now))
        if sum(1 for txt_, ts_ in dq if txt_ == msg.text and now - ts_ <= 60) >= 3:
            dq.clear()
            await apply_action(context, gid, user, asp["action"], asp["duration"], msg=msg, reason="spam"); return

    # night mode
    nm = s["night"]
    if nm["enabled"] and nm["action"] == "mute" and in_window(nm["start"], nm["end"]):
        now = datetime.now(timezone.utc); cur = now.hour * 60 + now.minute
        endm = hhmm_to_min(nm["end"]) or 420
        mins = ((endm - cur) % 1440) or 720
        try:
            await context.bot.restrict_chat_member(gid, user.id, permissions=ChatPermissions.no_permissions(),
                                                   until_date=now + timedelta(minutes=mins + 5))
        except TelegramError: pass
        await try_delete(msg)
        try:
            await context.bot.send_message(gid, (nm["message"] or "🌙 Night mode is on — chat is muted until {end}.").replace("{end}", nm["end"]))
        except TelegramError: pass
        return

# ============================== JOBS ==============================
async def del_msg_job(context):
    d = context.job.data
    try: await context.bot.delete_message(d["chat_id"], d["message_id"])
    except TelegramError: pass

async def sched_del_job(context):
    gid = context.job.chat_id
    ids = TRACK.get(gid)
    if not ids: return
    n = 0
    while ids:
        try: await context.bot.delete_message(gid, ids.popleft()); n += 1
        except TelegramError: pass
    try: await context.bot.send_message(gid, f"🧹 Scheduled cleanup: deleted {n} messages.")
    except TelegramError: pass

async def recurring_sweep(context):
    conn = db()
    rows = conn.execute("SELECT id,group_id,message_text,interval_minutes,last_sent FROM recurring_messages WHERE is_active=1").fetchall()
    conn.close()
    now = datetime.now(timezone.utc)
    for r in rows:
        last = None
        if r["last_sent"]:
            try: last = datetime.fromisoformat(r["last_sent"])
            except ValueError: last = None
        if last and (now - last).total_seconds() < r["interval_minutes"] * 60: continue
        try:
            await context.bot.send_message(r["group_id"], esc(r["message_text"]), parse_mode=ParseMode.HTML)
        except TelegramError:
            continue
        conn = db(); conn.execute("UPDATE recurring_messages SET last_sent=? WHERE id=?", (now.isoformat(), r["id"])); conn.commit(); conn.close()

async def on_cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in GROUPS: return
    c = get_settings(chat.id)["deleting"]["commands"]
    if c["enabled"] and application.job_queue:
        context.job_queue.run_once(del_msg_job, max(1, c["delay"]), chat_id=chat.id,
                                   data={"chat_id": chat.id, "message_id": update.effective_message.message_id})

async def on_error(update, context):
    log.error("Update error", exc_info=context.error)

# ============================== APP SETUP (Flask + PTB webhook bridge) ==============================
application = Application.builder().token(TOKEN).updater(None).build()

conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(ask_entry, pattern=r"^inp_")],
    states={ASK: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_input)]},
    fallbacks=[CommandHandler("cancel", cancel_input), CallbackQueryHandler(cancel_input, pattern="^cancel_input$")],
    per_chat=True, per_user=True, per_message=False, allow_reentry=True)

application.add_handler(conv)
for name, fn in [("start", cmd_start), ("help", cmd_help), ("settings", cmd_settings), ("reload", cmd_reload),
                 ("lang", cmd_lang), ("rules", cmd_rules), ("ban", lambda u, c: _moderate(u, c, "ban")),
                 ("unban", lambda u, c: _moderate(u, c, "unban")), ("kick", lambda u, c: _moderate(u, c, "kick")),
                 ("mute", lambda u, c: _moderate(u, c, "mute")), ("unmute", lambda u, c: _moderate(u, c, "unmute")),
                 ("warn", cmd_warn), ("unwarn", cmd_unwarn), ("warns", cmd_warns), ("del", cmd_del),
                 ("mod", cmd_mod), ("unmod", cmd_unmod), ("admin", cmd_admin), ("free", cmd_free), ("staff", cmd_staff)]:
    application.add_handler(CommandHandler(name, fn))
application.add_handler(CallbackQueryHandler(on_callback))
application.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, on_message))
application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.COMMAND, on_cmd_delete), group=1)
application.add_error_handler(on_error)

async def bot_startup():
    await application.initialize()
    await application.start()
    if application.job_queue:
        application.job_queue.run_repeating(recurring_sweep, interval=60, first=20, name="recurring_sweep")
    if WEBHOOK_URL:
        await application.bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET or None,
                                          allowed_updates=["message", "callback_query"], drop_pending_updates=True)
        log.info("✅ Webhook set → %s", WEBHOOK_URL)
    else:
        log.warning("⚠️ WEBHOOK_URL / RENDER_EXTERNAL_URL not set — bot won't receive updates!")

BOT_LOOP = asyncio.new_event_loop()
def _run_bot():
    asyncio.set_event_loop(BOT_LOOP)
    BOT_LOOP.run_until_complete(bot_startup())
    BOT_LOOP.run_forever()

init_db()
threading.Thread(target=_run_bot, name="bot-loop", daemon=True).start()

app = Flask(__name__)

@app.route("/")
def index(): return "✅ GroupManagerBot is running!"

@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return "forbidden", 403
    update = Update.de_json(request.get_json(force=True), application.bot)
    if update:
        asyncio.run_coroutine_threadsafe(application.process_update(update), BOT_LOOP)
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)
