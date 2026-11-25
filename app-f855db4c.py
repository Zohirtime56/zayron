# ZYRON Backend - Flask API - SHARED DB with bot
# Deploy on PythonAnywhere Web: WSGI -> from app import app as application
import sqlite3, os, time, json, threading
from flask import Flask, request, jsonify
from flask_cors import CORS

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "zyron.db")
TOKEN_FILE = os.path.join(BASE, "token.txt")
ADMIN_ID = 7527825632

MIN_WITHDRAW = 10000
WITHDRAW_COOLDOWN = 48 * 3600          # طلب واحد كل 48 ساعة
BOOST_COSTS = {2: 1000, 5: 5000, 10: 10000}
TASK_REWARDS = {"ch": 500, "grp": 500, "x": 500, "inv": 1000}

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
        status TEXT DEFAULT 'pending', created_at INTEGER
    )""")
    # أعمدة جديدة لقواعد بيانات قديمة
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

def _accrue(cur, r, now=None):
    """يحسب الأرباح المعلقة منذ آخر tick ويرجع القيمة الجديدة."""
    now = now or int(time.time() * 1000)
    pending = r["pending"] or 0
    if r["mining"]:
        last = r["last_tick"] or now
        elapsed = max(0, (now - last)) / 1000
        lvl = level_from(max(r["onchain"] or 0, r["balance"] or 0))
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

def notify_admin(text):
    try:
        import urllib.request, urllib.parse
        token = open(TOKEN_FILE).read().strip() if os.path.exists(TOKEN_FILE) else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token: return
        data = urllib.parse.urlencode({"chat_id": ADMIN_ID, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print("notify fail", e)

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
    # إحالة: المكافأة للمحيل وليس للمستخدم الجديد
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
    lvl = level_from(max(u["onchain"] or 0, u["balance"] or 0))
    # إحصائيات السحب
    c = db(); cur = c.cursor()
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM withdraws WHERE user_id=? AND status!='rejected'", (uid,))
    total_wd = cur.fetchone()[0] or 0
    cur.execute("SELECT amount, created_at FROM withdraws WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (uid,))
    pw = cur.fetchone()
    c.close()
    return jsonify({"ok": True, "user": {
        "balance": u["balance"], "pending": u["pending"], "mining": bool(u["mining"]),
        "boost": u["boost"], "refs": u["referrals"], "refEarn": u["ref_earn"],
        "onchain": u["onchain"], "level": lvl, "wallet": u["wallet"],
        "totalWd": total_wd,
        "pendingWithdraw": (pw["amount"] if pw else 0),
        "lastWithdrawAt": (pw["created_at"] if pw else 0),
    }})

@app.route("/api/mine", methods=["POST"])
def mine():
    d = request.json or {}
    uid = get_uid(d); act = d.get("action", "")
    if not uid:
        return jsonify({"ok": False, "error": "no uid"})
    get_user(uid)  # يجمع الأرباح المعلقة أولاً
    with _db_lock:
        c = db(); cur = c.cursor(); now = int(time.time() * 1000)
        cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        r = cur.fetchone()
        if act == "start":
            # last_tick تجدد بعد التجميع باش ما ضيعش الوقت
            cur.execute("UPDATE users SET mining=1, last_tick=? WHERE user_id=?", (now, uid))
        elif act == "stop":
            # التجميع تم في get_user قبل الإيقاف - ما كيضيع والو
            cur.execute("UPDATE users SET mining=0 WHERE user_id=?", (uid,))
        elif act == "claim":
            cur.execute("UPDATE users SET balance=balance+pending, pending=0, last_tick=? WHERE user_id=?", (now, uid))
        c.commit(); c.close()
    u = get_user(uid)
    lvl = level_from(max(u["onchain"] or 0, u["balance"] or 0))
    return jsonify({"ok": True, "mining": bool(u["mining"]), "pending": u["pending"],
                    "balance": u["balance"], "level": lvl})

@app.route("/api/withdraw", methods=["POST"])
def withdraw():
    d = request.json or {}
    uid = get_uid(d)
    try:
        amt = float(d.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid amount"})
    w = (d.get("wallet") or "").strip()
    if not uid:
        return jsonify({"ok": False, "error": "no uid"})
    u = get_user(uid)
    if amt < MIN_WITHDRAW:
        return jsonify({"ok": False, "error": f"Min {MIN_WITHDRAW}"})
    if amt > (u["balance"] or 0):
        return jsonify({"ok": False, "error": "Not enough balance"})
    if not w.startswith(("UQ", "EQ")) or len(w) < 40:
        return jsonify({"ok": False, "error": "Invalid wallet"})
    now = int(time.time())
    with _db_lock:
        c = db(); cur = c.cursor()
        # طلب واحد معلق كحد أقصى
        cur.execute("SELECT id FROM withdraws WHERE user_id=? AND status='pending'", (uid,))
        if cur.fetchone():
            c.close()
            return jsonify({"ok": False, "error": "لديك طلب سحب قيد المعالجة"})
        # 48 ساعة بين كل طلب
        cur.execute("SELECT MAX(created_at) FROM withdraws WHERE user_id=?", (uid,))
        last = cur.fetchone()[0] or 0
        if now - last < WITHDRAW_COOLDOWN:
            c.close()
            left = int((WITHDRAW_COOLDOWN - (now - last)) / 3600) + 1
            return jsonify({"ok": False, "error": f"انتظر {left} ساعة قبل طلب جديد"})
        cur.execute("UPDATE users SET balance=balance-?, wallet=? WHERE user_id=?", (amt, w, uid))
        cur.execute("INSERT INTO withdraws (user_id, amount, wallet, created_at) VALUES (?,?,?,?)",
                    (uid, amt, w, now))
        c.commit(); c.close()
    notify_admin(f"💸 طلب سحب ZYRON\nالمستخدم: {uid} (@{u['username']})\nالمبلغ: {amt} ZYN\nالمحفظة: {w}")
    u = get_user(uid)
    return jsonify({"ok": True, "balance": u["balance"]})

@app.route("/api/boost", methods=["POST"])
def boost():
    d = request.json or {}
    uid = get_uid(d)
    try:
        m = int(d.get("mult") or 1)
    except (TypeError, ValueError):
        m = 1
    # تأكد أن المضاعف مسموح - بلا هاد الفحص كان يمكن شراء أي مضاعف ببلاش
    if m not in BOOST_COSTS:
        return jsonify({"ok": False, "error": "Invalid boost"})
    cost = BOOST_COSTS[m]
    u = get_user(uid)
    if (u["balance"] or 0) < cost:
        return jsonify({"ok": False, "error": "Not enough"})
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
    d = request.json or {}
    uid = get_uid(d)
    if not uid:
        return jsonify({"ok": False, "error": "no uid"})
    try:
        bal = max(0.0, float(d.get("onchain") or 0))
    except (TypeError, ValueError):
        bal = 0.0
    w = d.get("wallet") or ""
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE users SET onchain=?, wallet=? WHERE user_id=?", (bal, w, uid))
        c.commit(); c.close()
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
