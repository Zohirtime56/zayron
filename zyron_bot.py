# ZYRON Miner Bot - telebot - shared DB (zyron.db) with app.py
# Same database as the Flask app -> one balance in the bot and the app.
# All user-facing text: Modern Standard Arabic + English only.
import time, os, json, threading, telebot
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

import zyron_core as core
from zyron_core import db, get_user, level_from, reward_for, _db_lock

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
                # only record the referrer. The +50 bonus is paid by core after the new user's first real activity.
                core.set_referrer(uid, parts[1])
            except Exception as e:
                print("referral error:", e)
        me = get_user(uid)
        total = (me["balance"] or 0) + (me["pending"] or 0)
        per_day = reward_for(level_from(me["locked"] or 0)) * (me["boost"] or 1)
        text = ("👋 مرحباً بك في ZYRON Miner ⛏️\n"
                "Welcome to ZYRON Miner ⛏️\n\n"
                f"💰 رصيدك: {int(total)} ZYN\n"
                f"⏰ سرعة التعدين: {per_day} ZYN / يوم\n"
                f"🏆 المستوى: {level_from(me['locked'] or 0)} / 500\n"
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
        action, wid = data.split("_")[1], int(data.split("_")[2])
        # the username shown is ALWAYS the requester's (from the DB), never the admin who pressed the button
        if action == "reject":
            res = core.reject_withdraw(wid, "admin_reject")
            if not res["ok"]:
                if res.get("error") == "not_found":
                    safe_edit("❌ طلب غير موجود / Request not found", chat_id, msg_id)
                else:
                    safe_edit(wd_admin_text(wid, res["user_id"], res["username"], res["amount"], res["wallet"],
                                            f"تمت معالجته مسبقاً ({res['status']})"), chat_id, msg_id)
                return
            if res["refunded"] > 0:
                new_status = "مرفوض ❌ (تم إرجاع الرصيد)"
                user_msg = (f"❌ تم رفض طلب السحب ({int(res['amount'])} ZYN) وتم إرجاع الرصيد إليك.\n"
                            f"Your withdrawal ({int(res['amount'])} ZYN) was rejected and the balance was refunded.")
            else:
                new_status = "مرفوض ❌ (لم يُخصم أي رصيد، لا إرجاع)"
                user_msg = (f"❌ تم رفض طلب السحب ({int(res['amount'])} ZYN).\n"
                            f"Your withdrawal ({int(res['amount'])} ZYN) was rejected.")
            safe_send(int(res["user_id"]), user_msg)
            safe_edit(wd_admin_text(wid, res["user_id"], res["username"], res["amount"], res["wallet"], new_status),
                      chat_id, msg_id)
            return
        want = {"accept": ("sending", "pending"), "done": ("done", "sending")}[action]
        r, ok = core.set_withdraw_status(wid, want[0], want[1])
        if not r:
            safe_edit("❌ طلب غير موجود / Request not found", chat_id, msg_id)
            return
        if not ok:
            safe_edit(wd_admin_text(wid, r["user_id"], r["username"], r["amount"], r["wallet"],
                                    f"تمت معالجته مسبقاً ({r['status']})"), chat_id, msg_id)
            return
        if action == "accept":
            safe_send(int(r["user_id"]),
                      "📤 طلب السحب الخاص بك قيد الإرسال، يرجى الانتظار قليلاً.\n"
                      "Your withdrawal is being sent, please wait a moment.")
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("✅ تم السحب", callback_data=f"wd_done_{wid}"))
            safe_edit(wd_admin_text(wid, r["user_id"], r["username"], r["amount"], r["wallet"], "قيد الإرسال 📤"),
                      chat_id, msg_id, kb)
        else:
            safe_send(int(r["user_id"]),
                      f"✅ تم السحب بنجاح! وصل {int(r['amount'])} ZYN إلى محفظتك.\n"
                      f"Withdrawal completed! {int(r['amount'])} ZYN has arrived in your wallet.")
            safe_edit(wd_admin_text(wid, r["user_id"], r["username"], r["amount"], r["wallet"], "تم السحب بنجاح ✅"),
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
        lvl = level_from(me["locked"] or 0)
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
                  f"كل صديق ينضم عبره ويقوم بأول تعدين (claim) تربح +{REFERRAL_BONUS} ZYN ويربح هو +{REFERRAL_BONUS} ZYN.\n"
                  f"Each friend earns you +{REFERRAL_BONUS} ZYN after their first mining claim.", chat_id, msg_id)
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
    get_user(uid, message.from_user.username or "")
    ok, res = core.link_wallet(uid, addr)
    if not ok:
        bot.reply_to(message, f"❌ {res}")
        return
    me = get_user(uid)
    bot.reply_to(message, f"✅ تم ربط المحفظة / Wallet linked:\n{addr}\n"
                          f"رصيد محفظتك / Wallet balance: {me['onchain']:,.2f} ZYN\n"
                          f"المقفل / Locked: {me['locked'] or 0:,.2f} ZYN | المستوى / Level: {level_from(me['locked'] or 0)}\n"
                          "المستوى يأتي من القفل فقط (Lock) في التطبيق.\nLevel comes only from locking ZYN in the app.")

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
    try:
        wid = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ رقم غير صحيح"); return
    res = core.reject_withdraw(wid, "admin_reject_cmd")
    if not res["ok"]:
        bot.reply_to(message, "❌ لم أجد هذا الطلب" if res.get("error") == "not_found" else "❌ هذا الطلب مُعالج مسبقاً")
        return
    if res["refunded"] > 0:
        safe_send(int(res["user_id"]),
                  f"❌ تم رفض طلب السحب ({int(res['amount'])} ZYN) وتم إرجاع الرصيد إليك.\n"
                  f"Your withdrawal ({int(res['amount'])} ZYN) was rejected and the balance was refunded.")
        bot.reply_to(message, f"✅ تم الرفض وإرجاع {int(res['refunded'])} ZYN إلى @{res['username'] or res['user_id']}")
    else:
        safe_send(int(res["user_id"]), f"❌ تم رفض طلب السحب ({int(res['amount'])} ZYN).")
        bot.reply_to(message, "✅ تم الرفض. لم يكن هناك رصيد مخصوم فلم يُرجع شيء.")

