# config.py — Environment variables & configuration (single source of truth)
import logging
import os

# ---------- Logging (shared by live.py and bot.py) ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------- Telegram ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# ---------- Database ----------
DB_PATH = os.getenv("DB_PATH", "bot.db")

# ---------- Flask / Render ----------
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))
