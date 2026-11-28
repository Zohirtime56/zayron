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
REFERRAL_BONUS = 50

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    print("ERROR: token.txt missing")
    raise SystemExit(1)

# No global parse_mode: user names with _ or special chars must never break a reply.
bot = telebot.TeleBot(BOT_TOKEN)

import zyron_core as core
from zyron_core import db, get_user, _db_lock

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
    kb.add(InlineKeyboardButton("💰 الرصيد", callback_data="balance"),
           InlineKeyboardButton("👥 الإحالات", callback_data="referral"))
    kb.add(InlineKeyboardButton("👛 ربط المحفظة", callback_data="wallet_help"),
           InlineKeyboardButton("🏆 المتصدرين", callback_data="leaderboard"))
    kb.add(InlineKeyboardButton("📋 المهام", callback_data="tasks"))
    return kb

def wd_admin_text(wid, uid, username, amt, w, status_line, fee=None, net=None):
    return (f"💸 طلب سحب ZYRON #{wid}\n"
            f"معرف المستخدم: {uid}\n"
            f"اسم المستخدم: @{username or 'بدون'}\n"
            f"المبلغ المخصوم: {amt:,.2f} ZYN | الرسوم: {(fee or 0):,.2f}\n"
            f"المبلغ الذي يجب إرساله: {(net if net is not None else amt):,.2f} ZYN\n"
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
        ue = core.user_economy(me)
        per_day = ue["perDay"]
        text = ("👋 مرحباً بك في ZYRON Miner ⛏️\n"
                "Welcome to ZYRON Miner ⛏️\n\n"
                f"💰 رصيدك: {int(total)} ZYN\n"
                f"⏰ سرعة التعدين: {per_day:,.2f} ZYN / يوم\n"
                f"🏆 المستوى: {ue['level']} / 500\n"
                f"👥 لكل صديق يقفل المستوى 1: +{REFERRAL_BONUS} ZYN لك وله\n\n"
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
            safe_edit(wd_admin_text(wid, r["user_id"], r["username"], r["amount"], r["wallet"], "قيد الإرسال 📤", r.get("fee"), r.get("net")),
                      chat_id, msg_id, kb)
        else:
            safe_send(int(r["user_id"]),
                      f"✅ تم السحب بنجاح! وصل {(r.get('net') if r.get('net') is not None else r['amount']):,.2f} ZYN إلى محفظتك.\n"
                      f"Withdrawal completed! {(r.get('net') if r.get('net') is not None else r['amount']):,.2f} ZYN has arrived in your wallet.")
            safe_edit(wd_admin_text(wid, r["user_id"], r["username"], r["amount"], r["wallet"], "تم السحب بنجاح ✅", r.get("fee"), r.get("net")),
                      chat_id, msg_id)
        return

    # ----- Regular user buttons -----
    me = get_user(uid, username)
    total = (me["balance"] or 0) + (me["pending"] or 0)

    if data == "balance":
        lvl = core.user_economy(me)["level"]
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
                  f"كل صديق ينضم عبره ويقفل المستوى 1 تربح +{REFERRAL_BONUS} ZYN ويربح هو +{REFERRAL_BONUS} ZYN (حد أقصى {core.REFERRAL_DAILY_CAP} إحالة في اليوم).\n"
                  f"Each friend who locks level 1 earns you +{REFERRAL_BONUS} ZYN (max {core.REFERRAL_DAILY_CAP} per day).", chat_id, msg_id)
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
                  "1️⃣ الانضمام إلى قناة ZYRON: https://t.me/zayronb\n"
                  "2️⃣ دعوة 5 أصدقاء (يقفلون المستوى 1)", chat_id, msg_id)

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
                          f"المقفل / Locked: {me['locked'] or 0:,.2f} ZYN | المستوى / Level: {core.user_economy(me)['level']}\n"
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
    cur.execute("SELECT COUNT(*) FROM withdraws WHERE status='queued'")
    wq = cur.fetchone()[0]
    c.close()
    bot.reply_to(message, f"👥 Users: {cc}\n💰 Total points: {int(ss or 0)}\n💸 Open withdrawals: {wp} | In budget queue: {wq}")

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
                f"احرق 30%: {r['burn']:,.2f} | ممكن بعد {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(r.get('unlock_at') or 0))}\n{r['wallet']}\n\n")
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
        if res.get("error") == "too_early":
            bot.reply_to(message, "⏳ مبكر: لا يمكن إنهاء الفك قبل " + time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(res.get("unlock_at") or 0)))
        else:
            bot.reply_to(message, f"❌ {res.get('error')} {res.get('status') or ''}")
        return
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
    # /withdraw AMOUNT  -> paid to your LINKED wallet only (a wallet typed here is only compared with the linked one)
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, "أرسل / Send: /withdraw المبلغ")
        return
    uid = message.from_user.id
    get_user(uid, message.from_user.username or "")
    ok, err, wid, _ = core.request_withdraw(uid, parts[1], parts[2].strip() if len(parts) > 2 else None)
    if not ok:
        bot.reply_to(message, f"❌ {err}")
        return
    c = db(); r = c.execute("SELECT amount, wallet, fee, net, status FROM withdraws WHERE id=?", (wid,)).fetchone(); c.close()
    if r["status"] == "queued":
        pos = core.queue_position(wid)
        bot.reply_to(message, f"⏳ الميزانية اليومية للسحب ممتلئة. طلبك ({r['amount']:,.2f} ZYN) دخل الطابور، موقعك {pos}. "
                              f"ستتم معالجته بالترتيب عند توفر الميزانية وستصلك رسالة.\n"
                              f"The daily budget is full. Your request is queued (position {pos}). It is processed in order, you will get a message.\nرقم الطلب / Request ID: {wid}")
        return
    bot.reply_to(message, f"✅ تم إرسال طلب سحب {r['amount']:,.2f} ZYN (الرسوم {r['fee']:,.2f}، ستستلم {r['net']:,.2f})\nرقم الطلب / Request ID: {wid}\nالحالة: قيد المعالجة ⏳")
    uname = message.from_user.username or ""
    safe_send(ADMIN_ID, wd_admin_text(wid, uid, uname, r["amount"], r["wallet"], "قيد المعالجة ⏳", r["fee"], r["net"]),
              InlineKeyboardMarkup().add(
                  InlineKeyboardButton("✅ قبول - قيد الإرسال", callback_data=f"wd_accept_{wid}"),
                  InlineKeyboardButton("❌ رفض", callback_data=f"wd_reject_{wid}")))

