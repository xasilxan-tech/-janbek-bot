import os, io, json, re, time, csv, datetime, requests
import monitoring as mon

BOT_TOKEN = os.environ["BOT_TOKEN"]
AI_KEY = os.environ["ANTHROPIC_API_KEY"]
OWNER_ID = int(os.environ.get("OWNER_ID", "721769003"))
MODEL = os.environ.get("MODEL", "claude-haiku-5-5")
TG = f"https://api.telegram.org/bot{BOT_TOKEN}/"

SHEET_TTL = int(os.environ.get("SHEET_TTL", "600"))  # soniya: jadvalni qancha tez-tez yangilash
_cache = {}

def fetch_csv(url, fallback_file):
    """Google Sheets CSV havolasidan oladi (SHEET_TTL soniya keshlanadi).
    Havola ishlamasa, eski nusxa yoki GitHub'dagi fayl ishlatiladi."""
    key = url or fallback_file
    now = time.time()
    c = _cache.get(key)
    if c and now - c[0] < SHEET_TTL:
        return c[1]
    text = None
    if url:
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            t = r.content.decode("utf-8-sig")
            if "<html" in t[:500].lower():
                raise RuntimeError("havola CSV emas (jadval ochiq emas bo'lishi mumkin)")
            if t.startswith("XATO"):
                raise RuntimeError(t[:80])
            text = t
        except requests.RequestException as e:
            code = getattr(getattr(e, "response", None), "status_code", "")
            print("Jadval xato:", type(e).__name__, code)   # URL (kalit) logga yozilmaydi
        except Exception as e:
            print("Jadval xato:", e)
    if text is None:
        if c:
            return c[1]
        with open(fallback_file, encoding="utf-8-sig") as f:
            text = f.read()
    _cache[key] = (now, text)
    return text

def system_prompt():
    PRICES = fetch_csv(os.environ.get("PRICE_SHEET_URL"), "narxlar_2026.csv")
    return f"""You are the assistant of the Telegram channel "Fermer ham texnika" (agricultural machinery). The business sells new machinery, orders machinery from factories, and buys and sells used machinery.

LANGUAGE (the most important rule): ALWAYS answer in the Karakalpak language, written ONLY in the Karakalpak Cyrillic alphabet (letters such as ә, ғ, қ, ң, ө, ү, ў, ҳ, ы). NEVER answer in Latin letters, and never in Uzbek or Russian, even if the customer writes in Latin letters, Uzbek or Russian. You understand all of these, but you always reply in Karakalpak Cyrillic. Keep machine model names (for example DF 804, LOVOL 904) and numbers exactly as they appear in the list.

RULES:
- If asked about a price, give the FACTORY PRICE from the list below (in so'm, written as "сум"). After the price add a short note that the price may change and that the operator confirms the exact price.
- Only use machinery and prices from the list. If something is not in the list, do not guess: say that the operator will give exact information.
- If the customer wants to order, ask one by one: name, phone number, desired machine, region (viloyat/tuman).
- When all 4 pieces of information are collected, end your reply with exactly one extra line, and nothing after it:
BUYURTMA: {{"ism": "...", "telefon": "...", "texnika": "...", "hudud": "..."}}
The customer never sees this line, the program removes it. Write the values as the customer gave them.
- Never reveal or discuss any debt amounts, other customers, or anything that is not in this message.
- If the customer sends only a 9-digit or 14-digit number: if you just asked for their phone number, treat it as the phone number; otherwise reply in Karakalpak Cyrillic that no debt record was found for this STIR (INN) number and ask them to check the number.
- Keep answers short and clear.

FACTORY PRICE LIST (CSV columns: supplier, type, model, factory price in so'm):
{PRICES}
"""

def tg(method, **kw):
    return requests.post(TG + method, json=kw, timeout=60).json()

def ask_ai(history):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": AI_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 800, "system": system_prompt(), "messages": history},
        timeout=120,
    )
    data = r.json()
    if "content" not in data:
        raise RuntimeError(str(data)[:500])
    return data["content"][0]["text"]

def _n(x):
    return str(x).lower().replace("\u2019", "'").replace("`", "'").strip()

