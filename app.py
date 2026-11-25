# ZYRON Backend - Flask API - shared DB (zyron.db) with the bot
# Deploy on PythonAnywhere Web: WSGI -> from app import app as application
import sqlite3, os, time, json, threading, urllib.request
from flask import Flask, request, jsonify
from flask_cors import CORS

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "zyron.db")
TOKEN_FILE = os.path.join(BASE, "token.txt")
ADMIN_ID = 7527825632

MIN_WITHDRAW = 10000
WITHDRAW_COOLDOWN = 48 * 3600          # one request per 48h, counted from the END of the previous request
BOOST_COSTS = {2: 1000, 5: 5000, 10: 10000}
TASK_REWARDS = {"ch": 500, "grp": 500, "x": 500, "inv": 1000}
ZYN_PER_TON = 10000                    # reference price: 10,000 ZYN = 1 TON

app = Flask(__name__)
CORS(app)

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
    # migrations for older databases
    cols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    if "last_bot_mine" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_bot_mine INTEGER DEFAULT 0")
    wcols = [r[1] for r in cur.execute("PRAGMA table_info(withdraws)").fetchall()]
    if "updated_at" not in wcols:
        cur.execute("ALTER TABLE withdraws ADD COLUMN updated_at INTEGER DEFAULT 0")
        cur.execute("UPDATE withdraws SET updated_at=created_at WHERE updated_at=0")
    c.commit(); c.close()

init_db()

# ---------- Level system (Dynamic ZYRON Vault) ----------
# The level is computed from the LOCKED amount (onchain ZYN in the connected wallet),
# not from the in-app balance. Anchors (at 10,000 ZYN = 1 TON):
#   level 1   = 500 ZYN       (0.05 TON)
#   level 10  = 5,000 ZYN     (0.5 TON)
#   level 50  = 50,000 ZYN    (5 TON)
#   level 100 = 200,000 ZYN   (20 TON)
#   level 500 = 5,000,000 ZYN (500 TON)
# Inside each segment the requirement grows geometrically (constant ratio per level),
# so the curve is smooth and every step is reachable.
def level_requirement(l):
    l = int(l)
    if l <= 1:   return 500.0
    if l <= 10:  return 500.0    * 10 ** ((l - 1) / 9)
    if l <= 50:  return 5000.0   * 10 ** ((l - 10) / 40)
    if l <= 100: return 50000.0  * 4  ** ((l - 50) / 50)
    return 200000.0 * 25 ** ((l - 100) / 400)

_REQS = [0.0] + [level_requirement(l) for l in range(1, 501)]

def level_from(h):
    """Level 1..500 from the locked amount. Minimum level is always 1."""
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

def _accrue(cur, r, now=None):
    """Accrue pending mining rewards since the last tick."""
    now = now or int(time.time() * 1000)
    pending = r["pending"] or 0
    if r["mining"]:
        last = r["last_tick"] or now
        elapsed = max(0, (now - last)) / 1000
        lvl = level_from(r["onchain"] or 0)
        add = elapsed * reward_for(lvl) * (r["boost"] or 1) / 86400
        if add > 0:
            pending += add
            cur.execute("UPDATE users SET pending=?, last_tick=? WHERE user_id=?",
                        (pending, now, r["user_id"]))
    return pending

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
        elif username and r["username"] != username:
            cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
        _accrue(cur, r, now)
        c.commit()
        cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        r = cur.fetchone()
        c.close()
        return dict(r)

# ---------- Telegram helpers (admin notifications with buttons) ----------
def _bot_token():
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip()
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

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

def notify_admin_withdraw(wid, uid, username, amt, w):
    text = (f"💸 طلب سحب ZYRON جديد #{wid}\n"
            f"معرف المستخدم: {uid}\n"
            f"اسم المستخدم: @{username or 'بدون'}\n"
            f"المبلغ: {int(amt)} ZYN\n"
            f"المحفظة: {w}\n"
            f"الحالة: قيد المعالجة ⏳")
    kb = {"inline_keyboard": [[
        {"text": "✅ قبول - قيد الإرسال", "callback_data": f"wd_accept_{wid}"},
        {"text": "❌ رفض", "callback_data": f"wd_reject_{wid}"}]]}
    tg_api("sendMessage", {"chat_id": ADMIN_ID, "text": text, "reply_markup": kb})

