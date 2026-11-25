# ZYRON Miner Bot - telebot - UNIFIED DB with Flask app.py
# Same zyron.db as app.py -> same balance in Bot and App
import sqlite3, time, os, json, telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "zyron.db")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN","").strip()
if not BOT_TOKEN:
    try: BOT_TOKEN = open(os.path.join(BASE,"token.txt")).read().strip()
    except: BOT_TOKEN=""
WEBAPP_URL = "https://zohirtime56.github.io/zayron/"
ADMIN_ID = 7527825632
MINE_REWARD = 100
MINE_COOLDOWN = 6*3600
REFERRAL_BONUS = 50

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    print("ERROR: token.txt missing"); raise SystemExit(1)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

def db():
    c=sqlite3.connect(DB, check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init_db():
    c=db(); cur=c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY, username TEXT, balance REAL DEFAULT 0,
        pending REAL DEFAULT 0, mining INTEGER DEFAULT 0, last_tick INTEGER DEFAULT 0,
        wallet TEXT DEFAULT '', referrals INTEGER DEFAULT 0, referred_by TEXT DEFAULT '',
        joined_at TEXT DEFAULT '', onchain REAL DEFAULT 0, boost INTEGER DEFAULT 1, tasks TEXT DEFAULT '{}', ref_earn REAL DEFAULT 0
    )""")
    c.commit(); c.close()
init_db()
def level_from(h):
    h=float(h or 0)
    if h>=100000: return 500
    if h>=50000: return 100
    if h>=10000: return 50
    if h>=1000: return 10
    if h>=533: return 3
    if h>=116: return 2
    return 1
def reward_for(l):
    if l>=500: return 500000
    if l>=100: return 100000
    if l>=50: return 30000
    if l>=10: return 6000
    if l>=3: return 854
    if l>=2: return 690
    return 600
def get_user(uid, username=""):
    c=db(); cur=c.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (str(uid),))
    r=cur.fetchone()
    if not r:
        cur.execute("INSERT INTO users (user_id, username, joined_at, last_tick) VALUES (?,?,?,?)", (str(uid), username, str(int(time.time())), int(time.time()*1000)))
        c.commit(); cur.execute("SELECT * FROM users WHERE user_id=?", (str(uid),)); r=cur.fetchone()
    if r["mining"]:
        now=int(time.time()*1000); elapsed=(now-(r["last_tick"] or now))/1000
        lvl=level_from(max(r["onchain"] or 0, r["balance"] or 0))
        add=elapsed*reward_for(lvl)*(r["boost"] or 1)/86400
        if add>0:
            cur.execute("UPDATE users SET pending=pending+?, last_tick=? WHERE user_id=?", (add, now, str(uid))); c.commit()
            cur.execute("SELECT * FROM users WHERE user_id=?", (str(uid),)); r=cur.fetchone()
    c.close(); return dict(r)
def add_balance(uid, amt):
    c=db(); cur=c.cursor(); cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, str(uid))); c.commit(); c.close()
def main_keyboard():
    kb=InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⛏️ افتح التعدين / Open Miner", web_app=WebAppInfo(url=WEBAPP_URL)))
    kb.add(InlineKeyboardButton("⛏️ تعدين ZYN", callback_data="mine"))
    kb.add(InlineKeyboardButton("💰 الرصيد ديالي", callback_data="balance"), InlineKeyboardButton("👥 الإحالات", callback_data="referral"))
    kb.add(InlineKeyboardButton("👛 ربط المحفظة", callback_data="wallet_help"), InlineKeyboardButton("🏆 المتصدرين", callback_data="leaderboard"))
    kb.add(InlineKeyboardButton("📋 المهام", callback_data="tasks"))
    return kb

@bot.message_handler(commands=['start'])
def start(message):
    uid=message.from_user.id; username=message.from_user.username or ""
    me=get_user(uid, username)
    parts=message.text.split()
    if len(parts)>1:
        try:
            ref=parts[1].replace("ref_","")
            ref_id=int(ref)
            if ref_id!=uid and not me["referred_by"]:
                c=db(); cur=c.cursor()
                cur.execute("UPDATE users SET referred_by=? WHERE user_id=?", (str(ref_id), str(uid)))
                c.commit(); c.close()
                add_balance(ref_id, REFERRAL_BONUS); add_balance(uid, REFERRAL_BONUS)
                c=db(); cur=c.cursor(); cur.execute("UPDATE users SET referrals=referrals+1 WHERE user_id=?", (str(ref_id),)); c.commit(); c.close()
                try: bot.send_message(ref_id, f"🎉 واحد جديد دخل من الرابط ديالك! ربحتي +{REFERRAL_BONUS} ZYN")
                except: pass
        except: pass
    # نفس الرصيد اللي في التطبيق
    me=get_user(uid)
    total=me["balance"]+me["pending"]
    text=(f"👋 مرحبا بيك فـ *ZYRON Miner* ⛏️\n\nعدّن *ZYN* في التطبيق أو هنا، الرصيد واحد.\n\n💰 رصيدك الكامل: *{int(total)} ZYN* (رصيد {int(me['balance'])} + معلق {int(me['pending'])})\n⏰ التعدين في التطبيق: *{reward_for(level_from(max(me['onchain'] or 0, me['balance'] or 0)))} ZYN / يوم*\n👥 كل صاحب تجيبو: *+{REFERRAL_BONUS} ZYN* ليك وليه\n\nبدا دابا وورك على تعدين 👇")
    bot.send_message(uid, text, reply_markup=main_keyboard())

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    bot.answer_callback_query(call.id)
    uid=call.from_user.id; username=call.from_user.username or ""
    data=call.data; me=get_user(uid, username)
    chat_id=call.message.chat.id; msg_id=call.message.message_id
    total=me["balance"]+me["pending"]
    if data=="mine":
        # تعدين البوت يضيف 100 لنفس الرصيد المشترك
        add_balance(uid, MINE_REWARD)
        me=get_user(uid)
        bot.edit_message_text(f"✅ صافي عدّنتي في البوت!\n\n💰 ربحتي +{MINE_REWARD} ZYN\n💳 رصيدك الكامل دابا: *{int(me['balance']+me['pending'])} ZYN* (نفس الرصيد في التطبيق)\n\nعدّن في التطبيق باش تربح أكثر ⛏️", chat_id, msg_id, reply_markup=main_keyboard())
    elif data=="balance":
        bot.edit_message_text(f"💰 رصيدك الكامل: *{int(total)} ZYN*\n(رصيد {int(me['balance'])} + معلق {int(me['pending'])})\n👥 الإحالات ديالك: {me['referrals']}\n👛 المحفظة: {me['wallet'] or 'مازال ما ربطتيش'}\n🏆 المستوى: {level_from(max(me['onchain'] or 0, me['balance'] or 0))} / 500\n\nنفس الرصيد كيبان في التطبيق.", chat_id, msg_id, reply_markup=main_keyboard())
    elif data=="referral":
        link=f"https://t.me/{bot.get_me().username}?start={uid}"
        bot.edit_message_text(f"👥 الرابط ديالك:\n`{link}`\n\nكل صاحب يدخل منو كتربح +{REFERRAL_BONUS} ZYN، وهو تا هو كيربح +{REFERRAL_BONUS} ZYN.", chat_id, msg_id, reply_markup=main_keyboard())
    elif data=="wallet_help":
        bot.edit_message_text("👛 باش تربط المحفظة:\n\nصيفط هاد الأمر فالبوت:\n`/wallet UQ... عنوان المحفظة ديالك`\n\nمثال:\n`/wallet UQBIVcSvumdQ1N9P3yHbUUJM...`\n\nلازم تكون محفظة TON، بحال Tonkeeper.", chat_id, msg_id, reply_markup=main_keyboard())
    elif data=="leaderboard":
        c=db(); cur=c.cursor(); cur.execute("SELECT username, balance, pending FROM users ORDER BY balance DESC LIMIT 10"); rows=cur.fetchall(); c.close()
        txt="🏆 المتصدرين Top 10:\n\n"
        for i,(u,b,p) in enumerate(rows,1):
            txt+=f"{i}. @{u or 'user'} - {int((b or 0)+(p or 0))} ZYN\n"
        bot.edit_message_text(txt, chat_id, msg_id, reply_markup=main_keyboard())
    elif data=="tasks":
        bot.edit_message_text("📋 المهام:\n\nدير المهام في التطبيق باش تربح +500 لكل مهمة.\n\n1️⃣ دخل للغروب ديال ZYRON\n2️⃣ تابعنا فـ X / Telegram\n3️⃣ جيب 5 ديال صحابك", chat_id, msg_id, reply_markup=main_keyboard())

@bot.message_handler(commands=['wallet'])
def wallet_cmd(message):
    uid=message.from_user.id; parts=message.text.split()
    if len(parts)<2: bot.reply_to(message, "صيفط هاكا: /wallet + عنوان المحفظة ديالك"); return
    addr=parts[1]
    if not addr.startswith(("UQ","EQ","0Q")) or len(addr)<40: bot.reply_to(message, "❌ هاد العنوان ما صحيحش."); return
    c=db(); cur=c.cursor(); cur.execute("UPDATE users SET wallet=? WHERE user_id=?", (addr, str(uid))); c.commit(); c.close()
    bot.reply_to(message, f"✅ تربطات المحفظة: {addr}\n\nنفس المحفظة غادي تبان في التطبيق.")

@bot.message_handler(commands=['balance'])
def balance_cmd(message):
    me=get_user(message.from_user.id, message.from_user.username or "")
    bot.reply_to(message, f"💰 رصيدك الكامل: {int(me['balance']+me['pending'])} ZYN (نفس الرصيد في التطبيق)")

@bot.message_handler(commands=['export'])
def export_cmd(message):
    if message.from_user.id!=ADMIN_ID: return
    c=db(); cur=c.cursor(); cur.execute("SELECT username, wallet, balance, pending FROM users WHERE balance>0 AND wallet!='' ORDER BY balance DESC"); rows=cur.fetchall(); c.close()
    txt=f"Total users with wallet: {len(rows)}\n\n"
    for r in rows[:100]: txt+=f"{r[1]} | {int(r[2]+r[3])} ZYN | @{r[0]}\n"
    bot.reply_to(message, txt[:4000])
@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    if message.from_user.id!=ADMIN_ID: return
    c=db(); cur=c.cursor(); cur.execute("SELECT COUNT(*), SUM(balance+pending) FROM users"); cc,ss=cur.fetchone(); c.close()
    bot.reply_to(message, f"👥 Users: {cc}\n💰 Total points: {int(ss or 0)}")
@bot.message_handler(commands=['withdraw'])
def withdraw_cmd(message):
    # من التطبيق: /withdraw 10000 UQ...
    parts=message.text.split()
    if len(parts)<3: bot.reply_to(message, "صيفط: /withdraw المبلغ المحفظة"); return
    try: amt=float(parts[1])
    except: bot.reply_to(message, "المبلغ غير صحيح"); return
    w=parts[2]; uid=message.from_user.id; me=get_user(uid)
    if amt<10000: bot.reply_to(message, "الحد الأدنى 10000"); return
    if amt>me["balance"]: bot.reply_to(message, "رصيدك لا يكفي"); return
    c=db(); cur=c.cursor(); cur.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amt, str(uid))); cur.execute("INSERT INTO withdraws (user_id, amount, wallet, created_at) VALUES (?,?,?,?)", (str(uid), amt, w, int(time.time()))); c.commit(); c.close()
    bot.reply_to(message, f"✅ تم إرسال طلب سحب {int(amt)} ZYN إلى {w}\nسيصلك بعد مراجعة المدير.")
    try: bot.send_message(ADMIN_ID, f"💸 طلب سحب جديد\nمن: @{message.from_user.username} ({uid})\nالمبلغ: {amt} ZYN\nالمحفظة: {w}")
    except: pass

print("ZYRON Bot is running... UNIFIED DB")
bot.infinity_polling()
