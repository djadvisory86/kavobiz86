"""
☕ КавоБот — облік кавового бізнесу Володимир + Вигран
Запуск: python bot.py
Потрібно: pip install python-telegram-bot aiohttp openpyxl
"""
import json, os, re, logging, tempfile
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import aiohttp

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
# КОНФІГ — ЗАМІНИ ПЕРЕД ЗАПУСКОМ
# ══════════════════════════════════════════════════════
BOT_TOKEN    = "8414849953:AAFeewGPh0BNSWhdY5jGkNdVgFeWVVt51sU"
GROQ_API_KEY = "ВАШ_GROQ_KEY"   # console.groq.com — безкоштовно

ID_VOLODYMYR = 373296886   # свій ID з @userinfobot
ID_VYGRAN    = 987654321   # ID Вигрна

DEBT_ALERT   = 15000   # нагадування якщо борг більше цієї суми
CONFIRM_SUM  = 5000    # підтвердження обома якщо сума більше

DATA_FILE  = "data.json"
PIN_FILE   = "pins.json"
EXPORT_DIR = "exports"

# ══════════════════════════════════════════════════════
# ЦІНИ
# ══════════════════════════════════════════════════════
PRICES = {
    "кава":      {"buy": 530, "sell": 700, "my": 530, "his": 630},
    "комплект":  {"buy": 680, "sell": 850, "my": 680, "his": 780},
    "молоко":    {"buy": 0,   "sell": 0,   "my": 0,   "his": 100},
    "айріш":     {"buy": 0,   "sell": 0,   "my": 0,   "his": 100},
    "стакан110": {"buy": 0,   "sell": 0,   "my": 0,   "his": 0},
    "стакан250": {"buy": 0,   "sell": 0,   "my": 0,   "his": 0},
}

DEFAULT_POINTS = {}  # Точки додаються через бота командою /points

# ══════════════════════════════════════════════════════
# БАЗА ДАНИХ
# ══════════════════════════════════════════════════════
def load() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "points": DEFAULT_POINTS.copy(),
        "transactions": [],
        "stock": {"кава": 0, "комплект": 0, "молоко": 0, "айріш": 0, "стакан110": 0, "стакан250": 0},
        "balance": 0.0,
        # balance > 0 = Вигран винен Володимиру
        # balance < 0 = Володимир винен Вигрну
        "weekly_sent": "",
        "stats": {
            "my":  {"кава": 0, "комплект": 0, "молоко": 0, "айріш": 0},
            "his": {"кава": 0, "комплект": 0, "молоко": 0, "айріш": 0},
        }
    }

def save(d: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def load_pins() -> dict:
    if os.path.exists(PIN_FILE):
        with open(PIN_FILE) as f:
            return json.load(f)
    return {}

def save_pins(p: dict):
    with open(PIN_FILE, "w") as f:
        json.dump(p, f)

def add_tx(d: dict, ttype: str, desc: str, amount: float, delta: float, meta: dict = None):
    tx = {
        "id": len(d["transactions"]) + 1,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "type": ttype, "description": desc,
        "amount": round(amount, 2),
        "balance_delta": round(delta, 2),
        "meta": meta or {},
    }
    d["transactions"].append(tx)
    d["balance"] = round(d.get("balance", 0) + delta, 2)
    save(d)
    return tx

# ══════════════════════════════════════════════════════
# ДОПОМІЖНІ ФУНКЦІЇ
# ══════════════════════════════════════════════════════
def ok(uid): return uid in (ID_VOLODYMYR, ID_VYGRAN)
def uname(uid): return "Володимир" if uid == ID_VOLODYMYR else "Коля"
def other_id(uid): return ID_VYGRAN if uid == ID_VOLODYMYR else ID_VOLODYMYR
def fm(v): return f"{abs(v):,.0f} грн".replace(",", " ")

def bal_line(b: float) -> str:
    if abs(b) < 1: return "✅ Рахунки зведені"
    if b > 0: return f"🔴 Вигран винен Володимиру: *{fm(b)}*"
    return f"🔴 Володимир винен Коля: *{fm(b)}*"

def find_point(name: str, points: dict) -> tuple[str, str] | tuple[None, None]:
    name_l = name.lower().strip()
    for p, owner in points.items():
        if name_l == p.lower() or name_l in p.lower() or p.lower() in name_l:
            return p, owner
    return None, None

# ══════════════════════════════════════════════════════
# СТАН ДІАЛОГІВ
# ══════════════════════════════════════════════════════
PIN_WAIT   = {}   # uid -> "set" | "check"
QUICK      = {}   # uid -> {step, ...}
PENDING    = {}   # key -> {desc, amount, confirmed_by, payer, comment}

# ══════════════════════════════════════════════════════
# AI — GROQ (безкоштовно)
# ══════════════════════════════════════════════════════
AI_PROMPT = """Ти асистент для обліку кавового бізнесу. Розпізнай операцію і поверни ТІЛЬКИ JSON.

Точки Володимира: Мурчик, Вдов, Сухопара, Гордівка, Торканівка
Точки Вигрна: Оляниця, Мамина вишня, Клуб, Ася, Ободівка агро, Ковалівка, Корпуса

Типи: sale=продаж, supply=постачання товару, payment=виплата грошей,
      rent=оренда терміналів, equipment=обладнання, delivery=доставка, unknown=незрозуміло

Товари: кава, комплект, молоко, айріш, стакан110, стакан250

Поверни JSON (без markdown):
{
  "type": "sale|supply|payment|rent|equipment|delivery|unknown",
  "point": "назва або null",
  "items": {"кава": 5},
  "amount": 1500,
  "payer": "volodymyr|vygran|null",
  "comment": "опис"
}"""

async def ai_parse(text: str) -> dict | None:
    if not GROQ_API_KEY or "GROQ" in GROQ_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "llama3-8b-8192",
                    "messages": [{"role": "system", "content": AI_PROMPT}, {"role": "user", "content": text}],
                    "temperature": 0.1, "max_tokens": 200,
                }
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    raw = data["choices"][0]["message"]["content"].strip()
                    raw = re.sub(r"```json|```", "", raw).strip()
                    return json.loads(raw)
    except Exception as e:
        logger.error(f"AI: {e}")
    return None

async def transcribe(path: str) -> str | None:
    if not GROQ_API_KEY or "GROQ" in GROQ_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            with open(path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("file", f, filename="audio.ogg", content_type="audio/ogg")
                form.add_field("model", "whisper-large-v3")
                form.add_field("language", "uk")
                async with s.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    data=form
                ) as r:
                    if r.status == 200:
                        res = await r.json()
                        return res.get("text", "")
    except Exception as e:
        logger.error(f"Voice: {e}")
    return None

