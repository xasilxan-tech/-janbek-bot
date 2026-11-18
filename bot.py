import os, io, json, re, time, csv, datetime, requests

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
- Never talk about debts, other customers, or anything that is not in this message.
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

histories = {}
offset = 0
print("Bot ishga tushdi.")

while True:
    try:
        ups = tg("getUpdates", timeout=30, offset=offset).get("result", [])
    except Exception as e:
        print("Telegram xato:", e)
        time.sleep(5)
        continue

    for u in ups:
        offset = u["update_id"] + 1
        msg = u.get("message")
        if not msg or "text" not in msg:
            continue
        chat = msg["chat"]["id"]
        text = msg["text"]

        if text == "/start":
            histories.pop(chat, None)
            tg("sendMessage", chat_id=chat,
               text="Ассалаўма әлейкум! Техника бойынша сораўыңызды жазың.")
            continue

        sender = (msg.get("from") or {}).get("id")
        if text.lower().startswith("qarz") and sender == OWNER_ID:
            if msg["chat"].get("type") == "private":
                tg("sendMessage", chat_id=chat, text=find_debts(text[4:].strip().lower()))
            else:
                tg("sendMessage", chat_id=chat,
                   text="🔒 Qarz ma'lumoti guruhda ko'rsatilmaydi. Menga shaxsiy chatda yozing.")
            continue

        hist = histories.setdefault(chat, [])
        hist.append({"role": "user", "content": text})
        hist[:] = hist[-20:]

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
                who = msg["from"].get("username") or msg["from"].get("first_name", "")
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
                    print("Jadval xato:", e)
                    tg("sendMessage", chat_id=OWNER_ID,
                       text="⚠️ Buyurtma jadvalga yozilmadi: " + str(e)[:300])
            except Exception as e:
                print("Buyurtma xato:", e)
            if not clean:
                clean = "Рахмет! Буйыртпаңыз қабыл етилди, оператор тез арада сиз бенен байланысады."

        tg("sendMessage", chat_id=chat, text=clean)
