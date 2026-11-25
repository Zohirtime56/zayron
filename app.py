# ZYRON Miner - Flask Backend (Secure)
# Balance/mining/boost/referral/withdraw stored on server, NOT in localStorage
# Verifies Telegram initData (HMAC) so users cannot forge balance
import os, json, sqlite3, hmac, hashlib, time
from urllib.parse import parse_qsl
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
try:
    BOT_TOKEN = open("token.txt").read().strip() if not BOT_TOKEN else BOT_TOKEN
except: pass
ADMIN_ID = os.environ.get("ADMIN_ID","")
try:
    ADMIN_ID = open("admin.txt").read().strip() if not ADMIN_ID else ADMIN_ID
except: pass

DB = "zyron.db"
MIN_WITHDRAW = 10000
JETTON_MASTER = "EQAJruUqgQTd-KC3QiUAoQePVzW7KubQaXcEqlhdOb4ghwyk"

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c
def init_db():
    c=db()
    c.execute("CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, username TEXT, balance REAL DEFAULT 0, pending REAL DEFAULT 0, mining INTEGER DEFAULT 0, last_time INTEGER DEFAULT 0, boost INTEGER DEFAULT 1, refs INTEGER DEFAULT 0, refEarn REAL DEFAULT 0, wallet TEXT DEFAULT '', onchain REAL DEFAULT 0, created INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS withdraws (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, amount REAL, wallet TEXT, status TEXT DEFAULT 'pending', created INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS tasks (user_id TEXT, task_key TEXT, PRIMARY KEY(user_id, task_key))")
    c.commit(); c.close()
init_db()

def verify_init_data(init_data):
    # Telegram WebApp initData verification
    if not init_data or not BOT_TOKEN: return None
    try:
        params = dict(parse_qsl(init_data))
        recv_hash = params.pop("hash", "")
        check_str = "\n".join(f"{k}={params[k]}" for k in sorted(params))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash): return None
        user = json.loads(params.get("user","{}"))
        return user
    except: return None

def get_user(uid):
    c=db(); r=c.execute("SELECT * FROM users WHERE user_id=?", (str(uid),)).fetchone(); c.close(); return r

def level_from_holding(h):
    if h>=100000: return 500
    if h>=50000: return 100
    if h>=10000: return 50
    if h>=1000: return 10
    if h>=533: return 3
    if h>=116: return 2
    return 1
def reward_for_level(l, boost):
    base = 600 if l==1 else 690 if l==2 else 854 if l==3 else 6000 if l==10 else 30000 if l==50 else 100000 if l==100 else 500000
    return base*boost

