# ZYRON core - shared by app.py (Flask) and zyron_bot.py (telebot).
# All money logic lives here, once, server-side. The app never sends balances or levels.
# Put this file in the SAME folder as app.py, zyron_bot.py and zyron.db.
import sqlite3, os, time, json, math, threading, urllib.request, urllib.error, urllib.parse, hmac, hashlib, base64

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("ZYRON_DB") or os.path.join(BASE, "zyron.db")

WITHDRAW_COOLDOWN = 48 * 3600
# Task "x" (follow on X) and "grp" (group) were removed. Only the channel task and the invite task remain.
TASK_REWARDS = {"ch": 500, "inv": 1000}
INV_REQUIRED = 5                 # task "inv" needs this many REAL referrals (a referral counts only after the friend locked level 1)
REFERRAL_BONUS = 50
REFERRAL_DAILY_CAP = 20          # max paid referrals per inviter in any 24h window
# Telegram channel used by task "ch". The bot MUST be an admin of the channel or getChatMember fails.
TASK_CHANNEL = os.environ.get("ZYN_CHANNEL", "@zayronb").strip()
INITDATA_MAX_AGE = int(os.environ.get("INITDATA_MAX_AGE", "86400"))

# ---- Economy (phase 4). All of these are editable here. Price / pool / min usd live in the `settings` table (admin sets them). ----
# Level requirement in USD at the anchor levels. Between anchors the requirement grows geometrically (same shape as before).
USD_ANCHORS = [(1, 0.10), (10, 0.50), (50, 3.0), (100, 10.0), (500, 30.0)]
YIELD_L1 = 0.007                 # daily yield on level_requirement at level 1 (0.7%/day)
YIELD_L500 = 0.0035              # ... at level 500 (0.35%/day), linear in between
YIELD_CAP = 0.010                # total daily rate (base + boost) never exceeds 1.0% of level_requirement
BOOST_MAX = 1.5                  # one boost only, x1.5 (L1: 0.7%*1.5 = 1.05%, cut to the 1.0% cap)
BOOST_PAYBACK_DAYS = 15          # boost price = 15 days of the extra yield it gives
FEE_L1 = 0.15                    # withdrawal fee at level 1 ... 
FEE_L500 = 0.05                  # ... and at level 500, linear in between
DAILY_BUDGET_PCT = 0.003         # global: 0.3% of pool_zyn per rolling 24h
PER_REQUEST_PCT = 0.05           # one request <= 5% of the effective locked amount
UNLOCK_DELAY = 7 * 86400         # unlock waits 7 days and the amount does not count meanwhile
DEFAULT_MIN_USD = 0.001          # minimum withdrawal in USD (settings key min_usd overrides). At price 0.000000088 = ~11,364 ZYN.
PRICE_MAX_JUMP = 5.0             # /setprice refuses a change bigger than x5 / below /5 unless you add "force" (typo guard)

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
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS price_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, key TEXT, old TEXT, new TEXT)")
    if "boost_level" not in cols:
        # level the boost was bought at. The boost bonus is only paid on min(level, boost_level).
        cur.execute("ALTER TABLE users ADD COLUMN boost_level INTEGER DEFAULT 0")
    if "ref_paid_at" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN ref_paid_at INTEGER DEFAULT 0")
    ucols = [r[1] for r in cur.execute("PRAGMA table_info(unlock_requests)").fetchall()]
    if "unlock_at" not in ucols:
        cur.execute("ALTER TABLE unlock_requests ADD COLUMN unlock_at INTEGER DEFAULT 0")
        cur.execute("UPDATE unlock_requests SET unlock_at=created_at+? WHERE status='pending'", (UNLOCK_DELAY,))
    try:
        # one wallet = one user, enforced by the database (works across the Flask and the bot process)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_wallet ON users(wallet) WHERE wallet<>''")
    except sqlite3.DatabaseError as e:
        print("WARNING: duplicate wallets in users, unique index not created:", e, flush=True)
    wcols = [r[1] for r in cur.execute("PRAGMA table_info(withdraws)").fetchall()]
    if "fee" not in wcols:
        cur.execute("ALTER TABLE withdraws ADD COLUMN fee REAL")
    if "net" not in wcols:
        # NULL = old request (no fee): counts as amount in the daily budget
        cur.execute("ALTER TABLE withdraws ADD COLUMN net REAL")
    if "updated_at" not in wcols:
        cur.execute("ALTER TABLE withdraws ADD COLUMN updated_at INTEGER DEFAULT 0")
        cur.execute("UPDATE withdraws SET updated_at=created_at WHERE updated_at=0")
    if "deducted" not in wcols:
        # NULL = old request created before this fix: we do NOT know if it was deducted, so a reject refunds nothing.
        cur.execute("ALTER TABLE withdraws ADD COLUMN deducted REAL")
    if "queued_at" not in wcols:
        cur.execute("ALTER TABLE withdraws ADD COLUMN queued_at INTEGER")
    if "admin_notified" not in wcols:
        # NULL = normal request (the caller already told the admin). 0 = promoted from the queue, admin not told yet. 1 = told.
        cur.execute("ALTER TABLE withdraws ADD COLUMN admin_notified INTEGER")
    cur.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY, done_at INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS boost_refunds (user_id TEXT, old_boost REAL, amount REAL, ts INTEGER)")
    c.commit()
    _migrate_boost_refund(c)
    c.close()


