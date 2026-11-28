# ZYRON core - shared by app.py (Flask) and zyron_bot.py (telebot).
# All money logic lives here, once, server-side. The app never sends balances or levels.
# Put this file in the SAME folder as app.py, zyron_bot.py and zyron.db.
import sqlite3, os, time, json, math, threading, urllib.request, urllib.error, urllib.parse, hmac, hashlib, base64

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("ZYRON_DB") or os.path.join(BASE, "zyron.db")

MIN_WITHDRAW = 10000
WITHDRAW_COOLDOWN = 48 * 3600
BOOST_COSTS = {2: 1000, 5: 5000, 10: 10000}
# Task "x" (follow on X) was removed: it cannot be verified from the server.
TASK_REWARDS = {"ch": 500, "grp": 500, "inv": 1000}
INV_REQUIRED = 5                 # task "inv" needs this many REAL referrals
REFERRAL_BONUS = 50
# Telegram channel / group used by tasks "ch" and "grp": set as env vars, e.g. @MyChannel or -1001234567890.
# The bot MUST be an admin of the channel (and a member/admin of the group) or getChatMember fails.
TASK_CHANNEL = os.environ.get("ZYN_CHANNEL", "").strip()
TASK_GROUP = os.environ.get("ZYN_GROUP", "").strip()
INITDATA_MAX_AGE = int(os.environ.get("INITDATA_MAX_AGE", "86400"))

# ---- Lock / Unlock (hybrid design) ----
# Lock: the user sends ZYN to LOCK_WALLET with the memo lock_memo(uid). The server only READS the chain (TonAPI).
# Unlock: MANUAL. The admin sends 70% back by hand and burns 30% by hand, OUTSIDE the server, then runs /unlock_done <id>.
# No wallet secrets of any kind live in this code, and there is no automatic unlock.
LOCK_WALLET = os.environ.get("ZYN_LOCK_WALLET", "").strip()
MIN_LOCK = float(os.environ.get("ZYN_MIN_LOCK", "1"))
UNLOCK_RETURN_PCT = 0.70         # user gets 70% back, 30% is burned by the admin
LOCK_VERIFY_COOLDOWN = 10        # seconds between two verify calls of the same user

# ---- On-chain (TonAPI) config. Set these as environment variables, never in the code. ----
TONAPI_KEY = os.environ.get("TONAPI_KEY", "").strip()
JETTON_MASTER = os.environ.get("ZYN_JETTON_MASTER", "EQAJruUqgQTd-KC3QiUAoQePVzW7KubQaXcEqlhdOb4ghwyk").strip()
DEFAULT_DECIMALS = int(os.environ.get("ZYN_DECIMALS", "9"))
ONCHAIN_TTL = int(os.environ.get("ONCHAIN_TTL", "300"))   # re-read the chain at most every 5 minutes per user
TONAPI_BASE = os.environ.get("TONAPI_BASE", "https://tonapi.io/v2")

_db_lock = threading.RLock()


def db():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")      # WAL: readers (sqlite3 CLI, /check_user) do not lock writers
    c.execute("PRAGMA synchronous=NORMAL")
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
    cur.execute("""CREATE TABLE IF NOT EXISTS withdraw_rejections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, user_id TEXT, username TEXT,
        amount REAL, balance REAL, onchain REAL, wallet TEXT, reason TEXT, withdraw_id INTEGER
    )""")
    # ---- migrations for older databases (safe to run many times) ----
    cols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    if "last_bot_mine" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_bot_mine INTEGER DEFAULT 0")
    if "ref_paid" not in cols:
        # 0 = referral bonus not paid yet (paid after the new user's first real activity)
        cur.execute("ALTER TABLE users ADD COLUMN ref_paid INTEGER DEFAULT 0")
        # users already referred before this fix were already paid under the old rule
        cur.execute("UPDATE users SET ref_paid=1 WHERE referred_by IS NOT NULL AND referred_by<>''")
    if "onchain_checked" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN onchain_checked INTEGER DEFAULT 0")
    if "locked" not in cols:
        # amount of ZYN really locked (verified on-chain). The ONLY thing that gives a level.
        cur.execute("ALTER TABLE users ADD COLUMN locked REAL DEFAULT 0")
    cur.execute("""CREATE TABLE IF NOT EXISTS locks (
        tx_key TEXT PRIMARY KEY, user_id TEXT, amount REAL, sender TEXT, wallet TEXT, ts INTEGER, created_at INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS unlock_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, amount REAL, payout REAL, burn REAL, wallet TEXT,
        status TEXT DEFAULT 'pending', created_at INTEGER, updated_at INTEGER DEFAULT 0
    )""")
    wcols = [r[1] for r in cur.execute("PRAGMA table_info(withdraws)").fetchall()]
    if "updated_at" not in wcols:
        cur.execute("ALTER TABLE withdraws ADD COLUMN updated_at INTEGER DEFAULT 0")
        cur.execute("UPDATE withdraws SET updated_at=created_at WHERE updated_at=0")
    if "deducted" not in wcols:
        # NULL = old request created before this fix: we do NOT know if it was deducted, so a reject refunds nothing.
        cur.execute("ALTER TABLE withdraws ADD COLUMN deducted REAL")
    c.commit(); c.close()


