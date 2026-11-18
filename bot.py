import os, json, re, time, requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
AI_KEY = os.environ["ANTHROPIC_API_KEY"]
OWNER_ID = int(os.environ.get("OWNER_ID", "721769003"))
MODEL = "claude-haiku-5-5"
TG = f"https://api.telegram.org/bot{BOT_TOKEN}/"

with open("narxlar_2026.csv", encoding="utf-8-sig") as f:
    PRICES = f.read()

SYSTEM = f"""Sen "Fermer ham texnika" kanalining yordamchisisan. Qishloq xo'jaligi texnikalari bo'yicha ma'lumot berasan. Biz texnika sotamiz va eski texnikani sotib olamiz.

Qoidalar:
- Faqat qoraqalpoq tilida, kirill alifbosida javob ber.
- Narx so'ralsa, quyidagi ro'yxatdagi ZAVOD NARXINI ayt. Ro'yxatda yo'q texnika haqida taxmin qilma, "Aniq ma'lumot uchun operator bilan bog'laning" deb ayt.
- Har narxdan keyin "Narx o'zgarishi mumkin, aniq narxni operator tasdiqlaydi" deb qo'sh.
- Mijoz buyurtma bermoqchi bo'lsa, ketma-ket so'ra: ism, telefon raqami, kerakli texnika, hudud (viloyat/tuman).
- 4 ta ma'lumot to'liq bo'lgach, javobingning oxirida faqat bitta qator yoz:
BUYURTMA: {{"ism": "...", "telefon": "...", "texnika": "...", "hudud": "..."}}
Bu qatorni mijozga ko'rsatilmaydi, uni dastur o'zi olib tashlaydi.
- Javoblar qisqa va aniq bo'lsin.

Zavod narxlari ro'yxati (etkazib beruvchi, tur, rusum, narx so'mda):
{PRICES}
"""

def tg(method, **kw):
    return requests.post(TG + method, json=kw, timeout=60).json()

def ask_ai(history):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": AI_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 800, "system": SYSTEM, "messages": history},
        timeout=120,
    )
    data = r.json()
    return data["content"][0]["text"]

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
               text="Assalomu alaykum! Texnika bo'yicha savolingizni yozing.")
            continue

        hist = histories.setdefault(chat, [])
        hist.append({"role": "user", "content": text})
        hist[:] = hist[-20:]

        try:
            reply = ask_ai(hist)
        except Exception as e:
            print("AI xato:", e)
            reply = "Kechirasiz, hozir javob bera olmayman. Keyinroq urinib ko'ring."
        hist.append({"role": "assistant", "content": reply})

        clean = reply
        m = re.search(r"BUYURTMA:\s*(\{.*\})", reply)
        if m:
            clean = reply.replace(m.group(0), "").strip()
            try:
                order = json.loads(m.group(1))
                who = msg["from"].get("username") or msg["from"].get("first_name", "")
                tg("sendMessage", chat_id=OWNER_ID, text=(
                    "Yangi buyurtma!\n"
                    f"Ism: {order.get('ism')}\n"
                    f"Telefon: {order.get('telefon')}\n"
                    f"Texnika: {order.get('texnika')}\n"
                    f"Hudud: {order.get('hudud')}\n"
                    f"Telegram: @{who} (chat: {chat})"))
            except Exception as e:
                print("Buyurtma xato:", e)
            if not clean:
                clean = "Rahmat! Buyurtmangiz qabul qilindi, operator tez orada siz bilan bog'lanadi."

        tg("sendMessage", chat_id=chat, text=clean)
