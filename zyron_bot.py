# ZYRON Miner Bot - telebot - shared DB (zyron.db) with app.py
# Same database as the Flask app -> one balance in the bot and the app.
# All user-facing text: Modern Standard Arabic + English only.
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

# No global parse_mode: user names with _ or special chars must never break a reply.
bot = telebot.TeleBot(BOT_TOKEN)

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
    cur.execute("""CREATE TABLE IF NOT EXISTS withdraws (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, amount REAL, wallet TEXT,
        status TEXT DEFAULT 'pending', created_at INTEGER, updated_at INTEGER DEFAULT 0
    )""")
    cols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    if "last_bot_mine" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_bot_mine INTEGER DEFAULT 0")
    wcols = [r[1] for r in cur.execute("PRAGMA table_info(withdraws)").fetchall()]
    if "updated_at" not in wcols:
        cur.execute("ALTER TABLE withdraws ADD COLUMN updated_at INTEGER DEFAULT 0")
        cur.execute("UPDATE withdraws SET updated_at=created_at WHERE updated_at=0")
    c.commit(); c.close()

init_db()

# ---------- Level system (same table as app.py) ----------
def level_requirement(l):
    l = int(l)
    if l <= 1:   return 500.0
    if l <= 10:  return 500.0    * 10 ** ((l - 1) / 9)
    if l <= 50:  return 5000.0   * 10 ** ((l - 10) / 40)
    if l <= 100: return 50000.0  * 4  ** ((l - 50) / 50)
    return 200000.0 * 25 ** ((l - 100) / 400)

_REQS = [0.0] + [level_requirement(l) for l in range(1, 501)]

def level_from(h):
    h = float(h or 0)
    if h < _REQS[1]:
        return 1
    lo, hi = 1, 500
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _REQS[mid] <= h:
            lo = mid
        else:
            hi = mid - 1
    return lo

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
        is_new = False
        if not r:
            is_new = True
            cur.execute("INSERT INTO users (user_id, username, joined_at, last_tick) VALUES (?,?,?,?)",
                        (uid, username, str(int(time.time())), now))
            c.commit()
            cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
            r = cur.fetchone()
        if r["mining"]:
            last = r["last_tick"] or now
            elapsed = max(0, now - last) / 1000
            lvl = level_from(r["onchain"] or 0)
            add = elapsed * reward_for(lvl) * (r["boost"] or 1) / 86400
            if add > 0:
                cur.execute("UPDATE users SET pending=pending+?, last_tick=? WHERE user_id=?",
                            (add, now, uid))
                c.commit()
                cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
                r = cur.fetchone()
        c.close()
        d = dict(r)
        d["_is_new"] = is_new
        return d

# ---------- Safe send/edit helpers (retry so a 503 is not lost) ----------
def safe_send(chat_id, text, kb=None, tries=3):
    for _ in range(tries):
        try:
            return bot.send_message(chat_id, text, reply_markup=kb)
        except Exception as e:
            print("send fail:", e)
            time.sleep(2)
    return None

def safe_edit(text, chat_id, msg_id, kb=None):
    """Avoid 'message is not modified' (400) and never crash on edit failures."""
    try:
        bot.edit_message_text(text, chat_id, msg_id, reply_markup=kb)
        return
    except Exception as e:
        if "not modified" in str(e).lower():
            return
    try:
        bot.send_message(chat_id, text, reply_markup=kb)
    except Exception as e:
        print("safe_edit fallback fail:", e)

def main_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⛏️ فتح التعدين / Open Miner", web_app=WebAppInfo(url=WEBAPP_URL)))
    kb.add(InlineKeyboardButton("⛏️ تعدين ZYN", callback_data="mine"))
    kb.add(InlineKeyboardButton("💰 الرصيد", callback_data="balance"),
           InlineKeyboardButton("👥 الإحالات", callback_data="referral"))
    kb.add(InlineKeyboardButton("👛 ربط المحفظة", callback_data="wallet_help"),
           InlineKeyboardButton("🏆 المتصدرين", callback_data="leaderboard"))
    kb.add(InlineKeyboardButton("📋 المهام", callback_data="tasks"))
    return kb

