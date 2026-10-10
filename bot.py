#!/usr/bin/env python3
import os
import sys
import time
import sqlite3
import shutil
import logging
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil
import telebot
from telebot.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# =========================
# Configuration
# =========================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
LOGS_DIR = DATA_DIR / "logs"
TEMP_DIR = DATA_DIR / "temp"
DB_PATH = DATA_DIR / "metadata.db"

for directory in [DATA_DIR, UPLOADS_DIR, LOGS_DIR, TEMP_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "8556512504").split(",") if x.strip()]
CPU_THRESHOLD = float(os.getenv("CPU_THRESHOLD", "95"))
MEMORY_THRESHOLD = float(os.getenv("MEMORY_THRESHOLD", "95"))
MAX_RUNNING_PROCESSES = int(os.getenv("MAX_RUNNING_PROCESSES", "15"))
MAX_FILES_PER_USER = int(os.getenv("MAX_FILES_PER_USER", "5"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing. Copy .env.example to .env and add your bot token.")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("hosting_bot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
DB_LOCK = threading.Lock()
PROCESS_LOCK = threading.Lock()
RUNNING_PROCESSES = {}
START_TIME = datetime.utcnow()


# =========================
# Database helpers
# =========================
def init_db():
    with DB_LOCK:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                filename TEXT,
                orig_name TEXT,
                path TEXT,
                uploaded_at TEXT,
                file_type TEXT,
                status TEXT DEFAULT 'Stopped',
                pid INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                joined_at TEXT,
                last_seen TEXT
            )
            """
        )
        conn.commit()


init_db()


def is_admin(user_id: int) -> bool:
    return int(user_id) in ADMIN_IDS


def upsert_user(user_id: int, username: str):
    with DB_LOCK:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (user_id, username, joined_at, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET username = excluded.username, last_seen = excluded.last_seen
            """,
            (user_id, username or "", datetime.utcnow().isoformat(), datetime.utcnow().isoformat()),
        )
        conn.commit()


