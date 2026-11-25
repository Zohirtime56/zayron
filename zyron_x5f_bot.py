import os, json, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBAPP_URL = "https://zohirtime56.github.io/zayron/"
DATA_FILE = "zyron_data.json"

def load():
    try:
        return json.load(open(DATA_FILE))
    except:
        return {}

def save(d):
    json.dump(d, open(DATA_FILE, "w"))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load()
    if uid not in data:
        data[uid] = {"balance": 0, "refs": 0}
        # referral
        if context.args:
            ref = context.args[0]
            if ref in data:
                data[ref]["balance"] += 50
        save(data)
    kb = [[InlineKeyboardButton("⛏️ افتح التعدين / Open Miner", web_app={"url": WEBAPP_URL})]]
    await update.message.reply_text("مرحبا في ZYRON Miner ⛏️\nرصيدك: %s ZYN\nWelcome to ZYRON Miner" % data[uid]["balance"], reply_markup=InlineKeyboardMarkup(kb))

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load()
    b = data.get(uid, {"balance": 0})["balance"]
    await update.message.reply_text(f"رصيدك: {b} ZYN\nYour balance: {b} ZYN")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    print("ZYRON Bot running...")
    app.run_polling()