BOOST_REFUND = {2: 1000.0, 5: 5000.0, 10: 10000.0}     # old x2 / x5 / x10 price in ZYN

def _migrate_boost_refund(c):
    """ONE time: every user with an old boost (x2/x5/x10) gets the old price back in balance and boost=1.
    The refund, the reset and the 'migrations' row are ONE transaction: all or nothing, and never twice
    (also safe if the Flask app and the bot import this file at the same moment)."""
    name = "phase5_boost_refund"
    cur = c.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        if cur.execute("SELECT 1 FROM migrations WHERE name=?", (name,)).fetchone():
            c.rollback(); return
        now = int(time.time())
        # only OLD boosts: boost > 1 and no boost_level (the new x1.5 always has boost_level >= 1)
        rows = cur.execute("SELECT user_id, boost FROM users WHERE boost>1 AND COALESCE(boost_level,0)=0").fetchall()
        refunded = 0.0; n = 0; unknown = 0
        for r in rows:
            amt = BOOST_REFUND.get(int(round(float(r["boost"] or 1))), 0.0)
            if amt > 0 and abs(float(r["boost"]) - round(float(r["boost"]))) < 1e-9:
                cur.execute("UPDATE users SET balance=COALESCE(balance,0)+?, boost=1 WHERE user_id=?", (amt, r["user_id"]))
                cur.execute("INSERT INTO boost_refunds (user_id, old_boost, amount, ts) VALUES (?,?,?,?)", (r["user_id"], r["boost"], amt, now))
                refunded += amt; n += 1
            else:
                cur.execute("UPDATE users SET boost=1 WHERE user_id=?", (r["user_id"],)); unknown += 1
        cur.execute("INSERT INTO migrations (name, done_at) VALUES (?,?)", (name, now))
        c.commit()
        print(f"migration {name}: {n} users refunded {refunded:,.0f} ZYN, {unknown} with an unknown boost value reset without refund", flush=True)
    except Exception:
        c.rollback(); raise


init_db()

# ---------------- Settings (price / pool are set by the admin, never read from a DEX) ----------------
def get_setting(cur, key, default=0.0):
    r = cur.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    try:
        v = float(r[0]) if r else float(default)
    except (TypeError, ValueError):
        v = float(default)
    return v if v == v and abs(v) != float("inf") else float(default)

def get_price(cur=None):
    """USD per 1 ZYN. 0 = not set yet (levels, mining and withdrawals stay off: fail closed)."""
    if cur is not None:
        return max(0.0, get_setting(cur, "price", 0.0))
    c = db()
    try:
        return max(0.0, get_setting(c, "price", 0.0))
    finally:
        c.close()

