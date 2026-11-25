# ZYRON Miner Bot - telebot version - works on Python 3.13
# Install: pip install pyTelegramBotAPI
# Run: python zyron_bot.py

import sqlite3
import time
import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# ---------- TOKEN ----------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    try:
        BOT_TOKEN = open("token.txt").read().strip()
    except:
        BOT_TOKEN = ""
WEBAPP_URL = "https://zohirtime56.github.io/zayron/"
ADMIN_ID = 7527825632

# Config - نفس ديالك
MINE_REWARD = 100
MINE_COOLDOWN = 6 * 3600
REFERRAL_BONUS = 50
DAILY_BONUS = 20

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN missing in token.txt")
    raise SystemExit(1)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# ---------- DATABASE ----------
db = sqlite3.connect("zyron.db", check_same_thread=False)
cur = db.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    balance INTEGER DEFAULT 0,
    last_mine INTEGER DEFAULT 0,
    wallet TEXT DEFAULT '',
    referrals INTEGER DEFAULT 0,
    referred_by INTEGER DEFAULT 0,
    joined_at TEXT
)""")
db.commit()

def get_user(user_id, username=""):
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        from datetime import datetime
        cur.execute("INSERT INTO users (user_id, username, joined_at) VALUES (?,?,?)", (user_id, username, datetime.now().isoformat()))
        db.commit()
        return get_user(user_id, username)
    return row

def add_balance(user_id, amount):
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
    db.commit()

def main_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⛏️ افتح التعدين / Open Miner", web_app=WebAppInfo(url=WEBAPP_URL)))
    kb.add(InlineKeyboardButton("⛏️ تعدين ZYN", callback_data="mine"))
    kb.add(InlineKeyboardButton("💰 الرصيد ديالي", callback_data="balance"), InlineKeyboardButton("👥 الإحالات", callback_data="referral"))
    kb.add(InlineKeyboardButton("👛 ربط المحفظة", callback_data="wallet_help"), InlineKeyboardButton("🏆 المتصدرين", callback_data="leaderboard"))
    kb.add(InlineKeyboardButton("📋 المهام", callback_data="tasks"))
    return kb

@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    me = get_user(user_id, username)
    parts = message.text.split()
    if len(parts) > 1:
        try:
            ref_id = int(parts[1])
            if ref_id != user_id and me[6] == 0:
                cur.execute("UPDATE users SET referred_by=? WHERE user_id=?", (ref_id, user_id))
                add_balance(ref_id, REFERRAL_BONUS)
                add_balance(user_id, REFERRAL_BONUS)
                cur.execute("UPDATE users SET referrals = referrals + 1 WHERE user_id=?", (ref_id,))
                db.commit()
                try:
                    bot.send_message(ref_id, f"🎉 واحد جديد دخل من الرابط ديالك! ربحتي +{REFERRAL_BONUS} ZYN")
                except:
                    pass
        except:
            pass
    text = (
        f"👋 مرحبا بيك فـ *ZYRON Miner* ⛏️\n\n"
        f"عدّن *ZYN* و جمع النقاط، ومن بعد غادي نحولوهم ليك لـ ZYN حقيقي فالمحفظة ديالك.\n\n"
        f"💰 كل تعدين: *+{MINE_REWARD} ZYN*\n"
        f"⏰ كل *6 ساعات* تقدر تعدّن\n"
        f"👥 كل صاحب تجيبو: *+{REFERRAL_BONUS} ZYN* ليك وليه\n\n"
        f"بدا دابا وورك على تعدين 👇"
    )
    bot.send_message(user_id, text, reply_markup=main_keyboard())

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    username = call.from_user.username or ""
    data = call.data
    me = get_user(user_id, username)
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data == "mine":
        now = int(time.time())
        last = me[3]
        left = MINE_COOLDOWN - (now - last)
        if left > 0:
            h = left // 3600
            m = (left % 3600) // 60
            bot.edit_message_text(f"⏳ باقي ليك {h}h و {m}m باش تقدر تعدّن عاود.\n\nالرصيد ديالك: {me[2]} ZYN", chat_id, msg_id, reply_markup=main_keyboard())
            return
        add_balance(user_id, MINE_REWARD)
        cur.execute("UPDATE users SET last_mine=? WHERE user_id=?", (now, user_id))
        db.commit()
        new_bal = get_user(user_id)[2]
        bot.edit_message_text(f"✅ صافي عدّنتي!\n\n💰 ربحتي +{MINE_REWARD} ZYN\n💳 الرصيد ديالك دابا: {new_bal} ZYN\n\nرجع من بعد 6 ساعات باش تعدّن عاود ⛏️", chat_id, msg_id, reply_markup=main_keyboard())

    elif data == "balance":
        bot.edit_message_text(f"💰 الرصيد ديالك: *{me[2]} ZYN*\n👥 الإحالات ديالك: {me[5]}\n👛 المحفظة: {me[4] or 'مازال ما ربطتيش'}\n\nربط المحفظة باش من بعد نصيفطو ليك التوكنات الحقيقية.", chat_id, msg_id, reply_markup=main_keyboard())

    elif data == "referral":
        me_bot = bot.get_me()
        link = f"https://t.me/{me_bot.username}?start={user_id}"
        bot.edit_message_text(f"👥 الرابط ديالك:\n`{link}`\n\nكل صاحب يدخل منو كتربح +{REFERRAL_BONUS} ZYN، وهو تا هو كيربح +{REFERRAL_BONUS} ZYN.", chat_id, msg_id, reply_markup=main_keyboard())

    elif data == "wallet_help":
        bot.edit_message_text("👛 باش تربط المحفظة:\n\nصيفط هاد الأمر فالبوت:\n`/wallet UQ... عنوان المحفظة ديالك`\n\nمثال:\n`/wallet UQBIVcSvumdQ1N9P3yHbUUJM...`\n\nلازم تكون محفظة TON، بحال Tonkeeper.", chat_id, msg_id, reply_markup=main_keyboard())

    elif data == "leaderboard":
        cur.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
        rows = cur.fetchall()
        txt = "🏆 المتصدرين Top 10:\n\n"
        for i, (u, b) in enumerate(rows, 1):
            txt += f"{i}. @{u or 'user'} - {b} ZYN\n"
        bot.edit_message_text(txt, chat_id, msg_id, reply_markup=main_keyboard())

    elif data == "tasks":
        bot.edit_message_text("📋 المهام:\n\n1️⃣ دخل للغروب ديال ZYRON\n2️⃣ تابعنا فـ X / Telegram\n3️⃣ جيب 5 ديال صحابك\n\nكل مهمة كتزيد ليك النقاط. قريبا غادي نزيدو المهام هنا.", chat_id, msg_id, reply_markup=main_keyboard())

@bot.message_handler(commands=['wallet'])
def wallet_cmd(message):
    user_id = message.from_user.id
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "صيفط هاكا: /wallet + عنوان المحفظة ديالك")
        return
    addr = parts[1]
    if not addr.startswith(("UQ", "EQ", "0Q")) or len(addr) < 40:
        bot.reply_to(message, "❌ هاد العنوان ما صحيحش. خاصو يبدا بـ UQ ولا EQ ويكون طويل.")
        return
    cur.execute("UPDATE users SET wallet=? WHERE user_id=?", (addr, user_id))
    db.commit()
    bot.reply_to(message, f"✅ تربطات المحفظة: {addr}\n\nمن بعد التوزيع غادي نصيفطو ليك ZYN لهاد العنوان.")

@bot.message_handler(commands=['balance'])
def balance_cmd(message):
    me = get_user(message.from_user.id, message.from_user.username or "")
    bot.reply_to(message, f"💰 الرصيد ديالك: {me[2]} ZYN")

@bot.message_handler(commands=['export'])
def export_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return
    cur.execute("SELECT user_id, username, wallet, balance FROM users WHERE balance > 0 AND wallet != '' ORDER BY balance DESC")
    rows = cur.fetchall()
    txt = f"Total users with wallet: {len(rows)}\n\n"
    for r in rows[:100]:
        txt += f"{r[2]} | {r[3]} ZYN | @{r[1]}\n"
    bot.reply_to(message, txt[:4000])

@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return
    cur.execute("SELECT COUNT(*), SUM(balance) FROM users")
    c, s = cur.fetchone()
    bot.reply_to(message, f"👥 Users: {c}\n💰 Total points: {s}")

print("ZYRON Bot is running...")
bot.infinity_polling()