@bot.message_handler(commands=['unlocks'])
def unlocks_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return
    rows = core.pending_unlocks()
    if not rows:
        bot.reply_to(message, "لا توجد طلبات فك / no pending unlocks"); return
    txt = "طلبات فك القفل المعلقة / Pending unlocks:\n\n"
    for r in rows:
        txt += (f"#{r['id']} | user {r['user_id']} | قفل {r['amount']:,.2f} | ارسل 70%: {r['payout']:,.2f} | "
                f"احرق 30%: {r['burn']:,.2f}\n{r['wallet']}\n\n")
    bot.reply_to(message, txt[:4000])

@bot.message_handler(commands=['unlock_done'])
def unlock_done_cmd(message):
    # ADMIN ONLY. Run it AFTER you sent the 70% and burned the 30% by hand. It only updates the database.
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    try:
        rid = int(parts[1])
    except (IndexError, ValueError):
        bot.reply_to(message, "أرسل / Send: /unlock_done رقم_الطلب"); return
    res = core.complete_unlock(rid)
    if not res["ok"]:
        bot.reply_to(message, f"❌ {res.get('error')} {res.get('status') or ''}"); return
    bot.reply_to(message, f"✅ تم. المقفل تنقص {res['amount']:,.2f}. المتبقي {res['locked_left']:,.2f} | المستوى الجديد {res['level']}")
    safe_send(int(res["user_id"]),
              f"🔓 تم فك القفل. أرسلنا لك {res['payout']:,.2f} ZYN (70%) وتم حرق {res['burn']:,.2f} ZYN (30%).\n"
              f"المقفل الآن: {res['locked_left']:,.2f} ZYN | المستوى: {res['level']}\n"
              f"Unlock done: {res['payout']:,.2f} ZYN sent to you, {res['burn']:,.2f} ZYN burned.")

@bot.message_handler(commands=['unlock_reject'])
def unlock_reject_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    try:
        rid = int(parts[1])
    except (IndexError, ValueError):
        bot.reply_to(message, "أرسل / Send: /unlock_reject رقم_الطلب"); return
    ok, r = core.reject_unlock(rid)
    bot.reply_to(message, "✅ تم الرفض، المقفل لم يتغير" if ok else "❌ لم أجد طلبا معلقا بهذا الرقم")
    if ok and r:
        safe_send(int(r["user_id"]), "❌ تم رفض طلب فك القفل. المقفل لم يتغير.\nYour unlock request was rejected. Locked amount unchanged.")

@bot.message_handler(commands=['check_user'])
def check_user_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "أرسل / Send: /check_user رقم_المستخدم")
        return
    rep = core.check_user_report(parts[1])
    bot.reply_to(message, rep or "❌ مستخدم غير موجود / User not found")

@bot.message_handler(commands=['rejections'])
def rejections_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return
    c = db(); cur = c.cursor()
    cur.execute("SELECT ts,user_id,username,amount,balance,reason FROM withdraw_rejections ORDER BY id DESC LIMIT 15")
    rows = cur.fetchall(); c.close()
    txt = "سجل الرفض / Rejected log:\n\n"
    for r in rows:
        txt += (f"{time.strftime('%m-%d %H:%M', time.gmtime(r['ts']))} | {r['user_id']} @{r['username'] or '-'} | "
                f"{int(r['amount'] or 0)} (bal {int(r['balance'] or 0)}) | {r['reason']}\n")
    bot.reply_to(message, txt[:4000] if rows else "لا يوجد / none")

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
    get_user(uid, message.from_user.username or "")
    ok, err, wid, _ = core.request_withdraw(uid, parts[1], w)
    if not ok:
        bot.reply_to(message, f"❌ {err}")
        return
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