# ══════════════════════════════════════════════════════
# ОБРОБКА ОПЕРАЦІЙ
# ══════════════════════════════════════════════════════
async def do_sale(point: str, owner: str, items: dict, d: dict) -> str:
    lines = [f"☕ *Продаж — {point}* ({'моя' if owner == 'volodymyr' else 'Вигрна'})\n"]
    total_pay = 0
    total_rev = 0
    for good, qty in items.items():
        p = PRICES.get(good)
        if not p:
            continue
        sell = p.get("sell", 0)
        pay  = p["my"] if owner == "volodymyr" else p["his"]
        rev  = sell * qty
        total_pay += pay * qty
        total_rev += rev
        lines.append(f"  {good}: {qty} шт × {sell} = {fm(rev)} → Вигрну: {fm(pay*qty)}")
        # склад
        if good in d["stock"]:
            d["stock"][good] = max(0, d["stock"][good] - qty)
        # статистика
        sk = "my" if owner == "volodymyr" else "his"
        if good in d["stats"][sk]:
            d["stats"][sk][good] += qty

    lines.append(f"\n💰 До виплати Вигрну: *{fm(total_pay)}*")
    lines.append(f"💵 Виручка: {fm(total_rev)}")
    lines.append(f"📈 Твій заробіток: *{fm(total_rev - total_pay)}*")

    # Продали → ми повинні Вигрну більше → баланс зменшується
    add_tx(d, "sale", f"Продаж {point}: {items}", total_pay, -total_pay,
           {"point": point, "owner": owner, "items": items})

    # Попередження залишків
    for good, qty in items.items():
        rem = d["stock"].get(good, 0)
        if rem <= 3:
            lines.append(f"⚠️ {good}: залишилось лише {rem} шт!")
    return "\n".join(lines)

async def do_supply(items: dict, amount: float, d: dict, comment: str = "") -> str:
    lines = ["📥 *Постачання від Вигрна*\n"]
    total = 0
    for good, qty in items.items():
        if good in d["stock"]:
            d["stock"][good] += qty
        buy = PRICES.get(good, {}).get("buy", 0)
        sub = buy * qty
        total += sub
        lines.append(f"  {good}: +{qty} шт" + (f" = {fm(sub)}" if sub else ""))
        lines.append(f"  → Склад тепер: {d['stock'].get(good, qty)} шт")
    if amount:
        total = amount
    if comment:
        lines.append(f"\n💬 {comment}")
    if total:
        lines.append(f"\n💰 Сума постачання: *{fm(total)}*")
    # Отримали товар → більше винні → баланс -
    add_tx(d, "supply", f"Постачання: {items}", total, -total, {"items": items})
    return "\n".join(lines)

async def do_payment(amount: float, payer: str, d: dict, comment: str = "") -> str:
    if payer == "volodymyr":
        desc  = f"💸 Володимир → Вигрну: *{fm(amount)}*"
        delta = amount   # заплатили → борг зменшився → баланс +
    else:
        desc  = f"💸 Вигран → Володимиру: *{fm(amount)}*"
        delta = -amount
    if comment:
        desc += f"\n💬 {comment}"
    add_tx(d, "payment", desc, amount, delta, {"payer": payer})
    return desc

async def do_expense(etype: str, amount: float, payer: str, d: dict, comment: str = "") -> str:
    half = round(amount / 2, 2)
    names = {"rent": "🏠 Оренда", "equipment": "🔧 Обладнання", "delivery": "🚚 Доставка"}
    name  = names.get(etype, "💰 Витрата")
    if payer == "vygran":
        desc  = f"{name}: *{fm(amount)}*\nВигран заплатив → ти йому винен половину ({fm(half)})"
        delta = -half
    else:
        desc  = f"{name}: *{fm(amount)}*\nТи заплатив → Вигран тобі винен половину ({fm(half)})"
        delta = half
    if comment:
        desc += f"\n💬 {comment}"
    add_tx(d, etype, desc, amount, delta, {"payer": payer, "amount": amount})
    return desc

# ══════════════════════════════════════════════════════
# КЛАВІАТУРА
# ══════════════════════════════════════════════════════
def main_kb():
    return ReplyKeyboardMarkup([
        ["☕ Продаж кави",       "📦 Продаж комплекту"],
        ["🥛 Молоко / Айріш",   "📥 Постачання"],
        ["💸 Виплата",           "🏠 Оренда / Витрати"],
        ["💰 Баланс",            "📊 Звіт"],
        ["📍 Точки",             "↩️ Скасувати останнє"],
        ["⚙️ Налаштування"],
    ], resize_keyboard=True)

# ══════════════════════════════════════════════════════
# КОМАНДИ
# ══════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ok(uid):
        await update.message.reply_text("⛔ Доступ заборонено.")
        return
    await update.message.reply_text(
        f"☕ Привіт, {uname(uid)}!\n\n"
        "Пиши як у групі: _«Оляниця 5 кав»_\n"
        "Або надішли голосове повідомлення\n"
        "Або використовуй кнопки нижче 👇",
        reply_markup=main_kb(), parse_mode="Markdown"
    )

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ok(uid): return
    pins = load_pins()
    if str(uid) in pins:
        PIN_WAIT[uid] = "check"
        await update.message.reply_text("🔐 Введи PIN:")
    else:
        await show_balance(update)