def get_uid(d):
    return str(d.get("user_id") or d.get("uid") or "").strip()

@app.route("/")
def home():
    return jsonify({"status": "ZYRON API running", "ok": True})

@app.route("/api/auth", methods=["POST"])
def auth():
    d = request.json or {}
    uid = get_uid(d)
    if not uid:
        return jsonify({"ok": False, "error": "no uid"})
    u = get_user(uid, d.get("username", ""))
    sp = d.get("start_param", "") or ""
    # referral: the bonus goes to the referrer, not the new user
    if sp and not u["referred_by"]:
        ref = str(sp).replace("ref_", "").replace("ref", "")
        if ref and ref != uid:
            with _db_lock:
                c = db(); cur = c.cursor()
                cur.execute("SELECT user_id FROM users WHERE user_id=?", (ref,))
                if cur.fetchone():
                    cur.execute("UPDATE users SET referred_by=? WHERE user_id=?", (ref, uid))
                    cur.execute("UPDATE users SET balance=balance+50, referrals=referrals+1, ref_earn=ref_earn+50 WHERE user_id=?", (ref,))
                    cur.execute("UPDATE users SET balance=balance+50 WHERE user_id=?", (uid,))
                    c.commit()
                c.close()
            u = get_user(uid)
    lvl = level_from(u["onchain"] or 0)
    c = db(); cur = c.cursor()
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM withdraws WHERE user_id=? AND status IN ('pending','sending','done')", (uid,))
    total_wd = cur.fetchone()[0] or 0
    cur.execute("SELECT amount, status, created_at FROM withdraws WHERE user_id=? AND status IN ('pending','sending') ORDER BY id DESC LIMIT 1", (uid,))
    pw = cur.fetchone()
    c.close()
    return jsonify({"ok": True, "user": {
        "balance": u["balance"], "pending": u["pending"], "mining": bool(u["mining"]),
        "boost": u["boost"], "refs": u["referrals"], "refEarn": u["ref_earn"],
        "onchain": u["onchain"], "level": lvl, "wallet": u["wallet"],
        "totalWd": total_wd,
        "pendingWithdraw": (pw["amount"] if pw else 0),
        "pendingWithdrawStatus": (pw["status"] if pw else ""),
        "lastWithdrawAt": (pw["created_at"] if pw else 0),
    }})

@app.route("/api/mine", methods=["POST"])
def mine():
    d = request.json or {}
    uid = get_uid(d); act = d.get("action", "")
    if not uid:
        return jsonify({"ok": False, "error": "no uid"})
    get_user(uid)  # accrues pending rewards first
    with _db_lock:
        c = db(); cur = c.cursor(); now = int(time.time() * 1000)
        if act == "start":
            cur.execute("UPDATE users SET mining=1, last_tick=? WHERE user_id=?", (now, uid))
        elif act == "stop":
            cur.execute("UPDATE users SET mining=0 WHERE user_id=?", (uid,))
        elif act == "claim":
            cur.execute("UPDATE users SET balance=balance+pending, pending=0, last_tick=? WHERE user_id=?", (now, uid))
        c.commit(); c.close()
    u = get_user(uid)
    return jsonify({"ok": True, "mining": bool(u["mining"]), "pending": u["pending"],
                    "balance": u["balance"], "level": level_from(u["onchain"] or 0)})