def parse_debts(text):
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text))]
    date = ""
    unit = "ming so'm" if re.search(r"минг\s*с[уў]м|ming\s*so", text, re.I) else ""
    cols = {}
    items, district = [], ""
    for cells in rows:
        joined = " ".join(cells)
        m = re.search(r"(?:Сана|Sana|Дата)\s*:\s*([0-9.]+)", joined)
        if m and not date:
            date = m.group(1).rstrip(".")
        low = [_n(c) for c in cells]
        if "inn" not in cols:
            for i, c in enumerate(low):
                if c in ("инн", "inn", "stir", "стир"):
                    cols = {"inn": i}
                    for j, h in enumerate(low):
                        if "номи" in h or "nomi" in h:
                            cols["name"] = j
                        elif ("умумий" in h or "umumiy" in h or "jami" in h) and ("қарз" in h or "qarz" in h):
                            cols["total"] = j
                        elif ("лизинг" in h or "lizing" in h) and ("қарз" in h or "qarz" in h):
                            cols["liz"] = j
                        elif "изох" in h or "izoh" in h:
                            cols["note"] = j
                    break
            if "inn" in cols:
                continue
        inn_i = None
        if "inn" in cols and cols["inn"] < len(cells) and re.fullmatch(r"\d{9,14}", cells[cols["inn"]]):
            inn_i = cols["inn"]
        elif "inn" not in cols:
            inn_i = next((i for i, c in enumerate(cells) if re.fullmatch(r"\d{9}|\d{14}", c)), None)
        if inn_i is None:
            d = next((c for c in cells if re.search(r"tuman|туман|shahar|shahri|шаҳар|шаҳри|шахри|район|rayon", c, re.I)), "")
            if d:
                district = d
            continue
        def at(k, fallback):
            i = cols.get(k)
            return cells[i] if i is not None and i < len(cells) else fallback
        rest = [c for c in cells[inn_i + 1:] if c]
        items.append({
            "inn": cells[inn_i],
            "name": at("name", rest[0] if rest else ""),
            "total": at("total", rest[1] if len(rest) > 1 else ""),
            "liz": at("liz", rest[2] if len(rest) > 2 else ""),
            "note": at("note", rest[3] if len(rest) > 3 else ""),
            "district": district,
        })
    return items, date, unit

def find_debts(q):
    if not q:
        return "INN yoki nom yozing, masalan: qarz 311105115"
    try:
        text = fetch_csv(os.environ.get("DEBT_SHEET_URL"), "qarzdorlik.csv")
    except FileNotFoundError:
        return "Qarzdorlik jadvali ulanmagan yoki kalit noto'g'ri."
    items, date, unit = parse_debts(text)
    if not items:
        return "Jadvalda INN li qatorlar topilmadi."
    digits = re.sub(r"\s+", "", q)
    if digits.isdigit():
        found = [i for i in items if i["inn"] == digits]
    else:
        qq = _n(q)
        found = [i for i in items if qq in _n(i["name"])]
    if not found:
        return f"'{q}' bo'yicha qarz ma'lumoti topilmadi."
    out = []
    for i in found[:5]:
        lines = [f"🏢 {i['name']}", f"INN: {i['inn']}"]
        if i["district"]:
            lines.append(f"Tuman: {i['district']}")
        lines.append(f"Umumiy qarz: {i['total']} {unit}".strip())
        lines.append(f"Lizing qarzi: {i['liz']} {unit}".strip())
        if i["note"]:
            lines.append(f"Izoh: {i['note']}")
        out.append("\n".join(lines))
    if len(found) > 5:
        out.append(f"... yana {len(found) - 5} ta topildi, aniqroq yozing.")
    if date:
        out.append(f"📅 Sana: {date}")
    return "\n\n".join(out)

def inn_query(text):
    """Xabar faqat INN (9 yoki 14 raqam) bo'lsa, raqamni qaytaradi. 'qarz'/'қарыз' so'zi oldidan kelsa ham bo'ladi."""
    t = re.sub(r"^\s*(?:qarz|қарыз|қарз|карз)\w*\s*", "", text.strip(), flags=re.I)
    t = re.sub(r"[\s\-]", "", t)
    return t if re.fullmatch(r"\d{9}|\d{14}", t) else None

_lookups = {}

def rate_ok(sender, limit=5, window=3600):
    """Bitta foydalanuvchi soatiga ko'pi bilan `limit` marta INN so'ray oladi (ro'yxatni terib chiqishdan himoya)."""
    now = time.time()
    lst = [t for t in _lookups.get(sender, []) if now - t < window]
    ok = len(lst) < limit
    if ok:
        lst.append(now)
    _lookups[sender] = lst
    return ok