async def show_balance(update: Update):
    d = load()
    b = d.get("balance", 0)
    s = d.get("stock", {})
    sm = d["stats"]["my"]
    sh = d["stats"]["his"]
    text = (
        f"💰 *БАЛАНС*\n{'─'*26}\n{bal_line(b)}\n\n"
        f"📦 *СКЛАД*\n"
        f"  ☕ Кава: {s.get('кава',0)} шт\n"
        f"  📦 Комплекти: {s.get('комплект',0)} шт\n"
        f"  🥛 Молоко: {s.get('молоко',0)} шт\n"
        f"  🍹 Айріш: {s.get('айріш',0)} шт\n"
        f"  🥤 Ст.110: {s.get('стакан110',0)} шт  "
        f"🥤 Ст.250: {s.get('стакан250',0)} шт\n\n"
        f"📊 *МОЇ ТОЧКИ продано:* кава {sm['кава']} | компл {sm['комплект']}\n"
        f"📊 *ТОЧКИ ВИГРНА продано:* кава {sh['кава']} | компл {sh['комплект']} | "
        f"молоко/айріш {sh['молоко']+sh['айріш']}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ok(uid): return
    d    = load()
    now  = datetime.now()
    mo   = now.strftime("%Y-%m")
    txs  = [t for t in d["transactions"] if t["date"][:7] == mo]
    sm   = d["stats"]["my"]
    sh   = d["stats"]["his"]

    profit = (sm["кава"]*170 + sm["комплект"]*170 +
              sh["кава"]*70  + sh["комплект"]*70  +
              sh["молоко"]*100 + sh["айріш"]*100)

    sales_sum = sum(t["amount"] for t in txs if t["type"] == "sale")
    sup_sum   = sum(t["amount"] for t in txs if t["type"] == "supply")
    pay_sum   = sum(t["amount"] for t in txs if t["type"] == "payment")
    exp_sum   = sum(t["amount"] for t in txs if t["type"] in ("rent","equipment","delivery"))

    text = (
        f"📊 *ЗВІТ {now.strftime('%B %Y').upper()}*\n{'═'*28}\n\n"
        f"☕ *МОЇ ТОЧКИ:*\n"
        f"  Кава: {sm['кава']} шт × 170 = {fm(sm['кава']*170)}\n"
        f"  Комплекти: {sm['комплект']} шт × 170 = {fm(sm['комплект']*170)}\n\n"
        f"☕ *ТОЧКИ ВИГРНА:*\n"
        f"  Кава: {sh['кава']} шт × 70 = {fm(sh['кава']*70)}\n"
        f"  Комплекти: {sh['комплект']} шт × 70 = {fm(sh['комплект']*70)}\n"
        f"  Молоко+Айріш: {sh['молоко']+sh['айріш']} шт × 100 = "
        f"{fm((sh['молоко']+sh['айріш'])*100)}\n\n"
        f"💚 *Твій прибуток: {fm(profit)}*\n"
        f"{'─'*28}\n"
        f"📥 Постачань: {fm(sup_sum)}\n"
        f"💸 Виплачено: {fm(pay_sum)}\n"
        f"🏠 Витрати: {fm(exp_sum)}\n"
        f"{'═'*28}\n"
        f"{bal_line(d['balance'])}\n\n"
        f"📦 Склад: кава {d['stock'].get('кава',0)} | "
        f"компл {d['stock'].get('комплект',0)} | "
        f"молоко {d['stock'].get('молоко',0)} | "
        f"айріш {d['stock'].get('айріш',0)}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ok(uid): return
    d   = load()
    txs = d.get("transactions", [])
    if not txs:
        await update.message.reply_text("❌ Немає операцій для скасування.")
        return
    last = txs.pop()
    d["balance"] = round(d["balance"] - last["balance_delta"], 2)
    # Відкат складу
    if last["type"] == "sale":
        for g, q in last.get("meta", {}).get("items", {}).items():
            if g in d["stock"]: d["stock"][g] += q
        sk = "my" if last["meta"].get("owner") == "volodymyr" else "his"
        for g, q in last["meta"].get("items", {}).items():
            if g in d["stats"][sk]:
                d["stats"][sk][g] = max(0, d["stats"][sk][g] - q)
    elif last["type"] == "supply":
        for g, q in last.get("meta", {}).get("items", {}).items():
            if g in d["stock"]: d["stock"][g] = max(0, d["stock"][g] - q)
    save(d)
    await update.message.reply_text(
        f"↩️ Скасовано: _{last['description'][:80]}_\n"
        f"Дата: {last['date']}\n{bal_line(d['balance'])}",
        parse_mode="Markdown"
    )

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ok(uid): return
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        from openpyxl.chart import LineChart, Reference
    except ImportError:
        await update.message.reply_text("❌ pip install openpyxl")
        return
    d   = load()
    txs = d.get("transactions", [])
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "Транзакції"
    hdrs = ["ID","Дата","Тип","Опис","Сума","Δ Баланс","Баланс"]
    for c, h in enumerate(hdrs, 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="6B4226")
    rb = 0
    for row, t in enumerate(txs, 2):
        rb += t.get("balance_delta", 0)
        for c, v in enumerate([t.get("id"), t.get("date"), t.get("type"),
                                t.get("description","")[:50], t.get("amount"),
                                t.get("balance_delta"), round(rb,2)], 1):
            ws.cell(row, c, v)
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(
            max(len(str(c.value or "")) for c in col) + 2, 40)
    # Лист балансу + графік
    ws2 = wb.create_sheet("Баланс")
    ws2.append(["Дата","Баланс"])
    rb = 0
    for t in txs:
        rb += t.get("balance_delta", 0)
        ws2.append([t["date"][:10], round(rb, 2)])
    if len(txs) > 1:
        ch = LineChart()
        ch.title = "Динаміка балансу"
        ch.style = 10
        dr = Reference(ws2, min_col=2, min_row=1, max_row=len(txs)+1)
        ch.add_data(dr, titles_from_data=True)
        ws2.add_chart(ch, "D2")
    os.makedirs(EXPORT_DIR, exist_ok=True)
    fname = f"{EXPORT_DIR}/кавобот_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    wb.save(fname)
    with open(fname, "rb") as f:
        await update.message.reply_document(f, filename=os.path.basename(fname),
            caption="📊 Готово! Відкрий у Excel або Google Sheets.")

async def cmd_setpin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ok(uid): return
    PIN_WAIT[uid] = "set"
    await update.message.reply_text("🔐 Введи новий 4-значний PIN:")

async def cmd_points(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ok(uid): return
    await show_points_menu(update, ctx)

async def show_points_menu(update, ctx):
    d   = load()
    pts = d.get("points", {})
    if not pts:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Додати мою точку",    callback_data="padd_vol"),
            InlineKeyboardButton("➕ Додати точку Коли",   callback_data="padd_vyg"),
        ]])
        await update.message.reply_text(
            "📍 *Точок поки немає*\nДодай першу точку:",
            parse_mode="Markdown", reply_markup=kb)
        return

    my  = [(p,o) for p,o in pts.items() if o=="volodymyr"]
    his = [(p,o) for p,o in pts.items() if o=="vygran"]

    lines = ["📍 *УПРАВЛІННЯ ТОЧКАМИ*\n"]
    lines.append("*Мої точки:*")
    for p,_ in my:
        lines.append(f"  • {p}")
    lines.append("\n*Точки Коли:*")
    for p,_ in his:
        lines.append(f"  • {p}")

    kb_rows = []
    # Кнопки для кожної точки
    for p, o in pts.items():
        short = p[:12]+"…" if len(p)>12 else p
        kb_rows.append([
            InlineKeyboardButton(f"✏️ {short}",  callback_data=f"pedit_{p}"),
            InlineKeyboardButton(f"🔄",          callback_data=f"pswap_{p}"),
            InlineKeyboardButton(f"🗑️",          callback_data=f"pdel_{p}"),
        ])
    kb_rows.append([
        InlineKeyboardButton("➕ Додати мою",        callback_data="padd_vol"),
        InlineKeyboardButton("➕ Додати Коли",        callback_data="padd_vyg"),
    ])
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb_rows)
    )

