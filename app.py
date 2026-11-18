"""SmartShelf ONE FILE (server + database + page), ready for Render. Local run: python app.py"""
import asyncio, base64, os, re, secrets, sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
KEY, FROM = os.getenv("YCLOUD_API_KEY", ""), os.getenv("YCLOUD_FROM", "")
SHOP = os.getenv("SHOP_NAME", "the shop")
TEMPLATE, TLANG = os.getenv("YCLOUD_TEMPLATE", ""), os.getenv("YCLOUD_TEMPLATE_LANG", "en")
try:
    TZ = ZoneInfo("Africa/Kigali")
except Exception:  # Windows without tzdata: Rwanda is always UTC+2 (no daylight saving)
    TZ = timezone(timedelta(hours=2))
DB = Path(os.getenv("DB_PATH", BASE / "smartshelf.db"))
PASSWORD = os.getenv("APP_PASSWORD", "")  # set on the host; empty = no login (local use only)

MSG = {
    "en": "Hello {name}, a friendly reminder from {shop}: you owe {amount} RWF, due on {due}. Thank you.",
    "rw": "Muraho {name}, turabibutsa ko mufite umwenda wa {amount} RWF mugomba kwishyura {shop} bitarenze {due}. Murakoze cyane.",
    "fr": "Bonjour {name}, petit rappel de {shop} : vous devez {amount} RWF, à payer avant le {due}. Merci beaucoup.",
}
SCHEMA = """
create table if not exists debts(id integer primary key autoincrement, name text not null, phone text not null,
 amount integer not null, lang text not null default 'en', due_date text not null, status text not null default 'open',
 reminded_count integer not null default 0, last_reminded_at text, last_try text, created_at text not null);
create table if not exists reminder_log(id integer primary key autoincrement, debt_id integer, sent_at text, ok integer, detail text);
create table if not exists products(id integer primary key autoincrement, name text not null, cost integer not null,
 price integer not null, qty integer not null);
"""

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def now(): return datetime.now(TZ)

def message(d):
    return MSG[d["lang"]].format(name=d["name"], shop=SHOP, amount=f'{d["amount"]:,}', due=d["due_date"])

def norm_phone(p):
    """Clean a phone number. Rwanda numbers must end up as +2507XXXXXXXX (9 digits after +250)."""
    p = re.sub(r"[^\d+]", "", p)
    if p.startswith("00"): p = "+" + p[2:]
    if re.fullmatch(r"07\d{8}", p): p = "+250" + p[1:]
    if re.fullmatch(r"7\d{8}", p): p = "+250" + p
    if re.fullmatch(r"250\d{9,10}", p): p = "+" + p
    if re.fullmatch(r"\+2500\d{9}", p): p = "+250" + p[5:]  # stray 0 after +250
    if p.startswith("+250") and not re.fullmatch(r"\+2507\d{8}", p):
        raise HTTPException(422, "Rwanda numbers look like +250788123456 (9 digits after +250, starting with 7)")
    if not re.fullmatch(r"\+\d{9,15}", p):
        raise HTTPException(422, "Enter the WhatsApp number with country code, like +250788123456")
    return p

