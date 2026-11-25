
# ZYRON Miner Bot - Telegram Mining Bot
# Install: pip install python-telegram-bot==20.7
# Run: python bot.py
# Put your NEW token in BOT_TOKEN (after you revoke the old one in BotFather)

import sqlite3
import time
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = "PUT_YOUR_NEW_TOKEN_HERE"
ADMIN_ID = 7527825632  # your Telegram ID

# Config - you can change these
MINE_REWARD = 100  # points per claim
MINE_COOLDOWN = 6 * 3600  # 6 hours in seconds
REFERRAL_BONUS = 50  # for inviter + invited
DAILY_BONUS = 20

logging.basicConfig(level=logging.INFO)

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
        cur.execute("INSERT INTO users (user_id, username, joined_at) VALUES (?,?,?)", (user_id, username, datetime.now().isoformat()))
        db.commit()
        return get_user(user_id, username)
    return row

def add_balance(user_id, amount):
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
    db.commit()

# ---------- KEYBOARD ----------
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛏️ تعدين ZYN", callback_data="mine")],
        [InlineKeyboardButton("💰 الرصيد ديالي", callback_data="balance"), InlineKeyboardButton("👥 الإحالات", callback_data="referral")],
        [InlineKeyboardButton("👛 ربط المحفظة", callback_data="wallet_help"), InlineKeyboardButton("🏆 المتصدرين", callback_data="leaderboard")],
        [InlineKeyboardButton("📋 المهام", callback_data="tasks")],
    ])

# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    me = get_user(user.id, user.username or "")

    # Referral system
    if args:
        try:
            ref_id = int(args[0])
            if ref_id != user.id and me[6] == 0:  # referred_by == 0
                cur.execute("UPDATE users SET referred_by=? WHERE user_id=?", (ref_id, user.id))
                add_balance(ref_id, REFERRAL_BONUS)
                add_balance(user.id, REFERRAL_BONUS)
                cur.execute("UPDATE users SET referrals = referrals + 1 WHERE user_id=?", (ref_id,))
                db.commit()
                try:
                    await context.bot.send_message(ref_id, f"🎉 واحد جديد دخل من الرابط ديالك! ربحتي +{REFERRAL_BONUS} ZYN")
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
    await update.message.reply_text(text, reply_markup=main_keyboard(), parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    username = query.from_user.username or ""
    data = query.data
    me = get_user(user_id, username)

    if data == "mine":
        now = int(time.time())
        last = me[3]
        left = MINE_COOLDOWN - (now - last)
        if left > 0:
            h = left // 3600
            m = (left % 3600) // 60
            await query.edit_message_text(f"⏳ باقي ليك {h}h و {m}m باش تقدر تعدّن عاود.\n\nالرصيد ديالك: {me[2]} ZYN", reply_markup=main_keyboard())
            return
        add_balance(user_id, MINE_REWARD)
        cur.execute("UPDATE users SET last_mine=? WHERE user_id=?", (now, user_id))
        db.commit()
        new_bal = get_user(user_id)[2]
        await query.edit_message_text(f"✅ صافي عدّنتي!\n\n💰 ربحتي +{MINE_REWARD} ZYN\n💳 الرصيد ديالك دابا: {new_bal} ZYN\n\nرجع من بعد 6 ساعات باش تعدّن عاود ⛏️", reply_markup=main_keyboard())

    elif data == "balance":
        await query.edit_message_text(f"💰 الرصيد ديالك: *{me[2]} ZYN*\n👥 الإحالات ديالك: {me[5]}\n👛 المحفظة: {me[4] or 'مازال ما ربطتيش'}\n\nربط المحفظة باش من بعد نصيفطو ليك التوكنات الحقيقية.", reply_markup=main_keyboard(), parse_mode="Markdown")

    elif data == "referral":
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start={user_id}"
        await query.edit_message_text(f"👥 الرابط ديالك:\n`{link}`\n\nكل صاحب يدخل منو كتربح +{REFERRAL_BONUS} ZYN، وهو تا هو كيربح +{REFERRAL_BONUS} ZYN.", reply_markup=main_keyboard(), parse_mode="Markdown")

    elif data == "wallet_help":
        await query.edit_message_text("👛 باش تربط المحفظة:\n\nصيفط هاد الأمر فالبوت:\n`/wallet UQ... عنوان المحفظة ديالك`\n\nمثال:\n`/wallet UQBIVcSvumdQ1N9P3yHbUUJM...`\n\nلازم تكون محفظة TON، بحال Tonkeeper.", reply_markup=main_keyboard(), parse_mode="Markdown")

    elif data == "leaderboard":
        cur.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
        rows = cur.fetchall()
        txt = "🏆 المتصدرين Top 10:\n\n"
        for i, (u, b) in enumerate(rows, 1):
            txt += f"{i}. @{u or 'user'} - {b} ZYN\n"
        await query.edit_message_text(txt, reply_markup=main_keyboard())

    elif data == "tasks":
        await query.edit_message_text("📋 المهام:\n\n1️⃣ دخل للغروب ديال ZYRON\n2️⃣ تابعنا فـ X / Telegram\n3️⃣ جيب 5 ديال صحابك\n\nكل مهمة كتزيد ليك النقاط. قريبا غادي نزيدو المهام هنا.", reply_markup=main_keyboard())

async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("صيفط هاكا: /wallet + عنوان المحفظة ديالك")
        return
    addr = context.args[0]
    if not addr.startswith(("UQ", "EQ", "0Q")) or len(addr) < 40:
        await update.message.reply_text("❌ هاد العنوان ما صحيحش. خاصو يبدا بـ UQ ولا EQ ويكون طويل.")
        return
    cur.execute("UPDATE users SET wallet=? WHERE user_id=?", (addr, user_id))
    db.commit()
    await update.message.reply_text(f"✅ تربطات المحفظة: {addr}\n\nمن بعد التوزيع غادي نصيفطو ليك ZYN لهاد العنوان.")

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = get_user(update.effective_user.id, update.effective_user.username or "")
    await update.message.reply_text(f"💰 الرصيد ديالك: {me[2]} ZYN")

# Admin: export wallets for distribution
async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cur.execute("SELECT user_id, username, wallet, balance FROM users WHERE balance > 0 AND wallet != '' ORDER BY balance DESC")
    rows = cur.fetchall()
    txt = f"Total users with wallet: {len(rows)}\n\n"
    for r in rows[:100]:
        txt += f"{r[2]} | {r[3]} ZYN | @{r[1]}\n"
    await update.message.reply_text(txt[:4000])

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cur.execute("SELECT COUNT(*), SUM(balance) FROM users")
    c, s = cur.fetchone()
    await update.message.reply_text(f"👥 Users: {c}\n💰 Total points: {s}")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("ZYRON Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