async def cmd_addpoint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ok(uid): return
    args = ctx.args
    if len(args) < 2:
        # Інтерактивний режим
        QUICK[uid] = {"step":"newpoint_name", "owner":"volodymyr"}
        await update.message.reply_text(
            "📍 Напиши назву нової точки\n(або /addpoint Назва мій|коля):")
        return
    owner_raw = args[-1].lower()
    owner = "volodymyr" if owner_raw in ("мій","моя","я","vol","volodymyr") else "vygran"
    name  = " ".join(args[:-1])
    d = load()
    d["points"][name] = owner
    save(d)
    who = "моя" if owner == "volodymyr" else "Коли"
    await update.message.reply_text(
        f"✅ Точка *{name}* додана ({who})", parse_mode="Markdown")

async def cmd_stock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ok(uid): return
    d = load()
    s = d.get("stock", {})
    em = {"кава":"☕","комплект":"📦","молоко":"🥛","айріш":"🍹","стакан110":"🥤","стакан250":"🥤"}
    lines = ["📦 *ЗАЛИШКИ НА СКЛАДІ*\n"]
    for g, q in s.items():
        w = " ⚠️ МАЛО!" if q <= 3 else ""
        lines.append(f"  {em.get(g,'•')} {g}: *{q} шт*{w}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ok(uid): return
    d   = load()
    txs = d.get("transactions",[])[-10:]
    if not txs:
        await update.message.reply_text("📋 Немає операцій.")
        return
    em = {"sale":"☕","supply":"📥","payment":"💸","rent":"🏠","equipment":"🔧","delivery":"🚚"}
    lines = ["📋 *Останні операції:*\n"]
    for t in reversed(txs):
        e = em.get(t["type"],"•")
        sign = "+" if t["balance_delta"] >= 0 else ""
        lines.append(f"{e} *#{t['id']}* {t['date']}\n"
                     f"   {t['description'][:60]}\n"
                     f"   Δ {sign}{fm(t['balance_delta'])}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ok(uid): return
    await update.message.reply_text(
        "⚙️ *КОМАНДИ*\n\n"
        "/balance — баланс і склад\n"
        "/report — місячний звіт\n"
        "/export — Excel файл\n"
        "/history — останні операції\n"
        "/stock — залишки товару\n"
        "/points — список точок\n"
        "/points — управління точками\n"
        "/setpin — PIN для балансу\n"
        "/undo — скасувати останнє\n",
        parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════
# ПІДТВЕРДЖЕННЯ ВЕЛИКИХ СУМ
# ══════════════════════════════════════════════════════
async def ask_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                      key: str, desc: str, amount: float, meta: dict):
    uid   = update.effective_user.id
    other = other_id(uid)
    PENDING[key] = {"desc": desc, "amount": amount, "confirmed_by": {uid}, "meta": meta}
    kb  = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Підтвердити", callback_data=f"ok_{key}"),
        InlineKeyboardButton("❌ Відхилити",   callback_data=f"no_{key}"),
    ]])
    msg = (f"⚠️ *Велика сума — потрібно підтвердження обох*\n\n"
           f"{desc}\nСума: *{fm(amount)}*\n\nЧекаю на {uname(other)}...")
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    try:
        await ctx.bot.send_message(other, msg + f"\n\n_{uname(uid)} ініціював_",
                                   parse_mode="Markdown", reply_markup=kb)
    except Exception:
        pass

# ══════════════════════════════════════════════════════
# CALLBACK КНОПКИ
# ══════════════════════════════════════════════════════
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    cb  = q.data
    await q.answer()
    if not ok(uid): return
    d = load()

    # Підтвердження/відхилення
    if cb.startswith("ok_") or cb.startswith("no_"):
        key = cb[3:]
        if key not in PENDING:
            await q.edit_message_text("❌ Операція не знайдена (вже оброблена?).")
            return
        if cb.startswith("no_"):
            PENDING.pop(key)
            await q.edit_message_text(f"❌ {uname(uid)} відхилив операцію.")
            try: await ctx.bot.send_message(other_id(uid), f"❌ {uname(uid)} відхилив операцію.")
            except: pass
            return
        PENDING[key]["confirmed_by"].add(uid)
        if len(PENDING[key]["confirmed_by"]) >= 2:
            p = PENDING.pop(key)
            m = p["meta"]
            result = ""
            if m.get("op") == "payment":
                result = await do_payment(p["amount"], m["payer"], d, m.get("comment",""))
            save(d)
            await q.edit_message_text(f"✅ Підтверджено обома!\n\n{result}", parse_mode="Markdown")
            await check_debt(ctx, d)
        else:
            await q.edit_message_text(f"✅ {uname(uid)} підтвердив. Чекаю на {uname(other_id(uid))}...")
        return

    # Вибір точки (швидкий продаж)
    if cb.startswith("pt_"):
        point = cb[3:]
        state = QUICK.get(uid, {})
        good  = state.get("good","кава")
        pending_qty = state.get("pending_qty")
        _, owner = find_point(point, d["points"])
        if "/" in good:
            QUICK[uid] = {"step":"milk_type","point":point,"owner":owner}
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🥛 Молоко", callback_data="mg_молоко"),
                InlineKeyboardButton("🍹 Айріш",  callback_data="mg_айріш"),
            ]])
            await q.edit_message_text(f"📍 {point} — що саме?", reply_markup=kb)
        elif pending_qty:
            QUICK.pop(uid, None)
            result = await do_sale(point, owner, {good: pending_qty}, d)
            await q.edit_message_text(result, parse_mode="Markdown")
            await check_debt(ctx, d)
        else:
            QUICK[uid] = {"step":"qty","point":point,"owner":owner,"good":good}
            await q.edit_message_text(f"📍 {point} — скільки *{good}*? Введи число:", parse_mode="Markdown")
        return

    if cb.startswith("pt2_"):
        parts = cb[4:].rsplit("_", 1)
        if len(parts) == 2:
            point, good = parts
            _, owner = find_point(point, d["points"])
            QUICK[uid] = {"step":"qty","point":point,"owner":owner,"good":good}
            await q.edit_message_text(
                f"📍 *{point}* — скільки *{good}*? Введи число:",
                parse_mode="Markdown")
        return

    # ── УПРАВЛІННЯ ТОЧКАМИ ──
    if cb.startswith("padd_"):
        owner = "volodymyr" if cb == "padd_vol" else "vygran"
        QUICK[uid] = {"step": "newpoint_name", "owner": owner}
        who = "мою" if owner == "volodymyr" else "Коли"
        await q.edit_message_text(f"✏️ Введи назву нової точки ({who}):")
        return

    if cb.startswith("pedit_"):
        point = cb[6:]
        QUICK[uid] = {"step": "rename_point", "old_name": point}
        owner = d["points"].get(point, "volodymyr")
        who = "моя" if owner == "volodymyr" else "Коли"
        await q.edit_message_text(
            f"✏️ *{point}* ({who})\n\nВведи нову назву\n(або надішли крапку . щоб скасувати):",
            parse_mode="Markdown")
        return

    if cb.startswith("pswap_"):
        point = cb[6:]
        old_owner = d["points"].get(point)
        new_owner = "vygran" if old_owner == "volodymyr" else "volodymyr"
        d["points"][point] = new_owner
        save(d)
        who_old = "моя" if old_owner == "volodymyr" else "Коли"
        who_new = "моя" if new_owner == "volodymyr" else "Коли"
        await q.edit_message_text(
            f"🔄 *{point}*\n{who_old} → *{who_new}*\n\n✅ Збережено!",
            parse_mode="Markdown")
        return

    if cb.startswith("pdel_"):
        point = cb[5:]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Так, видалити", callback_data=f"pdelok_{point}"),
            InlineKeyboardButton("❌ Скасувати",     callback_data="pdelno"),
        ]])
        await q.edit_message_text(
            f"🗑️ Видалити точку *{point}*?\n\n⚠️ Операції з цією точкою залишаться в історії.",
            parse_mode="Markdown", reply_markup=kb)
        return

    if cb.startswith("pdelok_"):
        point = cb[7:]
        if point in d["points"]:
            del d["points"][point]
            save(d)
            await q.edit_message_text(f"🗑️ Точку *{point}* видалено.", parse_mode="Markdown")
        return

    if cb == "pdelno":
        await q.edit_message_text("❌ Скасовано.")
        return

    if cb.startswith("mg_"):
        good  = cb[3:]
        state = QUICK.get(uid,{})
        QUICK[uid] = {"step":"qty","point":state["point"],"owner":state["owner"],"good":good}
        await q.edit_message_text(f"📍 {state['point']} — скільки *{good}*? Введи число:", parse_mode="Markdown")
        return

    if cb in ("pay_vol","pay_vyg"):
        payer = "volodymyr" if cb == "pay_vol" else "vygran"
        QUICK[uid] = {"step":"pay_amount","payer":payer}
        who = "Ти → Вигрну" if payer == "volodymyr" else "Вигран → Тобі"
        await q.edit_message_text(f"💸 {who}\nВведи суму (грн):")
        return

    if cb.startswith("ex_"):
        etype = cb[3:]
        QUICK[uid] = {"step":"exp_payer","etype":etype}
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Я платив",      callback_data="ep_volodymyr"),
            InlineKeyboardButton("Вигран платив", callback_data="ep_vygran"),
        ]])
        await q.edit_message_text("Хто заплатив?", reply_markup=kb)
        return

    if cb.startswith("ep_"):
        payer = cb[3:]
        state = QUICK.get(uid,{})
        QUICK[uid] = {"step":"exp_amount","etype":state.get("etype","rent"),"payer":payer}
        await q.edit_message_text("Введи суму витрати (грн):")
        return