FREE_DAILY = int(os.environ.get("FREE_LOOKUPS", "0"))      # fermerga kuniga nechta INN bepul; 0 = cheklovsiz (hamma ko'radi)
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "LizingQQRes")
state = {"day": "", "used": {}, "extra": {}, "notified": []}

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state.update(json.load(f))
    except Exception:
        pass
    for k, v in (("day", ""), ("used", {}), ("extra", {}), ("notified", [])):
        state.setdefault(k, v)

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        print("State saqlanmadi:", type(e).__name__)

def today():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))).strftime("%Y-%m-%d")

def roll_day():
    d = today()
    if state.get("day") != d:
        state["day"], state["used"], state["notified"] = d, {}, []

def take_quota(sender):
    """Bitta muvaffaqiyatli INN so'roviga ruxsat bor-yo'qligini tekshiradi va hisobdan ayiradi."""
    if FREE_DAILY <= 0:
        return True
    roll_day()
    k = str(sender)
    if state["used"].get(k, 0) < FREE_DAILY:
        state["used"][k] = state["used"].get(k, 0) + 1
    elif state["extra"].get(k, 0) > 0:
        state["extra"][k] -= 1
    else:
        return False
    save_state()
    return True

def has_quota(sender):
    if FREE_DAILY <= 0:
        return True
    roll_day()
    k = str(sender)
    return state["used"].get(k, 0) < FREE_DAILY or state["extra"].get(k, 0) > 0

def grant(uid, n):
    k = str(uid)
    state["extra"][k] = state["extra"].get(k, 0) + n
    save_state()
    return state["extra"][k]

def limit_text():
    return (f"Бир күнде {FREE_DAILY} сораў тегин. Бүгинги лимит питти. "
            f"Қосымша сораў ушын төлем етип, оператор менен байланысың: @{OWNER_USERNAME}")

def load_debts():
    try:
        return parse_debts(fetch_csv(os.environ.get("DEBT_SHEET_URL"), "qarzdorlik.csv"))
    except FileNotFoundError:
        return None

def debt_record(inn):
    """(holat, yozuv, sana, birlik); holat: ok / yoq / xato"""
    d = load_debts()
    if d is None:
        return "xato", None, "", ""
    items, date, unit = d
    for i in items:
        if i["inn"] == inn:
            return "ok", i, date, unit
    return "yoq", None, date, unit

def fmt_public(i, date, unit):
    """Fermerga: qoraqalpoq tilida, kirill; izoh va tuman ko'rsatilmaydi."""
    u = "мың сум" if unit else "сум"
    lines = [f"🏢 {i['name']}", f"СТИР: {i['inn']}", f"Жәми қарыз: {i['total']} {u}"]
    if i["liz"]:
        lines.append(f"Лизинг қарызы: {i['liz']} {u}")
    if date:
        lines.append(f"📅 Сана: {date}")
    lines.append("Мағлыўмат өзгериўи мүмкин. Анық мағлыўматты оператор береди.")
    return "\n".join(lines)

def save_order(order, who, now):
    """Buyurtmani Google jadvalga yozadi (ORDER_SHEET_URL bo'lsa). Yetishmagan ustunlar ro'yxatini qaytaradi."""
    url = os.environ.get("ORDER_SHEET_URL")
    if not url:
        return []
    r = requests.post(url, json={
        "key": os.environ.get("ORDER_SHEET_KEY", ""),
        "sana": now,
        "ism": order.get("ism"),
        "telefon": order.get("telefon"),
        "texnika": order.get("texnika"),
        "hudud": order.get("hudud"),
        "telegram": "@" + str(who),
    }, timeout=60)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(str(data)[:300])
    return data.get("missing") or []

def watch(msg, text, reply=None):
    """Egasiga: kim nima yozdi (va bot nima javob berdi). Egasining o'z xabarlari yuborilmaydi."""
    frm = msg.get("from") or {}
    if frm.get("id") == OWNER_ID or not state.get("monitor", True):
        return
    uname = ("@" + frm["username"]) if frm.get("username") else "username yo'q"
    where = "" if msg["chat"].get("type") == "private" else f"\n👥 Guruh: {msg['chat'].get('title', '')}"
    out = f"💬 {frm.get('first_name', '')} ({uname}, ID: {frm.get('id')}){where}\n{text[:500]}"
    if reply is not None:
        out += f"\n\n🤖 {reply[:800]}"
    try:
        tg("sendMessage", chat_id=OWNER_ID, text=out)
    except Exception as e:
        print("Kuzatuv xato:", type(e).__name__)