async def send(d):
    """Send one reminder through YCloud and log the result. Raises on failure."""
    with closing(db()) as c, c:
        c.execute("update debts set last_try=? where id=?", (now().date().isoformat(), d["id"]))
    err = None
    if not (KEY and FROM):
        err = "Fill YCLOUD_API_KEY and YCLOUD_FROM in .env"
    else:
        if TEMPLATE:
            params = [{"type": "text", "text": t} for t in (d["name"], f'{d["amount"]:,}', d["due_date"])]
            payload = {"from": FROM, "to": d["phone"], "type": "template", "template": {
                "name": TEMPLATE, "language": {"code": TLANG}, "components": [{"type": "body", "parameters": params}]}}
        else:
            payload = {"from": FROM, "to": d["phone"], "type": "text", "text": {"body": message(d)}}
        try:
            async with httpx.AsyncClient(timeout=20) as h:
                r = await h.post("https://api.ycloud.com/v2/whatsapp/messages/sendDirectly",
                                 headers={"X-API-Key": KEY}, json=payload)
            if r.status_code >= 300:
                err = f"YCloud {r.status_code}: {r.text[:900]}"
                if "(#100)" in r.text:
                    err += "  HINT: check the number (+250 then 9 digits starting with 7, and it must have WhatsApp)."
                elif "131047" in r.text or "24" in r.text and "hour" in r.text.lower():
                    err += "  HINT: this person must message you first, or you must use an approved template."
        except httpx.HTTPError as e:
            err = f"Network error: {e}"
    with closing(db()) as c, c:
        c.execute("insert into reminder_log(debt_id,sent_at,ok,detail) values(?,?,?,?)",
                  (d["id"], now().isoformat(timespec="seconds"), 0 if err else 1, err or "sent"))
        if not err:
            c.execute("update debts set reminded_count=reminded_count+1,last_reminded_at=? where id=?",
                      (now().isoformat(timespec="seconds"), d["id"]))
    if err: raise HTTPException(502, err)

async def scheduler():
    """Every minute: open debts whose due date has come and were never reminded get one automatic reminder (one try per day)."""
    while True:
        try:
            today = now().date().isoformat()
            with closing(db()) as c:
                rows = c.execute("select * from debts where status='open' and reminded_count=0 and due_date<=? "
                                 "and coalesce(last_try,'')<?", (today, today)).fetchall()
            for r in rows:
                try: await send(dict(r))
                except HTTPException as e: print("Auto reminder failed:", r["name"], e.detail)
        except Exception as e:
            print("Scheduler error:", e)
        await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(_):
    with closing(db()) as c, c:
        c.executescript(SCHEMA)
        for r in c.execute("select id, phone from debts where status='open'").fetchall():  # repair saved numbers
            try:
                fixed = norm_phone(r["phone"])
                if fixed != r["phone"]: c.execute("update debts set phone=? where id=?", (fixed, r["id"]))
            except HTTPException:
                pass
        if not c.execute("select 1 from debts").fetchone() and not c.execute("select 1 from products").fetchone():
            far = (now() + timedelta(days=30)).date().isoformat()
            c.executemany("insert into products(name,cost,price,qty) values(?,?,?,?)",
                          [("Sugar 1kg", 800, 1000, 3), ("Rice 5kg", 5500, 6500, 20), ("Cooking oil 1L", 2200, 2800, 4)])
            c.execute("insert into debts(name,phone,amount,lang,due_date,created_at) values(?,?,?,?,?,?)",
                      ("Sample customer", "+250788000000", 5000, "en", far, now().isoformat(timespec="seconds")))
    t = asyncio.create_task(scheduler())
    yield
    t.cancel()

app = FastAPI(title="SmartShelf", lifespan=lifespan)

@app.middleware("http")
async def gate(request, call_next):
    """Password gate (browser login popup). /healthz stays open so a free pinger can keep the app awake."""
    if PASSWORD and request.url.path != "/healthz":
        ok, h = False, request.headers.get("authorization", "")
        if h.startswith("Basic "):
            try:
                pw = base64.b64decode(h[6:]).decode().partition(":")[2]
                ok = secrets.compare_digest(pw.encode(), PASSWORD.encode())
            except Exception:
                pass
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="SmartShelf"'})
    return await call_next(request)

@app.get("/healthz")
def healthz(): return {"ok": True}

class DebtIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    phone: str
    amount: int = Field(gt=0)
    lang: str = Field(pattern="^(en|rw|fr)$")
    due_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    cost: int = Field(ge=0)
    price: int = Field(ge=0)
    qty: int = Field(ge=0)

class QtyIn(BaseModel):
    qty: int = Field(ge=0)

def get_debt(i):
    with closing(db()) as c: r = c.execute("select * from debts where id=?", (i,)).fetchone()
    if not r: raise HTTPException(404, "Debt not found")
    return dict(r)