# ══════════════════════════════════════════════════════
# РОЗУМНИЙ ЛОКАЛЬНИЙ ПАРСИНГ (без AI)
# ══════════════════════════════════════════════════════

# Словник синонімів товарів — всі можливі написання
GOOD_ALIASES = {
    "кава":      ["кав", "кава", "кави", "каву", "кавою", "coffee", "кофе", "коф"],
    "комплект":  ["компл", "комплект", "комплекти", "комплектів", "комплекту",
                  "коплект", "коплектів", "комплектов", "компл.", "комп",
                  "комлпект", "комлпекти", "комлп", "кмплект"],
    "молоко":    ["молок", "молоко", "молока", "молоку"],
    "айріш":     ["айріш", "айриш", "irish", "айрiш"],
    "стакан110": ["ст110", "стакан110", "110мл", "стакани110"],
    "стакан250": ["ст250", "стакан250", "250мл", "стакани250"],
}

# Словник синонімів точок — часткові збіги
POINT_ALIASES = {
    "мурчик":        ["мурч", "мурчик", "мурчика"],
    "вдов":          ["вдов"],
    "сухопара":      ["сухоп", "сухопара"],
    "гордівка":      ["гордів", "гордівка"],
    "торканівка":    ["торкан", "торканівка"],
    "оляниця":       ["олян", "оляниця"],
    "мамина вишня":  ["мамин", "вишня", "мамина", "мамина вишня"],
    "клуб":          ["клуб"],
    "ася":           ["ася"],
    "ободівка агро": ["ободів", "ободівка", "агро"],
    "ковалівка":     ["ковалів", "ковалівка"],
    "корпуса":       ["корпус", "корпуса"],
}

def normalize_good(word: str) -> str | None:
    """Повертає стандартну назву товару або None"""
    w = word.lower().strip()
    for good, aliases in GOOD_ALIASES.items():
        if any(w.startswith(a) or a.startswith(w[:4]) for a in aliases if len(w) >= 3):
            return good
    return None

def normalize_point(text: str, points: dict) -> tuple[str, str] | tuple[None, None]:
    """Шукає назву точки в тексті"""
    tl = text.lower()
    # Спочатку точний збіг
    for point, owner in points.items():
        if point.lower() in tl:
            return point, owner
    # Потім по аліасах
    for key, aliases in POINT_ALIASES.items():
        for alias in aliases:
            if alias in tl:
                # Знаходимо реальну точку в словнику
                for point, owner in points.items():
                    if point.lower().startswith(key[:4]):
                        return point, owner
    return None, None

def smart_parse_supply(text: str) -> tuple[dict, float]:
    """Парсить постачання з вільного тексту"""
    items  = {}
    amount = 0.0
    tl     = text.lower()

    # Шукаємо паттерн: число + товар АБО товар + число
    # Наприклад: "20 комплектів", "+10 кав", "кава 5"
    tokens = re.findall(r'[\+\-]?\d+|[а-яіїєa-z]+\.?', tl)

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # Якщо це число
        if re.match(r'[\+\-]?\d+', tok):
            num = abs(int(tok))
            # Дивимось наступний токен — чи це товар?
            if i + 1 < len(tokens):
                good = normalize_good(tokens[i+1])
                if good:
                    items[good] = items.get(good, 0) + num
                    i += 2
                    continue
        # Якщо це товар — дивимось попередній або наступний токен
        else:
            good = normalize_good(tok)
            if good:
                # Шукаємо число поруч
                num = None
                if i > 0 and re.match(r'[\+\-]?\d+', tokens[i-1]):
                    num = abs(int(tokens[i-1]))
                elif i + 1 < len(tokens) and re.match(r'[\+\-]?\d+', tokens[i+1]):
                    num = abs(int(tokens[i+1]))
                    i += 1
                if num:
                    items[good] = items.get(good, 0) + num
        i += 1

    # Сума в грн
    m = re.search(r'(\d+)\s*грн', tl)
    if m:
        amount = float(m.group(1))

    return items, amount