init_db()

# ---------------- Levels (same anchors as before) ----------------
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

# ---------------- On-chain balance (TonAPI) ----------------
def fetch_onchain(wallet):
    """Real ZYN balance of `wallet` read from the TON blockchain through TonAPI.
    Returns float, or None if TonAPI could not be reached (caller keeps the old value).
    Returns 0.0 if the wallet holds no ZYN."""
    if not wallet:
        return 0.0
    url = f"{TONAPI_BASE}/accounts/{wallet}/jettons/{JETTON_MASTER}"
    headers = {"Accept": "application/json"}
    if TONAPI_KEY:
        headers["Authorization"] = f"Bearer {TONAPI_KEY}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            j = json.loads(resp.read().decode())
        dec = (j.get("jetton") or {}).get("decimals", DEFAULT_DECIMALS)
        return max(0.0, float(j.get("balance") or 0) / (10 ** int(dec)))
    except urllib.error.HTTPError as e:
        if e.code == 404:          # wallet has no ZYN jetton wallet
            return 0.0
        print("tonapi http error", e.code, flush=True)
        return None
    except Exception as e:
        print("tonapi error", e, flush=True)
        return None

def _accrue_row(cur, r, now):
    """Move mining rewards since last_tick into pending, using the level at the CURRENT locked amount."""
    if r["mining"]:
        last = r["last_tick"] or now
        elapsed = max(0, now - last) / 1000
        add = elapsed * reward_for(level_from(r["locked"] or 0)) * (r["boost"] or 1) / 86400
        if add > 0:
            cur.execute("UPDATE users SET pending=pending+?, last_tick=? WHERE user_id=?", (add, now, r["user_id"]))
        return add
    return 0

def set_onchain(uid, value):
    """Store the chain-read wallet balance. DISPLAY ONLY: it gives no level (level comes from `locked`)."""
    uid = str(uid); now = int(time.time() * 1000)
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        r = cur.fetchone()
        if r:
            _accrue_row(cur, r, now)
            cur.execute("UPDATE users SET onchain=?, onchain_checked=? WHERE user_id=?", (float(value), int(time.time()), uid))
            c.commit()
        c.close()

def refresh_onchain(uid, force=False):
    """Re-read the user's onchain from TonAPI if the stored value is older than ONCHAIN_TTL."""
    uid = str(uid)
    c = db(); cur = c.cursor()
    cur.execute("SELECT wallet, onchain, onchain_checked FROM users WHERE user_id=?", (uid,))
    r = cur.fetchone(); c.close()
    if not r:
        return None
    if not r["wallet"]:
        if (r["onchain"] or 0) != 0:
            set_onchain(uid, 0.0)
        return 0.0
    if not force and time.time() - (r["onchain_checked"] or 0) < ONCHAIN_TTL:
        return r["onchain"]
    bal = fetch_onchain(r["wallet"])          # network call outside the DB lock
    if bal is None:
        return r["onchain"]                   # TonAPI down: keep last verified value
    set_onchain(uid, bal)
    return bal