histories = {}

BOT_USERNAME = os.environ.get("BOT_USERNAME", "janbek_ia_bot")
PRIVATE_ONLY = f"Қарыз мағлыўматы тек жеке чатта көрсетиледи. Маған жеке жазың: @{BOT_USERNAME}"
DM_SENT = "✅ Қарыз мағлыўматы жеке чатыңызға жиберилди."
TOO_MANY = "Көп сораў жибердиңиз. Кейинирек қайта урынып көриң."
NOT_NOW = "Ҳәзир мағлыўматты көрсете алмайман. Кейинирек қайта урынып көриң."

def handle(msg):
    if "text" not in msg:
        mon.handle_photo(msg)   # xızmetkerdiń monitoring súwretleri
        return
    chat = msg["chat"]["id"]
    text = msg["text"]
    is_private = msg["chat"].get("type") == "private"
    frm = msg.get("from") or {}
    sender = frm.get("id")
    who = frm.get("username") or frm.get("first_name", "")

    if mon.handle_text(msg):    # /esabat, /qaldi, /blanka, /bekor hám monitoringtegi INN
        return

    if text == "/start":
        mon.S.pop(chat, None)
        histories.pop(chat, None)
        watch(msg, "/start")
        tg("sendMessage", chat_id=chat,
           text="Ассалаўма әлейкум! Техника бойынша сораўыңызды жазың. Қарызыңызды билиў ушын СТИР (ИНН) нөмириңизди жазың.")
        return

    if sender == OWNER_ID and is_private and text.lower().lstrip("/").startswith("ruxsat"):
        g = re.match(r"^/?ruxsat\s+(\d+)\s+(\d+)\s*$", text.strip(), re.I)
        if not g:
            tg("sendMessage", chat_id=chat, text="Format: ruxsat 123456789 20  (ID va nechta qo'shimcha so'rov)")
            return
        uid, n = int(g.group(1)), int(g.group(2))
        left = grant(uid, n)
        tg("sendMessage", chat_id=chat, text=f"✅ ID {uid}: +{n} ta so'rov ochildi (pullik qolgan: {left}).")
        tg("sendMessage", chat_id=uid, text=f"Сизге {n} қосымша сораў ашылды. СТИР (ИНН) нөмириңизди жазың.")
        return

    g = re.match(r"^/?kuzat\s+(on|off)\s*$", text.strip(), re.I)
    if g and sender == OWNER_ID and is_private:
        state["monitor"] = g.group(1).lower() == "on"
        save_state()
        tg("sendMessage", chat_id=chat,
           text="👁 Kuzatuv yoqildi: kim nima yozsa, sizga ko'rinadi." if state["monitor"] else "👁 Kuzatuv o'chirildi.")
        return

    inn = inn_query(text)
    owner_cmd = text.lower().startswith("qarz")

    # --- Egasi: qarz <INN/nom> yoki shunchaki INN ---
    if sender == OWNER_ID and (owner_cmd or inn):
        if is_private:
            q = text[4:].strip().lower() if owner_cmd else inn
            tg("sendMessage", chat_id=chat, text=find_debts(q))
        else:
            tg("sendMessage", chat_id=chat,
               text="🔒 Qarz ma'lumoti guruhda ko'rsatilmaydi. Menga shaxsiy chatda yozing.")
        return

    # --- Fermer: INN yozsa. Qarz har doim uning SHAXSIY chatiga boradi; guruhga hech narsa chiqmaydi ---
    if inn and sender != OWNER_ID:
        if not rate_ok(sender, limit=30):
            tg("sendMessage", chat_id=chat, text=TOO_MANY)
            return
        status, rec, date, unit = debt_record(inn)
        if status == "ok":
            if not has_quota(sender):
                tg("sendMessage", chat_id=chat, text=limit_text())
                if str(sender) not in state["notified"]:
                    state["notified"].append(str(sender))
                    save_state()
                    tg("sendMessage", chat_id=OWNER_ID, text=(
                        "💳 Bepul limit tugadi\n"
                        f"👤 @{who} (ID: {sender})\n"
                        f"So'ragan INN: {inn}\n\n"
                        f"To'lov qilsa, ruxsat bering:\nruxsat {sender} 20"))
                return
            r = tg("sendMessage", chat_id=chat if is_private else sender, text=fmt_public(rec, date, unit))
            if is_private or (isinstance(r, dict) and r.get("ok")):
                take_quota(sender)
                if not is_private:
                    tg("sendMessage", chat_id=chat, text=DM_SENT)
                tg("sendMessage", chat_id=OWNER_ID, text=(
                    "🔎 Qarz so'raldi" + ("" if is_private else " (guruhdan, shaxsiyga yuborildi)") + "\n"
                    f"👤 @{who} (ID: {sender})\n"
                    f"🏢 {rec['name']}\n"
                    f"INN: {inn}"))
                return
            # guruhda, lekin fermer botga hali /start bosmagan: shaxsiyga yuborib bo'lmadi
            tg("sendMessage", chat_id=chat, text=PRIVATE_ONLY)
            return
        if not is_private:
            tg("sendMessage", chat_id=chat, text=PRIVATE_ONLY)   # topilgan-topilmaganini guruhda bildirmaymiz
            return
        if status == "xato" and os.environ.get("DEBT_SHEET_URL"):
            tg("sendMessage", chat_id=chat, text=NOT_NOW)
            return
        # shaxsiy chatda topilmadi: oddiy suhbat (masalan, telefon raqami) sifatida AI ga ketadi

    hist = histories.setdefault(chat, [])
    hist.append({"role": "user", "content": text})
    hist[:] = hist[-20:]
    while hist and hist[0]["role"] != "user":   # Claude API birinshi xabardıń "user" bolıwın talap etedi
        hist.pop(0)

    try:
        reply = ask_ai(hist)
    except Exception as e:
        print("AI xato:", e)
        tg("sendMessage", chat_id=OWNER_ID, text="AI XATO: " + str(e)[:800])
        reply = "Кеширесиз, ҳәзир жуўап бере алмайман. Кейинирек қайта урынып көриң."
    hist.append({"role": "assistant", "content": reply})

    clean = reply
    m = re.search(r"BUYURTMA:\s*(\{.*\})", reply)
    if m:
        clean = reply.replace(m.group(0), "").strip()
        try:
            order = json.loads(m.group(1))
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))).strftime("%d.%m.%Y %H:%M")
            tg("sendMessage", chat_id=OWNER_ID, text=(
                "🆕 YANGI BUYURTMA\n\n"
                f"👤 Ism: {order.get('ism')}\n"
                f"📞 Telefon: {order.get('telefon')}\n"
                f"🚜 Texnika: {order.get('texnika')}\n"
                f"📍 Hudud: {order.get('hudud')}\n"
                f"💬 Telegram: @{who} (chat: {chat})\n\n"
                f"⏰ {now}"))
            try:
                missing = save_order(order, who, now)
                if missing:
                    tg("sendMessage", chat_id=OWNER_ID,
                       text="⚠️ Jadvalda bu ustunlar topilmadi: " + ", ".join(missing))
            except Exception as e:
                print("Jadval xato:", type(e).__name__)
                tg("sendMessage", chat_id=OWNER_ID,
                   text="⚠️ Buyurtma jadvalga yozilmadi: " + str(e)[:300])
        except Exception as e:
            print("Buyurtma xato:", type(e).__name__)
        if not clean:
            clean = "Рахмет! Буйыртпаңыз қабыл етилди, оператор тез арада сиз бенен байланысады."

    tg("sendMessage", chat_id=chat, text=clean)
    watch(msg, text, clean)

def main():
    load_state()
    mon.setup(tg, BOT_TOKEN, OWNER_ID)
    offset = 0
    print("Bot ishga tushdi.")
    while True:
        try:
            ups = tg("getUpdates", timeout=30, offset=offset).get("result", [])
        except Exception as e:
            print("Telegram xato:", type(e).__name__)   # xabarda bot tokeni bo'lishi mumkin, shuning uchun faqat tur
            time.sleep(5)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            try:
                if u.get("callback_query"):
                    mon.handle_callback(u["callback_query"])
                    continue
                msg = u.get("message")
                if not msg:
                    continue
                handle(msg)
            except Exception as e:
                print("Xato:", mon.safe(e)[:300])
        try:
            mon.tick()                       # saat 9 daǵı kúnlik esabat
        except Exception as e:
            print("Esabat xato:", mon.safe(e)[:300])

if __name__ == "__main__":
    main()
