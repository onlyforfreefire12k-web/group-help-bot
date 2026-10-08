# live.py — ONLY the application startup / orchestration layer.
# Render Start Command: python live.py
#
#   Main Thread        → Flask health server (0.0.0.0:$PORT)
#   Background Thread  → Telegram bot (dedicated asyncio loop → run_polling(stop_signals=None))

import logging
import threading
import traceback

from flask import Flask

import config

log = logging.getLogger("live")

# ---------------- Flask app (main thread) ----------------
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return "✅ GroupManagerBot is running!"

# ---------------- Telegram bot (background thread) ----------------
def run_bot():
    # Lazy import: if bot.py has an error, the exception is caught in _bot_worker
    # and Flask keeps serving the Render health endpoint.
    from bot import run_bot as bot_entry
    bot_entry()

def _bot_worker():
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        run_bot()
    except Exception:
        # Full traceback → Render logs (requirement: clear debugging)
        log.critical("❌ Telegram bot thread crashed!\n%s", traceback.format_exc())
        traceback.print_exc()
    finally:
        try:
            loop.close()
        except Exception:
            pass

def start_bot_thread():
    # Exactly ONE thread, started exactly ONCE → exactly ONE polling instance.
    thread = threading.Thread(target=_bot_worker, name="telegram-bot", daemon=True)
    thread.start()
    return thread

# ---------------- Entry point ----------------
if __name__ == "__main__":
    if not config.BOT_TOKEN:
        log.warning("⚠️ BOT_TOKEN is not set! The Telegram bot will not start. "
                    "Set it in Render → Environment and redeploy. Flask health server will still run.")

    start_bot_thread()

    log.info("🌐 Flask health server listening on %s:%s", config.HOST, config.PORT)
    # use_reloader=False → guarantees Flask never forks a second process
    # (a fork would create a SECOND polling instance → Telegram 409 Conflict).
    app.run(host=config.HOST, port=config.PORT, threaded=True, use_reloader=False)