def wd_admin_text(wid, uid, username, amt, w, status_line):
    return (f"💸 طلب سحب ZYRON #{wid}\n"
            f"معرف المستخدم: {uid}\n"
            f"اسم المستخدم: @{username or 'بدون'}\n"
            f"المبلغ: {int(amt)} ZYN\n"
            f"المحفظة: {w}\n"
            f"الحالة: {status_line}")

# ---------- /start ----------
@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    username = message.from_user.username or ""
    print(f"new user /start: {uid} {username}", flush=True)
    try:
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
                        safe_send(ref_id,
                                  f"🎉 انضم مستخدم جديد عبر رابطك! ربحت +{REFERRAL_BONUS} ZYN\n"
                                  f"A new user joined via your link! You earned +{REFERRAL_BONUS} ZYN")
            except Exception as e:
                print("referral error:", e)
        me = get_user(uid)
        total = (me["balance"] or 0) + (me["pending"] or 0)
        per_day = reward_for(level_from(me["onchain"] or 0)) * (me["boost"] or 1)
        text = ("👋 مرحباً بك في ZYRON Miner ⛏️\n"
                "Welcome to ZYRON Miner ⛏️\n\n"
                f"💰 رصيدك: {int(total)} ZYN\n"
                f"⏰ سرعة التعدين: {per_day} ZYN / يوم\n"
                f"🏆 المستوى: {level_from(me['onchain'] or 0)} / 500\n"
                f"👥 لكل صديق تدعوه: +{REFERRAL_BONUS} ZYN لك وله\n\n"
                "ابدأ التعدين الآن 👇")
        safe_send(uid, text, main_keyboard())
    except Exception as e:
        # Never stay silent: fall back to a minimal plain reply.
        print("start error:", e)
        safe_send(uid, "👋 مرحباً بك في ZYRON Miner\nWelcome to ZYRON Miner", main_keyboard())

