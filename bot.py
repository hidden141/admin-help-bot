import os
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, deque

import httpx
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("friend-bot")

# ---------- Config (.env / environment se) ----------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}
TIMEOUT_MIN = int(os.getenv("ADMIN_TIMEOUT_MIN", "5"))  # itne min me admin reply na de to bot bolega

# Gemini (OpenAI-compatible endpoint)
client = AsyncOpenAI(
    api_key=os.environ["GEMINI_API_KEY"],
    base_url=os.getenv(
        "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
    ),
)
MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash")

SYSTEM_PROMPT = """Tu ek friendly Telegram group ka assistant bot hai, jo admin ki jagah tab reply karta hai jab admin available nahi hota.

Style:
- Hinglish me baat kar (Hindi + English mix, Roman script), bilkul dost ki tarah: casual, warm, thoda mazaakiya.
- Reply chhota rakh (2-4 line), thode emojis theek hain.
- User jis language/tone me likhe, usi ke hisaab se thoda adjust kar.

Rules:
- User ke sawal ka seedha, helpful jawab de.
- Agar baat admin ke decision ki hai (payment, refund, ban/unban, permission, group rules me chhoot, personal baat), to kuch promise mat kar. Bol ki admin aate hi dekh lega.
- Jo cheez pata nahi ya group ke baare me pakka nahi, andaza mat laga, honestly bol de ki admin hi sahi bata payega.
- Kisi ko gaali ya insult mat kar, agar koi badtameezi kare to politely shant reh.
- Khud ko admin mat bata. Tu admin ka assistant bot hai.
"""

# chat_id -> last few messages
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))
# chat_id -> bot "away mode" ON hai ya nahi
away_mode: dict[int, bool] = defaultdict(bool)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def ai_reply(chat_id: int, user_name: str, text: str) -> str:
    h = history[chat_id]
    h.append({"role": "user", "content": f"{user_name}: {text}"})

    # conversation 'user' se shuru honi chahiye
    while h and h[0]["role"] != "user":
        h.popleft()

    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + list(h),
        )
        answer = (resp.choices[0].message.content or "").strip()
        if not answer:
            raise ValueError("empty reply")
    except Exception:
        log.exception("AI call fail hui")
        return "Yaar abhi thoda issue aa gaya 😅 Admin aate hi tumhara msg dekh lega!"

    h.append({"role": "assistant", "content": answer})
    return answer


# ---------- Commands (sirf admin) ----------
async def cmd_away(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    away_mode[update.effective_chat.id] = True
    await update.message.reply_text("Done bhai 🫡 Ab main sambhal leta hu. Wapas aao to /back bhej dena.")


async def cmd_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    away_mode[update.effective_chat.id] = False
    cancel_pending(context, update.effective_chat.id)
    await update.message.reply_text("Welcome back admin 😎 Main chup ho gaya.")


# ---------- Auto-timeout logic ----------
def pending_name(chat_id: int) -> str:
    return f"pending:{chat_id}"


def cancel_pending(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    for job in context.job_queue.get_jobs_by_name(pending_name(chat_id)):
        job.schedule_removal()


async def delayed_reply(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    answer = await ai_reply(d["chat_id"], d["user_name"], d["text"])
    await context.bot.send_message(
        chat_id=d["chat_id"],
        text=answer,
        reply_to_message_id=d["message_id"],
    )


# ---------- Main message handler ----------
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not msg.text or not user or user.is_bot:
        return

    chat_id = update.effective_chat.id

    # Admin ne kuch bola = admin online hai, pending replies cancel
    if is_admin(user.id):
        cancel_pending(context, chat_id)
        return

    name = user.first_name or "User"

    if away_mode[chat_id]:
        # Admin ne /away kiya hai, turant reply
        await context.bot.send_chat_action(chat_id, "typing")
        answer = await ai_reply(chat_id, name, msg.text)
        await msg.reply_text(answer)
    else:
        # Admin ko TIMEOUT_MIN minute do, uske baad bot reply karega
        context.job_queue.run_once(
            delayed_reply,
            when=TIMEOUT_MIN * 60,
            name=pending_name(chat_id),
            data={
                "chat_id": chat_id,
                "message_id": msg.message_id,
                "user_name": name,
                "text": msg.text,
            },
        )


# ---------- Render free plan: dummy web server + self-ping ----------
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_web():
    port = int(os.getenv("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


async def keepalive(context: ContextTypes.DEFAULT_TYPE):
    # Render free service 15 min idle pe sleep hoti hai, isliye khud ko ping karo
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.get(url)
    except Exception:
        log.warning("keepalive ping fail hui")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("away", cmd_away))
    app.add_handler(CommandHandler("back", cmd_back))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUPS),
            on_message,
        )
    )
    threading.Thread(target=run_web, daemon=True).start()
    app.job_queue.run_repeating(keepalive, interval=600, first=60)
    log.info("Bot chalu ho gaya...")
    app.run_polling()


if __name__ == "__main__":
    main()
