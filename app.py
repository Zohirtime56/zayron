# ZYRON Backend - Flask API - SHARED DB with bot
# Deploy on PythonAnywhere Web: WSGI -> from app import app as application
import sqlite3, os, time, json
from flask import Flask, request, jsonify
from flask_cors import CORS

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "zyron.db")
TOKEN_FILE = os.path.join(BASE, "token.txt")
ADMIN_ID = 7527825632

app = Flask(__name__)
CORS(app)

def db():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db(); cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY, username TEXT, balance REAL DEFAULT 0,
        pending REAL DEFAULT 0, mining INTEGER DEFAULT 0, last_tick INTEGER DEFAULT 0,
        wallet TEXT DEFAULT '', referrals INTEGER DEFAULT 0, referred_by TEXT DEFAULT '',
        joined_at TEXT DEFAULT '', onchain REAL DEFAULT 0, boost INTEGER DEFAULT 1, tasks TEXT DEFAULT '{}', ref_earn REAL DEFAULT 0
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS withdraws (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, amount REAL, wallet TEXT, status TEXT DEFAULT 'pending', created_at INTEGER
    )""")
    c.commit(); c.close()

init_db()

def level_from(h):
    h = float(h or 0)
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
    # تحديث المعلق اذا كان التعدين شغال
    if r["mining"]:
        now=int(time.time()*1000); elapsed=(now-(r["last_tick"] or now))/1000
        lvl=level_from(max(r["onchain"] or 0, r["balance"] or 0))
        add=elapsed*reward_for(lvl)*(r["boost"] or 1)/86400
        if add>0:
            cur.execute("UPDATE users SET pending=pending+?, last_tick=? WHERE user_id=?", (add, now, str(uid)))
            c.commit(); cur.execute("SELECT * FROM users WHERE user_id=?", (str(uid),)); r=cur.fetchone()
    c.close(); return dict(r)

def notify_admin(text):
    try:
        import urllib.request, urllib.parse
        token=open(TOKEN_FILE).read().strip() if os.path.exists(TOKEN_FILE) else os.environ.get("TELEGRAM_BOT_TOKEN","")
        if not token: return
        data=urllib.parse.urlencode({"chat_id":ADMIN_ID,"text":text}).encode()
        req=urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print("notify fail", e)

@app.route("/")
def home(): return jsonify({"status":"ZYRON API running","ok":True})

@app.route("/api/auth", methods=["POST"])
def auth():
    d=request.json or {}; uid=str(d.get("uid") or d.get("user_id") or "")
    if not uid: return jsonify({"ok":False,"error":"no uid"})
    u=get_user(uid, d.get("username",""))
    sp=d.get("start_param","") or ""
    # احالة
    if sp and not u["referred_by"]:
        ref=str(sp).replace("ref_","").replace("ref","")
        if ref and ref!=uid:
            c=db(); cur=c.cursor()
            cur.execute("SELECT * FROM users WHERE user_id=?", (ref,))
            if cur.fetchone():
                cur.execute("UPDATE users SET referred_by=?, ref_earn=ref_earn+50 WHERE user_id=?", (ref, uid))
                cur.execute("UPDATE users SET balance=balance+50, referrals=referrals+1 WHERE user_id=?", (ref,))
                cur.execute("UPDATE users SET balance=balance+50 WHERE user_id=?", (uid,))
                c.commit()
            c.close(); u=get_user(uid)
    lvl=level_from(max(u["onchain"] or 0, u["balance"] or 0))
    return jsonify({"ok":True,"user":{"balance":u["balance"],"pending":u["pending"],"mining":bool(u["mining"]),"boost":u["boost"],"refs":u["refs"] if "refs" in u else u["referrals"],"refEarn":u["ref_earn"],"onchain":u["onchain"],"level":lvl,"wallet":u["wallet"]}})

@app.route("/api/mine", methods=["POST"])
def mine():
    d=request.json or {}; uid=str(d.get("user_id") or d.get("uid") or ""); act=d.get("action","")
    u=get_user(uid)
    c=db(); cur=c.cursor(); now=int(time.time()*1000)
    if act=="start":
        cur.execute("UPDATE users SET mining=1, last_tick=? WHERE user_id=?", (now, uid))
    elif act=="stop":
        cur.execute("UPDATE users SET mining=0 WHERE user_id=?", (uid,))
    elif act=="claim":
        cur.execute("UPDATE users SET balance=balance+pending, pending=0 WHERE user_id=?", (uid,))
    c.commit(); c.close()
    u=get_user(uid); lvl=level_from(max(u["onchain"] or 0, u["balance"] or 0))
    return jsonify({"ok":True,"mining":bool(u["mining"]),"pending":u["pending"],"balance":u["balance"],"level":lvl})

@app.route("/api/withdraw", methods=["POST"])
def withdraw():
    d=request.json or {}; uid=str(d.get("user_id") or ""); amt=float(d.get("amount") or 0); w=(d.get("wallet") or "").strip()
    u=get_user(uid)
    if amt<10000: return jsonify({"ok":False,"error":"Min 10000"})
    if amt>u["balance"]: return jsonify({"ok":False,"error":"Not enough balance"})
    if not w.startswith(("UQ","EQ")): return jsonify({"ok":False,"error":"Invalid wallet"})
    c=db(); cur=c.cursor()
    cur.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amt, uid))
    cur.execute("INSERT INTO withdraws (user_id, amount, wallet, created_at) VALUES (?,?,?,?)", (uid, amt, w, int(time.time())))
    c.commit(); c.close()
    notify_admin(f"💸 طلب سحب ZYRON\nالمستخدم: {uid} (@{u['username']})\nالمبلغ: {amt} ZYN\nالمحفظة: {w}")
    u=get_user(uid)
    return jsonify({"ok":True,"balance":u["balance"]})

@app.route("/api/boost", methods=["POST"])
def boost():
    d=request.json or {}; uid=str(d.get("user_id") or ""); m=int(d.get("mult") or 1)
    cost={2:1000,5:5000,10:10000}.get(m,0)
    u=get_user(uid)
    if u["balance"]<cost: return jsonify({"ok":False,"error":"Not enough"})
    c=db(); cur=c.cursor(); cur.execute("UPDATE users SET balance=balance-?, boost=? WHERE user_id=?", (cost, m, uid)); c.commit(); c.close()
    return jsonify({"ok":True,"boost":m})

@app.route("/api/task", methods=["POST"])
def task():
    d=request.json or {}; uid=str(d.get("user_id") or ""); k=d.get("task") or ""
    u=get_user(uid); tasks=json.loads(u["tasks"] or "{}")
    if tasks.get(k): return jsonify({"ok":False,"error":"Already done"})
    reward=1000 if k=="inv" else 500
    tasks[k]=1
    c=db(); cur=c.cursor(); cur.execute("UPDATE users SET balance=balance+?, tasks=? WHERE user_id=?", (reward, json.dumps(tasks), uid)); c.commit(); c.close()
    return jsonify({"ok":True,"reward":reward})

@app.route("/api/update_onchain", methods=["POST"])
def update_onchain():
    d=request.json or {}; uid=str(d.get("user_id") or ""); bal=float(d.get("onchain") or 0); w=d.get("wallet") or ""
    c=db(); cur=c.cursor(); cur.execute("UPDATE users SET onchain=?, wallet=? WHERE user_id=?", (bal, w, uid)); c.commit(); c.close()
    return jsonify({"ok":True})

if __name__=="__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