# ---------- Callbacks ----------
@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    uid = call.from_user.id
    username = call.from_user.username or ""
    data = call.data or ""
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    # ----- Admin: withdrawal flow buttons -----
    if data.startswith("wd_accept_") or data.startswith("wd_reject_") or data.startswith("wd_done_"):
        if uid != ADMIN_ID:
            return
        action, wid = data.split("_")[1], data.split("_")[2]
        now = int(time.time())
        with _db_lock:
            c = db(); cur = c.cursor()
            cur.execute("SELECT user_id, amount, wallet, status FROM withdraws WHERE id=?", (wid,))
            r = cur.fetchone()
            if not r:
                c.close()
                safe_edit("❌ طلب غير موجود / Request not found", chat_id, msg_id)
                return
            if action == "accept" and r["status"] == "pending":
                cur.execute("UPDATE withdraws SET status='sending', updated_at=? WHERE id=?", (now, wid))
                new_status = "قيد الإرسال 📤"
            elif action == "done" and r["status"] == "sending":
                cur.execute("UPDATE withdraws SET status='done', updated_at=? WHERE id=?", (now, wid))
                new_status = "تم السحب بنجاح ✅"
            elif action == "reject" and r["status"] in ("pending", "sending"):
                # refund: the amount was deducted when the request was created
                cur.execute("UPDATE withdraws SET status='rejected', updated_at=? WHERE id=?", (now, wid))
                cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (r["amount"], r["user_id"]))
                new_status = "مرفوض ❌ (تم إرجاع الرصيد)"
            else:
                c.close()
                safe_edit(wd_admin_text(wid, r["user_id"], "", r["amount"], r["wallet"],
                                        f"تمت معالجته مسبقاً ({r['status']})"), chat_id, msg_id)
                return
            c.commit(); c.close()
        # notify the user
        if action == "accept":
            safe_send(int(r["user_id"]),
                      "📤 طلب السحب الخاص بك قيد الإرسال، يرجى الانتظار قليلاً.\n"
                      "Your withdrawal is being sent, please wait a moment.")
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("✅ تم السحب", callback_data=f"wd_done_{wid}"))
            safe_edit(wd_admin_text(wid, r["user_id"], username, r["amount"], r["wallet"], new_status),
                      chat_id, msg_id, kb)
        elif action == "done":
            safe_send(int(r["user_id"]),
                      f"✅ تم السحب بنجاح! وصل {int(r['amount'])} ZYN إلى محفظتك.\n"
                      f"Withdrawal completed! {int(r['amount'])} ZYN has arrived in your wallet.")
            safe_edit(wd_admin_text(wid, r["user_id"], username, r["amount"], r["wallet"], new_status),
                      chat_id, msg_id)
        elif action == "reject":
            safe_send(int(r["user_id"]),
                      f"❌ تم رفض طلب السحب ({int(r['amount'])} ZYN) وتم إرجاع الرصيد إليك.\n"
                      f"Your withdrawal ({int(r['amount'])} ZYN) was rejected and the balance was refunded.")
            safe_edit(wd_admin_text(wid, r["user_id"], username, r["amount"], r["wallet"], new_status),
                      chat_id, msg_id)
        return

    # ----- Regular user buttons -----
    me = get_user(uid, username)
    total = (me["balance"] or 0) + (me["pending"] or 0)

    if data == "mine":
        now = int(time.time())
        last = me.get("last_bot_mine") or 0
        if now - last < MINE_COOLDOWN:
            left = MINE_COOLDOWN - (now - last)
            h, m = left // 3600, (left % 3600) // 60
            safe_edit(f"⏳ يمكنك التعدين في البوت بعد {h} ساعة و {m} دقيقة.\n"
                      f"You can mine here again in {h}h {m}m.\n\n"
                      f"💰 رصيدك: {int(total)} ZYN\n"
                      "عدّن في التطبيق لتربح أكثر ⛏️", chat_id, msg_id)
            return
        with _db_lock:
            c = db(); cur = c.cursor()
            cur.execute("UPDATE users SET balance=balance+?, last_bot_mine=? WHERE user_id=?",
                        (MINE_REWARD, now, str(uid)))
            c.commit(); c.close()
        me = get_user(uid)
        safe_edit(f"✅ تم التعدين بنجاح!\n\n"
                  f"💰 ربحت +{MINE_REWARD} ZYN\n"
                  f"💳 رصيدك الكامل: {int((me['balance'] or 0) + (me['pending'] or 0))} ZYN\n\n"
                  "⏳ المكافأة التالية بعد 6 ساعات.\n"
                  "Next reward in 6 hours.", chat_id, msg_id)
    elif data == "balance":
        lvl = level_from(me["onchain"] or 0)
        safe_edit(f"💰 رصيدك الكامل: {int(total)} ZYN\n"
                  f"(رصيد {int(me['balance'] or 0)} + معلق {int(me['pending'] or 0)})\n"
                  f"👥 إحالاتك: {me['referrals'] or 0}\n"
                  f"👛 المحفظة: {me['wallet'] or 'غير مربوطة'}\n"
                  f"🏆 المستوى: {lvl} / 500", chat_id, msg_id)
    elif data == "referral":
        try:
            bot_username = bot.get_me().username
        except Exception:
            bot_username = "ZyronMinerBot"
        link = f"https://t.me/{bot_username}?start={uid}"
        safe_edit(f"👥 رابط الإحالة الخاص بك:\n{link}\n\n"
                  f"كل صديق ينضم عبره تربح +{REFERRAL_BONUS} ZYN ويربح هو +{REFERRAL_BONUS} ZYN.\n"
                  f"Each friend who joins earns you +{REFERRAL_BONUS} ZYN.", chat_id, msg_id)
    elif data == "wallet_help":
        safe_edit("👛 لربط محفظتك أرسل هذا الأمر:\n"
                  "/wallet UQ... عنوان محفظتك\n\n"
                  "To link your wallet, send:\n"
                  "/wallet UQ... your wallet address\n\n"
                  "يجب أن تكون محفظة TON مثل Tonkeeper.", chat_id, msg_id)
    elif data == "leaderboard":
        c = db(); cur = c.cursor()
        cur.execute("SELECT username, balance, pending FROM users ORDER BY (balance+pending) DESC LIMIT 10")
        rows = cur.fetchall(); c.close()
        txt = "🏆 المتصدرين / Top 10:\n\n"
        for i, r in enumerate(rows, 1):
            name = (r["username"] or "user")
            txt += f"{i}. @{name} - {int((r['balance'] or 0) + (r['pending'] or 0))} ZYN\n"
        if not rows:
            txt += "لا يوجد أحد بعد / No one yet."
        safe_edit(txt, chat_id, msg_id)
    elif data == "tasks":
        safe_edit("📋 المهام / Tasks:\n\n"
                  "أنجز المهام في التطبيق لتربح +500 لكل مهمة.\n"
                  "Complete tasks in the app to earn +500 each.\n\n"
                  "1️⃣ الانضمام إلى مجموعة ZYRON\n"
                  "2️⃣ متابعتنا على X / Telegram\n"
                  "3️⃣ دعوة 5 أصدقاء", chat_id, msg_id)