# ---------- Budget queue: promote what fits, tell the admin (buttons) and the user ----------
def _announce_promotions():
    for r in core.unnotified_promotions():
        if not core.mark_notified(r["id"]):
            continue
        safe_send(ADMIN_ID, wd_admin_text(r["id"], r["user_id"], r["username"], r["amount"], r["wallet"], "خرج من الطابور، قيد المعالجة ⏳", r.get("fee"), r.get("net")),
                  InlineKeyboardMarkup().add(
                      InlineKeyboardButton("✅ قبول - قيد الإرسال", callback_data=f"wd_accept_{r['id']}"),
                      InlineKeyboardButton("❌ رفض", callback_data=f"wd_reject_{r['id']}")))
        try:
            safe_send(int(r["user_id"]), f"✅ طلب السحب الخاص بك ({r['amount']:,.2f} ZYN) خرج من الطابور وقيد المعالجة.\n"
                                         f"Your withdrawal left the queue and is being processed.")
        except Exception:
            pass

def _queue_worker():
    while True:
        try:
            core.promote_queue()
            _announce_promotions()
        except Exception as e:
            print("queue worker error:", e, flush=True)
        time.sleep(60)

@bot.message_handler(commands=['wqueue'])
def wqueue_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return
    rows = core.queued_list()
    if not rows:
        bot.reply_to(message, "الطابور فارغ / queue empty"); return
    bot.reply_to(message, "طابور السحب (الأول بالأول) / withdrawal queue:\n" +
                 "\n".join(f"{i}. #{r['id']} @{r['username'] or r['user_id']} - net {r['net']:,.2f} ZYN" for i, r in enumerate(rows, 1)) +
                 "\n(/reject رقم_الطلب كيرجع الرصيد)")

# ---------- Admin: economy settings (price and pool are typed by hand, never read from a DEX) ----------
def _admin_set(message, key, usage):
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        bot.reply_to(message, usage); return
    ok, msg = core.set_setting(key, parts[1], force=(len(parts) > 2 and parts[2].lower() == "force"))
    bot.reply_to(message, ("✅ " if ok else "❌ ") + msg)

@bot.message_handler(commands=['setprice'])
def setprice_cmd(message):
    _admin_set(message, "price", "أرسل / Send: /setprice 0.000000088   (USD لكل 1 ZYN)")

@bot.message_handler(commands=['setpool'])
def setpool_cmd(message):
    _admin_set(message, "pool_zyn", "أرسل / Send: /setpool 15000000   (ZYN في الـ pool)")

@bot.message_handler(commands=['setminusd'])
def setminusd_cmd(message):
    _admin_set(message, "min_usd", "أرسل / Send: /setminusd 0.001   (أقل سحب بالدولار)")

@bot.message_handler(commands=['economy'])
def economy_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return
    e = core.economy_public()
    c = db(); budget, used, left = core.withdraw_budget(c, int(time.time()), e["pool"]); c.close()
    bot.reply_to(message, f"price: {e['price']:g} USD/ZYN\npool: {e['pool']:,.0f} ZYN\nmin withdraw: ${e['minUsd']:g}"
                          f" ({(e['minWithdrawZyn'] or 0):,.2f} ZYN)\nL1 needs: {(core.level_requirement(1) or 0):,.2f} ZYN\n"
                          f"daily budget: {budget:,.2f} | used 24h: {used:,.2f} | left: {left:,.2f} ZYN\n"
                          f"queued: {len(core.queued_list())}")

threading.Thread(target=_queue_worker, daemon=True).start()
print("ZYN_JETTON_MASTER in use:", core.JETTON_MASTER, "(check it is the REAL ZYN master)", flush=True)

while True:
    try:
        print("ZYRON Bot is running... UNIFIED DB", flush=True)
        # short polling timeouts so /start is answered within seconds;
        # no skip_pending: messages sent while the bot was down are still answered.
        bot.infinity_polling(timeout=10, long_polling_timeout=10)
    except Exception as e:
        print("polling error, restarting in 5s:", e, flush=True)
        time.sleep(5)

