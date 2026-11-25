import os, json, telebot
# Token: first try env, then token.txt (persists across consoles)
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    try:
        TOKEN = open("token.txt").read().strip()
    except:
        TOKEN = ""
WEBAPP_URL = "https://zohirtime56.github.io/zayron/"
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
if not ADMIN_ID:
    try:
        ADMIN_ID = open("admin.txt").read().strip()
    except:
        ADMIN_ID = ""
if not TOKEN or ":" not in TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN is missing. Create token.txt with your bot token.")
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)
DATA_FILE = "zyron_data.json"
def load():
    try: return json.load(open(DATA_FILE))
    except: return {}
def save(d): json.dump(d, open(DATA_FILE,"w"))

@bot.message_handler(commands=['start'])
def start(m):
    uid=str(m.from_user.id); data=load()
    # Handle withdraw deep-link: /start wd_10000_UQ...
    _parts = m.text.split()
    if len(_parts)>1 and _parts[1].startswith('wd_'):
        try:
            _, _amt, _wallet = _parts[1].split('_',2)
            _amt=int(_amt)
            bot.reply_to(m, f"✅ تم استلام طلب السحب: {_amt} ZYN\nمحفظة: {_wallet[:12]}...\nسيصلك خلال 24 ساعة.\nWithdraw request received: {_amt} ZYN")
            if ADMIN_ID:
                try: bot.send_message(int(ADMIN_ID), f"🔔 طلب سحب جديد\nUser: {uid} (@{m.from_user.username})\nAmount: {_amt} ZYN\nWallet: {_wallet}")
                except: pass
            return
        except Exception as e:
            print("wd parse err", e)
    is_new = uid not in data
    if is_new:
        data[uid]={"balance":0,"refs":0,"refEarn":0,"boost":1,"wallet":""}
        parts=m.text.split()
        if len(parts)>1:
            ref=parts[1].strip()
            if ref in data and ref!=uid:
                data[ref]["balance"]+=50
                data[ref]["refs"]+=1
                data[ref]["refEarn"]+=50
                try: bot.send_message(int(ref), "🎉 صديق جديد انضم من رابطك! +50 ZYN\nNew friend joined via your link! +50 ZYN")
                except: pass
        save(data)
    kb=telebot.types.InlineKeyboardMarkup()
    kb.add(telebot.types.InlineKeyboardButton(text="⛏️ افتح التعدين / Open Miner", web_app=telebot.types.WebAppInfo(WEBAPP_URL)))
    kb.add(telebot.types.InlineKeyboardButton(text="💸 سحب / Withdraw", callback_data="wd"), telebot.types.InlineKeyboardButton(text="🔗 إحالة / Referral", callback_data="ref"))
    kb.add(telebot.types.InlineKeyboardButton(text="🚀 تعزيز / Boost", callback_data="boost"))
    bot.reply_to(m, f"مرحبا في ZYRON Miner ⛏️\nرصيدك: {data[uid]['balance']} ZYN | إحالاتك: {data[uid]['refs']} | تعزيز: x{data[uid]['boost']}\nWelcome to ZYRON Miner\n/start • /balance • /withdraw • /referral • /boost", reply_markup=kb)

@bot.message_handler(commands=['balance'])
def bal(m):
    data=load(); u=data.get(str(m.from_user.id),{"balance":0,"refs":0,"boost":1})
    bot.reply_to(m, f"رصيدك في البوت: {u['balance']} ZYN\n⚠️ الرصيد الحقيقي هو في التطبيق (Mini App)، وليس هنا. افتح التطبيق لترى رصيدك.\nYour real balance is in the Mini App, not here. Open the Mini App to see it.\nرصيدك: {u['balance']} ZYN\nإحالاتك: {u['refs']} | تعزيز: x{u['boost']}\nYour balance: {u['balance']} ZYN")

@bot.message_handler(commands=['referral'])
def ref_cmd(m):
    uid=str(m.from_user.id); data=load(); u=data.get(uid,{"balance":0,"refs":0,"refEarn":0})
    link=f"https://t.me/ZyronMinerBot?start={uid}"
    bot.reply_to(m, f"🔗 رابط إحالتك:\n{link}\n\nأصدقاء: {u['refs']} | أرباح: {u['refEarn']} ZYN\nFriends: {u['refs']} | Earnings: {u['refEarn']} ZYN")

@bot.message_handler(commands=['withdraw'])
def wd_cmd(m):
    bot.reply_to(m, "💸 للسحب:\n1. افتح التطبيق من الزر أسفل\n2. اذهب إلى قسم السحب\n3. أدخل محفظتك (UQ... / EQ...) والمبلغ (الحد الأدنى 10,000 ZYN)\n\nTo withdraw:\n1. Open the Mini App\n2. Go to Withdraw\n3. Enter your wallet and amount (min 10,000 ZYN)")

@bot.message_handler(commands=['boost'])
def boost_cmd(m):
    bot.reply_to(m, "🚀 التعزيز:\n• x2 = 1,000 ZYN\n• x5 = 5,000 ZYN\n• x10 = 10,000 ZYN\n\nاذهب إلى قسم التعزيز في التطبيق للشراء.\nGo to Boost section in the Mini App to buy.")

@bot.callback_query_handler(func=lambda c: True)
def cb(c):
    if c.data=="wd": bot.answer_callback_query(c.id, "افتح التطبيق ← قسم السحب / Open App → Withdraw")
    elif c.data=="ref":
        uid=str(c.from_user.id); bot.answer_callback_query(c.id, f"رابطك: https://t.me/ZyronMinerBot?start={uid}")
    elif c.data=="boost": bot.answer_callback_query(c.id, "افتح التطبيق ← قسم التعزيز / Open App → Boost")
    try: bot.answer_callback_query(c.id)
    except: pass

@bot.message_handler(content_types=['web_app_data'])
def web_data(m):
    try:
        d=json.loads(m.web_app_data.data)
        uid=str(m.from_user.id); data=load()
        if uid not in data: data[uid]={"balance":0,"refs":0,"refEarn":0,"boost":1,"wallet":""}
        if d.get('type')=='withdraw':
            amt=int(d.get('amount',0)); wallet=d.get('wallet','')
            data[uid]['wallet']=wallet
            save(data)
            bot.reply_to(m, f"✅ تم استلام طلب السحب: {amt} ZYN\nمحفظة: {wallet[:10]}...\nسيصلك خلال 24 ساعة.\nWithdraw request received: {amt} ZYN")
            if ADMIN_ID:
                try: bot.send_message(int(ADMIN_ID), f"🔔 طلب سحب جديد\nUser: {uid} (@{m.from_user.username})\nAmount: {amt} ZYN\nWallet: {wallet}")
                except: pass
        elif d.get('type')=='boost':
            mult=int(d.get('mult',1)); data[uid]['boost']=mult; save(data)
            bot.reply_to(m, f"✅ تم تفعيل التعزيز x{mult}\nBoost x{mult} activated")
    except Exception as e:
        print("web_data error", e)

print("ZYRON Bot running...")
bot.infinity_polling()
