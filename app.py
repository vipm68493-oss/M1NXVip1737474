import os
import sys
import re
import time
import sqlite3
import threading
import subprocess
import asyncio
import sysconfig
import pkgutil
from datetime import datetime
from flask import Flask, request, session, redirect, url_for, render_template_string
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, MenuButtonCommands
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "m1nx-secret-change-me")

OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD", "owner")
OWNER_ID = int(os.environ.get("OWNER_ID", "7765423734"))
DB_PATH = "/tmp/m1nx.db"

# ==================== AUTO STDLIB DETECTION ====================
def get_stdlib_modules():
    """Automatically get all standard library module names."""
    stdlib = set(sys.builtin_module_names)
    try:
        stdlib_path = sysconfig.get_paths()["stdlib"]
        for m in pkgutil.iter_modules([stdlib_path]):
            stdlib.add(m.name)
    except Exception:
        pass
    # Add common ones that might be missed
    stdlib.update({
        "urllib", "http", "email", "xml", "html", "test", "unittest",
        "tkinter", "sqlite3", "json", "re", "os", "sys", "time",
        "datetime", "math", "random", "threading", "asyncio",
        "subprocess", "typing", "collections", "pathlib", "logging",
        "traceback", "base64", "hashlib", "uuid", "socket", "io",
        "shutil", "tempfile", "glob", "csv", "itertools", "functools",
        "ssl", "ftplib", "smtplib", "pickle", "copy", "warnings",
        "contextlib", "abc", "inspect", "ast", "dis", "gc", "weakref",
        "struct", "array", "binascii", "codecs", "locale", "gettext",
        "argparse", "configparser", "shlex", "signal", "errno"
    })
    return stdlib

STDLIB_MODULES = get_stdlib_modules()