async def smart_parse_free(text: str, d: dict, update, ctx, uid: int):
    """
    Локальний розумний парсинг вільного тексту.
    Повертає: None якщо не розпізнав (треба AI),
              "" якщо розпізнав але нічого не зробив,
              result string якщо виконав операцію.
    """
    tl = text.lower().strip()

    # ── ПРОДАЖ: точка + число + товар (в будь-якому порядку) ──
    point, owner = normalize_point(tl, d["points"])
    if point:
        items = {}
        tokens = re.findall(r'\d+|[а-яіїєa-z]+\.?', tl)
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if re.match(r'\d+', tok):
                num = int(tok)
                if i + 1 < len(tokens):
                    good = normalize_good(tokens[i+1])
                    if good:
                        items[good] = items.get(good, 0) + num
                        i += 2; continue
            else:
                good = normalize_good(tok)
                if good:
                    num = None
                    if i > 0 and re.match(r'\d+', tokens[i-1]):
                        num = int(tokens[i-1])
                    elif i+1 < len(tokens) and re.match(r'\d+', tokens[i+1]):
                        num = int(tokens[i+1]); i += 1
                    if num:
                        items[good] = items.get(good, 0) + num
                    else:
                        # Товар є але числа немає — запитуємо кількість
                        QUICK[uid] = {"step":"qty","point":point,"owner":owner,"good":good}
                        await update.message.reply_text(
                            f"📍 *{point}* — скільки *{good}*?",
                            parse_mode="Markdown")
                        return ""
            i += 1

        if items:
            result = await do_sale(point, owner, items, d)
            await update.message.reply_text(result, parse_mode="Markdown")
            return result

        # Точка є але немає товарів — питаємо що продав
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("☕ Кава",      callback_data=f"pt2_{point}_кава"),
             InlineKeyboardButton("📦 Комплект", callback_data=f"pt2_{point}_комплект")],
            [InlineKeyboardButton("🥛 Молоко",   callback_data=f"pt2_{point}_молоко"),
             InlineKeyboardButton("🍹 Айріш",    callback_data=f"pt2_{point}_айріш")],
        ])
        await update.message.reply_text(
            f"📍 *{point}* — що продав?", parse_mode="Markdown", reply_markup=kb)
        return ""

    # ── ТІЛЬКИ ТОВАР БЕЗ ТОЧКИ — питаємо точку ──
    # Наприклад: "Комплектів", "кава", "5 комплектів"
    solo_good = None
    solo_qty  = None
    tokens = re.findall(r'\d+|[а-яіїєa-z]+\.?', tl)
    for i, tok in enumerate(tokens):
        g = normalize_good(tok)
        if g:
            solo_good = g
            # Шукаємо число поряд
            if i > 0 and re.match(r'\d+', tokens[i-1]):
                solo_qty = int(tokens[i-1])
            elif i+1 < len(tokens) and re.match(r'\d+', tokens[i+1]):
                solo_qty = int(tokens[i+1])
            break

    if solo_good:
        if solo_qty:
            # Є товар і кількість — питаємо точку
            QUICK[uid] = {"step":"point","good":solo_good,"pending_qty":solo_qty}
        else:
            # Є тільки товар — питаємо точку, потім кількість
            QUICK[uid] = {"step":"point","good":solo_good}
        pts = list(d["points"].keys())
        kb  = InlineKeyboardMarkup([[InlineKeyboardButton(p, callback_data=f"pt_{p}")] for p in pts])
        msg = f"📍 *{solo_good}* — обери точку:"
        if solo_qty:
            msg = f"📍 {solo_good} {solo_qty} шт — обери точку:"
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
        return ""

    # ── ПОСТАЧАННЯ ──
    supply_keywords = ["привіз", "поставив", "постачання", "прийшло",
                       "привезли", "отримав", "закупка", "привіз коля",
                       "коля привіз", "привіз товар"]
    has_plus = bool(re.search(r'\+\d', tl))
    has_supply_word = any(kw in tl for kw in supply_keywords)

    if has_plus or has_supply_word:
        items, amount = smart_parse_supply(text)
        if items:
            result = await do_supply(items, amount, d)
            await update.message.reply_text(result, parse_mode="Markdown")
            return result

    # ── ВИПЛАТА ──
    pay_keywords = ["передав", "відав", "віддав", "оплатив", "заплатив",
                    "розрахував", "повернув", "зп", "зарплата"]
    m_amount = re.search(r'(\d+)', tl.replace(" ", ""))
    if any(kw in tl for kw in pay_keywords) and m_amount:
        amount = float(m_amount.group())
        payer  = "vygran" if any(w in tl for w in ["коля","він","партнер"]) else "volodymyr"
        result = await do_payment(amount, payer, d, text)
        await update.message.reply_text(result, parse_mode="Markdown")
        return result

    # ── ОРЕНДА ──
    if any(w in tl for w in ["оренда", "оренди", "оплата оренди"]):
        m = re.search(r'(\d+)', tl)
        if m:
            payer  = "vygran" if any(w in tl for w in ["коля","він"]) else "volodymyr"
            result = await do_expense("rent", float(m.group(1)), payer, d, text)
            await update.message.reply_text(result, parse_mode="Markdown")
            return result

    # ── ОБЛАДНАННЯ ──
    if any(w in tl for w in ["купюрник", "принтер", "обладнання", "купив"]):
        m = re.search(r'(\d+)', tl)
        if m:
            payer  = "vygran" if any(w in tl for w in ["коля","він"]) else "volodymyr"
            result = await do_expense("equipment", float(m.group(1)), payer, d, text)
            await update.message.reply_text(result, parse_mode="Markdown")
            return result

    return None  # Передаємо AI



