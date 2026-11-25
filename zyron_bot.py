import os, json, telebot
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBAPP_URL = "https://zohirtime56.github.io/zayron/"
bot = telebot.TeleBot(TOKEN)
DATA_FILE = "zyron_data.json"
def load():
    try: return json.load(open(DATA_FILE))
    except: return {}
def save(d): json.dump(d, open(DATA_FILE,"w"))
@bot.message_handler(commands=['start'])
def start(m):
    uid=str(m.from_user.id); data=load()
    if uid not in data:
        data[uid]={"balance":0,"refs":0}
        parts=m.text.split()
        if len(parts)>1:
            ref=parts[1]
            if ref in data: data[ref]["balance"]+=50
        save(data)
    kb=telebot.types.InlineKeyboardMarkup()
    kb.add(telebot.types.InlineKeyboardButton(text="⛏️ افتح التعدين / Open Miner", web_app=telebot.types.WebAppInfo(WEBAPP_URL)))
    bot.reply_to(m, f"مرحبا في ZYRON Miner ⛏️\nرصيدك: {data[uid]['balance']} ZYN\nWelcome to ZYRON Miner", reply_markup=kb)
@bot.message_handler(commands=['balance'])
def bal(m):
    data=load(); b=data.get(str(m.from_user.id),{"balance":0})["balance"]
    bot.reply_to(m, f"رصيدك: {b} ZYN\nYour balance: {b} ZYN")
print("ZYRON Bot running...")
bot.infinity_polling()