# ---------- Commands ----------
@bot.message_handler(commands=['wallet'])
def wallet_cmd(message):
    uid = message.from_user.id
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "أرسل هكذا / Send like this:\n/wallet UQ... عنوان المحفظة")
        return
    addr = parts[1].strip()
    if not addr.startswith(("UQ", "EQ", "0Q")) or len(addr) < 40:
        bot.reply_to(message, "❌ عنوان غير صالح. يجب أن يبدأ بـ UQ أو EQ وأن يكون عنوان TON كاملاً.\nInvalid address.")
        return
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE users SET wallet=? WHERE user_id=?", (addr, str(uid)))
        c.commit(); c.close()
    bot.reply_to(message, f"✅ تم ربط المحفظة / Wallet linked:\n{addr}")

@bot.message_handler(commands=['balance'])
def balance_cmd(message):
    me = get_user(message.from_user.id, message.from_user.username or "")
    bot.reply_to(message, f"💰 رصيدك الكامل / Total balance: {int((me['balance'] or 0) + (me['pending'] or 0))} ZYN")

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
    cur.execute("SELECT COUNT(*) FROM withdraws WHERE status IN ('pending','sending')")
    wp = cur.fetchone()[0]
    c.close()
    bot.reply_to(message, f"👥 Users: {cc}\n💰 Total points: {int(ss or 0)}\n💸 Open withdrawals: {wp}")

@bot.message_handler(commands=['approve'])
def approve_cmd(message):
    # admin fallback: /approve <id> = mark as sending
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "أرسل / Send: /approve رقم_الطلب")
        return
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE withdraws SET status='sending', updated_at=? WHERE id=? AND status='pending'",
                    (int(time.time()), parts[1]))
        n = cur.rowcount
        c.commit(); c.close()
    bot.reply_to(message, "✅ أصبح قيد الإرسال" if n else "❌ لم أجد هذا الطلب أو أنه مُعالج مسبقاً")