async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if not ok(uid): return
    text = update.message.text.strip()
    d    = load()

    # PIN
    if uid in PIN_WAIT:
        mode = PIN_WAIT.pop(uid)
        if mode == "set":
            if re.match(r"^\d{4}$", text):
                pins = load_pins(); pins[str(uid)] = text; save_pins(pins)
                await update.message.reply_text("✅ PIN встановлено!")
            else:
                await update.message.reply_text("❌ PIN — 4 цифри.")
        elif mode == "check":
            pins = load_pins()
            if pins.get(str(uid)) == text:
                await show_balance(update)
            else:
                await update.message.reply_text("❌ Невірний PIN.")
        return

    # Кнопки меню
    if text == "💰 Баланс":        await cmd_balance(update, ctx); return
    if text == "📊 Звіт":          await cmd_report(update, ctx); return
    if text == "↩️ Скасувати останнє": await cmd_undo(update, ctx); return
    if text == "⚙️ Налаштування":  await cmd_settings(update, ctx); return
    if text == "📍 Точки":         await cmd_points(update, ctx); return

    if text in ("☕ Продаж кави","📦 Продаж комплекту","🥛 Молоко / Айріш"):
        gmap = {"☕ Продаж кави":"кава","📦 Продаж комплекту":"комплект","🥛 Молоко / Айріш":"молоко/айріш"}
        QUICK[uid] = {"step":"point","good":gmap[text]}
        pts = list(d["points"].keys())
        kb  = InlineKeyboardMarkup([[InlineKeyboardButton(p, callback_data=f"pt_{p}")] for p in pts])
        await update.message.reply_text(f"📍 Обери точку:", reply_markup=kb)
        return

    if text == "📥 Постачання":
        QUICK[uid] = {"step":"supply_text"}
        await update.message.reply_text(
            "📥 Опиши постачання, наприклад:\n_+20 комплектів +10 кава 1500грн_",
            parse_mode="Markdown"); return

    if text == "💸 Виплата":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Я → Вигрну",    callback_data="pay_vol"),
            InlineKeyboardButton("Вигран → Мені", callback_data="pay_vyg"),
        ]])
        await update.message.reply_text("💸 Хто кому?", reply_markup=kb); return

    if text == "🏠 Оренда / Витрати":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Оренда",      callback_data="ex_rent"),
             InlineKeyboardButton("🔧 Обладнання",  callback_data="ex_equipment")],
            [InlineKeyboardButton("🚚 Доставка",    callback_data="ex_delivery")],
        ])
        await update.message.reply_text("Тип витрати:", reply_markup=kb); return

    # Кроки діалогу
    state = QUICK.get(uid, {})
    step  = state.get("step","")

    if step == "qty":
        m = re.search(r"\d+", text)
        if not m: await update.message.reply_text("❌ Введи число."); return
        qty   = int(m.group())
        point = state["point"]; owner = state["owner"]; good = state["good"]
        QUICK.pop(uid, None)
        result = await do_sale(point, owner, {good: qty}, d)
        await update.message.reply_text(result, parse_mode="Markdown")
        await check_debt(ctx, d); return

    if step == "pay_amount":
        m = re.search(r"[\d.]+", text)
        if not m: await update.message.reply_text("❌ Введи суму."); return
        amount = float(m.group())
        payer  = state["payer"]
        QUICK.pop(uid, None)
        if amount >= CONFIRM_SUM:
            await ask_confirm(update, ctx, f"pay_{uid}_{int(amount)}",
                              f"Виплата {uname(ID_VOLODYMYR if payer=='volodymyr' else ID_VYGRAN)}",
                              amount, {"op":"payment","payer":payer,"comment":""})
        else:
            result = await do_payment(amount, payer, d)
            await update.message.reply_text(result, parse_mode="Markdown")
            await check_debt(ctx, d); return

    if step == "exp_amount":
        m = re.search(r"[\d.]+", text)
        if not m: await update.message.reply_text("❌ Введи суму."); return
        amount = float(m.group())
        QUICK.pop(uid, None)
        result = await do_expense(state["etype"], amount, state["payer"], d)
        await update.message.reply_text(result, parse_mode="Markdown")
        await check_debt(ctx, d); return

    if step == "newpoint_name":
        QUICK.pop(uid, None)
        if text.strip() == ".":
            await update.message.reply_text("❌ Скасовано."); return
        name  = text.strip().title()
        owner = state.get("owner", "volodymyr")
        d["points"][name] = owner
        save(d)
        who = "моя" if owner == "volodymyr" else "Коли"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Ще одну мою",   callback_data="padd_vol"),
            InlineKeyboardButton("➕ Ще одну Коли",  callback_data="padd_vyg"),
        ]])
        await update.message.reply_text(
            f"✅ Точка *{name}* додана ({who})\n\nДодати ще?",
            parse_mode="Markdown", reply_markup=kb)
        return

    if step == "rename_point":
        QUICK.pop(uid, None)
        if text.strip() == ".":
            await update.message.reply_text("❌ Скасовано."); return
        old_name = state.get("old_name","")
        new_name = text.strip().title()
        if old_name in d["points"]:
            owner = d["points"].pop(old_name)
            d["points"][new_name] = owner
            save(d)
            who = "моя" if owner == "volodymyr" else "Коли"
            await update.message.reply_text(
                f"✅ *{old_name}* → *{new_name}* ({who})",
                parse_mode="Markdown")
        return

    if step == "supply_text":
        QUICK.pop(uid, None)
        items, amount = smart_parse_supply(text)
        if items:
            result = await do_supply(items, amount, d)
            await update.message.reply_text(result, parse_mode="Markdown")
            await check_debt(ctx, d)
        else:
            await update.message.reply_text(
                "❓ Не розпізнав товари. Спробуй:\n"
                "_+20 комплектів +10 кава_\n"
                "_20 компл 10 кав 1500грн_",
                parse_mode="Markdown")
        return

    # Вільний текст — спочатку локальний парсинг, потім AI
    result = await smart_parse_free(text, d, update, ctx, uid)
    if result is not None:
        if result:
            save(d)
            await check_debt(ctx, d)
        return

    # AI як запасний варіант
    msg = await update.message.reply_text("🔍 Розпізнаю через AI...")
    parsed = await ai_parse(text)

    if not parsed or parsed.get("type") == "unknown":
        await msg.edit_text(
            "❓ Не зрозумів. Спробуй:\n"
            "• _Оляниця 5 кав_\n"
            "• _+20 комплектів_\n"
            "• _Передав 5000_\n"
            "• _Оренда 2000_\n"
            "Або скористайся кнопками 👇",
            parse_mode="Markdown"
        ); return

    ptype  = parsed.get("type","")
    items  = parsed.get("items") or {}
    amount = float(parsed.get("amount") or 0)
    point  = parsed.get("point") or ""
    payer  = parsed.get("payer") or "volodymyr"
    comment= parsed.get("comment") or ""
    result_text = ""

    if ptype == "sale" and point:
        pname, owner = find_point(point, d["points"])
        if pname:
            result_text = await do_sale(pname, owner, items, d)
        else:
            result_text = f"❓ Точка «{point}» не знайдена. /addpoint для додавання."
    elif ptype == "supply" and items:
        result_text = await do_supply(items, amount, d, comment)
    elif ptype == "payment" and amount:
        if amount >= CONFIRM_SUM:
            await msg.edit_text("⚠️ Велика сума — чекаю підтвердження...")
            await ask_confirm(update, ctx, f"pay_{uid}_{int(amount)}",
                              f"Виплата: {fm(amount)}", amount,
                              {"op":"payment","payer":payer,"comment":comment})
            return
        result_text = await do_payment(amount, payer, d, comment)
    elif ptype in ("rent","equipment","delivery") and amount:
        result_text = await do_expense(ptype, amount, payer, d, comment)
    else:
        result_text = "🤔 Не вистачає даних. Скористайся кнопками 👇"

    await msg.edit_text(result_text or "✅ Готово", parse_mode="Markdown")
    if result_text:
        save(d)
        await check_debt(ctx, d)

