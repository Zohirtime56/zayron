# ZYRON Miner Bot - telebot - UNIFIED DB with Flask app.py
# Same zyron.db as app.py -> same balance in Bot and App
import sqlite3, time, os, json, threading, telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "zyron.db")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    try:
        BOT_TOKEN = open(os.path.join(BASE, "token.txt")).read().strip()
    except Exception:
        BOT_TOKEN = ""
WEBAPP_URL = "https://zohirtime56.github.io/zayron/"
ADMIN_ID = 7527825632
MINE_REWARD = 100
MINE_COOLDOWN = 6 * 3600
REFERRAL_BONUS = 50
MIN_WITHDRAW = 10000
WITHDRAW_COOLDOWN = 48 * 3600

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    print("ERROR: token.txt missing")
    raise SystemExit(1)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

_db_lock = threading.Lock()

def db():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    return c

def init_db():
    c = db(); cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY, username TEXT, balance REAL DEFAULT 0,
        pending REAL DEFAULT 0, mining INTEGER DEFAULT 0, last_tick INTEGER DEFAULT 0,
        wallet TEXT DEFAULT '', referrals INTEGER DEFAULT 0, referred_by TEXT DEFAULT '',
        joined_at TEXT DEFAULT '', onchain REAL DEFAULT 0, boost INTEGER DEFAULT 1,
        tasks TEXT DEFAULT '{}', ref_earn REAL DEFAULT 0, last_bot_mine INTEGER DEFAULT 0
    )""")
    cols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    if "last_bot_mine" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_bot_mine INTEGER DEFAULT 0")
    c.commit(); c.close()

init_db()

def level_from(h):
    h = float(h or 0)
    if h <= 0: return 0
    if h >= 100000000: return 500
    if h >= 61100000: return 401 + int((h - 61100000) // 389000)
    if h >= 32400000: return 301 + int((h - 32400000) // 287000)
    if h >= 13200000: return 201 + int((h - 13200000) // 192000)
    if h >= 2850000: return 101 + int((h - 2850000) // 103500)
    return 1 + int(h // 28500)

def reward_for(l):
    l = int(l or 0)
    if l <= 0: return 0
    if l >= 500: return 1000000
    return 600 + l * l * 4

def get_user(uid, username=""):
    uid = str(uid)
    with _db_lock:
        c = db(); cur = c.cursor(); now = int(time.time() * 1000)
        cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        r = cur.fetchone()
        if not r:
            cur.execute("INSERT INTO users (user_id, username, joined_at, last_tick) VALUES (?,?,?,?)",
                        (uid, username, str(int(time.time())), now))
            c.commit()
            cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
            r = cur.fetchone()
        if r["mining"]:
            last = r["last_tick"] or now
            elapsed = max(0, now - last) / 1000
            lvl = level_from(max(r["onchain"] or 0, r["balance"] or 0))
            add = elapsed * reward_for(lvl) * (r["boost"] or 1) / 86400
            if add > 0:
                cur.execute("UPDATE users SET pending=pending+?, last_tick=? WHERE user_id=?",
                            (add, now, uid))
                c.commit()
                cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
                r = cur.fetchone()
        c.close()
        return dict(r)

def add_balance(uid, amt):
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, str(uid)))
        c.commit(); c.close()

def safe_edit(text, chat_id, msg_id, kb=None):
    """يتجنب خطأ 'message is not modified' وأخطاء Markdown."""
    try:
        bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb or main_keyboard())
    except Exception as e:
        if "not modified" not in str(e).lower():
            try:
                bot.send_message(chat_id, text, reply_markup=kb or main_keyboard())
            except Exception:
                pass

def main_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⛏️ افتح التعدين / Open Miner", web_app=WebAppInfo(url=WEBAPP_URL)))
    kb.add(InlineKeyboardButton("⛏️ تعدين ZYN", callback_data="mine"))
    kb.add(InlineKeyboardButton("💰 الرصيد ديالي", callback_data="balance"),
           InlineKeyboardButton("👥 الإحالات", callback_data="referral"))
    kb.add(InlineKeyboardButton("👛 ربط المحفظة", callback_data="wallet_help"),
           InlineKeyboardButton("🏆 المتصدرين", callback_data="leaderboard"))
    kb.add(InlineKeyboardButton("📋 المهام", callback_data="tasks"))
    return kb

@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    username = message.from_user.username or ""
    me = get_user(uid, username)
    parts = (message.text or "").split()
    if len(parts) > 1:
        try:
            ref_id = int(parts[1].replace("ref_", ""))
            if ref_id != uid and not me["referred_by"]:
                with _db_lock:
                    c = db(); cur = c.cursor()
                    cur.execute("SELECT user_id FROM users WHERE user_id=?", (str(ref_id),))
                    exists = cur.fetchone() is not None
                    if exists:
                        cur.execute("UPDATE users SET referred_by=? WHERE user_id=?", (str(ref_id), str(uid)))
                        cur.execute("UPDATE users SET balance=balance+?, referrals=referrals+1, ref_earn=ref_earn+? WHERE user_id=?",
                                    (REFERRAL_BONUS, REFERRAL_BONUS, str(ref_id)))
                        cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (REFERRAL_BONUS, str(uid)))
                        c.commit()
                    c.close()
                if exists:
                    try:
                        bot.send_message(ref_id, f"🎉 واحد جديد دخل من الرابط ديالك! ربحتي +{REFERRAL_BONUS} ZYN")
                    except Exception:
                        pass
        except (ValueError, Exception):
            pass
    me = get_user(uid)
    total = (me["balance"] or 0) + (me["pending"] or 0)
    per_day = reward_for(level_from(max(me["onchain"] or 0, me["balance"] or 0)))
    text = (f"👋 مرحبا بيك فـ *ZYRON Miner* ⛏️\n\n"
            f"عدّن *ZYN* في التطبيق أو هنا، الرصيد واحد.\n\n"
            f"💰 رصيدك الكامل: *{int(total)} ZYN* (رصيد {int(me['balance'] or 0)} + معلق {int(me['pending'] or 0)})\n"
            f"⏰ التعدين في التطبيق: *{per_day} ZYN / يوم*\n"
            f"👥 كل صاحب تجيبو: *+{REFERRAL_BONUS} ZYN* ليك وليه\n\n"
            f"بدا دابا وورك على تعدين 👇")
    bot.send_message(uid, text, reply_markup=main_keyboard())

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    uid = call.from_user.id
    username = call.from_user.username or ""
    data = call.data
    me = get_user(uid, username)
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    total = (me["balance"] or 0) + (me["pending"] or 0)

    if data == "mine":
        # مكافأة البوت كل 6 ساعات (بلا cooldown كانت كتخلي الناس يضغطو بلا حدود)
        now = int(time.time())
        last = me.get("last_bot_mine") or 0
        if now - last < MINE_COOLDOWN:
            left = MINE_COOLDOWN - (now - last)
            h, m = left // 3600, (left % 3600) // 60
            safe_edit(f"⏳ مازال باقي {h}س {m}د باش تقدر تعدّن في البوت من جديد.\n\n"
                      f"💰 رصيدك: *{int(total)} ZYN*\nعدّن في التطبيق باش تربح أكثر ⛏️",
                      chat_id, msg_id)
            return
        with _db_lock:
            c = db(); cur = c.cursor()
            cur.execute("UPDATE users SET balance=balance+?, last_bot_mine=? WHERE user_id=?",
                        (MINE_REWARD, now, str(uid)))
            c.commit(); c.close()
        me = get_user(uid)
        safe_edit(f"✅ صافي عدّنتي في البوت!\n\n"
                  f"💰 ربحتي +{MINE_REWARD} ZYN\n"
                  f"💳 رصيدك الكامل دابا: *{int((me['balance'] or 0) + (me['pending'] or 0))} ZYN* (نفس الرصيد في التطبيق)\n\n"
                  f"⏳ المكافأة الجاية بعد 6 ساعات. عدّن في التطبيق باش تربح أكثر ⛏️",
                  chat_id, msg_id)
    elif data == "balance":
        lvl = level_from(max(me["onchain"] or 0, me["balance"] or 0))
        safe_edit(f"💰 رصيدك الكامل: *{int(total)} ZYN*\n"
                  f"(رصيد {int(me['balance'] or 0)} + معلق {int(me['pending'] or 0)})\n"
                  f"👥 الإحالات ديالك: {me['referrals'] or 0}\n"
                  f"👛 المحفظة: {me['wallet'] or 'مازال ما ربطتيش'}\n"
                  f"🏆 المستوى: {lvl} / 500\n\n"
                  f"نفس الرصيد كيبان في التطبيق.", chat_id, msg_id)
    elif data == "referral":
        try:
            bot_username = bot.get_me().username
        except Exception:
            bot_username = "ZyronMinerBot"
        link = f"https://t.me/{bot_username}?start={uid}"
        safe_edit(f"👥 الرابط ديالك:\n`{link}`\n\n"
                  f"كل صاحب يدخل منو كتربح +{REFERRAL_BONUS} ZYN، وهو تا هو كيربح +{REFERRAL_BONUS} ZYN.",
                  chat_id, msg_id)
    elif data == "wallet_help":
        safe_edit("👛 باش تربط المحفظة:\n\n"
                  "صيفط هاد الأمر فالبوت:\n`/wallet UQ... عنوان المحفظة ديالك`\n\n"
                  "مثال:\n`/wallet UQBIVcSvumdQ1N9P3yHbUUJM...`\n\n"
                  "لازم تكون محفظة TON، بحال Tonkeeper.", chat_id, msg_id)
    elif data == "leaderboard":
        c = db(); cur = c.cursor()
        cur.execute("SELECT username, balance, pending FROM users ORDER BY (balance+pending) DESC LIMIT 10")
        rows = cur.fetchall(); c.close()
        txt = "🏆 المتصدرين Top 10:\n\n"
        for i, r in enumerate(rows, 1):
            name = (r["username"] or "user").replace("_", "\\_").replace("*", "")
            txt += f"{i}. @{name} - {int((r['balance'] or 0) + (r['pending'] or 0))} ZYN\n"
        if not rows:
            txt += "مازال ما كاين حتى واحد."
        safe_edit(txt, chat_id, msg_id)
    elif data == "tasks":
        safe_edit("📋 المهام:\n\nدير المهام في التطبيق باش تربح +500 لكل مهمة.\n\n"
                  "1️⃣ دخل للغروب ديال ZYRON\n2️⃣ تابعنا فـ X / Telegram\n3️⃣ جيب 5 ديال صحابك",
                  chat_id, msg_id)

@bot.message_handler(commands=['wallet'])
def wallet_cmd(message):
    uid = message.from_user.id
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "صيفط هاكا: /wallet + عنوان المحفظة ديالك")
        return
    addr = parts[1].strip()
    if not addr.startswith(("UQ", "EQ", "0Q")) or len(addr) < 40:
        bot.reply_to(message, "❌ هاد العنوان ما صحيحش. لازم يبدا بـ UQ أو EQ ويكون عنوان TON كامل.")
        return
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE users SET wallet=? WHERE user_id=?", (addr, str(uid)))
        c.commit(); c.close()
    bot.reply_to(message, f"✅ تربطات المحفظة: {addr}\n\nنفس المحفظة غادي تبان في التطبيق.")

@bot.message_handler(commands=['balance'])
def balance_cmd(message):
    me = get_user(message.from_user.id, message.from_user.username or "")
    bot.reply_to(message, f"💰 رصيدك الكامل: {int((me['balance'] or 0) + (me['pending'] or 0))} ZYN (نفس الرصيد في التطبيق)")

@bot.message_handler(commands=['export'])
def export_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return
    c = db(); cur = c.cursor()
    cur.execute("SELECT username, wallet, balance, pending FROM users WHERE balance>0 AND wallet!='' ORDER BY balance DESC")
    rows = cur.fetchall(); c.close()
    txt = f"Total users with wallet: {len(rows)}\n\n"
    for r in rows[:100]:
        txt += f"{r[1]} | {int((r[2] or 0) + (r[3] or 0))} ZYN | @{r[0]}\n"
    bot.reply_to(message, txt[:4000])

@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return
    c = db(); cur = c.cursor()
    cur.execute("SELECT COUNT(*), SUM(balance+pending) FROM users")
    cc, ss = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM withdraws WHERE status='pending'")
    wp = cur.fetchone()[0]
    c.close()
    bot.reply_to(message, f"👥 Users: {cc}\n💰 Total points: {int(ss or 0)}\n💸 Pending withdrawals: {wp}")

@bot.message_handler(commands=['approve'])
def approve_cmd(message):
    # للمدير فقط: /approve <withdraw_id>
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "صيفط: /approve رقم_الطلب")
        return
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE withdraws SET status='approved' WHERE id=?", (parts[1],))
        n = cur.rowcount
        c.commit(); c.close()
    bot.reply_to(message, "✅ تم الموافقة" if n else "❌ ما لقيتش هاد الطلب")

@bot.message_handler(commands=['reject'])
def reject_cmd(message):
    # للمدير فقط: /reject <withdraw_id> - يرجع الرصيد للمستخدم
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "صيفط: /reject رقم_الطلب")
        return
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("SELECT user_id, amount, status FROM withdraws WHERE id=?", (parts[1],))
        r = cur.fetchone()
        if not r:
            c.close(); bot.reply_to(message, "❌ ما لقيتش هاد الطلب"); return
        if r["status"] != "pending":
            c.close(); bot.reply_to(message, "❌ هاد الطلب تدار من قبل"); return
        cur.execute("UPDATE withdraws SET status='rejected' WHERE id=?", (parts[1],))
        cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (r["amount"], r["user_id"]))
        c.commit(); c.close()
    try:
        bot.send_message(int(r["user_id"]), f"❌ طلب السحب ديالك ({int(r['amount'])} ZYN) ترفض، والرصيد رجع ليك.")
    except Exception:
        pass
    bot.reply_to(message, "✅ تم الرفض وإرجاع الرصيد")

@bot.message_handler(commands=['withdraw'])
def withdraw_cmd(message):
    # من البوت: /withdraw 10000 UQ...
    parts = (message.text or "").split()
    if len(parts) < 3:
        bot.reply_to(message, "صيفط: /withdraw المبلغ المحفظة")
        return
    try:
        amt = float(parts[1])
    except ValueError:
        bot.reply_to(message, "المبلغ غير صحيح")
        return
    w = parts[2].strip()
    uid = message.from_user.id
    me = get_user(uid)
    if amt < MIN_WITHDRAW:
        bot.reply_to(message, f"الحد الأدنى {MIN_WITHDRAW}")
        return
    if amt > (me["balance"] or 0):
        bot.reply_to(message, "رصيدك لا يكفي")
        return
    if not w.startswith(("UQ", "EQ")) or len(w) < 40:
        bot.reply_to(message, "❌ عنوان المحفظة غير صحيح")
        return
    now = int(time.time())
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("SELECT id FROM withdraws WHERE user_id=? AND status='pending'", (str(uid),))
        if cur.fetchone():
            c.close()
            bot.reply_to(message, "⏳ عندك طلب سحب قيد المعالجة، تسنى حتى يتدار.")
            return
        cur.execute("SELECT MAX(created_at) FROM withdraws WHERE user_id=?", (str(uid),))
        last = cur.fetchone()[0] or 0
        if now - last < WITHDRAW_COOLDOWN:
            c.close()
            left = int((WITHDRAW_COOLDOWN - (now - last)) / 3600) + 1
            bot.reply_to(message, f"⏳ طلب واحد كل 48 ساعة. باقي عليك {left} ساعة.")
            return
        cur.execute("UPDATE users SET balance=balance-?, wallet=? WHERE user_id=?", (amt, w, str(uid)))
        cur.execute("INSERT INTO withdraws (user_id, amount, wallet, created_at) VALUES (?,?,?,?)",
                    (str(uid), amt, w, now))
        wid = cur.lastrowid
        c.commit(); c.close()
    bot.reply_to(message, f"✅ تم إرسال طلب سحب {int(amt)} ZYN إلى {w}\nرقم الطلب: {wid}\nسيصلك بعد مراجعة المدير.")
    try:
        uname = (message.from_user.username or "").replace("_", "\\_")
        bot.send_message(ADMIN_ID,
                         f"💸 طلب سحب جديد #{wid}\nمن: @{uname} ({uid})\nالمبلغ: {amt} ZYN\nالمحفظة: {w}\n\n/approve {wid} أو /reject {wid}")
    except Exception:
        pass

while True:
    try:
        print("ZYRON Bot is running... UNIFIED DB")
        bot.infinity_polling(timeout=20, long_polling_timeout=20)
    except Exception as e:
        print("polling error, restarting in 5s:", e)
        time.sleep(5)