@app.get("/api/state")
def state():
    with closing(db()) as c:
        debts = [dict(r) for r in c.execute("select * from debts where status='open' order by due_date, id")]
        products = [dict(r) for r in c.execute("select * from products order by name")]
    for d in debts: d["preview"] = message(d)
    return {"debts": debts, "products": products, "today": now().date().isoformat(),
            "stats": {"open": len(debts), "total": sum(d["amount"] for d in debts),
                      "reminded": sum(1 for d in debts if d["reminded_count"])},
            "whatsapp_ready": bool(KEY and FROM)}

@app.post("/api/debts")
def add_debt(d: DebtIn):
    try: datetime.strptime(d.due_date, "%Y-%m-%d")
    except ValueError: raise HTTPException(422, "Pick a valid due date")
    with closing(db()) as c, c:
        c.execute("insert into debts(name,phone,amount,lang,due_date,created_at) values(?,?,?,?,?,?)",
                  (d.name.strip(), norm_phone(d.phone), d.amount, d.lang, d.due_date, now().isoformat(timespec="seconds")))
    return {"ok": True}

@app.post("/api/debts/{i}/remind")
async def remind(i: int):
    d = get_debt(i); await send(d); return {"sent": True, "message": message(d)}

@app.post("/api/debts/{i}/paid")
def paid(i: int):
    get_debt(i)
    with closing(db()) as c, c: c.execute("update debts set status='paid' where id=?", (i,))
    return {"ok": True}

@app.post("/api/products")
def add_product(p: ProductIn):
    with closing(db()) as c, c:
        c.execute("insert into products(name,cost,price,qty) values(?,?,?,?)", (p.name.strip(), p.cost, p.price, p.qty))
    return {"ok": True}

@app.post("/api/products/{i}/qty")
def set_qty(i: int, q: QtyIn):
    with closing(db()) as c, c: c.execute("update products set qty=? where id=?", (q.qty, i))
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def index(): return PAGE

PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SmartShelf</title>
<style>
:root{--bg:#f1f5ee;--surface:#fff;--ink:#10281c;--muted:#58665e;--line:#d9e2d6;--bar:#0f3b27;--accent:#1b8a4b;--tab:#c8f1d6;--ok:#1b7a45;--warn:#c2410c;--bubble:#dff3e5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.45 "Schibsted Grotesk",system-ui,sans-serif}
header{background:var(--bar);color:#fff;display:flex;justify-content:space-between;align-items:center;padding:14px clamp(16px,4vw,40px);gap:12px;flex-wrap:wrap}
.brand{font-weight:800;font-size:24px;letter-spacing:-.02em}
nav{display:flex;gap:6px}
nav button{font:inherit;font-weight:600;color:#fff;background:none;border:0;padding:8px 18px;border-radius:999px;cursor:pointer;opacity:.75}
nav button[aria-selected=true]{background:var(--tab);color:var(--bar);opacity:1}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
main{max-width:1100px;margin:0 auto;padding:28px clamp(16px,4vw,40px) 48px}
h1{font-size:clamp(28px,4vw,40px);letter-spacing:-.025em;margin:0 0 4px;font-weight:800}
.sub{color:var(--muted);margin:0 0 22px}
.banner{background:#fde8df;color:#7c2d12;border-radius:10px;padding:10px 14px;margin-bottom:18px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:22px}
.stat,.panel{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.stat span{color:var(--muted);font-size:15px}.stat b{display:block;font-size:30px;font-weight:800}
.grid{display:grid;grid-template-columns:1.15fr 1fr;gap:22px;align-items:start}
@media(max-width:860px){.grid{grid-template-columns:1fr}}
h2{font-size:20px;margin:0 0 12px}
.row{display:grid;grid-template-columns:1fr auto auto;gap:12px;align-items:center;width:100%;padding:12px 8px;border:0;border-top:1px solid var(--line);background:none;font:inherit;color:inherit;text-align:left;cursor:pointer;border-radius:8px}
.row:first-of-type{border-top:0}.row[aria-pressed=true]{background:var(--bubble)}
.row small{display:block;color:var(--muted);font-size:14px}.amt{font-weight:800}
.chip{font-size:14px;font-weight:600;padding:3px 10px;border-radius:999px;border:1px solid;white-space:nowrap}
.chip.sent{color:var(--ok)}.chip.wait{color:var(--warn)}
form{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px;padding-top:16px;border-top:1px solid var(--line)}
input,select{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px 12px;flex:1 1 140px;min-width:0}
label.f{display:flex;flex-direction:column;font-size:14px;color:var(--muted);flex:1 1 140px}label.f input,label.f select{flex:none}
.btn{font:inherit;font-weight:700;border:0;border-radius:10px;padding:11px 18px;cursor:pointer;background:var(--accent);color:#fff}
.btn.ghost{background:none;color:var(--ink);border:1px solid var(--line)}.btn[disabled]{opacity:.5}
.bubble{background:var(--bubble);border-radius:14px 14px 14px 4px;padding:14px 16px;min-height:88px;font-size:18px;margin:12px 0}
.note{font-size:14px;color:var(--muted)}.note.err{color:var(--warn)}
.actions{display:flex;gap:10px;flex-wrap:wrap}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 8px;border-top:1px solid var(--line)}
th{font-size:15px;color:var(--muted);border-top:0}.n{text-align:right}.low{color:var(--warn);font-weight:600}
.q{display:inline-flex;gap:8px;align-items:center}.q button{width:32px;height:32px;border-radius:8px;border:1px solid var(--line);background:none;font:inherit;cursor:pointer}
[hidden]{display:none!important}
</style></head><body>
<header><div class="brand">SmartShelf</div>
<nav role="tablist"><button role="tab" id="t-stock" aria-selected="false">Stock</button><button role="tab" id="t-credits" aria-selected="true">Credits</button></nav></header>
<main>
<section id="credits">
 <h1>Credits</h1><p class="sub">Who owes the shop money. Reminders go out on WhatsApp on the due date.</p>
 <div class="banner" id="nokey" hidden>WhatsApp is not set up. Fill YCLOUD_API_KEY and YCLOUD_FROM in the .env file and restart.</div>
 <div class="stats"><div class="stat"><span>Open debts</span><b id="s-open">0</b></div><div class="stat"><span>Total owed</span><b id="s-total">0 RWF</b></div><div class="stat"><span>Reminders sent</span><b id="s-sent">0</b></div></div>
 <div class="grid">
  <div class="panel"><h2>Customers who owe</h2><div id="list"></div>
   <form id="add">
    <label class="f">Customer name<input id="n" required maxlength="40"></label>
    <label class="f">WhatsApp number<input id="p" type="tel" placeholder="+250788123456" required></label>
    <label class="f">Amount (RWF)<input id="a" type="number" min="1" required></label>
    <label class="f">Due date (reminder day)<input id="d" type="date" required></label>
    <label class="f">Message language<select id="l"><option value="rw">Kinyarwanda</option><option value="en">English</option><option value="fr">Français</option></select></label>
    <button class="btn" type="submit" style="align-self:flex-end">Add debt</button>
   </form><p class="note err" id="formerr"></p></div>
  <div class="panel"><h2>WhatsApp reminder</h2>
   <p class="note" id="hint">Pick a customer to see the message.</p>
   <div id="composer" hidden><div>To <strong id="to"></strong> <span class="note" id="ph"></span></div>
    <div class="bubble" id="msg"></div><p class="note" id="status"></p>
    <div class="actions"><button class="btn" id="send">Send reminder now</button><button class="btn ghost" id="paid">Mark as paid</button></div></div></div>
 </div>
</section>
<section id="stock" hidden>
 <h1>Stock</h1><p class="sub">Products running low (under 5) are flagged so you restock before they run out.</p>
 <div class="panel"><table><thead><tr><th>Product</th><th class="n">Bought at</th><th class="n">Price</th><th class="n">In stock</th></tr></thead><tbody id="rows"></tbody></table>
  <form id="addp"><input id="pn" placeholder="Product name" required maxlength="40"><input id="pc" type="number" min="0" placeholder="Bought at (RWF)" required><input id="pp" type="number" min="0" placeholder="Price (RWF)" required><input id="pq" type="number" min="0" placeholder="Quantity" required><button class="btn" type="submit">Add product</button></form></div>
</section></main>
<script>
const $=i=>document.getElementById(i), fmt=n=>Number(n).toLocaleString("en-US");
let S=null, sel=null;
async function api(path,method="GET",body){
  const r=await fetch("/api"+path,{method,headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):undefined});
  const j=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(typeof j.detail==="string"?j.detail:(j.detail&&j.detail[0]&&j.detail[0].msg)||"Something went wrong");
  return j;
}
function el(tag,cls,text){const e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e}
async function load(){
  S=await api("/state");
  $("nokey").hidden=S.whatsapp_ready; $("s-open").textContent=S.stats.open; $("s-total").textContent=fmt(S.stats.total)+" RWF"; $("s-sent").textContent=S.stats.reminded;
  if(!$("d").value) $("d").value=S.today;
  const box=$("list"); box.textContent="";
  if(!S.debts.length) box.appendChild(el("p","note","No open debts. Add one below."));
  S.debts.forEach(d=>{
    const b=el("button","row"); b.type="button"; b.setAttribute("aria-pressed",sel===d.id);
    const l=el("span",null,d.name); l.appendChild(el("small",null,"Due "+d.due_date));
    const sent=d.reminded_count>0;
    b.append(l,el("span","amt",fmt(d.amount)+" RWF"),el("span","chip "+(sent?"sent":"wait"),sent?"Reminder sent":"Not yet reminded"));
    b.onclick=()=>{sel=d.id; $("status").textContent=""; $("status").className="note"; show(); load()};
    box.appendChild(b);
  });
  show();
  const tb=$("rows"); tb.textContent="";
  S.products.forEach(p=>{
    const tr=el("tr"); tr.append(el("td",null,p.name),el("td","n",fmt(p.cost)+" RWF"),el("td","n",fmt(p.price)+" RWF"));
    const td=el("td","n"), q=el("span","q"+(p.qty<5?" low":"")); 
    const m=el("button",null,"−"), pl=el("button",null,"+");
    m.onclick=()=>api("/products/"+p.id+"/qty","POST",{qty:Math.max(0,p.qty-1)}).then(load);
    pl.onclick=()=>api("/products/"+p.id+"/qty","POST",{qty:p.qty+1}).then(load);
    q.append(m,el("span",null,p.qty+(p.qty<5?" (low)":"")),pl); td.appendChild(q); tr.appendChild(td); tb.appendChild(tr);
  });
}
function show(){
  const d=S.debts.find(x=>x.id===sel); $("hint").hidden=!!d; $("composer").hidden=!d; if(!d) return;
  $("to").textContent=d.name; $("ph").textContent=d.phone; $("msg").textContent=d.preview;
}
$("send").onclick=async()=>{
  const st=$("status"); $("send").disabled=true; st.className="note"; st.textContent="Sending…";
  try{await api("/debts/"+sel+"/remind","POST"); st.textContent="Sent on WhatsApp."; await load()}
  catch(e){st.className="note err"; st.textContent=e.message}
  $("send").disabled=false;
};
$("paid").onclick=async()=>{await api("/debts/"+sel+"/paid","POST"); sel=null; load()};
$("add").onsubmit=async e=>{
  e.preventDefault(); $("formerr").textContent="";
  try{await api("/debts","POST",{name:$("n").value,phone:$("p").value,amount:+$("a").value,lang:$("l").value,due_date:$("d").value});
    $("n").value=$("p").value=$("a").value=""; await load()}
  catch(x){$("formerr").textContent=x.message}
};
$("addp").onsubmit=async e=>{e.preventDefault(); await api("/products","POST",{name:$("pn").value,cost:+$("pc").value,price:+$("pp").value,qty:+$("pq").value}); e.target.reset(); load()};
function tab(c){$("credits").hidden=!c;$("stock").hidden=c;$("t-credits").setAttribute("aria-selected",c);$("t-stock").setAttribute("aria-selected",!c)}
$("t-credits").onclick=()=>tab(true); $("t-stock").onclick=()=>tab(false);
load();
</script></body></html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", 8001)))