# ══════════════════════════════════════════════════════
# ГОЛОСОВІ ПОВІДОМЛЕННЯ
# ══════════════════════════════════════════════════════
async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ok(uid): return
    msg  = await update.message.reply_text("🎤 Транскрибую...")
    voice = update.message.voice
    file  = await ctx.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        text = await transcribe(tmp.name)
        os.unlink(tmp.name)
    if not text:
        await msg.edit_text("❌ Не вдалось розпізнати. Перевір GROQ_API_KEY.")
        return
    await msg.edit_text(f"🎤 *Розпізнав:* _{text}_", parse_mode="Markdown")
    update.message.text = text
    await on_text(update, ctx)

# ══════════════════════════════════════════════════════
# АВТО-ЗАДАЧІ
# ══════════════════════════════════════════════════════
async def check_debt(ctx: ContextTypes.DEFAULT_TYPE, d: dict):
    b = abs(d.get("balance", 0))
    if b >= DEBT_ALERT:
        debtor = "Вигран" if d["balance"] > 0 else "Володимир"
        cred   = "Володимиру" if d["balance"] > 0 else "Вигрну"
        msg    = f"🔔 *Нагадування!*\n{debtor} винен {cred} *{fm(b)}*\nЧас розрахуватись? 😊"
        for u in (ID_VOLODYMYR, ID_VYGRAN):
            try: await ctx.bot.send_message(u, msg, parse_mode="Markdown")
            except: pass

async def weekly_job(ctx: ContextTypes.DEFAULT_TYPE):
    if datetime.now().weekday() != 0: return   # тільки понеділок
    d     = load()
    today = datetime.now().strftime("%Y-%m-%d")
    if d.get("weekly_sent") == today: return
    txs   = d.get("transactions",[])
    week  = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d")
    wtxs  = [t for t in txs if t["date"][:10] >= week]
    sales = [t for t in wtxs if t["type"]=="sale"]
    msg   = (f"📊 *Тижневий звіт*\n\n"
             f"Продажів за тиждень: {len(sales)}\n"
             f"Сума: {fm(sum(t['amount'] for t in sales))}\n\n"
             f"{bal_line(d['balance'])}")
    for u in (ID_VOLODYMYR, ID_VYGRAN):
        try: await ctx.bot.send_message(u, msg, parse_mode="Markdown")
        except: pass
    d["weekly_sent"] = today
    save(d)

# ══════════════════════════════════════════════════════
# ВЕБ-СЕРВЕР (aiohttp) — роздає дашборд і API
# ══════════════════════════════════════════════════════
from aiohttp import web as aioWeb
import asyncio

WEB_PORT = int(os.environ.get("PORT", 8080))

async def web_index(request):
    """Роздає dashboard.html"""
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    if not os.path.exists(html_path):
        return aioWeb.Response(text="dashboard.html not found", status=404)
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return aioWeb.Response(text=content, content_type="text/html", charset="utf-8")

async def api_get_data(request):
    """API: повертає всі дані (для синхронізації з веб)"""
    # Простий захист — перевіряємо секретний токен в заголовку
    token = request.headers.get("X-Token", "")
    if token != BOT_TOKEN[:20]:
        return aioWeb.json_response({"error": "unauthorized"}, status=401)
    d = load()
    return aioWeb.json_response(d)

async def api_post_data(request):
    """API: зберігає дані з веб-дашборду"""
    token = request.headers.get("X-Token", "")
    if token != BOT_TOKEN[:20]:
        return aioWeb.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        if "transactions" not in body:
            return aioWeb.json_response({"error": "invalid data"}, status=400)
        save(body)
        return aioWeb.json_response({"ok": True})
    except Exception as e:
        return aioWeb.json_response({"error": str(e)}, status=500)

async def api_health(request):
    """Health check для Railway"""
    return aioWeb.json_response({"status": "ok", "bot": "☕ КавоБот"})

def make_web_app():
    app = aioWeb.Application()
    app.router.add_get("/",        web_index)
    app.router.add_get("/health",  api_health)
    app.router.add_get("/api/data",  api_get_data)
    app.router.add_post("/api/data", api_post_data)
    return app

# ══════════════════════════════════════════════════════
# ЗАПУСК — БОТ + ВЕБ СЕРВЕР РАЗОМ
# ══════════════════════════════════════════════════════
async def run_bot(tg_app):
    """Запускає Telegram бота"""
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(allowed_updates=["message","callback_query"])
    logger.info("☕ КавоБот запущено!")

async def run_web():
    """Запускає веб-сервер"""
    web_app = make_web_app()
    runner  = aioWeb.AppRunner(web_app)
    await runner.setup()
    site = aioWeb.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    logger.info(f"🌐 Веб-сервер: http://0.0.0.0:{WEB_PORT}")

async def main_async():
    os.makedirs(EXPORT_DIR, exist_ok=True)

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start",    cmd_start))
    tg_app.add_handler(CommandHandler("balance",  cmd_balance))
    tg_app.add_handler(CommandHandler("report",   cmd_report))
    tg_app.add_handler(CommandHandler("export",   cmd_export))
    tg_app.add_handler(CommandHandler("undo",     cmd_undo))
    tg_app.add_handler(CommandHandler("points",   cmd_points))
    tg_app.add_handler(CommandHandler("addpoint", cmd_addpoint))
    tg_app.add_handler(CommandHandler("stock",    cmd_stock))
    tg_app.add_handler(CommandHandler("history",  cmd_history))
    tg_app.add_handler(CommandHandler("setpin",   cmd_setpin))
    tg_app.add_handler(CommandHandler("settings", cmd_settings))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    tg_app.add_handler(MessageHandler(filters.VOICE, on_voice))
    tg_app.add_handler(CallbackQueryHandler(on_callback))
    tg_app.job_queue.run_daily(
        weekly_job,
        time=datetime.strptime("09:00", "%H:%M").time()
    )

    # Запускаємо веб і бота одночасно
    await run_web()
    await run_bot(tg_app)

    # Тримаємо живим
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Зупинка...")
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