# ==================== DATABASE ====================
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS users (
        uid INTEGER PRIMARY KEY,
        username TEXT,
        allowed INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid INTEGER,
        filename TEXT,
        code TEXT,
        UNIQUE(uid, filename)
    );
    """)
    c.commit()
    c.close()

init_db()

def db_get(key, default=None):
    c = db()
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return row["value"] if row else default

def db_set(key, value):
    c = db()
    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    c.commit()
    c.close()

def db_user(uid, username=None, allowed=None):
    c = db()
    row = c.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
    if row is None:
        c.execute("INSERT INTO users(uid,username,allowed) VALUES(?,?,?)",
                  (uid, username or "", 1 if allowed else 0))
    else:
        if username is not None:
            c.execute("UPDATE users SET username=? WHERE uid=?", (username, uid))
        if allowed is not None:
            c.execute("UPDATE users SET allowed=? WHERE uid=?", (1 if allowed else 0, uid))
    c.commit()
    c.close()

def db_get_user(uid):
    c = db()
    row = c.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
    c.close()
    return dict(row) if row else None

def db_all_users():
    c = db()
    rows = c.execute("SELECT * FROM users").fetchall()
    c.close()
    return [dict(r) for r in rows]

def db_save_file(uid, filename, code):
    c = db()
    c.execute("INSERT OR REPLACE INTO files(uid,filename,code) VALUES(?,?,?)",
              (uid, filename, code))
    c.commit()
    c.close()

def db_get_file(uid, filename):
    c = db()
    row = c.execute("SELECT * FROM files WHERE uid=? AND filename=?",
                    (uid, filename)).fetchone()
    c.close()
    return dict(row) if row else None

def db_user_files(uid):
    c = db()
    rows = c.execute("SELECT filename FROM files WHERE uid=?", (uid,)).fetchall()
    c.close()
    return [r["filename"] for r in rows]

def db_del_file(uid, filename):
    c = db()
    c.execute("DELETE FROM files WHERE uid=? AND filename=?", (uid, filename))
    c.commit()
    c.close()

# ==================== STATE ====================
STATE = {
    "bot_username": None,
    "bot_running": False,
    "processes": {},
    "logs": {},
}

def is_allowed(uid):
    if uid == OWNER_ID:
        return True
    u = db_get_user(uid)
    return bool(u and u["allowed"])

def add_log(key, line):
    STATE["logs"].setdefault(key, [])
    STATE["logs"][key].append(f"[{datetime.now().strftime('%H:%M:%S')}] {line}")
    if len(STATE["logs"][key]) > 200:
        STATE["logs"][key].pop(0)

def proc_key(uid, fname):
    return f"{uid}:{fname}"

# ==================== MENU & COMMANDS ====================
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "OPEN MAIN MENU"),
        BotCommand("files", "LIST YOUR FILES"),
        BotCommand("status", "SYSTEM STATUS"),
        BotCommand("help", "HOW TO USE"),
    ])
    # This sets the 3-line Menu button
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

def main_kb(uid):
    kb = [
        [InlineKeyboardButton("📂 MY FILES", callback_data="files"),
         InlineKeyboardButton("🚀 RUNNING", callback_data="running")],
        [InlineKeyboardButton("📊 STATUS", callback_data="status"),
         InlineKeyboardButton("❓ HELP", callback_data="help")],
        [InlineKeyboardButton("📤 UPLOAD FILE", callback_data="upload")],
    ]
    if uid == OWNER_ID:
        kb.append([InlineKeyboardButton("👥 USERS", callback_data="users")])
    return InlineKeyboardMarkup(kb)

# ==================== TELEGRAM HANDLERS ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    uname = user.username or "NOUSERNAME"

    db_user(uid, uname)
    if not is_allowed(uid):
        kb = [[InlineKeyboardButton("📋 COPY OWNER ID", callback_data=f"copyowner:{OWNER_ID}")]]
        await update.message.reply_text(
            f"⛔ *VERIFICATION REQUIRED*\n\n"
            f"HELLO @{uname.upper()}, YOU ARE NOT VERIFIED YET.\n"
            f"PLEASE CONTACT THE OWNER AND ASK FOR ACCESS.\n\n"
            f"👑 OWNER ID: `{OWNER_ID}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    role = "👑 OWNER" if uid == OWNER_ID else "✅ VERIFIED USER"
    await update.message.reply_text(
        f"*M1NX VIP PYTHON BOT HOSTING*\n\n"
        f"HELLO @{uname.upper()}\n"
        f"ROLE: {role}\n"
        f"ID: `{uid}`\n\n"
        f"SEND ME A `.PY` FILE TO HOST IT, OR USE THE BUTTONS BELOW.",
        parse_mode="Markdown",
        reply_markup=main_kb(uid),
    )

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = update.effective_user.id

    if data.startswith("copyowner:"):
        oid = data.split(":", 1)[1]
        await q.edit_message_text(
            f"👑 *OWNER ID*\n\n`{oid}`\n\nCOPY AND MESSAGE THE OWNER.",
            parse_mode="Markdown",
        )
        return

    if not is_allowed(uid):
        await q.edit_message_text("⛔ VERIFICATION REQUIRED. CONTACT OWNER.")
        return

    if data == "files":
        files = db_user_files(uid)
        if not files:
            await q.edit_message_text("📂 YOU HAVE NO FILES YET. UPLOAD ONE!")
            return
        kb = []
        for fname in files:
            p = STATE["processes"].get(proc_key(uid, fname))
            running = p and p.poll() is None
            icon = "🟢" if running else "🔴"
            kb.append([InlineKeyboardButton(f"{icon} {fname.upper()}",
                                            callback_data=f"file:{fname}")])
        kb.append([InlineKeyboardButton("⬅️ BACK", callback_data="back")])
        await q.edit_message_text("📂 *YOUR FILES*", parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif data == "upload":
        await q.edit_message_text("📤 SEND ME A `.PY` FILE NOW.")

    elif data.startswith("file:"):
        fname = data.split(":", 1)[1]
        f = db_get_file(uid, fname)
        if not f:
            await q.edit_message_text("❌ FILE NOT FOUND.")
            return
        p = STATE["processes"].get(proc_key(uid, fname))
        running = p and p.poll() is None
        status = "🟢 RUNNING" if running else "🔴 STOPPED"
        kb = [
            [InlineKeyboardButton("▶️ RUN", callback_data=f"run:{fname}"),
             InlineKeyboardButton("⏹ STOP", callback_data=f"stop:{fname}")],
            [InlineKeyboardButton("📥 DOWNLOAD", callback_data=f"dl:{fname}"),
             InlineKeyboardButton("📋 LOGS", callback_data=f"log:{fname}")],
            [InlineKeyboardButton("🗑 DELETE", callback_data=f"del:{fname}")],
            [InlineKeyboardButton("⬅️ BACK", callback_data="files")],
        ]
        await q.edit_message_text(
            f"📄 *{fname.upper()}*\nSTATUS: {status}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("run:"):
        fname = data.split(":", 1)[1]
        f = db_get_file(uid, fname)
        if not f:
            await q.edit_message_text("❌ FILE NOT FOUND.")
            return
        pk = proc_key(uid, fname)
        p = STATE["processes"].get(pk)
        if p and p.poll() is None:
            await q.edit_message_text(f"⚠️ `{fname}` ALREADY RUNNING.", parse_mode="Markdown")
            return

        path = f"/tmp/{uid}_{fname}"
        with open(path, "w") as fp:
            fp.write(f["code"])

        msg = await q.edit_message_text(f"⏳ *PREPARING {fname.upper()}...*", parse_mode="Markdown")
        t0 = time.time()

        # Detect imports and filter out stdlib
        imports = set(re.findall(r"^\s*(?:import|from)\s+([a-zA-Z0-9_]+)", f["code"], re.M))
        to_install = [m for m in imports if m not in STDLIB_MODULES]

        if to_install:
            await msg.edit_text(
                f"📦 *INSTALLING:* {', '.join(to_install).upper()}\n"
                f"⏱️ *PLEASE WAIT...*",
                parse_mode="Markdown",
            )
            try:
                proc_install = subprocess.Popen(
                    [sys.executable, "-m", "pip", "install", "-q", *to_install],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                )
                # Read real-time output for loading screen
                for line in iter(proc_install.stdout.readline, ""):
                    elapsed = int(time.time() - t0)
                    if line.strip():
                        await msg.edit_text(
                            f"📦 *INSTALLING...* ({elapsed}s)\n```\n{line.strip()[:200]}\n```",
                            parse_mode="Markdown",
                        )
                proc_install.wait()
                if proc_install.returncode != 0:
                    await msg.edit_text("❌ *INSTALL FAILED*. CHECK PACKAGE NAME.")
                    return
            except Exception as e:
                await msg.edit_text(f"❌ *INSTALL ERROR:* {str(e)[:200]}")
                return

        elapsed = int(time.time() - t0)
        await msg.edit_text(
            f"🚀 *STARTING {fname.upper()}...*\n⏱️ *PREP TOOK {elapsed}s*",
            parse_mode="Markdown",
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        STATE["processes"][pk] = proc
        add_log(pk, f"STARTED PID {proc.pid}")

        def collect():
            for line in iter(proc.stdout.readline, ""):
                add_log(pk, line.rstrip())
            proc.wait()
            add_log(pk, "PROCESS ENDED")

        threading.Thread(target=collect, daemon=True).start()
        await msg.edit_text(
            f"✅ *{fname.upper()} IS RUNNING*\nPID: `{proc.pid}`\n\nUSE *LOGS* TO SEE OUTPUT.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📋 LOGS", callback_data=f"log:{fname}"),
                  InlineKeyboardButton("⏹ STOP", callback_data=f"stop:{fname}")]]
            ),
        )

    elif data.startswith("stop:"):
        fname = data.split(":", 1)[1]
        p = STATE["processes"].get(proc_key(uid, fname))
        if not p or p.poll() is not None:
            await q.edit_message_text(f"⚠️ `{fname}` NOT RUNNING.", parse_mode="Markdown")
            return
        p.terminate()
        add_log(proc_key(uid, fname), "STOPPED")
        await q.edit_message_text(f"🛑 STOPPED `{fname}`", parse_mode="Markdown")

    elif data.startswith("dl:"):
        fname = data.split(":", 1)[1]
        f = db_get_file(uid, fname)
        if not f:
            await q.edit_message_text("❌ FILE NOT FOUND.")
            return
        tmp = f"/tmp/{uid}_{fname}"
        with open(tmp, "w") as fp:
            fp.write(f["code"])
        await q.message.reply_document(document=open(tmp, "rb"), filename=fname)
        await q.edit_message_text(f"📥 SENT `{fname}`", parse_mode="Markdown")

    elif data.startswith("log:"):
        fname = data.split(":", 1)[1]
        lines = STATE["logs"].get(proc_key(uid, fname), [])
        text = "\n".join(lines[-25:]) if lines else "NO LOGS YET."
        await q.edit_message_text(f"📋 *LOGS FOR {fname.upper()}*\n```\n{text}\n```",
                                  parse_mode="Markdown")

    elif data.startswith("del:"):
        fname = data.split(":", 1)[1]
        p = STATE["processes"].get(proc_key(uid, fname))
        if p and p.poll() is None:
            await q.edit_message_text("⚠️ STOP THE BOT BEFORE DELETING.", parse_mode="Markdown")
            return
        db_del_file(uid, fname)
        STATE["processes"].pop(proc_key(uid, fname), None)
        STATE["logs"].pop(proc_key(uid, fname), None)
        await q.edit_message_text(f"🗑 DELETED `{fname}`", parse_mode="Markdown")

    elif data == "running":
        lines = []
        for pk, p in STATE["processes"].items():
            if pk.startswith(f"{uid}:"):
                fname = pk.split(":", 1)[1]
                alive = p.poll() is None
                lines.append(f"{'🟢' if alive else '🔴'} `{fname}`")
        if not lines:
            await q.edit_message_text("🚀 NO PROCESSES RUNNING.")
            return
        await q.edit_message_text("🚀 *RUNNING BOTS*\n\n" + "\n".join(lines),
                                  parse_mode="Markdown")

    elif data == "status":
        text = (
            f"📊 *SYSTEM STATUS*\n\n"
            f"BOT USERNAME: @{STATE['bot_username'] or 'N/A'}\n"
            f"BOT STATUS: {'🟢 RUNNING' if STATE['bot_running'] else '🔴 STOPPED'}\n"
            f"TOTAL USERS: {len(db_all_users())}\n"
            f"YOUR FILES: {len(db_user_files(uid))}\n"
        )
        await q.edit_message_text(text, parse_mode="Markdown")

    elif data == "help":
        text = (
            "*📖 HELP*\n\n"
            "SEND A `.PY` FILE TO HOST IT.\n\n"
            "COMMANDS:\n"
            "/start - MAIN MENU\n"
            "/files - YOUR FILES\n"
            "/status - SYSTEM STATUS\n\n"
            "USE BUTTONS TO RUN, STOP, DOWNLOAD, VIEW LOGS, OR DELETE."
        )
        await q.edit_message_text(text, parse_mode="Markdown")

    elif data == "users":
        if uid != OWNER_ID:
            await q.edit_message_text("❌ OWNER ONLY.")
            return
        all_users = db_all_users()
        if not all_users:
            await q.edit_message_text("👥 NO USERS YET.")
            return
        kb = []
        for u in all_users:
            icon = "🟢" if u["allowed"] or u["uid"] == OWNER_ID else "🔴"
            kb.append([InlineKeyboardButton(
                f"{icon} @{(u['username'] or 'NOUSERNAME').upper()} ({u['uid']})",
                callback_data=f"user:{u['uid']}")])
        kb.append([InlineKeyboardButton("⬅️ BACK", callback_data="back")])
        await q.edit_message_text(
            f"👥 *USERS* ({len(all_users)} TOTAL)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("user:"):
        if uid != OWNER_ID:
            await q.edit_message_text("❌ OWNER ONLY.")
            return
        target = int(data.split(":", 1)[1])
        info = db_get_user(target)
        if not info:
            await q.edit_message_text("❌ USER NOT FOUND.")
            return
        status = "🟢 ALLOWED" if info["allowed"] or target == OWNER_ID else "🔴 BLOCKED"
        btn_label = "⛔ DISABLE" if info["allowed"] else "✅ ENABLE"
        kb = [
            [InlineKeyboardButton(btn_label, callback_data=f"toggle:{target}")],
            [InlineKeyboardButton("⬅️ BACK", callback_data="users")],
        ]
        await q.edit_message_text(
            f"👤 *USER INFO*\n\n"
            f"USERNAME: @{(info['username'] or 'NOUSERNAME').upper()}\n"
            f"ID: `{target}`\n"
            f"STATUS: {status}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("toggle:"):
        if uid != OWNER_ID:
            await q.edit_message_text("❌ OWNER ONLY.")
            return
        target = int(data.split(":", 1)[1])
        info = db_get_user(target)
        if not info:
            await q.edit_message_text("❌ USER NOT FOUND.")
            return
        new_val = 0 if info["allowed"] else 1
        db_user(target, allowed=new_val)
        status = "🟢 ALLOWED" if new_val else "🔴 BLOCKED"
        await q.edit_message_text(
            f"✅ UPDATED @{(info['username'] or 'NOUSERNAME').upper()}\nSTATUS: {status}",
            parse_mode="Markdown",
        )

    elif data == "back":
        await start_cmd(update, context)

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or "NOUSERNAME"
    if not is_allowed(uid):
        await update.message.reply_text("⛔ VERIFICATION REQUIRED. CONTACT OWNER.")
        return
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".py"):
        await update.message.reply_text("❌ ONLY `.PY` FILES ALLOWED.")
        return

    loading = await update.message.reply_text("⏳ UPLOADING FILE...")
    f = await doc.get_file()
    content = await f.download_as_bytearray()
    fname = doc.file_name
    db_save_file(uid, fname, content.decode("utf-8", errors="ignore"))
    add_log(proc_key(uid, fname), "UPLOADED")

    try:
        await context.bot.send_message(
            OWNER_ID,
            f"📤 *NEW UPLOAD*\nUSER: @{uname.upper()} (`{uid}`)\nFILE: `{fname}`",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await loading.edit_text(
        f"✅ *{fname.upper()} UPLOADED*\n\nUSE THE BUTTONS BELOW.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ RUN", callback_data=f"run:{fname}"),
            InlineKeyboardButton("📋 DETAILS", callback_data=f"file:{fname}"),
        ]]),
    )

def run_bot(token):
    async def main():
        application = (
            Application.builder()
            .token(token)
            .post_init(post_init)
            .build()
        )
        application.add_handler(CommandHandler("start", start_cmd))
        application.add_handler(CommandHandler("files", lambda u, c: buttons(u, c)))
        application.add_handler(CommandHandler("status", lambda u, c: buttons(u, c)))
        application.add_handler(CallbackQueryHandler(buttons))
        application.add_handler(MessageHandler(filters.Document.ALL, handle_doc))

        await application.initialize()
        me = await application.bot.get_me()
        STATE["bot_username"] = me.username
        STATE["bot_running"] = True
        await application.start()
        await application.updater.start_polling()
        await asyncio.Event().wait()

    asyncio.run(main())

# ==================== FLASK WEB ====================
@app.route("/", methods=["GET", "POST"])
def index():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    if request.method == "POST":
        token = request.form.get("token", "").strip()
        if token and ":" in token:
            db_set("bot_token", token)
            STATE["bot_running"] = False
            STATE["bot_username"] = None
            threading.Thread(target=run_bot, args=(token,), daemon=True).start()
            return redirect(url_for("index"))

    # Auto-start bot if token exists in DB and not running
    saved_token = db_get("bot_token")
    if saved_token and not STATE["bot_running"] and not STATE["bot_username"]:
        threading.Thread(target=run_bot, args=(saved_token,), daemon=True).start()

    running = STATE["bot_running"]
    html = """
    <!doctype html><html><head><meta charset="utf-8">
    <title>M1NX VIP BOT HOSTING</title>
    <style>
    *{box-sizing:border-box}
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);
         color:#e6edf3;margin:0;padding:24px;min-height:100vh}
    .container{max-width:760px;margin:auto}
    h1{font-weight:800;background:linear-gradient(90deg,#00f5d4,#7b2ff7);
       -webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:4px}
    .sub{color:#9aa4b2;margin-top:0;font-size:14px}
    .card{background:rgba(22,27,34,.85);border:1px solid #30363d;
          border-radius:16px;padding:22px;margin:16px 0;
          backdrop-filter:blur(8px);box-shadow:0 8px 32px rgba(0,0,0,.35)}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
    .stat{background:#0d1117;border:1px solid #30363d;border-radius:12px;padding:14px;text-align:center}
    .stat .val{font-size:20px;font-weight:700;margin-top:4px}
    .stat .lbl{font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}
    input{width:100%;padding:14px;margin:8px 0;border-radius:12px;
          border:1px solid #30363d;background:#0d1117;color:#fff;font-size:15px}
    button{padding:14px 26px;border:none;border-radius:12px;cursor:pointer;
           font-weight:700;font-size:15px;background:linear-gradient(90deg,#00f5d4,#7b2ff7);
           color:#0d1117}
    button:hover{opacity:.9}
    a{color:#58a6ff}
    .ok{color:#3fb950}.bad{color:#f85149}
    </style></head><body>
    <div class="container">
      <h1>🤖 M1NX VIP BOT HOSTING</h1>
      <p class="sub">PREMIUM PYTHON BOT CONTROL PANEL • <a href="/logout">LOGOUT</a></p>
      <div class="card">
        <div class="grid">
          <div class="stat"><div class="lbl">STATUS</div>
            <div class="val {{ 'ok' if running else 'bad' }}">{{ status }}</div></div>
          <div class="stat"><div class="lbl">BOT</div>
            <div class="val">@{{ username }}</div></div>
          <div class="stat"><div class="lbl">USERS</div>
            <div class="val">{{ user_count }}</div></div>
          <div class="stat"><div class="lbl">FILES</div>
            <div class="val">{{ file_count }}</div></div>
        </div>
      </div>
      <div class="card">
        <h3>🔑 SET BOT TOKEN</h3>
        <form method="POST">
          <input type="text" name="token" placeholder="123456:ABC-XYZ..." required>
          <button type="submit">SAVE & START</button>
        </form>
      </div>
    </div></body></html>
    """
    return render_template_string(
        html,
        status="🟢 RUNNING" if running else "🔴 STOPPED",
        username=STATE["bot_username"] or "N/A",
        user_count=len(db_all_users()),
        file_count=sum(len(db_user_files(u["uid"])) for u in db_all_users()),
        running=running,
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == OWNER_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "❌ WRONG PASSWORD"
    html = """
    <!doctype html><html><head><meta charset="utf-8"><title>LOGIN</title>
    <style>
    body{font-family:system-ui,sans-serif;
         background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);
         color:#e6edf3;display:flex;justify-content:center;align-items:center;
         height:100vh;margin:0}
    .card{background:rgba(22,27,34,.9);padding:34px;border-radius:18px;
          width:340px;border:1px solid #30363d;box-shadow:0 8px 32px rgba(0,0,0,.4)}
    h2{margin-top:0;background:linear-gradient(90deg,#00f5d4,#7b2ff7);
       -webkit-background-clip:text;-webkit-text-fill-color:transparent}
    input{width:100%;padding:14px;margin:8px 0;border-radius:12px;
          border:1px solid #30363d;background:#0d1117;color:#fff;box-sizing:border-box}
    button{width:100%;padding:14px;border:none;border-radius:12px;cursor:pointer;
           font-weight:700;background:linear-gradient(90deg,#00f5d4,#7b2ff7);color:#0d1117}
    .err{color:#f85149}
    </style></head><body>
    <div class="card">
      <h2>🔐 M1NX HOSTING</h2>
      <p style="color:#8b949e;font-size:13px">ENTER OWNER PASSWORD</p>
      {% if error %}<p class="err">{{ error }}</p>{% endif %}
      <form method="POST">
        <input type="password" name="password" placeholder="OWNER PASSWORD" required>
        <button type="submit">LOGIN</button>
      </form>
    </div></body></html>
    """
    return render_template_string(html, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/health")
def health():
    return "ok"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)