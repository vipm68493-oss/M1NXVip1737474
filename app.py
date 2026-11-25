import os
import sys
import threading
import subprocess
import asyncio
from datetime import datetime
from flask import Flask, request, session, redirect, url_for, render_template_string
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "m1nx-secret-change-me")

# ==================== CONFIG ====================
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")

# 🔴 REAL ADMIN ID (fixed, never changes)
REAL_ADMIN_ID = 7765423734

STATE = {
    "bot_token": None,
    "bot_username": None,
    "bot_running": False,
    "users": {},            # user_id -> {"username": ..., "allowed": False}
    "files": {},            # filename -> code
    "processes": {},        # filename -> Popen
    "logs": {},             # filename -> [lines]
}

# ==================== HELPERS ====================
def is_logged_in():
    return session.get("logged_in") is True

def now():
    return datetime.now().strftime("%H:%M:%S")

def add_log(key, line):
    STATE["logs"].setdefault(key, [])
    STATE["logs"][key].append(f"[{now()}] {line}")
    if len(STATE["logs"][key]) > 200:
        STATE["logs"][key].pop(0)

def is_allowed(uid):
    if uid == REAL_ADMIN_ID:
        return True
    return STATE["users"].get(uid, {}).get("allowed", False)

def main_menu_kb(uid):
    kb = [
        [InlineKeyboardButton("📂 My Files", callback_data="files"),
         InlineKeyboardButton("🚀 Running Bots", callback_data="running")],
        [InlineKeyboardButton("📊 Status", callback_data="status"),
         InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
    if uid == REAL_ADMIN_ID:
        kb.append([InlineKeyboardButton("👥 Users", callback_data="users")])
    return InlineKeyboardMarkup(kb)

# ==================== TELEGRAM HANDLERS ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    uname = user.username or "NoUsername"

    # Register/update user
    if uid not in STATE["users"]:
        STATE["users"][uid] = {"username": uname, "allowed": False}
    else:
        STATE["users"][uid]["username"] = uname

    if not is_allowed(uid):
        kb = [[InlineKeyboardButton("📋 Copy Admin ID", callback_data=f"copyadmin:{REAL_ADMIN_ID}")]]
        await update.message.reply_text(
            f"⛔ *Access Denied*\n\n"
            f"Hello @{uname},\n"
            f"Your ID `{uid}` is not authorized to use this bot.\n\n"
            f"Please contact the admin:\n"
            f"👑 Admin ID: `{REAL_ADMIN_ID}`\n\n"
            f"Ask the admin to enable your access.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    role = "👑 REAL ADMIN" if uid == REAL_ADMIN_ID else "✅ Authorized User"
    await update.message.reply_text(
        f"*M1NX VIP Python Bot Hosting*\n\n"
        f"Hello @{uname}\n"
        f"Role: {role}\n"
        f"ID: `{uid}`\n\n"
        f"Send me a `.py` file to host it.",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(uid),
    )

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = update.effective_user.id

    # Copy admin id
    if data.startswith("copyadmin:"):
        aid = data.split(":", 1)[1]
        await q.edit_message_text(
            f"👑 *Admin ID*\n\n`{aid}`\n\nCopy the ID above and message the admin.",
            parse_mode="Markdown",
        )
        return

    if not is_allowed(uid):
        await q.edit_message_text("⛔ Access denied. Contact admin.")
        return

    # ---------- FILES ----------
    if data == "files":
        if not STATE["files"]:
            await q.edit_message_text("📂 No files uploaded yet.")
            return
        kb = []
        for fname in STATE["files"]:
            running = fname in STATE["processes"] and STATE["processes"][fname].poll() is None
            icon = "🟢" if running else "🔴"
            kb.append([InlineKeyboardButton(f"{icon} {fname}", callback_data=f"file:{fname}")])
        kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
        await q.edit_message_text("📂 *Hosted Files*", parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("file:"):
        fname = data.split(":", 1)[1]
        if fname not in STATE["files"]:
            await q.edit_message_text("❌ File not found.")
            return
        running = fname in STATE["processes"] and STATE["processes"][fname].poll() is None
        status = "🟢 Running" if running else "🔴 Stopped"
        kb = [
            [InlineKeyboardButton("▶️ Run", callback_data=f"run:{fname}"),
             InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{fname}")],
            [InlineKeyboardButton("📥 Download", callback_data=f"dl:{fname}"),
             InlineKeyboardButton("📋 Logs", callback_data=f"log:{fname}")],
            [InlineKeyboardButton("🗑 Delete", callback_data=f"del:{fname}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="files")],
        ]
        await q.edit_message_text(
            f"📄 *{fname}*\nStatus: {status}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("run:"):
        fname = data.split(":", 1)[1]
        if fname in STATE["processes"] and STATE["processes"][fname].poll() is None:
            await q.edit_message_text(f"⚠️ `{fname}` already running.", parse_mode="Markdown")
            return
        path = f"/tmp/{fname}"
        with open(path, "w") as f:
            f.write(STATE["files"][fname])
        proc = subprocess.Popen(
            [sys.executable, "-u", path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        STATE["processes"][fname] = proc
        add_log(fname, f"Started PID {proc.pid}")

        def collect():
            for line in iter(proc.stdout.readline, ""):
                add_log(fname, line.rstrip())
            proc.wait()
            add_log(fname, "Process ended")

        threading.Thread(target=collect, daemon=True).start()
        await q.edit_message_text(f"🚀 Started `{fname}` (PID {proc.pid})", parse_mode="Markdown")

    elif data.startswith("stop:"):
        fname = data.split(":", 1)[1]
        proc = STATE["processes"].get(fname)
        if not proc or proc.poll() is not None:
            await q.edit_message_text(f"⚠️ `{fname}` not running.", parse_mode="Markdown")
            return
        proc.terminate()
        add_log(fname, "Stopped")
        await q.edit_message_text(f"🛑 Stopped `{fname}`", parse_mode="Markdown")

    elif data.startswith("dl:"):
        fname = data.split(":", 1)[1]
        if fname not in STATE["files"]:
            await q.edit_message_text("❌ File not found.")
            return
        tmp = f"/tmp/{fname}"
        with open(tmp, "w") as f:
            f.write(STATE["files"][fname])
        await q.message.reply_document(document=open(tmp, "rb"), filename=fname)
        await q.edit_message_text(f"📥 Sent `{fname}`", parse_mode="Markdown")

    elif data.startswith("log:"):
        fname = data.split(":", 1)[1]
        lines = STATE["logs"].get(fname, [])
        text = "\n".join(lines[-25:]) if lines else "No logs yet."
        await q.edit_message_text(f"📋 *Logs for {fname}*\n```\n{text}\n```",
                                  parse_mode="Markdown")

    elif data.startswith("del:"):
        fname = data.split(":", 1)[1]
        proc = STATE["processes"].get(fname)
        if proc and proc.poll() is None:
            await q.edit_message_text("⚠️ Stop the bot before deleting.", parse_mode="Markdown")
            return
        STATE["files"].pop(fname, None)
        STATE["processes"].pop(fname, None)
        STATE["logs"].pop(fname, None)
        await q.edit_message_text(f"🗑 Deleted `{fname}`", parse_mode="Markdown")

    # ---------- RUNNING ----------
    elif data == "running":
        if not STATE["processes"]:
            await q.edit_message_text("🚀 No processes running.")
            return
        lines = []
        for fname, proc in STATE["processes"].items():
            alive = proc.poll() is None
            lines.append(f"{'🟢' if alive else '🔴'} `{fname}` (PID {proc.pid})")
        await q.edit_message_text("🚀 *Running Bots*\n\n" + "\n".join(lines),
                                  parse_mode="Markdown")

    # ---------- STATUS ----------
    elif data == "status":
        text = (
            f"📊 *System Status*\n\n"
            f"Bot Username: @{STATE['bot_username'] or 'N/A'}\n"
            f"Bot Status: {'🟢 Running' if STATE['bot_running'] else '🔴 Stopped'}\n"
            f"Total Users: {len(STATE['users'])}\n"
            f"Allowed Users: {sum(1 for u in STATE['users'].values() if u['allowed'])}\n"
            f"Hosted Files: {len(STATE['files'])}\n"
            f"Running Processes: {sum(1 for p in STATE['processes'].values() if p.poll() is None)}"
        )
        await q.edit_message_text(text, parse_mode="Markdown")

    # ---------- HELP ----------
    elif data == "help":
        text = (
            "*📖 Help*\n\n"
            "Send a `.py` file to host it.\n\n"
            "Commands:\n"
            "/start - Main menu\n"
            "/files - List files\n"
            "/status - System status\n\n"
            "Use inline buttons to run, stop, download, view logs, or delete files."
        )
        await q.edit_message_text(text, parse_mode="Markdown")

    # ---------- USERS (ADMIN ONLY) ----------
    elif data == "users":
        if uid != REAL_ADMIN_ID:
            await q.edit_message_text("❌ Admin only.")
            return
        if not STATE["users"]:
            await q.edit_message_text("👥 No users yet.")
            return
        kb = []
        for u_id, info in STATE["users"].items():
            icon = "🟢" if info["allowed"] else "🔴"
            label = f"{icon} @{info['username']} ({u_id})"
            kb.append([InlineKeyboardButton(label, callback_data=f"user:{u_id}")])
        kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
        await q.edit_message_text(
            f"👥 *Users* ({len(STATE['users'])} total)\n\n"
            f"🟢 = allowed   🔴 = blocked",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("user:"):
        if uid != REAL_ADMIN_ID:
            await q.edit_message_text("❌ Admin only.")
            return
        target = int(data.split(":", 1)[1])
        info = STATE["users"].get(target)
        if not info:
            await q.edit_message_text("❌ User not found.")
            return
        status = "🟢 Allowed" if info["allowed"] else "🔴 Blocked"
        new_state = "off" if info["allowed"] else "on"
        btn_label = "⛔ Disable" if info["allowed"] else "✅ Enable"
        kb = [
            [InlineKeyboardButton(btn_label, callback_data=f"toggle:{target}")],
            [InlineKeyboardButton("⬅️ Back", callback_data="users")],
        ]
        await q.edit_message_text(
            f"👤 *User Info*\n\n"
            f"Username: @{info['username']}\n"
            f"ID: `{target}`\n"
            f"Status: {status}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif data.startswith("toggle:"):
        if uid != REAL_ADMIN_ID:
            await q.edit_message_text("❌ Admin only.")
            return
        target = int(data.split(":", 1)[1])
        if target not in STATE["users"]:
            await q.edit_message_text("❌ User not found.")
            return
        STATE["users"][target]["allowed"] = not STATE["users"][target]["allowed"]
        info = STATE["users"][target]
        status = "🟢 Allowed" if info["allowed"] else "🔴 Blocked"
        await q.edit_message_text(
            f"✅ Updated @{info['username']}\nStatus: {status}",
            parse_mode="Markdown",
        )

    elif data == "back":
        await start_cmd(update, context)

async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("⛔ Access denied. Contact admin.")
        return
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".py"):
        await update.message.reply_text("❌ Only `.py` files allowed.")
        return
    f = await doc.get_file()
    content = await f.download_as_bytearray()
    fname = doc.file_name
    STATE["files"][fname] = content.decode("utf-8", errors="ignore")
    add_log(fname, "Uploaded")
    kb = [[InlineKeyboardButton("▶️ Run Now", callback_data=f"run:{fname}"),
           InlineKeyboardButton("📋 Details", callback_data=f"file:{fname}")]]
    await update.message.reply_text(
        f"✅ Saved `{fname}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )

def run_bot(token):
    async def main():
        application = Application.builder().token(token).build()
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
    if not is_logged_in():
        return redirect(url_for("login"))

    if request.method == "POST":
        token = request.form.get("token", "").strip()
        if token and ":" in token:
            STATE["bot_token"] = token
            STATE["bot_running"] = False
            STATE["bot_username"] = None
            threading.Thread(target=run_bot, args=(token,), daemon=True).start()
            return redirect(url_for("index"))

    running = STATE["bot_running"]
    html = """
    <!doctype html><html><head><meta charset="utf-8">
    <title>M1NX VIP Bot Hosting</title>
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
    ul{padding-left:18px}
    .ok{color:#3fb950}.bad{color:#f85149}
    </style></head><body>
    <div class="container">
      <h1>🤖 M1NX VIP Bot Hosting</h1>
      <p class="sub">Premium Python Bot Control Panel • <a href="/logout">Logout</a></p>

      <div class="card">
        <div class="grid">
          <div class="stat"><div class="lbl">Status</div>
            <div class="val {{ 'ok' if running else 'bad' }}">{{ status }}</div></div>
          <div class="stat"><div class="lbl">Bot</div>
            <div class="val">@{{ username }}</div></div>
          <div class="stat"><div class="lbl">Users</div>
            <div class="val">{{ user_count }}</div></div>
          <div class="stat"><div class="lbl">Files</div>
            <div class="val">{{ file_count }}</div></div>
        </div>
      </div>

      <div class="card">
        <h3>🔑 Set Bot Token</h3>
        <form method="POST">
          <input type="text" name="token" placeholder="123456:ABC-xyz..." required>
          <button type="submit">Save & Start</button>
        </form>
      </div>

      <div class="card">
        <h3>📂 Hosted Files</h3>
        {% if files %}
          <ul>{% for f in files %}<li>{{ f }}</li>{% endfor %}</ul>
        {% else %}
          <p>No files yet. Send a .py file to the bot.</p>
        {% endif %}
      </div>
    </div></body></html>
    """
    return render_template_string(
        html,
        status="🟢 Running" if running else "🔴 Stopped",
        username=STATE["bot_username"] or "N/A",
        user_count=len(STATE["users"]),
        file_count=len(STATE["files"]),
        files=list(STATE["files"].keys()),
        running=running,
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "❌ Wrong password"
    html = """
    <!doctype html><html><head><meta charset="utf-8"><title>Login</title>
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
      <h2>🔐 M1NX Hosting</h2>
      <p style="color:#8b949e;font-size:13px">Enter admin password</p>
      {% if error %}<p class="err">{{ error }}</p>{% endif %}
      <form method="POST">
        <input type="password" name="password" placeholder="Admin password" required>
        <button type="submit">Login</button>
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