def link_wallet(uid, wallet):
    """Attach a wallet to a user. One wallet = one user. Onchain is then read from the chain, never from the client.
    Returns (ok, error_or_level)."""
    uid = str(uid); wallet = (wallet or "").strip()
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("SELECT wallet, locked FROM users WHERE user_id=?", (uid,))
        me = cur.fetchone()
        cur.execute("SELECT id FROM unlock_requests WHERE user_id=? AND status='pending'", (uid,))
        open_unlock = cur.fetchone(); c.close()
    if me and (me["locked"] or 0) > 0 and wallet != (me["wallet"] or ""):
        return False, "unlock first: you cannot change or disconnect the wallet while ZYN is locked"
    if not wallet:
        # disconnect (only possible when nothing is locked)
        with _db_lock:
            now = int(time.time() * 1000)
            c = db(); cur = c.cursor()
            cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
            r = cur.fetchone()
            if r:
                _accrue_row(cur, r, now)
                cur.execute("UPDATE users SET wallet='', onchain=0, onchain_checked=? WHERE user_id=?", (int(time.time()), uid))
                c.commit()
            c.close()
        return True, 1
    if not wallet.startswith(("UQ", "EQ", "0Q", "kQ")) or len(wallet) < 40:
        return False, "invalid wallet"
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("SELECT user_id FROM users WHERE wallet=? AND user_id<>?", (wallet, uid))
        other = cur.fetchone(); c.close()
    if other:
        return False, "wallet already linked to another account"
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE users SET wallet=? WHERE user_id=?", (wallet, uid))
        c.commit(); c.close()
    refresh_onchain(uid, force=True)
    c = db(); r = c.execute("SELECT locked FROM users WHERE user_id=?", (uid,)).fetchone(); c.close()
    return True, level_from((r["locked"] if r else 0) or 0)

# ---------------- Users ----------------
def get_user(uid, username=""):
    """Fetch (or create) the user, refresh onchain from the chain if stale, accrue mining. Server is the only source of truth."""
    uid = str(uid)
    try:
        refresh_onchain(uid)
    except Exception as e:
        print("refresh_onchain error", e, flush=True)
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
        elif username and r["username"] != username:
            cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
        _accrue_row(cur, r, now)
        c.commit()
        cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        r = cur.fetchone()
        c.close()
        d = dict(r); d["_is_new"] = is_new
        return d

