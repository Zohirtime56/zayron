# ZYRON Backend - Flask API - shared DB (zyron.db) with the bot
# Deploy on PythonAnywhere Web: WSGI -> from app import app as application
# Needs zyron_core.py in the same folder. Env vars: TONAPI_KEY, TELEGRAM_BOT_TOKEN (or token.txt)
import os, time, json, urllib.request
from flask import Flask, request, jsonify
from flask_cors import CORS
import zyron_core as core
from zyron_core import db, get_user, level_from, _db_lock, TASK_REWARDS

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE, "token.txt")
ADMIN_ID = 7527825632

app = Flask(__name__)
CORS(app)

# ---------- Telegram helpers (admin notifications with buttons) ----------
def _bot_token():
    return core.bot_token()

def tg_api(method, payload, retries=3):
    """Call the Bot API with simple retry so a transient 503 is not lost."""
    token = _bot_token()
    if not token:
        return None
    data = json.dumps(payload).encode()
    for _ in range(retries):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/{method}",
                data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            print("tg_api fail", method, e)
            time.sleep(2)
    return None

def notify_admin_withdraw(wid, uid, username, amt, w, fee=None, net=None):
    text = (f"💸 طلب سحب ZYRON جديد #{wid}\n"
            f"معرف المستخدم: {uid}\n"
            f"اسم المستخدم: @{username or 'بدون'}\n"
            f"المبلغ المخصوم: {amt:,.2f} ZYN\n"
            f"الرسوم: {(fee or 0):,.2f} ZYN\n"
            f"المبلغ الذي يجب إرساله: {(net if net is not None else amt):,.2f} ZYN\n"
            f"المحفظة (المربوطة): {w}\n"
            f"الحالة: قيد المعالجة ⏳")
    kb = {"inline_keyboard": [[
        {"text": "✅ قبول - قيد الإرسال", "callback_data": f"wd_accept_{wid}"},
        {"text": "❌ رفض", "callback_data": f"wd_reject_{wid}"}]]}
    tg_api("sendMessage", {"chat_id": ADMIN_ID, "text": text, "reply_markup": kb})

def identity(d):
    """Identity ONLY from Telegram initData (HMAC verified). user_id/uid in the request body are ignored."""
    return core.verify_init_data((d or {}).get("init_data", ""))

def deny():
    return jsonify({"ok": False, "error": "unauthorized"}), 401

def notify_referrer(ref):
    tg_api("sendMessage", {"chat_id": ref, "text": f"🎉 صديقك قفل المستوى 1! ربحت +{core.REFERRAL_BONUS} ZYN\nYour friend locked level 1! +{core.REFERRAL_BONUS} ZYN"})

@app.route("/")
def home():
    return jsonify({"status": "ZYRON API running", "ok": True})