def set_setting(key, value, force=False):
    """Admin only (the bot checks ADMIN_ID). Returns (ok, message). Mining is settled for everybody at the OLD price first."""
    if key not in ("price", "pool_zyn", "min_usd"):
        return False, "unknown setting"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False, "invalid number"
    if not (v == v) or v <= 0 or v == float("inf"):
        return False, "must be a positive number"
    nowms = int(time.time() * 1000)
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            old = get_setting(cur, key, 0.0)
            if key == "price" and old > 0 and not force and (v > old * PRICE_MAX_JUMP or v < old / PRICE_MAX_JUMP):
                c.rollback()
                return False, f"change is bigger than x{PRICE_MAX_JUMP:g} (old {old:g}). Add the word force to confirm."
            if key == "price":
                for r in cur.execute("SELECT * FROM users WHERE mining=1").fetchall():
                    _accrue_row(cur, r, nowms)       # everybody is paid up to now at the old price
            cur.execute("INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, repr(v)))
            cur.execute("INSERT INTO price_log (ts,key,old,new) VALUES (?,?,?,?)", (int(time.time()), key, repr(old), repr(v)))
            c.commit()
        except Exception as e:
            c.rollback(); print("set_setting error", e, flush=True); return False, "error"
        finally:
            c.close()
    return True, f"{key} = {v:g} (old {old:g})"

# ---------------- Levels (USD requirement, converted to ZYN with the admin price) ----------------
def _usd_req_table():
    t = [0.0] * 501
    for (a, ua), (b, ub) in zip(USD_ANCHORS, USD_ANCHORS[1:]):
        for l in range(a, b + 1):
            t[l] = ua * (ub / ua) ** ((l - a) / (b - a))
    return t

_REQ_USD = _usd_req_table()

def req_usd(l):
    l = max(1, min(500, int(l)))
    return _REQ_USD[l]

def level_requirement(l, price=None):
    """ZYN that must be (effectively) locked for level l. None while the price is not set."""
    price = get_price() if price is None else price
    return None if price <= 0 else req_usd(l) / price

def level_from(locked, price=None):
    """Level of an effective locked amount. 0 = not enough for level 1 (or price not set)."""
    price = get_price() if price is None else price
    if price <= 0:
        return 0
    usd = float(locked or 0) * price * (1 + 1e-9)
    if usd < _REQ_USD[1]:
        return 0
    lo, hi = 1, 500
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _REQ_USD[mid] <= usd:
            lo = mid
        else:
            hi = mid - 1
    return lo

def yield_rate(l):
    l = max(1, min(500, int(l)))
    return YIELD_L1 + (YIELD_L500 - YIELD_L1) * (l - 1) / 499

def fee_rate(l):
    l = max(1, min(500, int(l or 1)))
    return FEE_L1 + (FEE_L500 - FEE_L1) * (l - 1) / 499

def base_daily(l, price):
    """ZYN per day without boost: yield% of the level REQUIREMENT (not of everything locked)."""
    if l <= 0 or price <= 0:
        return 0.0
    return yield_rate(l) * req_usd(l) / price

def boost_bonus_daily(l, price):
    """Extra ZYN per day that the x1.5 boost gives at level l, with base+boost <= YIELD_CAP."""
    if l <= 0 or price <= 0:
        return 0.0
    eff = min(yield_rate(l) * BOOST_MAX, YIELD_CAP)
    return max(0.0, eff - yield_rate(l)) * req_usd(l) / price

def daily_reward(l, price, boost=1, boost_level=0):
    """ZYN per day for a user at level l. The boost bonus is paid only on min(l, boost_level), so a boost bought
    at a low level can never earn on a much higher level."""
    d = base_daily(l, price)
    if d > 0 and (boost or 1) > 1 and (boost_level or 0) > 0:
        d += boost_bonus_daily(min(int(l), int(boost_level)), price)
    return d

def boost_cost(l, price, boost=1, boost_level=0):
    """15 days of the EXTRA daily yield. If a boost is already owned only the difference is paid. 0 = nothing to buy."""
    if l <= 0 or price <= 0:
        return 0.0
    have = boost_bonus_daily(min(int(l), int(boost_level)), price) if (boost or 1) > 1 and (boost_level or 0) > 0 else 0.0
    return max(0.0, BOOST_PAYBACK_DAYS * (boost_bonus_daily(l, price) - have))

def eff_locked(cur, uid, locked=None):
    """Locked ZYN that still counts: locked minus the amount of a pending unlock (it does not count for 7 days)."""
    if locked is None:
        r = cur.execute("SELECT locked FROM users WHERE user_id=?", (uid,)).fetchone()
        locked = (r[0] if r else 0) or 0
    p = cur.execute("SELECT COALESCE(SUM(amount),0) FROM unlock_requests WHERE user_id=? AND status='pending'", (uid,)).fetchone()[0] or 0
    return max(0.0, float(locked or 0) - float(p))

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
    """Move mining rewards since last_tick into pending, at the CURRENT effective level and price.
    last_tick always moves forward while mining is on, even when the reward is 0 (so a later lock can never be paid backwards)."""
    if r["mining"]:
        last = r["last_tick"] or now
        elapsed = max(0, now - last) / 1000
        price = get_price(cur)
        lvl = level_from(eff_locked(cur, r["user_id"], r["locked"]), price)
        add = elapsed * daily_reward(lvl, price, r["boost"], r["boost_level"]) / 86400
        cur.execute("UPDATE users SET pending=pending+?, last_tick=? WHERE user_id=?", (add, now, r["user_id"]))
        return add
    return 0


def set_onchain(uid, value):
    """Store the chain-read wallet balance. DISPLAY ONLY: it gives no level (level comes from `locked`)."""
    uid = str(uid); now = int(time.time() * 1000)
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
            r = cur.fetchone()
            if r:
                _accrue_row(cur, r, now)
                cur.execute("UPDATE users SET onchain=?, onchain_checked=? WHERE user_id=?", (float(value), int(time.time()), uid))
            c.commit()
        except Exception:
            c.rollback(); raise
        finally:
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
    """Attach a wallet to a user. One wallet = one user. Everything (locked check, owner check, update) runs in ONE
    BEGIN IMMEDIATE transaction, so two processes cannot race. Returns (ok, error_or_level)."""
    uid = str(uid); wallet = (wallet or "").strip()
    if wallet and (not wallet.startswith(("UQ", "EQ", "0Q", "kQ")) or len(wallet) < 40):
        return False, "invalid wallet"
    now = int(time.time() * 1000)
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
            me = cur.fetchone()
            if not me:
                c.rollback(); return False, "no user"
            if (me["locked"] or 0) > 0 and wallet != (me["wallet"] or ""):
                c.rollback(); return False, "unlock first: you cannot change or disconnect the wallet while ZYN is locked"
            if wallet:
                cur.execute("SELECT user_id FROM users WHERE wallet=? AND user_id<>?", (wallet, uid))
                if cur.fetchone():
                    c.rollback(); return False, "wallet already linked to another account"
            _accrue_row(cur, me, now)
            if wallet:
                cur.execute("UPDATE users SET wallet=? WHERE user_id=?", (wallet, uid))
            else:
                cur.execute("UPDATE users SET wallet='', onchain=0, onchain_checked=? WHERE user_id=?", (int(time.time()), uid))
            c.commit()
        except sqlite3.IntegrityError:
            c.rollback(); return False, "wallet already linked to another account"
        except Exception as e:
            c.rollback(); print("link_wallet error", e, flush=True); return False, "error"
        finally:
            c.close()
    if wallet:
        refresh_onchain(uid, force=True)
    c = db(); r = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    lvl = level_from(eff_locked(c, uid, r["locked"]), get_price(c)) if r else 0
    c.close()
    return True, lvl


# ---------------- Users ----------------
def get_user(uid, username=""):
    """Fetch (or create) the user, refresh onchain from the chain if stale, accrue mining. Server is the only source of truth.
    The SELECT, the INSERT and the accrual run in one BEGIN IMMEDIATE transaction (no race between processes)."""
    uid = str(uid)
    try:
        refresh_onchain(uid)
    except Exception as e:
        print("refresh_onchain error", e, flush=True)
    with _db_lock:
        c = db(); cur = c.cursor(); now = int(time.time() * 1000)
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
            r = cur.fetchone()
            is_new = False
            if not r:
                is_new = True
                cur.execute("INSERT INTO users (user_id, username, joined_at, last_tick) VALUES (?,?,?,?)",
                            (uid, username, str(int(time.time())), now))
                cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
                r = cur.fetchone()
            elif username and r["username"] != username:
                cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
            _accrue_row(cur, r, now)
            cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
            r = cur.fetchone()
            c.commit()
        except Exception:
            c.rollback(); raise
        finally:
            c.close()
        d = dict(r); d["_is_new"] = is_new
        return d


# ---------------- Withdrawals ----------------
def log_rejection(cur, uid, username, amount, balance, onchain, wallet, reason, wid=None):
    cur.execute("""INSERT INTO withdraw_rejections (ts,user_id,username,amount,balance,onchain,wallet,reason,withdraw_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (int(time.time()), str(uid), username or "", amount, balance, onchain, wallet or "", reason, wid))

def withdraw_budget(cur, now, pool):
    """(budget, used, left): global daily budget = DAILY_BUDGET_PCT of pool_zyn; used = NET payouts of requests
    (pending, sending, done) created in the last 24h. Old requests without a fee count their full amount."""
    budget = pool * DAILY_BUDGET_PCT
    used = cur.execute("SELECT COALESCE(SUM(COALESCE(net, amount)),0) FROM withdraws WHERE created_at>=? AND status IN ('pending','sending','done')",
                       (now - 86400,)).fetchone()[0] or 0
    return budget, used, max(0.0, budget - used)

def request_withdraw(uid, amount, wallet=None, now=None):
    """Create a withdrawal. EVERYTHING is checked and deducted in ONE BEGIN IMMEDIATE transaction.
    The payout wallet is the user's LINKED wallet; this function never changes the linked wallet.
    `wallet` (optional) must be the same wallet or the request is refused.
    Returns (ok, error_text, withdraw_id, username)."""
    uid = str(uid); now = now or int(time.time())
    u = get_user(uid)                      # accrue + onchain refresh, outside the write lock
    username = u.get("username", "")
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        amt = float("nan")
    given = (wallet or "").strip()
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
        row = cur.fetchone()
        bal = (row["balance"] or 0) if row else 0
        onc = (row["onchain"] or 0) if row else 0
        linked = (row["wallet"] or "") if row else ""

        def reject(reason, msg):
            log_rejection(cur, uid, username, None if math.isnan(amt) else amt, bal, onc, linked or given, reason)
            c.commit(); c.close()
            return False, msg, None, username

        if not row:
            return reject("no_user", "no uid")
        if math.isnan(amt) or math.isinf(amt) or amt <= 0:
            return reject("invalid_amount", "مبلغ غير صالح")
        price = get_price(cur); pool = get_setting(cur, "pool_zyn", 0.0)
        if price <= 0 or pool <= 0:
            return reject("economy_not_set", "السحب متوقف مؤقتا (السعر أو الـ pool غير محدد) / Withdrawals are paused")
        if not linked or not linked.startswith(("UQ", "EQ", "0Q", "kQ")) or len(linked) < 40:
            return reject("no_wallet", "اربط محفظتك أولا / Connect your wallet first")
        if given and given != linked and (to_raw(given) is None or to_raw(given) != to_raw(linked)):
            return reject("wallet_mismatch", "السحب فقط إلى المحفظة المربوطة / Withdrawals go only to your linked wallet")
        eff = eff_locked(cur, uid, row["locked"])
        lvl = level_from(eff, price)
        if lvl < 1:
            return reject("no_lock", "تحتاج قفل المستوى 1 على الأقل للسحب / Lock at least level 1 to withdraw")
        min_usd = get_setting(cur, "min_usd", DEFAULT_MIN_USD)
        min_zyn = min_usd / price
        if amt + 1e-9 < min_zyn:
            return reject("below_minimum", f"الحد الأدنى للسحب {min_zyn:,.2f} ZYN (${min_usd:g})")
        cap = eff * PER_REQUEST_PCT
        if amt > cap + 1e-9:
            return reject("above_request_cap", f"الحد الأقصى للطلب الواحد {cap:,.2f} ZYN (5% من المقفل)")
        if amt > bal + 1e-9:
            return reject("insufficient_balance", "الرصيد غير كافٍ")
        cur.execute("SELECT id FROM withdraws WHERE user_id=? AND status IN ('pending','sending','queued')", (uid,))
        if cur.fetchone():
            return reject("open_request_exists", "لديك طلب سحب قيد المعالجة")
        cur.execute("SELECT MAX(updated_at) FROM withdraws WHERE user_id=? AND status IN ('done','rejected')", (uid,))
        last_end = cur.fetchone()[0] or 0
        if last_end and now - last_end < WITHDRAW_COOLDOWN:
            left = int((WITHDRAW_COOLDOWN - (now - last_end)) / 3600) + 1
            return reject("cooldown", f"طلب واحد كل 48 ساعة بعد انتهاء آخر طلب. المتبقي {left} ساعة")
        fee = round(amt * fee_rate(lvl), 6); net = round(amt - fee, 6)
        _promote(cur, now, price, pool)          # older queued requests go first (first come, first served)
        budget, used, left_b = withdraw_budget(cur, now, pool)
        if net > budget + 1e-9:
            # could never be paid, not even with an empty budget: it would block the whole queue forever
            return reject("above_daily_budget", f"الطلب أكبر من الميزانية اليومية الكاملة ({budget:,.2f} ZYN). قلل المبلغ / Amount is above the whole daily budget")
        nq = cur.execute("SELECT COUNT(*) FROM withdraws WHERE status='queued'").fetchone()[0]
        queued = nq > 0 or net > left_b + 1e-9    # never jump the queue, even if this one would fit
        # atomic deduction: only succeeds if the balance is still enough right now
        cur.execute("UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?", (amt, uid, amt))
        if cur.rowcount != 1:
            return reject("insufficient_balance_race", "الرصيد غير كافٍ")
        cur.execute("""INSERT INTO withdraws (user_id, amount, wallet, status, created_at, updated_at, deducted, fee, net, queued_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""", (uid, amt, linked, 'queued' if queued else 'pending', now, now, amt, fee, net,
                                                      now if queued else None))
        wid = cur.lastrowid
        c.commit(); c.close()
    return True, "", wid, username


def _promote(cur, now, price, pool):
    """Inside an open write transaction: move queued requests to 'pending' in strict id order while the 24h budget
    has room. Stops at the first one that does not fit (no jumping). The promoted request's 24h window starts NOW.
    Returns the promoted ids."""
    if price <= 0 or pool <= 0:
        return []
    budget, used, left = withdraw_budget(cur, now, pool)
    out = []
    for r in cur.execute("SELECT id, COALESCE(net, amount) AS n FROM withdraws WHERE status='queued' ORDER BY id").fetchall():
        if r["n"] <= left + 1e-9:
            cur.execute("UPDATE withdraws SET status='pending', created_at=?, updated_at=?, admin_notified=0 WHERE id=? AND status='queued'",
                        (now, now, r["id"]))
            left -= r["n"]; out.append(r["id"])
        else:
            break
    return out

def promote_queue(now=None):
    """Public: promote what fits. Safe to call often (a cheap read first). Returns promoted ids."""
    now = now or int(time.time())
    c = db()
    try:
        if not c.execute("SELECT 1 FROM withdraws WHERE status='queued' LIMIT 1").fetchone():
            return []
    finally:
        c.close()
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            price = get_price(cur); pool = get_setting(cur, "pool_zyn", 0.0)
            ids = _promote(cur, now, price, pool)
            c.commit()
        except Exception:
            c.rollback(); raise
        finally:
            c.close()
    return ids

def queue_position(wid):
    """1-based position of a queued request, or None if it is not queued."""
    c = db()
    try:
        r = c.execute("SELECT status FROM withdraws WHERE id=?", (wid,)).fetchone()
        if not r or r["status"] != "queued":
            return None
        return c.execute("SELECT COUNT(*) FROM withdraws WHERE status='queued' AND id<=?", (wid,)).fetchone()[0]
    finally:
        c.close()

def user_queue(cur, uid):
    """Queue info for one user, or None. Position counts everybody queued with a smaller id (including the user)."""
    r = cur.execute("SELECT id, amount, COALESCE(net, amount) AS net FROM withdraws WHERE user_id=? AND status='queued' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    if not r:
        return None
    pos = cur.execute("SELECT COUNT(*) FROM withdraws WHERE status='queued' AND id<=?", (r["id"],)).fetchone()[0]
    total = cur.execute("SELECT COUNT(*) FROM withdraws WHERE status='queued'").fetchone()[0]
    return {"id": r["id"], "position": pos, "total": total, "amount": r["amount"], "net": r["net"]}

def unnotified_promotions():
    """Promoted-from-queue requests the admin has not been told about yet (the bot sends the buttons)."""
    c = db()
    try:
        return [dict(x) for x in c.execute("SELECT w.*, u.username AS username FROM withdraws w LEFT JOIN users u ON u.user_id=w.user_id "
                                           "WHERE w.status='pending' AND w.admin_notified=0 ORDER BY w.id").fetchall()]
    finally:
        c.close()

def mark_notified(wid):
    """Atomic: True only for the one caller that flips 0 -> 1 (no double notification)."""
    with _db_lock:
        c = db(); cur = c.cursor()
        cur.execute("UPDATE withdraws SET admin_notified=1 WHERE id=? AND admin_notified=0", (wid,))
        n = cur.rowcount; c.commit(); c.close()
    return n == 1

def queued_list():
    c = db()
    try:
        return [dict(x) for x in c.execute("SELECT w.id, w.user_id, w.amount, COALESCE(w.net,w.amount) AS net, w.queued_at, u.username AS username "
                                           "FROM withdraws w LEFT JOIN users u ON u.user_id=w.user_id WHERE w.status='queued' ORDER BY w.id").fetchall()]
    finally:
        c.close()

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
        cur.execute("UPDATE withdraws SET status='rejected', updated_at=? WHERE id=? AND status IN ('pending','sending','queued')", (now, wid))
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
    cur.execute("SELECT COUNT(*) FROM withdraws WHERE user_id=? AND status IN ('pending','sending','queued')", (uid,))
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
    c = db(); price = get_price(c); eff = eff_locked(c, uid, r["locked"]); c.close()
    lvl = level_from(eff, price)
    c = db(); uo = c.execute("SELECT id, amount FROM unlock_requests WHERE user_id=? AND status='pending'", (uid,)).fetchall(); c.close()
    uo_txt = ", ".join(f"#{x['id']}={x['amount']:,.0f}" for x in uo) or "-"
    return (f"👤 {uid} @{r['username'] or '-'}\n"
            f"balance: {r['balance'] or 0:,.2f}\n"
            f"pending: {r['pending'] or 0:,.2f}\n"
            f"wallet balance (stored, no level): {r['onchain'] or 0:,.2f}\n"
            f"wallet balance (live): {live_txt}\n"
            f"LOCKED: {r['locked'] or 0:,.2f} (counts: {eff:,.2f}) | pending unlock: {uo_txt}\n"
            f"price: {price:g} USD/ZYN\n"
            f"level: {lvl} | boost: x{r['boost'] or 1}\n"
            f"mining: {'ON' if r['mining'] else 'OFF'} (≈ {daily_reward(lvl, price, r['boost'], r['boost_level']):,.2f}/day)\n"
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
    """Pays the referral bonus ONCE to both sides, only after the invited friend has really LOCKED level 1
    (effective locked >= level 1 requirement at the current price). Max REFERRAL_DAILY_CAP paid referrals per inviter
    per 24h: above that the bonus waits (call again later, e.g. from /api/auth). Returns the referrer id if paid, else None."""
    uid = str(uid); now = int(time.time())
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            me = cur.execute("SELECT referred_by, ref_paid, locked FROM users WHERE user_id=?", (uid,)).fetchone()
            if not me or me["ref_paid"] != 0 or not me["referred_by"]:
                c.rollback(); return None
            ref = me["referred_by"]
            if level_from(eff_locked(cur, uid, me["locked"]), get_price(cur)) < 1:
                c.rollback(); return None
            if not cur.execute("SELECT 1 FROM users WHERE user_id=?", (ref,)).fetchone():
                c.rollback(); return None
            n = cur.execute("SELECT COUNT(*) FROM users WHERE referred_by=? AND ref_paid=1 AND ref_paid_at>=?", (ref, now - 86400)).fetchone()[0]
            if n >= REFERRAL_DAILY_CAP:
                c.rollback(); return None
            cur.execute("UPDATE users SET ref_paid=1, ref_paid_at=? WHERE user_id=? AND ref_paid=0", (now, uid))
            if cur.rowcount != 1:
                c.rollback(); return None
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
    """Atomic claim: pending -> balance. (The referral bonus is no longer paid here: it needs a level-1 LOCK.)
    Returns (amount_claimed, None) - the second value is kept so old callers still work."""
    uid = str(uid); now = int(time.time() * 1000)
    get_user(uid)  # accrues pending
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT pending FROM users WHERE user_id=?", (uid,))
            r = cur.fetchone(); got = (r["pending"] or 0) if r else 0
            cur.execute("UPDATE users SET balance=balance+pending, pending=0, last_tick=? WHERE user_id=?", (now, uid))
            c.commit()
        except Exception:
            c.rollback(); raise
        finally:
            c.close()
    return got, None


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
    c = db(); r2 = c.execute("SELECT locked FROM users WHERE user_id=?", (uid,)).fetchone()
    lvl = level_from(eff_locked(c, uid, r2["locked"] if r2 else 0), get_price(c)); c.close()
    if credited <= 0:
        return False, "no new lock transfer found (check amount, memo and sender wallet; it can take a minute)", lvl
    return True, credited, lvl


def request_unlock(uid, amount=None, now=None):
    """User asks to unlock. Nothing is paid by the server. From now the requested amount does NOT count (level, mining,
    limits) and the admin can only finish it after UNLOCK_DELAY (7 days): then he sends 70%, burns 30% by hand and runs
    /unlock_done. Returns (ok, error_or_row_dict)."""
    uid = str(uid); now = int(now or time.time()); nowms = int(time.time() * 1000)
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
            u = cur.fetchone()
            if not u:
                c.rollback(); return False, "no user"
            locked = eff_locked(cur, uid, u["locked"])
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
            _accrue_row(cur, u, nowms)             # pay mining up to now at the level BEFORE the amount stops counting
            cur.execute("INSERT INTO unlock_requests (user_id,amount,payout,burn,wallet,status,created_at,updated_at,unlock_at) VALUES (?,?,?,?,?,'pending',?,?,?)",
                        (uid, amt, payout, burn, u["wallet"], now, now, now + UNLOCK_DELAY))
            rid = cur.lastrowid
            c.commit()
        except Exception as e:
            c.rollback(); print("request_unlock error", e, flush=True); return False, "error"
        finally:
            c.close()
    return True, {"id": rid, "user_id": uid, "amount": amt, "payout": payout, "burn": burn, "wallet": u["wallet"],
                  "created_at": now, "unlock_at": now + UNLOCK_DELAY}


def complete_unlock(rid, now=None):
    """ADMIN ONLY (the bot checks ADMIN_ID). Call AFTER the admin sent the 70% and burned the 30% by hand.
    Atomically: pending -> done, locked -= amount (mining settled at the old level first). Once only."""
    now = int(now or time.time()); nowms = int(time.time() * 1000)
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
            if now < (r["unlock_at"] or 0):
                c.rollback(); return {"ok": False, "error": "too_early", "unlock_at": r["unlock_at"]}
            cur.execute("SELECT * FROM users WHERE user_id=?", (r["user_id"],))
            u = cur.fetchone()
            if not u or (u["locked"] or 0) + 1e-9 < r["amount"]:
                c.rollback(); return {"ok": False, "error": "locked_less_than_amount"}
            _accrue_row(cur, u, nowms)
            cur.execute("UPDATE users SET locked=MAX(0, locked-?) WHERE user_id=?", (r["amount"], r["user_id"]))
            cur.execute("UPDATE unlock_requests SET status='done', updated_at=? WHERE id=? AND status='pending'", (now, rid))
            cur.execute("SELECT locked FROM users WHERE user_id=?", (r["user_id"],))
            left = cur.fetchone()["locked"] or 0
            price_now = get_price(cur)
            c.commit()
        except Exception as e:
            c.rollback(); print("complete_unlock error", e, flush=True); return {"ok": False, "error": "error"}
        finally:
            c.close()
    return {"ok": True, "user_id": r["user_id"], "amount": r["amount"], "payout": r["payout"], "burn": r["burn"],
            "wallet": r["wallet"], "locked_left": left, "level": level_from(left, price_now)}


def reject_unlock(rid):
    """Admin cancels a pending unlock request. The amount counts again from now (mining settled first at the lower level)."""
    nowms = int(time.time() * 1000)
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            r0 = cur.execute("SELECT * FROM unlock_requests WHERE id=? AND status='pending'", (rid,)).fetchone()
            if r0:
                u = cur.execute("SELECT * FROM users WHERE user_id=?", (r0["user_id"],)).fetchone()
                if u:
                    _accrue_row(cur, u, nowms)
            cur.execute("UPDATE unlock_requests SET status='rejected', updated_at=? WHERE id=? AND status='pending'", (int(time.time()), rid))
            ok = cur.rowcount == 1
            c.commit()
        except Exception:
            c.rollback(); raise
        r = c.execute("SELECT * FROM unlock_requests WHERE id=?", (rid,)).fetchone(); c.close()
    return (ok, dict(r) if r else None)


def pending_unlocks():
    c = db(); rows = [dict(x) for x in c.execute("SELECT * FROM unlock_requests WHERE status='pending' ORDER BY id").fetchall()]; c.close()
    return rows


# ---------------- Boost (one boost, x1.5 at most) ----------------
def buy_boost(uid):
    """Buy (or upgrade) the single boost. Price = 15 days of the extra yield at the user's CURRENT level and price.
    Bonus is paid only on min(level, boost_level). Returns (ok, error_or_dict)."""
    uid = str(uid); nowms = int(time.time() * 1000)
    get_user(uid)
    with _db_lock:
        c = db(); cur = c.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            u = cur.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
            if not u:
                c.rollback(); return False, "no user"
            price = get_price(cur)
            lvl = level_from(eff_locked(cur, uid, u["locked"]), price)
            if lvl < 1:
                c.rollback(); return False, "lock at least level 1 first"
            cost = boost_cost(lvl, price, u["boost"], u["boost_level"])
            if cost <= 1e-9:
                c.rollback(); return False, "you already have the maximum boost for your level"
            _accrue_row(cur, u, nowms)
            cur.execute("UPDATE users SET balance=balance-?, boost=?, boost_level=? WHERE user_id=? AND balance>=?",
                        (cost, BOOST_MAX, lvl, uid, cost))
            if cur.rowcount != 1:
                c.rollback(); return False, "الرصيد غير كافٍ"
            c.commit()
        except Exception as e:
            c.rollback(); print("buy_boost error", e, flush=True); return False, "error"
        finally:
            c.close()
    return True, {"boost": BOOST_MAX, "boost_level": lvl, "cost": cost}


# ---------------- What the app is allowed to see (everything is computed here, the app only displays) ----------------
def economy_public():
    c = db()
    try:
        price = get_price(c); pool = get_setting(c, "pool_zyn", 0.0); min_usd = get_setting(c, "min_usd", DEFAULT_MIN_USD)
    finally:
        c.close()
    return {"price": price, "pool": pool, "minUsd": min_usd,
            "minWithdrawZyn": (min_usd / price) if price > 0 else None,
            "levelsUsd": [round(_REQ_USD[l], 8) for l in range(1, 501)],
            "yieldL1": YIELD_L1, "yieldL500": YIELD_L500, "yieldCap": YIELD_CAP,
            "feeL1": FEE_L1, "feeL500": FEE_L500, "boostMax": BOOST_MAX, "boostDays": BOOST_PAYBACK_DAYS,
            "budgetPct": DAILY_BUDGET_PCT, "requestPct": PER_REQUEST_PCT, "unlockDays": UNLOCK_DELAY // 86400,
            "unlockReturnPct": UNLOCK_RETURN_PCT, "refBonus": REFERRAL_BONUS, "refDailyCap": REFERRAL_DAILY_CAP,
            "taskRewards": dict(TASK_REWARDS), "invRequired": INV_REQUIRED, "cooldownHours": WITHDRAW_COOLDOWN // 3600,
            "channel": TASK_CHANNEL}


def user_economy(u):
    """Per-user numbers for /api/auth (u = row dict from get_user)."""
    now = int(time.time())
    try:
        promote_queue(now)                     # the queue moves even if nobody withdraws
    except Exception as e:
        print("promote_queue error", e, flush=True)
    c = db()
    try:
        price = get_price(c); pool = get_setting(c, "pool_zyn", 0.0)
        queue = user_queue(c, u["user_id"])
        eff = eff_locked(c, u["user_id"], u["locked"])
        lvl = level_from(eff, price)
        budget, used, left = withdraw_budget(c, now, pool)
    finally:
        c.close()
    per_day = daily_reward(lvl, price, u["boost"], u["boost_level"])
    return {"effLocked": eff, "level": lvl, "perDay": per_day,
            "fee": fee_rate(lvl) if lvl >= 1 else None,
            "maxRequest": eff * PER_REQUEST_PCT, "budget": budget, "budgetUsed": used, "budgetLeft": left,
            "boostCost": boost_cost(lvl, price, u["boost"], u["boost_level"]),
            "boostBonusPerDay": boost_bonus_daily(lvl, price),
            "boostLevel": u["boost_level"] or 0, "queue": queue}