@app.route("/api/withdraw", methods=["POST"])
def withdraw():
    d = request.json or {}
    uid = get_uid(d)
    try:
        amt = float(d.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "مبلغ غير صالح"})
    w = (d.get("wallet") or "").strip()
    if not uid:
        return jsonify({"ok": False, "error": "no uid"})
    u = get_user(uid)
    if amt < MIN_WITHDRAW:
        return jsonify({"ok": False, "error": f"الحد الأدنى للسحب {MIN_WITHDRAW} ZYN"})
    if amt > (u["balance"] or 0):
        return jsonify({"ok": False, "error": "الرصيد غير كافٍ"})
    if not w.startswith(("UQ", "EQ")) or len(w) < 40:
        return jsonify({"ok": False, "error": "عنوان المحفظة غير صالح"})
    now = int(time.time())
    with _db_lock:
        c = db(); cur = c.cursor()
        # one open request maximum: block on pending AND sending
        cur.execute("SELECT id FROM withdraws WHERE user_id=? AND status IN ('pending','sending')", (uid,))
        if cur.fetchone():
            c.close()
            return jsonify({"ok": False, "error": "لديك طلب سحب قيد المعالجة"})
        # 48h counted from the END (done / rejected) of the last request
        cur.execute("SELECT MAX(updated_at) FROM withdraws WHERE user_id=? AND status IN ('done','rejected')", (uid,))
        last_end = cur.fetchone()[0] or 0
        if last_end and now - last_end < WITHDRAW_COOLDOWN:
            c.close()
            left = int((WITHDRAW_COOLDOWN - (now - last_end)) / 3600) + 1
            return jsonify({"ok": False, "error": f"طلب واحد كل 48 ساعة بعد انتهاء آخر طلب. المتبقي {left} ساعة"})
        cur.execute("UPDATE users SET balance=balance-?, wallet=? WHERE user_id=?", (amt, w, uid))
        cur.execute("INSERT INTO withdraws (user_id, amount, wallet, status, created_at, updated_at) VALUES (?,?,?,'pending',?,?)",
                    (uid, amt, w, now, now))
        wid = cur.lastrowid
        c.commit(); c.close()
    notify_admin_withdraw(wid, uid, u["username"], amt, w)
    u = get_user(uid)
    return jsonify({"ok": True, "balance": u["balance"], "withdraw_id": wid})

@app.route("/api/withdrawals", methods=["POST"])
def withdrawals():
    """Withdrawal history: last 10 requests for the user."""
    d = request.json or {}
    uid = get_uid(d)
    if not uid:
        return jsonify({"ok": False, "error": "no uid"})
    c = db(); cur = c.cursor()
    cur.execute("""SELECT id, amount, wallet, status, created_at, updated_at
                   FROM withdraws WHERE user_id=? ORDER BY id DESC LIMIT 10""", (uid,))
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return jsonify({"ok": True, "withdrawals": rows})

@app.route("/api/boost", methods=["POST"])
def boost():
    d = request.json or {}
    uid = get_uid(d)
    try:
        m = int(d.get("mult") or 1)
    except (TypeError, ValueError):
        m = 1
    if m not in BOOST_COSTS:
        return jsonify({"ok": False, "error": "Invalid boost"})
    cost = BOOST_COSTS[m]
    u = get_user(uid)
    if (u["balance"] or 0) < cost:
        return jsonify({"ok": False, "error": "الرصيد غير كافٍ"})
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE users SET balance=balance-?, boost=? WHERE user_id=?", (cost, m, uid))
        c.commit(); c.close()
    return jsonify({"ok": True, "boost": m})

@app.route("/api/task", methods=["POST"])
def task():
    d = request.json or {}
    uid = get_uid(d); k = d.get("task") or ""
    if k not in TASK_REWARDS:
        return jsonify({"ok": False, "error": "Unknown task"})
    u = get_user(uid)
    try:
        tasks = json.loads(u["tasks"] or "{}")
    except (TypeError, ValueError):
        tasks = {}
    if tasks.get(k):
        return jsonify({"ok": False, "error": "Already done"})
    reward = TASK_REWARDS[k]
    tasks[k] = 1
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE users SET balance=balance+?, tasks=? WHERE user_id=?",
                    (reward, json.dumps(tasks), uid))
        c.commit(); c.close()
    return jsonify({"ok": True, "reward": reward})

@app.route("/api/update_onchain", methods=["POST"])
def update_onchain():
    """Update the locked (onchain) amount. Also used on wallet disconnect with onchain=0, wallet=''."""
    d = request.json or {}
    uid = get_uid(d)
    if not uid:
        return jsonify({"ok": False, "error": "no uid"})
    try:
        bal = max(0.0, float(d.get("onchain") or 0))
    except (TypeError, ValueError):
        bal = 0.0
    w = d.get("wallet") or ""
    if not w:
        bal = 0.0  # disconnect: no wallet means nothing locked
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE users SET onchain=?, wallet=? WHERE user_id=?", (bal, w, uid))
        c.commit(); c.close()
    return jsonify({"ok": True, "level": level_from(bal)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