@bot.message_handler(commands=['reject'])
def reject_cmd(message):
    # admin fallback: /reject <id> = reject and refund
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "أرسل / Send: /reject رقم_الطلب")
        return
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("SELECT user_id, amount, status FROM withdraws WHERE id=?", (parts[1],))
        r = cur.fetchone()
        if not r:
            c.close(); bot.reply_to(message, "❌ لم أجد هذا الطلب"); return
        if r["status"] not in ("pending", "sending"):
            c.close(); bot.reply_to(message, "❌ هذا الطلب مُعالج مسبقاً"); return
        cur.execute("UPDATE withdraws SET status='rejected', updated_at=? WHERE id=?", (int(time.time()), parts[1]))
        cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (r["amount"], r["user_id"]))
        c.commit(); c.close()
    safe_send(int(r["user_id"]),
              f"❌ تم رفض طلب السحب ({int(r['amount'])} ZYN) وتم إرجاع الرصيد إليك.\n"
              f"Your withdrawal ({int(r['amount'])} ZYN) was rejected and the balance was refunded.")
    bot.reply_to(message, "✅ تم الرفض وإرجاع الرصيد")

@bot.message_handler(commands=['withdraw'])
def withdraw_cmd(message):
    parts = (message.text or "").split()
    if len(parts) < 3:
        bot.reply_to(message, "أرسل / Send: /withdraw المبلغ المحفظة")
        return
    try:
        amt = float(parts[1])
    except ValueError:
        bot.reply_to(message, "المبلغ غير صحيح / Invalid amount")
        return
    w = parts[2].strip()
    uid = message.from_user.id
    me = get_user(uid)
    if amt < MIN_WITHDRAW:
        bot.reply_to(message, f"الحد الأدنى للسحب {MIN_WITHDRAW} ZYN")
        return
    if amt > (me["balance"] or 0):
        bot.reply_to(message, "الرصيد غير كافٍ / Not enough balance")
        return
    if not w.startswith(("UQ", "EQ")) or len(w) < 40:
        bot.reply_to(message, "❌ عنوان المحفظة غير صالح / Invalid wallet")
        return
    now = int(time.time())
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("SELECT id FROM withdraws WHERE user_id=? AND status IN ('pending','sending')", (str(uid),))
        if cur.fetchone():
            c.close()
            bot.reply_to(message, "⏳ لديك طلب سحب قيد المعالجة.\nYou already have an open withdrawal request.")
            return
        cur.execute("SELECT MAX(updated_at) FROM withdraws WHERE user_id=? AND status IN ('done','rejected')", (str(uid),))
        last_end = cur.fetchone()[0] or 0
        if last_end and now - last_end < WITHDRAW_COOLDOWN:
            c.close()
            left = int((WITHDRAW_COOLDOWN - (now - last_end)) / 3600) + 1
            bot.reply_to(message, f"⏳ طلب واحد كل 48 ساعة بعد انتهاء آخر طلب. المتبقي {left} ساعة.")
            return
        cur.execute("UPDATE users SET balance=balance-?, wallet=? WHERE user_id=?", (amt, w, str(uid)))
        cur.execute("INSERT INTO withdraws (user_id, amount, wallet, status, created_at, updated_at) VALUES (?,?,?,'pending',?,?)",
                    (str(uid), amt, w, now, now))
        wid = cur.lastrowid
        c.commit(); c.close()
    bot.reply_to(message, f"✅ تم إرسال طلب سحب {int(amt)} ZYN\nرقم الطلب / Request ID: {wid}\nالحالة: قيد المعالجة ⏳")
    uname = message.from_user.username or ""
    safe_send(ADMIN_ID, wd_admin_text(wid, uid, uname, amt, w, "قيد المعالجة ⏳"),
              InlineKeyboardMarkup().add(
                  InlineKeyboardButton("✅ قبول - قيد الإرسال", callback_data=f"wd_accept_{wid}"),
                  InlineKeyboardButton("❌ رفض", callback_data=f"wd_reject_{wid}")))

while True:
    try:
        print("ZYRON Bot is running... UNIFIED DB", flush=True)
        # short polling timeouts so /start is answered within seconds;
        # no skip_pending: messages sent while the bot was down are still answered.
        bot.infinity_polling(timeout=10, long_polling_timeout=10)
    except Exception as e:
        print("polling error, restarting in 5s:", e, flush=True)
        time.sleep(5)
