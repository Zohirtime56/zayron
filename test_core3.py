# Offline tests for Lock / Unlock (no network, no keys). Run: python3 test_core3.py
import os, tempfile, time, base64
d=tempfile.mkdtemp(); os.environ["ZYRON_DB"]=d+"/t.db"
def friendly(wc,h,bounce=False):
    import zyron_core as c
    b=bytes([0x11 if bounce else 0x51, wc&0xFF])+h+b""
    b+=c._crc16(b).to_bytes(2,"big"); return base64.urlsafe_b64encode(b).decode()
import zyron_core as core
ok=lambda c,m: print(("PASS " if c else "FAIL ")+m) or (c or exit(1))
HM,HL,HU,HV=[bytes([i])*32 for i in (1,2,3,4)]
core.JETTON_MASTER=friendly(0,HM,True); core.LOCK_WALLET=friendly(0,HL)
UW=friendly(0,HU); VW=friendly(0,HV)
ok(core.to_raw(UW)=="0:"+HU.hex() and core.to_raw("0:"+HU.hex().upper())=="0:"+HU.hex(),"address friendly/raw normalised")
ok(core.to_raw("UQ"+"A"*46) is None,"bad crc/address rejected")
# no network: make wallet link not call TonAPI
core.fetch_onchain=lambda w: 123.0
for u in ("1","2"): core.get_user(u,"u"+u)
ok(core.link_wallet("1",UW)[0] and core.link_wallet("2",VW)[0],"wallets linked")
def ev(key,sender,amt,comment,recip=None,master=None,status="ok",prog=False,dec=9):
    return {"event_id":key,"timestamp":1,"in_progress":prog,"actions":[{"type":"JettonTransfer","status":status,
      "JettonTransfer":{"sender":{"address":"0:"+sender.hex()},"recipient":{"address":"0:"+(recip or HL).hex()},
      "amount":str(int(amt*10**dec)),"comment":comment,"jetton":{"address":"0:"+(master or HM).hex(),"decimals":dec}}}]}
def row(q,*a):
    c=core.db(); r=c.execute(q,a).fetchone(); c.close(); return r
# purchase / wallet balance alone gives no level
ok(core.level_from(row("select locked from users where user_id='1'")[0])==1 and row("select onchain from users where user_id='1'")[0]==123,"wallet balance 123 -> locked 0, level 1")
# lock
e=[ev("tx1",HU,5000,"ZYNLOCK1")]
ok(core.claim_lock("1",e)[:2]==(True,5000.0),"valid lock credited 5000")
ok(row("select locked from users where user_id='1'")[0]==5000 and core.level_from(5000)==10,"locked=5000 -> level 10")
ok(core.claim_lock("1",e)[0] is False and row("select locked from users where user_id='1'")[0]==5000,"same tx NOT credited twice (idempotent)")
bad=[ev("b1",HU,100,"ZYNLOCK2"),            # wrong memo (other user's)
     ev("b2",HU,100,""),                     # no memo
     ev("b3",HV,100,"ZYNLOCK1"),             # right memo, sender is not user 1's wallet
     ev("b4",HU,100,"ZYNLOCK1",recip=HV),    # not sent to lock wallet
     ev("b5",HU,100,"ZYNLOCK1",master=HV),   # fake jetton
     ev("b6",HU,100,"ZYNLOCK1",status="failed"),
     ev("b7",HU,100,"ZYNLOCK1",prog=True),
     ev("b8",HU,0.5,"ZYNLOCK1")]            # below minimum
ok(core.claim_lock("1",bad)[0] is False and row("select locked from users where user_id='1'")[0]==5000,"8 invalid transfers all ignored")
ok(row("select locked from users where user_id='2'")[0]==0,"user 2 got nothing")
ok(core.claim_lock("1",e+[ev("tx2",HU,45000,"ZYNLOCK1")])[:2]==(True,45000.0) and core.level_from(50000)==50,"second tx adds, level 50")
c=core.db(); n=c.execute("select count(*) from locks").fetchone()[0]; c.close(); ok(n==2,"2 lock rows")
ok(core.link_wallet("1",VW)[0] is False and core.link_wallet("1","")[0] is False,"cannot change/disconnect wallet while locked")
# user without wallet / lock not configured
core.get_user("3","u3"); ok(core.claim_lock("3",e)==(False,"connect your wallet first",None),"no wallet -> refused")
# unlock request: nothing changes yet
ok(core.request_unlock("2")==(False,"nothing is locked"),"unlock with nothing locked refused")
ok(core.request_unlock("1",60000)[0] is False,"unlock more than locked refused")
ok(core.request_unlock("1",-5)[0] is False and core.request_unlock("1","abc")[0] is False,"bad amounts refused")
okk,r=core.request_unlock("1",10000)
ok(okk and r["payout"]==7000 and r["burn"]==3000,"unlock 10000 -> 7000 back, 3000 burn")
ok(row("select locked from users where user_id='1'")[0]==50000,"locked unchanged until admin confirms")
ok(core.request_unlock("1",1000)[0] is False,"second pending request refused")
# complete
res=core.complete_unlock(r["id"])
ok(res["ok"] and res["locked_left"]==40000 and res["level"]==core.level_from(40000),"admin done -> locked 40000, level drops")
ok(core.complete_unlock(r["id"])["error"]=="already_processed" and row("select locked from users where user_id='1'")[0]==40000,"cannot complete twice")
ok(core.complete_unlock(999)["error"]=="not_found","unknown id")
okk,r2=core.request_unlock("1"); ok(okk and r2["amount"]==40000,"default = unlock everything")
c=core.db(); c.execute("UPDATE users SET locked=100 WHERE user_id='1'"); c.commit(); c.close()
ok(core.complete_unlock(r2["id"])["error"]=="locked_less_than_amount","guard if locked dropped meanwhile")
ok(core.reject_unlock(r2["id"])[0] and core.pending_unlocks()==[],"reject works, nothing pending")
# mining uses locked only
c=core.db(); c.execute("UPDATE users SET locked=5000, mining=1, last_tick=? WHERE user_id='2'",(int(time.time()*1000)-86400000,)); c.commit(); c.close()
u=core.get_user("2"); ok(abs(u["pending"]-core.reward_for(10))<5,"mining speed from locked level 10 (~%d/day)"%core.reward_for(10))
# lock disabled
core.LOCK_WALLET=""; ok(core.claim_lock("1",e)[0] is False,"lock disabled without ZYN_LOCK_WALLET")
# no private-key handling in the code
src=open(os.path.join(os.path.dirname(os.path.abspath(__file__)),"zyron_core.py")).read().lower()
ok("mnemonic" not in src and "seed" not in src and "private_key" not in src and "sendboc" not in src,"no key/seed/send code in core")
print("ALL OK")