@app.route("/api/auth", methods=["POST"])
def auth():
    d = request.json or {}
    uid, uname = identity(d)
    if not uid:
        return deny()
    u = get_user(uid, uname)
    sp = d.get("start_param", "") or ""
    # referral: only RECORD the referrer here. The bonus is paid later, after the new user's first real activity.
    if sp and not u["referred_by"]:
        core.set_referrer(uid, sp)
        u = get_user(uid)
    # a friend who invited this user is paid after this user LOCKED level 1 (also retried here if the daily cap blocked it before)
    ref = core.pay_referral_if_active(uid)
    if ref:
        notify_referrer(ref)
        u = get_user(uid)
    ue = core.user_economy(u)
    lvl = ue["level"]
    c = db(); cur = c.cursor()
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM withdraws WHERE user_id=? AND status IN ('pending','sending','done')", (uid,))
    total_wd = cur.fetchone()[0] or 0
    cur.execute("SELECT amount, status, created_at FROM withdraws WHERE user_id=? AND status IN ('pending','sending','queued') ORDER BY id DESC LIMIT 1", (uid,))
    pw = cur.fetchone()
    cur.execute("SELECT id, amount, payout, burn, created_at, unlock_at FROM unlock_requests WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (uid,))
    ur = cur.fetchone()
    c.close()
    li = core.lock_info(uid)
    return jsonify({"ok": True, "economy": core.economy_public(), "user": {
        "balance": u["balance"], "pending": u["pending"], "mining": bool(u["mining"]),
        "boost": u["boost"], "boostLevel": ue["boostLevel"], "boostCost": ue["boostCost"], "boostBonusPerDay": ue["boostBonusPerDay"],
        "effLocked": ue["effLocked"], "perDay": ue["perDay"], "fee": ue["fee"], "maxRequest": ue["maxRequest"],
        "budgetLeft": ue["budgetLeft"], "tasksDone": json.loads(u["tasks"] or "{}"), "refs": u["referrals"], "refEarn": u["ref_earn"],
        "onchain": u["onchain"], "locked": u["locked"] or 0, "level": lvl,
        "lockWallet": li["lock_wallet"], "lockMemo": li["memo"], "lockEnabled": li["enabled"],
        "jettonMaster": li["jetton_master"], "minLock": li["min_lock"],
        "unlockPending": (dict(ur) if ur else None), "wallet": u["wallet"],
        "totalWd": total_wd,
        "pendingWithdraw": (pw["amount"] if pw else 0),
        "pendingWithdrawStatus": (pw["status"] if pw else ""),
        "withdrawQueue": ue["queue"],
        "lastWithdrawAt": (pw["created_at"] if pw else 0),
    }})

@app.route("/api/mine", methods=["POST"])
def mine():
    d = request.json or {}
    uid, uname = identity(d); act = d.get("action", "")
    if not uid:
        return deny()
    get_user(uid)  # accrues pending rewards first
    if act == "claim":
        core.claim_mining(uid)
    else:
        with _db_lock:
            c = db(); cur = c.cursor(); now = int(time.time() * 1000)
            if act == "start":
                cur.execute("UPDATE users SET mining=1, last_tick=? WHERE user_id=?", (now, uid))
            elif act == "stop":
                cur.execute("UPDATE users SET mining=0 WHERE user_id=?", (uid,))
            c.commit(); c.close()
    u = get_user(uid)
    ue = core.user_economy(u)
    return jsonify({"ok": True, "mining": bool(u["mining"]), "pending": u["pending"],
                    "balance": u["balance"], "level": ue["level"], "perDay": ue["perDay"]})

@app.route("/api/withdraw", methods=["POST"])
def withdraw():
    d = request.json or {}
    uid, uname = identity(d)
    if not uid:
        return deny()
    # the payout wallet is ALWAYS the linked wallet. A wallet sent by the client is only compared with it, never saved.
    ok, err, wid, username = core.request_withdraw(uid, d.get("amount"), (d.get("wallet") or "").strip())
    if not ok:
        return jsonify({"ok": False, "error": err})
    c = db(); r = c.execute("SELECT amount, wallet, fee, net, status FROM withdraws WHERE id=?", (wid,)).fetchone(); c.close()
    u = get_user(uid)
    if r["status"] == "queued":
        # not rejected: waiting for the daily budget. The admin is told when it is promoted (bot), not now.
        pos = core.queue_position(wid)
        c = db(); left = core.withdraw_budget(c, int(time.time()), core.get_setting(c, "pool_zyn", 0.0))[2]; c.close()
        return jsonify({"ok": True, "queued": True, "position": pos, "budgetLeft": left, "balance": u["balance"],
                        "withdraw_id": wid, "fee": r["fee"], "net": r["net"]})
    notify_admin_withdraw(wid, uid, username, r["amount"], r["wallet"], r["fee"], r["net"])
    return jsonify({"ok": True, "queued": False, "balance": u["balance"], "withdraw_id": wid, "fee": r["fee"], "net": r["net"]})

@app.route("/api/withdrawals", methods=["POST"])
def withdrawals():
    """Withdrawal history: last 10 requests for the user."""
    d = request.json or {}
    uid, uname = identity(d)
    if not uid:
        return deny()
    c = db(); cur = c.cursor()
    cur.execute("""SELECT id, amount, wallet, status, created_at, updated_at, fee, net
                   FROM withdraws WHERE user_id=? ORDER BY id DESC LIMIT 10""", (uid,))
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return jsonify({"ok": True, "withdrawals": rows})

@app.route("/api/boost", methods=["POST"])
def boost():
    """One boost only (x1.5 at most, cut by the 1.2%/day cap). The price is computed on the server."""
    d = request.json or {}
    uid, uname = identity(d)
    if not uid:
        return deny()
    ok, res = core.buy_boost(uid)
    if not ok:
        return jsonify({"ok": False, "error": res})
    return jsonify({"ok": True, "boost": res["boost"], "cost": res["cost"]})

@app.route("/api/task", methods=["POST"])
def task():
    d = request.json or {}
    uid, uname = identity(d)
    if not uid:
        return deny()
    ok, res = core.claim_task(uid, d.get("task") or "")
    if not ok:
        return jsonify({"ok": False, "error": res})
    return jsonify({"ok": True, "reward": res})

def notify_admin_unlock(info, username):
    text = (f"🔓 طلب فك القفل #{info['id']}\n"
            f"المستخدم: {info['user_id']} @{username or 'بدون'}\n"
            f"المبلغ المقفول المطلوب فكه: {info['amount']:,.2f} ZYN\n"
            f"ارسل له يدويا (70%): {info['payout']:,.2f} ZYN\n"
            f"احرق يدويا (30%): {info['burn']:,.2f} ZYN\n"
            f"المحفظة: {info['wallet']}\n"
            f"⏳ لا يمكن إنهاؤه قبل: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(info['unlock_at']))} (7 أيام)\n\n"
            f"بعد الإرسال والحرق اكتب: /unlock_done {info['id']}\n"
            f"للرفض: /unlock_reject {info['id']}")
    tg_api("sendMessage", {"chat_id": ADMIN_ID, "text": text})

@app.route("/api/lock_verify", methods=["POST"])
def lock_verify():
    """User says 'I sent ZYN to the lock wallet'. The server reads the chain; the app sends no amount."""
    d = request.json or {}
    uid, uname = identity(d)
    if not uid:
        return deny()
    get_user(uid, uname)
    ok, res, lvl = core.claim_lock(uid)
    ref = core.pay_referral_if_active(uid) if ok else None
    if ref:
        notify_referrer(ref)
    u = get_user(uid)
    ue = core.user_economy(u)
    out = {"ok": ok, "locked": u["locked"] or 0, "level": ue["level"], "effLocked": ue["effLocked"]}
    if ok: out["credited"] = res
    else: out["error"] = res
    return jsonify(out)

@app.route("/api/unlock_request", methods=["POST"])
def unlock_request():
    d = request.json or {}
    uid, uname = identity(d)
    if not uid:
        return deny()
    get_user(uid, uname)
    ok, res = core.request_unlock(uid, d.get("amount"))
    if not ok:
        return jsonify({"ok": False, "error": res})
    notify_admin_unlock(res, uname)
    return jsonify({"ok": True, "request": res})

@app.route("/api/update_onchain", methods=["POST"])
def update_onchain():
    """The app only tells us WHICH wallet is connected (or '' on disconnect).
    `onchain` is just the wallet balance (display). The level comes ONLY from the locked amount."""
    d = request.json or {}
    uid, uname = identity(d)
    if not uid:
        return deny()
    get_user(uid)
    ok, res = core.link_wallet(uid, d.get("wallet") or "")
    if not ok:
        return jsonify({"ok": False, "error": res})
    u = get_user(uid)
    ue = core.user_economy(u)
    return jsonify({"ok": True, "level": ue["level"], "onchain": u["onchain"], "locked": u["locked"] or 0, "effLocked": ue["effLocked"]})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