# ---------------- Withdrawals ----------------
def log_rejection(cur, uid, username, amount, balance, onchain, wallet, reason, wid=None):
    cur.execute("""INSERT INTO withdraw_rejections (ts,user_id,username,amount,balance,onchain,wallet,reason,withdraw_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (int(time.time()), str(uid), username or "", amount, balance, onchain, wallet or "", reason, wid))

def request_withdraw(uid, amount, wallet, now=None):
    """Create a withdrawal. The balance is re-read from the DB INSIDE the same transaction and deducted
    atomically (UPDATE ... WHERE balance >= amount). Returns (ok, error_text, withdraw_id, username)."""
    uid = str(uid); now = now or int(time.time())
    u = get_user(uid)                      # accrue + onchain refresh, outside the write lock
    username = u.get("username", "")
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        amt = float("nan")
    wallet = (wallet or "").strip()
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT balance, onchain FROM users WHERE user_id=?", (uid,))
        row = cur.fetchone()
        bal = (row["balance"] or 0) if row else 0
        onc = (row["onchain"] or 0) if row else 0

        def reject(reason, msg):
            log_rejection(cur, uid, username, None if math.isnan(amt) else amt, bal, onc, wallet, reason)
            c.commit(); c.close()
            return False, msg, None, username

        if not row:
            return reject("no_user", "no uid")
        if math.isnan(amt) or math.isinf(amt) or amt <= 0:
            return reject("invalid_amount", "مبلغ غير صالح")
        if amt < MIN_WITHDRAW:
            return reject("below_minimum", f"الحد الأدنى للسحب {MIN_WITHDRAW} ZYN")
        if amt > bal + 1e-9:
            return reject("insufficient_balance", "الرصيد غير كافٍ")
        if not wallet.startswith(("UQ", "EQ")) or len(wallet) < 40:
            return reject("invalid_wallet", "عنوان المحفظة غير صالح")
        cur.execute("SELECT id FROM withdraws WHERE user_id=? AND status IN ('pending','sending')", (uid,))
        if cur.fetchone():
            return reject("open_request_exists", "لديك طلب سحب قيد المعالجة")
        cur.execute("SELECT MAX(updated_at) FROM withdraws WHERE user_id=? AND status IN ('done','rejected')", (uid,))
        last_end = cur.fetchone()[0] or 0
        if last_end and now - last_end < WITHDRAW_COOLDOWN:
            left = int((WITHDRAW_COOLDOWN - (now - last_end)) / 3600) + 1
            return reject("cooldown", f"طلب واحد كل 48 ساعة بعد انتهاء آخر طلب. المتبقي {left} ساعة")
        # atomic deduction: only succeeds if the balance is still enough right now
        cur.execute("UPDATE users SET balance=balance-?, wallet=? WHERE user_id=? AND balance>=?", (amt, wallet, uid, amt))
        if cur.rowcount != 1:
            return reject("insufficient_balance_race", "الرصيد غير كافٍ")
        cur.execute("""INSERT INTO withdraws (user_id, amount, wallet, status, created_at, updated_at, deducted)
                       VALUES (?,?,?,'pending',?,?,?)""", (uid, amt, wallet, now, now, amt))
        wid = cur.lastrowid
        c.commit(); c.close()
    return True, "", wid, username

def reject_withdraw(wid, reason="admin_reject"):
    """Reject a pending/sending request. Refunds ONLY what was really deducted (withdraws.deducted),
    exactly once. Returns dict(ok, refunded, user_id, username, amount, wallet, status) ."""
    now = int(time.time())
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT * FROM withdraws WHERE id=?", (wid,))
        r = cur.fetchone()
        if not r:
            c.close(); return {"ok": False, "error": "not_found"}
        cur.execute("SELECT username, balance, onchain FROM users WHERE user_id=?", (r["user_id"],))
        u = cur.fetchone()
        username = (u["username"] if u else "") or ""
        # claim the request atomically: only one reject can ever win
        cur.execute("UPDATE withdraws SET status='rejected', updated_at=? WHERE id=? AND status IN ('pending','sending')", (now, wid))
        if cur.rowcount != 1:
            st = r["status"]
            c.close(); return {"ok": False, "error": "already_processed", "status": st, "username": username,
                               "user_id": r["user_id"], "amount": r["amount"], "wallet": r["wallet"]}
        refund = r["deducted"] if r["deducted"] is not None else 0.0
        refund = max(0.0, min(float(refund), float(r["amount"] or 0)))
        if refund > 0:
            cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (refund, r["user_id"]))
            cur.execute("UPDATE withdraws SET deducted=0 WHERE id=?", (wid,))   # cannot be refunded twice
        log_rejection(cur, r["user_id"], username, r["amount"], u["balance"] if u else 0, u["onchain"] if u else 0,
                      r["wallet"], reason, wid)
        c.commit(); c.close()
    return {"ok": True, "refunded": refund, "user_id": r["user_id"], "username": username,
            "amount": r["amount"], "wallet": r["wallet"], "legacy": r["deducted"] is None}

def set_withdraw_status(wid, new_status, expected):
    """accept (pending->sending) / done (sending->done). Returns row or None."""
    now = int(time.time())
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE withdraws SET status=?, updated_at=? WHERE id=? AND status=?", (new_status, now, wid, expected))
        ok = cur.rowcount == 1
        c.commit()
        cur.execute("SELECT w.*, u.username AS username FROM withdraws w LEFT JOIN users u ON u.user_id=w.user_id WHERE w.id=?", (wid,))
        r = cur.fetchone(); c.close()
    return (dict(r) if r else None), ok

def check_user_report(uid):
    """Text for the admin command /check_user <id>. Reads stored values + a LIVE chain read when possible."""
    uid = str(uid).strip()
    c = db(); cur = c.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    r = cur.fetchone()
    if not r:
        c.close(); return None
    cur.execute("SELECT COUNT(*) FROM withdraws WHERE user_id=? AND status IN ('pending','sending')", (uid,))
    open_wd = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM withdraw_rejections WHERE user_id=?", (uid,))
    rej = cur.fetchone()[0]
    c.close()
    try:
        joined = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(r["joined_at"])))
    except Exception:
        joined = r["joined_at"] or "?"
    live = fetch_onchain(r["wallet"]) if r["wallet"] else 0.0
    live_txt = "TonAPI unreachable" if live is None else f"{live:,.2f}"
    lvl = level_from(r["locked"] or 0)
    c = db(); uo = c.execute("SELECT id, amount FROM unlock_requests WHERE user_id=? AND status='pending'", (uid,)).fetchall(); c.close()
    uo_txt = ", ".join(f"#{x['id']}={x['amount']:,.0f}" for x in uo) or "-"
    return (f"👤 {uid} @{r['username'] or '-'}\n"
            f"balance: {r['balance'] or 0:,.2f}\n"
            f"pending: {r['pending'] or 0:,.2f}\n"
            f"wallet balance (stored, no level): {r['onchain'] or 0:,.2f}\n"
            f"wallet balance (live): {live_txt}\n"
            f"LOCKED: {r['locked'] or 0:,.2f} | pending unlock: {uo_txt}\n"
            f"level: {lvl} | boost: x{r['boost'] or 1}\n"
            f"mining: {'ON' if r['mining'] else 'OFF'} (≈ {int(reward_for(lvl) * (r['boost'] or 1)):,}/day)\n"
            f"referrals: {r['referrals'] or 0} (earned {r['ref_earn'] or 0:,.0f})\n"
            f"joined_at: {joined}\n"
            f"wallet: {r['wallet'] or '-'}\n"
            f"open withdrawals: {open_wd} | rejected attempts: {rej}")


# ---------------- Telegram identity (initData HMAC) ----------------
def bot_token():
    f = os.path.join(BASE, "token.txt")
    if os.path.exists(f):
        return open(f).read().strip()
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def verify_init_data(init_data, token=None, now=None, max_age=None):
    """Check Telegram.WebApp.initData. Returns (user_id_str, username) or (None, None).
    The ONLY source of identity for the API."""
    token = token or bot_token()
    if not init_data or not token or not isinstance(init_data, str):
        return None, None
    try:
        p = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        h = p.pop("hash", "")
        if not h:
            return None, None
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(p.items()))
        key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc = hmac.new(key, dcs.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, h):
            return None, None
        now = now if now is not None else time.time()
        age = now - int(p.get("auth_date", 0))
        if age > (max_age or INITDATA_MAX_AGE) or age < -300:
            return None, None
        u = json.loads(p["user"])
        return str(int(u["id"])), (u.get("username") or "")
    except Exception:
        return None, None


# ---------------- Referrals: bonus only after real activity ----------------
def set_referrer(uid, ref):
    """Remember who referred uid. NO bonus here. Only for existing users, never self, only once."""
    uid = str(uid); ref = str(ref or "").replace("ref_", "").replace("ref", "").strip()
    if not ref.isdigit() or ref == uid:
        return False
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("SELECT referred_by, joined_at FROM users WHERE user_id=?", (uid,))
        me = cur.fetchone()
        cur.execute("SELECT user_id FROM users WHERE user_id=?", (ref,))
        # only a NEW account (joined < 1 hour ago) can be attached to a referrer
        try: fresh = me is not None and time.time() - int(me["joined_at"] or 0) < 3600
        except ValueError: fresh = False
        ok = fresh and not me["referred_by"] and cur.fetchone() is not None
        if ok:
            cur.execute("UPDATE users SET referred_by=?, ref_paid=0 WHERE user_id=? AND (referred_by IS NULL OR referred_by='')", (ref, uid))
            c.commit()
        c.close()
    return ok


def pay_referral_if_active(uid):
    """Call after a REAL activity (first successful mining claim). Pays once, to both sides.
    Returns the referrer id if paid, else None."""
    uid = str(uid)
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("UPDATE users SET ref_paid=1 WHERE user_id=? AND ref_paid=0 AND referred_by IS NOT NULL AND referred_by<>''", (uid,))
            if cur.rowcount != 1:
                c.rollback(); return None
            cur.execute("SELECT referred_by FROM users WHERE user_id=?", (uid,))
            ref = cur.fetchone()["referred_by"]
            cur.execute("UPDATE users SET balance=balance+?, referrals=referrals+1, ref_earn=ref_earn+? WHERE user_id=?",
                        (REFERRAL_BONUS, REFERRAL_BONUS, ref))
            cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (REFERRAL_BONUS, uid))
            c.commit()
            return ref
        except Exception as e:
            c.rollback(); print("pay_referral error", e, flush=True); return None
        finally:
            c.close()


def claim_mining(uid):
    """Atomic claim: pending -> balance. Triggers the referral bonus on the first claim with something to claim."""
    uid = str(uid); now = int(time.time() * 1000)
    get_user(uid)  # accrues pending
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT pending FROM users WHERE user_id=?", (uid,))
        r = cur.fetchone(); got = (r["pending"] or 0) if r else 0
        cur.execute("UPDATE users SET balance=balance+pending, pending=0, last_tick=? WHERE user_id=?", (now, uid))
        c.commit(); c.close()
    ref = pay_referral_if_active(uid) if got > 0 else None
    return got, ref


# ---------------- Tasks: server-side verification ----------------
def tg_member(chat, user_id):
    """True only if Telegram says the user is in the chat. Fails closed (False) on any error."""
    token = bot_token()
    if not token or not chat:
        return False
    try:
        data = json.dumps({"chat_id": chat, "user_id": int(user_id)}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/getChatMember",
                                     data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            j = json.loads(resp.read().decode())
        if not j.get("ok"):
            return False
        m = j["result"]
        return m.get("status") in ("creator", "administrator", "member") or (m.get("status") == "restricted" and m.get("is_member"))
    except Exception as e:
        print("getChatMember error", e, flush=True)
        return False


def claim_task(uid, k, is_member=None):
    """Verify then pay a task once. Returns (ok, reward_or_error). is_member is injectable for tests."""
    uid = str(uid)
    if k not in TASK_REWARDS:
        return False, "Unknown task"
    is_member = is_member or tg_member
    u = get_user(uid)
    try:
        if json.loads(u["tasks"] or "{}").get(k):
            return False, "Already done"
    except (TypeError, ValueError):
        pass
    # verification (network call) happens BEFORE touching the DB
    if k == "ch":
        if not is_member(TASK_CHANNEL, uid): return False, "join the channel first"
    elif k == "grp":
        if not is_member(TASK_GROUP, uid): return False, "join the group first"
    elif k == "inv":
        if (u["referrals"] or 0) < INV_REQUIRED: return False, f"need {INV_REQUIRED} referrals"
    reward = TASK_REWARDS[k]
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT tasks FROM users WHERE user_id=?", (uid,))
            try: tasks = json.loads(cur.fetchone()["tasks"] or "{}")
            except (TypeError, ValueError): tasks = {}
            if tasks.get(k):
                c.rollback(); return False, "Already done"
            tasks[k] = 1
            cur.execute("UPDATE users SET balance=balance+?, tasks=? WHERE user_id=?", (reward, json.dumps(tasks), uid))
            c.commit()
        finally:
            c.close()
    return True, reward


# ---------------- Lock / Unlock ----------------
def _crc16(b):
    crc = 0
    for x in b:
        crc ^= x << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def to_raw(addr):
    """Normalise a TON address (raw '0:hex' or friendly EQ../UQ..) to 'wc:hex'. Returns None if invalid."""
    try:
        a = (addr or "").strip()
        if not a:
            return None
        if ":" in a:
            wc, h = a.split(":", 1)
            if len(h) != 64:
                return None
            int(h, 16)
            return f"{int(wc)}:{h.lower()}"
        b = base64.urlsafe_b64decode(a.replace("+", "-").replace("/", "_") + "=" * (-len(a) % 4))
        if len(b) != 36 or _crc16(b[:34]) != int.from_bytes(b[34:], "big"):
            return None
        wc = b[1] - 256 if b[1] > 127 else b[1]
        return f"{wc}:{b[2:34].hex()}"
    except Exception:
        return None


def lock_memo(uid):
    """The comment the user must put in the transfer. Ties the transfer to ONE account."""
    return f"ZYNLOCK{uid}"


def lock_info(uid):
    return {"lock_wallet": LOCK_WALLET, "memo": lock_memo(uid), "jetton_master": JETTON_MASTER,
            "min_lock": MIN_LOCK, "enabled": bool(LOCK_WALLET and to_raw(LOCK_WALLET))}


def fetch_lock_events():
    """Latest ZYN transfers that touched the lock wallet, read from TonAPI. Returns list of events or None on error."""
    if not LOCK_WALLET:
        return None
    url = f"{TONAPI_BASE}/accounts/{LOCK_WALLET}/jettons/{JETTON_MASTER}/history?limit=100"
    headers = {"Accept": "application/json"}
    if TONAPI_KEY:
        headers["Authorization"] = f"Bearer {TONAPI_KEY}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode()).get("events") or []
    except Exception as e:
        print("tonapi lock history error", e, flush=True)
        return None


def _match_lock_actions(events, uid, user_wallet):
    """Yield (tx_key, amount, sender_raw, ts) for every transfer that really is a lock of `uid`:
    ZYN jetton (master compared), recipient = lock wallet, sender = the user's linked wallet,
    memo = lock_memo(uid), action ok, event not in progress."""
    master = to_raw(JETTON_MASTER); lockw = to_raw(LOCK_WALLET); mine = to_raw(user_wallet)
    memo = lock_memo(uid)
    if not (master and lockw and mine):
        return
    for ev in events or []:
        if ev.get("in_progress"):
            continue
        for i, a in enumerate(ev.get("actions") or []):
            if a.get("type") != "JettonTransfer" or a.get("status") != "ok":
                continue
            t = a.get("JettonTransfer") or {}
            if to_raw((t.get("jetton") or {}).get("address")) != master:
                continue
            if to_raw((t.get("recipient") or {}).get("address")) != lockw:
                continue
            if to_raw((t.get("sender") or {}).get("address")) != mine:
                continue
            if (t.get("comment") or "").strip() != memo:
                continue
            try:
                dec = int((t.get("jetton") or {}).get("decimals", DEFAULT_DECIMALS))
                amt = round(int(t.get("amount")) / (10 ** dec), 6)
            except (TypeError, ValueError):
                continue
            if amt < MIN_LOCK:
                continue
            yield f"{ev.get('event_id')}:{i}", amt, mine, int(ev.get("timestamp") or 0)


_last_verify = {}


def claim_lock(uid, events=None, now=None):
    """Verify the user's lock transfers on-chain and credit each one ONCE (tx_key is the primary key).
    Returns (ok, credited_amount_or_error, new_level). `events` is injectable for tests."""
    uid = str(uid)
    if not LOCK_WALLET or not to_raw(LOCK_WALLET):
        return False, "lock is not configured yet", None
    t = time.time() if now is None else now
    if events is None and t - _last_verify.get(uid, 0) < LOCK_VERIFY_COOLDOWN:
        return False, "wait a few seconds then try again", None
    _last_verify[uid] = t
    c = db(); r = c.execute("SELECT wallet FROM users WHERE user_id=?", (uid,)).fetchone(); c.close()
    if not r:
        return False, "no user", None
    if not r["wallet"]:
        return False, "connect your wallet first", None
    if events is None:
        events = fetch_lock_events()           # network call outside the DB lock
        if events is None:
            return False, "could not read the blockchain now, try again later", None
    credited = 0.0
    nowms = int(time.time() * 1000)
    for key, amt, sender, ts in list(_match_lock_actions(events, uid, r["wallet"])):
        with _db_lock:
            c = db(); cur = c.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                cur.execute("INSERT OR IGNORE INTO locks (tx_key,user_id,amount,sender,wallet,ts,created_at) VALUES (?,?,?,?,?,?,?)",
                            (key, uid, amt, sender, r["wallet"], ts, int(time.time())))
                if cur.rowcount == 1:
                    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
                    row = cur.fetchone()
                    _accrue_row(cur, row, nowms)       # settle mining at the OLD level first
                    cur.execute("UPDATE users SET locked=COALESCE(locked,0)+? WHERE user_id=?", (amt, uid))
                    credited += amt
                c.commit()
            except Exception as e:
                c.rollback(); print("claim_lock error", e, flush=True)
            finally:
                c.close()
    c = db(); r2 = c.execute("SELECT locked FROM users WHERE user_id=?", (uid,)).fetchone(); c.close()
    lvl = level_from((r2["locked"] if r2 else 0) or 0)
    if credited <= 0:
        return False, "no new lock transfer found (check amount, memo and sender wallet; it can take a minute)", lvl
    return True, credited, lvl


def request_unlock(uid, amount=None):
    """User asks to unlock. NOTHING moves and the level does NOT change yet: the admin sends 70% and burns 30% by hand,
    then runs /unlock_done. Returns (ok, error_or_row_dict)."""
    uid = str(uid)
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT wallet, locked FROM users WHERE user_id=?", (uid,))
            u = cur.fetchone()
            if not u:
                c.rollback(); return False, "no user"
            locked = u["locked"] or 0
            if locked <= 0:
                c.rollback(); return False, "nothing is locked"
            if not u["wallet"]:
                c.rollback(); return False, "no wallet linked"
            cur.execute("SELECT id FROM unlock_requests WHERE user_id=? AND status='pending'", (uid,))
            if cur.fetchone():
                c.rollback(); return False, "you already have a pending unlock request"
            try:
                amt = locked if amount in (None, "", 0) else float(amount)
            except (TypeError, ValueError):
                c.rollback(); return False, "invalid amount"
            if not (amt == amt) or amt <= 0 or amt > locked + 1e-9:
                c.rollback(); return False, "invalid amount"
            amt = min(amt, locked)
            # payout only to a wallet that really locked (never to a wallet nobody locked from)
            mine = to_raw(u["wallet"])
            cur.execute("SELECT sender FROM locks WHERE user_id=? AND sender=? LIMIT 1", (uid, mine))
            if not cur.fetchone():
                c.rollback(); return False, "wallet does not match the wallet that locked"
            payout = round(amt * UNLOCK_RETURN_PCT, 6); burn = round(amt - payout, 6)
            now = int(time.time())
            cur.execute("INSERT INTO unlock_requests (user_id,amount,payout,burn,wallet,status,created_at,updated_at) VALUES (?,?,?,?,?,'pending',?,?)",
                        (uid, amt, payout, burn, u["wallet"], now, now))
            rid = cur.lastrowid
            c.commit()
        except Exception as e:
            c.rollback(); print("request_unlock error", e, flush=True); return False, "error"
        finally:
            c.close()
    return True, {"id": rid, "user_id": uid, "amount": amt, "payout": payout, "burn": burn, "wallet": u["wallet"]}


def complete_unlock(rid):
    """ADMIN ONLY (the bot checks ADMIN_ID). Call AFTER the admin sent the 70% and burned the 30% by hand.
    Atomically: pending -> done, locked -= amount (mining settled at the old level first). Once only."""
    now = int(time.time()); nowms = int(time.time() * 1000)
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM unlock_requests WHERE id=?", (rid,))
            r = cur.fetchone()
            if not r:
                c.rollback(); return {"ok": False, "error": "not_found"}
            if r["status"] != "pending":
                c.rollback(); return {"ok": False, "error": "already_processed", "status": r["status"]}
            cur.execute("SELECT * FROM users WHERE user_id=?", (r["user_id"],))
            u = cur.fetchone()
            if not u or (u["locked"] or 0) + 1e-9 < r["amount"]:
                c.rollback(); return {"ok": False, "error": "locked_less_than_amount"}
            _accrue_row(cur, u, nowms)
            cur.execute("UPDATE users SET locked=MAX(0, locked-?) WHERE user_id=?", (r["amount"], r["user_id"]))
            cur.execute("UPDATE unlock_requests SET status='done', updated_at=? WHERE id=? AND status='pending'", (now, rid))
            cur.execute("SELECT locked FROM users WHERE user_id=?", (r["user_id"],))
            left = cur.fetchone()["locked"] or 0
            c.commit()
        except Exception as e:
            c.rollback(); print("complete_unlock error", e, flush=True); return {"ok": False, "error": "error"}
        finally:
            c.close()
    return {"ok": True, "user_id": r["user_id"], "amount": r["amount"], "payout": r["payout"], "burn": r["burn"],
            "wallet": r["wallet"], "locked_left": left, "level": level_from(left)}


def reject_unlock(rid):
    """Admin cancels a pending unlock request. Nothing changes in the locked amount."""
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE unlock_requests SET status='rejected', updated_at=? WHERE id=? AND status='pending'", (int(time.time()), rid))
        ok = cur.rowcount == 1
        c.commit()
        r = c.execute("SELECT * FROM unlock_requests WHERE id=?", (rid,)).fetchone(); c.close()
    return (ok, dict(r) if r else None)


def pending_unlocks():
    c = db(); rows = [dict(x) for x in c.execute("SELECT * FROM unlock_requests WHERE status='pending' ORDER BY id").fetchall()]; c.close()
    return rows