def add_file_record(user_id: int, username: str, safe_name: str, orig_name: str, storage_path: str, file_type: str):
    with DB_LOCK:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO files (user_id, username, filename, orig_name, path, uploaded_at, file_type, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'Stopped')
            """,
            (user_id, username or "", safe_name, orig_name, str(storage_path), datetime.utcnow().isoformat(), file_type),
        )
        conn.commit()
        return cur.lastrowid


def list_user_files(user_id: int):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, filename, orig_name, uploaded_at, file_type, status, pid
        FROM files
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,),
    )
    return cur.fetchall()


def get_file_record(file_id: int):
    cur = conn.cursor()
    cur.execute("SELECT * FROM files WHERE id = ?", (file_id,))
    return cur.fetchone()


def update_file_status(file_id: int, status: str, pid: Optional[int] = None):
    with DB_LOCK:
        cur = conn.cursor()
        cur.execute(
            "UPDATE files SET status = ?, pid = ? WHERE id = ?",
            (status, pid, file_id),
        )
        conn.commit()


def remove_file_record(file_id: int):
    with DB_LOCK:
        cur = conn.cursor()
        cur.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()


# =========================
# System monitoring
# =========================
def get_system_load():
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent
        return float(cpu), float(mem)
    except Exception:
        return 0.0, 0.0


def is_load_ok():
    cpu, mem = get_system_load()
    proc_count = len(RUNNING_PROCESSES)
    if proc_count >= MAX_RUNNING_PROCESSES:
        return False, f"Too many active processes ({proc_count}/{MAX_RUNNING_PROCESSES})"
    if cpu >= CPU_THRESHOLD:
        return False, f"High CPU usage ({cpu:.1f}%)"
    if mem >= MEMORY_THRESHOLD:
        return False, f"High memory usage ({mem:.1f}%)"
    return True, None


def get_file_type(name: str):
    name = (name or "").lower()
    if name.endswith(".py"):
        return "python"
    if name.endswith(".js"):
        return "javascript"
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tar", ".tar.gz", ".tgz")):
        return "archive"
    return "unknown"


def find_main_python(directory: Path):
    candidates = ["main.py", "bot.py", "app.py", "server.py", "index.py", "script.py"]
    for item in candidates:
        path = directory / item
        if path.exists() and path.is_file():
            return path
    for root, _, files in os.walk(directory):
        for file_name in sorted(files):
            if file_name.lower().endswith(".py"):
                return Path(root) / file_name
    return None


def extract_archive(path: Path, dest_dir: Path):
    try:
        if path.suffix.lower() == ".zip":
            import zipfile
            with zipfile.ZipFile(path, "r") as zf:
                zf.extractall(dest_dir)
        elif path.name.endswith((".tar.gz", ".tgz")):
            import tarfile
            with tarfile.open(path, "r:gz") as tf:
                tf.extractall(dest_dir)
        elif path.suffix.lower() == ".tar":
            import tarfile
            with tarfile.open(path, "r") as tf:
                tf.extractall(dest_dir)
        else:
            return False, "Unsupported archive format"
        return True, None
    except Exception as e:
        return False, str(e)


# =========================
# Telegram keyboard UI
# =========================
def main_menu_kb(user_id: int):
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("📤 Upload File"), KeyboardButton("📁 My Files"))
    kb.add(KeyboardButton("📊 Status"), KeyboardButton("⚡ System"))
    if is_admin(user_id):
        kb.add(KeyboardButton("👑 Admin"))
    return kb


def files_kb(user_id: int):
    files = list_user_files(user_id)
    kb = InlineKeyboardMarkup()
    for file_record in files:
        file_id, _, orig_name, _, file_type, status, pid = file_record
        label = orig_name[:20]
        emoji = "🟢" if status == "Running" else "🔴"
        kb.add(InlineKeyboardButton(f"{emoji} {label} ({file_type})", callback_data=f"manage:{file_id}"))
    return kb


def file_action_kb(file_id: int, running: bool):
    kb = InlineKeyboardMarkup()
    if running:
        kb.row(
            InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{file_id}"),
            InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}"),
        )
    else:
        kb.row(
            InlineKeyboardButton("▶️ Run", callback_data=f"start:{file_id}"),
            InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}"),
        )
    kb.row(
        InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{file_id}"),
        InlineKeyboardButton("📄 Logs", callback_data=f"logs:{file_id}"),
    )
    return kb


def admin_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("👥 Users", callback_data="admin_users"),
        InlineKeyboardButton("📁 Files", callback_data="admin_files"),
    )
    kb.add(
        InlineKeyboardButton("⚡ System", callback_data="admin_system"),
        InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
    )
    return kb


# =========================
# Bot handlers
# =========================
@bot.message_handler(commands=["start", "help"])
def start_handler(message):
    user = message.from_user
    upsert_user(user.id, user.username)
    text = (
        "<b>🚀 Telegram Hosting Bot</b>\n\n"
        f"Welcome <b>{user.first_name}</b>\n"
        f"Your ID: <code>{user.id}</code>\n"
        f"Files: {len(list_user_files(user.id))}/{MAX_FILES_PER_USER}"
    )
    if is_admin(user.id):
        text += "\n\n👑 You are an administrator"
    bot.send_message(message.chat.id, text, reply_markup=main_menu_kb(user.id))


@bot.message_handler(func=lambda m: m.text == "📤 Upload File")
def upload_prompt(message):
    bot.send_message(message.chat.id, "📤 Upload a file.\n\nYou can send a Python (.py), JavaScript (.js), or ZIP (.zip) archive.")


@bot.message_handler(func=lambda m: m.text == "📁 My Files")
def my_files(message):
    files = list_user_files(message.from_user.id)
    if not files:
        bot.send_message(message.chat.id, "📁 You have no uploaded files yet.")
        return
    bot.send_message(message.chat.id, "<b>📁 Your Files</b>", reply_markup=files_kb(message.from_user.id))


@bot.message_handler(func=lambda m: m.text == "📊 Status")
def status_handler(message):
    cpu, mem = get_system_load()
    bot.send_message(
        message.chat.id,
        f"<b>📊 System Status</b>\n\nCPU: {cpu:.1f}%\nMemory: {mem:.1f}%\nActive processes: {len(RUNNING_PROCESSES)}",
    )


@bot.message_handler(func=lambda m: m.text == "⚡ System")
def system_handler(message):
    cpu, mem = get_system_load()
    uptime = datetime.utcnow() - START_TIME
    hours, rem = divmod(uptime.seconds, 3600)
    mins, secs = divmod(rem, 60)
    bot.send_message(
        message.chat.id,
        f"<b>⚡ System Overview</b>\n\nCPU: {cpu:.1f}%\nMemory: {mem:.1f}%\nUptime: {uptime.days}d {hours}h {mins}m {secs}s",
    )


@bot.message_handler(func=lambda m: m.text == "👑 Admin" and is_admin(m.from_user.id))
def admin_panel(message):
    bot.send_message(message.chat.id, "<b>👑 Admin Panel</b>", reply_markup=admin_kb())


@bot.message_handler(content_types=["document"])
def doc_handler(message):
    user = message.from_user
    files = list_user_files(user.id)
    if len(files) >= MAX_FILES_PER_USER:
        bot.reply_to(message, f"❌ File limit reached. Maximum allowed: {MAX_FILES_PER_USER}.")
        return

    document = message.document
    file_name = document.file_name or "uploaded_file"
    file_type = get_file_type(file_name)

    user_dir = UPLOADS_DIR / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{int(time.time())}_{file_name}"
    target_path = user_dir / safe_name

    try:
        file_info = bot.get_file(document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(target_path, "wb") as f:
            f.write(downloaded)
    except Exception as e:
        bot.reply_to(message, f"❌ Error downloading or saving the file: {e}")
        return

    if file_type in ["zip", "archive"]:
        extract_dir = TEMP_DIR / f"extract_{int(time.time())}_{user.id}"
        extract_dir.mkdir(parents=True, exist_ok=True)
        ok, err = extract_archive(target_path, extract_dir)
        if not ok:
            bot.reply_to(message, f"❌ Archive extraction failed: {err}")
            try:
                target_path.unlink()
            except Exception:
                pass
            return
        main_file = find_main_python(extract_dir)
        if not main_file:
            bot.reply_to(message, "❌ No valid main Python file was found inside the archive.")
            shutil.rmtree(extract_dir, ignore_errors=True)
            try:
                target_path.unlink()
            except Exception:
                pass
            return
        storage_path = str(extract_dir)
        file_type = "python"
        bot.reply_to(message, f"✅ Archive extracted successfully. Main file: <code>{main_file.name}</code>")
    else:
        storage_path = str(target_path)

    file_id = add_file_record(user.id, user.username or "", safe_name, file_name, storage_path, file_type)
    bot.reply_to(message, f"✅ File saved successfully. File ID: <code>{file_id}</code>\nUse the Run button to start it.")


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    data = call.data or ""
    chat_id = call.message.chat.id
    user_id = call.from_user.id

    if data.startswith("manage:"):
        file_id = int(data.split(":", 1)[1])
        record = get_file_record(file_id)
        if not record:
            bot.answer_callback_query(call.id, "Not found")
            return
        if record["user_id"] != user_id and not is_admin(user_id):
            bot.answer_callback_query(call.id, "Access denied")
            return
        running = file_id in RUNNING_PROCESSES
        text = (
            f"<b>📁 File Manager</b>\n\n"
            f"Name: {record['orig_name']}\n"
            f"Type: {record['file_type']}\n"
            f"Status: {'🟢 Running' if running else '🔴 Stopped'}\n"
            f"PID: {record['pid'] or 'N/A'}"
        )
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=file_action_kb(file_id, running))
        bot.answer_callback_query(call.id, "Opened")
        return

    if data.startswith("start:"):
        file_id = int(data.split(":", 1)[1])
        start_process(file_id, chat_id)
        bot.answer_callback_query(call.id, "Starting...")
        return

    if data.startswith("stop:"):
        file_id = int(data.split(":", 1)[1])
        stop_process(file_id)
        bot.answer_callback_query(call.id, "Stopped")
        return

    if data.startswith("restart:"):
        file_id = int(data.split(":", 1)[1])
        stop_process(file_id)
        time.sleep(1)
        start_process(file_id, chat_id)
        bot.answer_callback_query(call.id, "Restarted")
        return

    if data.startswith("delete:"):
        file_id = int(data.split(":", 1)[1])
        record = get_file_record(file_id)
        if record:
            stop_process(file_id)
            path = record["path"]
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                logger.error(e)
            remove_file_record(file_id)
        bot.answer_callback_query(call.id, "Deleted")
        return

    if data.startswith("logs:"):
        file_id = int(data.split(":", 1)[1])
        logs = get_logs(file_id)
        if len(logs) > 3500:
            logs = logs[-3500:]
        bot.send_message(chat_id, f"<b>📄 Logs</b>\n<pre>{logs}</pre>")
        bot.answer_callback_query(call.id, "Logs loaded")
        return

    if data == "admin_users":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Admin only")
            return
        cur = conn.cursor()
        cur.execute("SELECT user_id, username, joined_at FROM users ORDER BY joined_at DESC LIMIT 20")
        rows = cur.fetchall()
        if not rows:
            bot.send_message(chat_id, "No users yet.")
            return
        text = "<b>👥 Users</b>\n"
        for row in rows:
            text += f"- <code>{row['user_id']}</code> / {row['username'] or 'N/A'}\n"
        bot.send_message(chat_id, text)
        bot.answer_callback_query(call.id, "Users")
        return

    if data == "admin_files":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Admin only")
            return
        cur = conn.cursor()
        cur.execute("SELECT id, user_id, orig_name, status FROM files ORDER BY id DESC LIMIT 20")
        rows = cur.fetchall()
        if not rows:
            bot.send_message(chat_id, "No files yet.")
            return
        text = "<b>📁 Files</b>\n"
        for row in rows:
            text += f"- <code>{row['id']}</code> | {row['orig_name']} | {row['status']} | user {row['user_id']}\n"
        bot.send_message(chat_id, text)
        bot.answer_callback_query(call.id, "Files")
        return

    if data == "admin_system":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Admin only")
            return
        cpu, mem = get_system_load()
        bot.send_message(chat_id, f"<b>⚡ System</b>\n\nCPU: {cpu:.1f}%\nMemory: {mem:.1f}%\nRunning: {len(RUNNING_PROCESSES)}")
        bot.answer_callback_query(call.id, "System")
        return

    if data == "admin_stats":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Admin only")
            return
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        users_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM files")
        files_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM files WHERE status = 'Running'")
        running_count = cur.fetchone()[0]
        bot.send_message(chat_id, f"<b>📊 Stats</b>\n\nUsers: {users_count}\nFiles: {files_count}\nRunning: {running_count}")
        bot.answer_callback_query(call.id, "Stats")
        return


# =========================
# Process management
# =========================
def get_logs(file_id: int):
    record = get_file_record(file_id)
    if not record:
        return "No record found"
    log_path = LOGS_DIR / f"run_{file_id}.log"
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return "Unable to read log file"
    return "No logs yet"


def start_process(file_id: int, chat_id: int):
    ok, reason = is_load_ok()
    if not ok:
        bot.send_message(chat_id, f"❌ Cannot start process: {reason}")
        return

    record = get_file_record(file_id)
    if not record:
        bot.send_message(chat_id, "❌ File not found")
        return

    storage_path = Path(record["path"])
    if storage_path.is_dir():
        main_python = find_main_python(storage_path)
        if not main_python:
            bot.send_message(chat_id, "❌ No Python main file found in the uploaded directory")
            return
        run_path = main_python
        workdir = str(main_python.parent)
    else:
        run_path = storage_path
        workdir = str(storage_path.parent)

    if not run_path.exists():
        bot.send_message(chat_id, "❌ File no longer exists")
        return

    log_path = LOGS_DIR / f"run_{file_id}.log"
    env = os.environ.copy()
    env.pop("BOT_TOKEN", None)

    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                [sys.executable, str(run_path)],
                cwd=workdir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                start_new_session=True,
            )
        update_file_status(file_id, "Running", process.pid)
        with PROCESS_LOCK:
            RUNNING_PROCESSES[file_id] = process
        bot.send_message(chat_id, f"✅ Script started successfully. PID: <code>{process.pid}</code>")

        def monitor(p, f_id):
            try:
                code = p.wait(timeout=1)
            except Exception:
                code = p.poll()
            update_file_status(f_id, "Stopped", None)
            with PROCESS_LOCK:
                RUNNING_PROCESSES.pop(f_id, None)
            if code not in (None, 0):
                try:
                    bot.send_message(chat_id, f"⚠️ Script exited with code {code}")
                except Exception:
                    pass

        threading.Thread(target=monitor, args=(process, file_id), daemon=True).start()
    except Exception as e:
        bot.send_message(chat_id, f"❌ Failed to start: {e}")
        logger.exception("Start process failed")


def stop_process(file_id: int):
    with PROCESS_LOCK:
        proc = RUNNING_PROCESSES.pop(file_id, None)
    if not proc:
        update_file_status(file_id, "Stopped", None)
        return False

    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        update_file_status(file_id, "Stopped", None)
        return True
    except Exception as e:
        logger.exception("Failed to stop process")
        return False


# =========================
# Main entry
# =========================
if __name__ == "__main__":
    logger.info("Bot is starting...")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