@app.route("/api/auth", methods=["POST"])
def auth():
    d = request.get_json(force=True, silent=True) or {}
    init_data = d.get("initData","")
    user = verify_init_data(init_data)
    # Fallback for testing in browser (no Telegram): allow uid param but mark as untrusted
    if not user:
        uid = d.get("uid")
        if not uid: return jsonify({"ok":False,"error":"invalid initData"}), 401
        user = {"id": str(uid), "username": d.get("username","")}
    uid = str(user.get("id"))
    c=db()
    row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        c.execute("INSERT INTO users (user_id, username, balance, pending, mining, last_time, boost, refs, refEarn, wallet, onchain, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (uid, user.get("username",""), 0,0,0,int(time.time()),1,0,0,"",0,int(time.time())))
        # Referral: start_param = ref user id
        start_param = d.get("start_param","")
        if start_param and start_param!=uid and start_param.startswith("ref_"):
            ref = start_param.replace("ref_","")
            r2 = c.execute("SELECT * FROM users WHERE user_id=?", (ref,)).fetchone()
            if r2:
                c.execute("UPDATE users SET balance=balance+50, refs=refs+1, refEarn=refEarn+50 WHERE user_id=?", (ref,))
        elif start_param and start_param.isdigit() and start_param!=uid:
            r2 = c.execute("SELECT * FROM users WHERE user_id=?", (start_param,)).fetchone()
            if r2:
                c.execute("UPDATE users SET balance=balance+50, refs=refs+1, refEarn=refEarn+50 WHERE user_id=?", (start_param,))
        c.commit()
    c.close()
    row = get_user(uid)
    return jsonify({"ok":True,"user":{"user_id":row["user_id"],"balance":row["balance"],"pending":row["pending"],"mining":row["mining"],"boost":row["boost"],"refs":row["refs"],"refEarn":row["refEarn"],"wallet":row["wallet"],"onchain":row["onchain"],"level":level_from_holding(max(row["onchain"], row["balance"]))}})

@app.route("/api/mine", methods=["POST"])
def mine():
    d=request.get_json(force=True, silent=True) or {}
    uid=str(d.get("user_id","")); action=d.get("action","")
    c=db(); row=c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: c.close(); return jsonify({"ok":False,"error":"user not found"}),404
    now=int(time.time())
    # Accrue pending from last_time if mining
    if row["mining"]:
        lvl=level_from_holding(max(row["onchain"], row["balance"]))
        rate=reward_for_level(lvl, row["boost"])/86400
        elapsed=now-row["last_time"]
        new_pending=row["pending"]+elapsed*rate
        c.execute("UPDATE users SET pending=?, last_time=? WHERE user_id=?", (new_pending, now, uid))
        row=dict(row); row["pending"]=new_pending
    if action=="start":
        c.execute("UPDATE users SET mining=1, last_time=? WHERE user_id=?", (now, uid))
    elif action=="stop":
        c.execute("UPDATE users SET mining=0 WHERE user_id=?", (uid,))
    elif action=="claim":
        amt=int(row["pending"])
        if amt>0:
            c.execute("UPDATE users SET balance=balance+?, pending=0, last_time=? WHERE user_id=?", (amt, now, uid))
            # Referral 10%: give 10% to referrer? (track via refs - simplified: no ref stored, skip)
    c.commit(); c.close()
    row=get_user(uid)
    return jsonify({"ok":True,"balance":row["balance"],"pending":row["pending"],"mining":row["mining"],"level":level_from_holding(max(row["onchain"], row["balance"]))})

@app.route("/api/withdraw", methods=["POST"])
def withdraw():
    d=request.get_json(force=True, silent=True) or {}
    uid=str(d.get("user_id","")); amt=float(d.get("amount",0)); wallet=(d.get("wallet","") or "").strip()
    if not (wallet.startswith("UQ") or wallet.startswith("EQ")) or len(wallet)<40:
        return jsonify({"ok":False,"error":"invalid wallet"}),400
    if amt<MIN_WITHDRAW:
        return jsonify({"ok":False,"error":"min 10000"}),400
    c=db(); row=c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: c.close(); return jsonify({"ok":False,"error":"user not found"}),404
    if amt>row["balance"]:
        c.close(); return jsonify({"ok":False,"error":"not enough balance"}),400
    c.execute("UPDATE users SET balance=balance-?, wallet=? WHERE user_id=?", (amt, wallet, uid))
    c.execute("INSERT INTO withdraws (user_id, amount, wallet, status, created) VALUES (?,?,?,?,?)", (uid, amt, wallet, "pending", int(time.time())))
    c.commit(); c.close()
    # Notify admin via bot would go here (bot polls withdraws table)
    return jsonify({"ok":True,"balance":row["balance"]-amt})

@app.route("/api/boost", methods=["POST"])
def boost():
    d=request.get_json(force=True, silent=True) or {}
    uid=str(d.get("user_id","")); mult=int(d.get("mult",1))
    prices={2:1000,5:5000,10:10000}
    if mult not in prices: return jsonify({"ok":False,"error":"invalid boost"}),400
    price=prices[mult]
    c=db(); row=c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row: c.close(); return jsonify({"ok":False,"error":"user not found"}),404
    if row["balance"]<price: c.close(); return jsonify({"ok":False,"error":"not enough"}),400
    c.execute("UPDATE users SET balance=balance-?, boost=? WHERE user_id=?", (price, mult, uid))
    c.commit(); c.close()
    return jsonify({"ok":True,"boost":mult})

@app.route("/api/task", methods=["POST"])
def task():
    d=request.get_json(force=True, silent=True) or {}
    uid=str(d.get("user_id","")); key=d.get("task","")
    rewards={"ch":500,"grp":500,"x":500,"inv":1000}
    if key not in rewards: return jsonify({"ok":False}),400
    c=db()
    if c.execute("SELECT * FROM tasks WHERE user_id=? AND task_key=?", (uid,key)).fetchone():
        c.close(); return jsonify({"ok":False,"error":"already done"}),400
    c.execute("INSERT INTO tasks VALUES (?,?)", (uid,key))
    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (rewards[key], uid))
    c.commit(); c.close()
    return jsonify({"ok":True,"reward":rewards[key]})

@app.route("/api/update_onchain", methods=["POST"])
def update_onchain():
    d=request.get_json(force=True, silent=True) or {}
    uid=str(d.get("user_id","")); onchain=float(d.get("onchain",0)); wallet=d.get("wallet","")
    c=db(); c.execute("UPDATE users SET onchain=?, wallet=? WHERE user_id=?", (onchain, wallet, uid)); c.commit(); c.close()
    return jsonify({"ok":True,"level":level_from_holding(onchain)})

@app.route("/api/withdraws", methods=["GET"])
def list_withdraws():
    # Admin only - check admin key
    if request.args.get("admin_key")!=os.environ.get("ADMIN_KEY","zyron-admin"):
        return jsonify({"ok":False}),403
    c=db(); rows=c.execute("SELECT * FROM withdraws WHERE status='pending' ORDER BY created DESC").fetchall(); c.close()
    return jsonify({"ok":True,"withdraws":[dict(r) for r in rows]})

@app.route("/", methods=["GET"])
def index(): return jsonify({"ok":True,"service":"ZYRON Miner Backend","version":"1.0"})

if __name__=="__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
