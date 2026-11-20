"""Janibek IA — texnika monitoringi.

Xızmetker súwret jiberedi -> "Kimdiki?" -> Rayon yamasa INN -> fermer -> texnika ->
tastıyıqlaw -> "Blanka basıldı ma?" -> Google Diskte slayt + kestede "Islendi".
Hár kúni MON_REPORT_HOUR (Tashkent) waqtında svod + Excel dizim jiberiledi.

Railway ózgeriwshileri:
  MON_URL          Apps Script veb-qosımshasınıń URL'i (bolmasa monitoring óshirili)
  MON_KEY          Code_monitoring.gs ishindegi API_KEY menen birdey
  MON_STAFF_IDS    BARLIQ rayonlardı kóretuǵın adminlerdiń Telegram ID'leri, útir menen (OWNER_ID ózi kiredi)
  vakillar.csv     rayon vakilleri: hár biri tek óz rayonın kóredi (Rayon,ID,Ism; ID ornında * = barlıq vakillerge)
  MON_REPORT_CHATS kúnlik esabat barıwı kerek chatlar, útir menen (bos bolsa OWNER_ID)
  MON_REPORT_HOUR  esabat saatı, Tashkent waqtı (ádette 9)
"""
import os, io, re, csv, time, html, base64, datetime, requests

TZ = datetime.timezone(datetime.timedelta(hours=5))
MON_URL = os.environ.get("MON_URL", "").strip()
MON_KEY = os.environ.get("MON_KEY", "").strip()
REPORT_HOUR = int(os.environ.get("MON_REPORT_HOUR", "9"))
MAX_PHOTOS = 8          # bir slaytqa eń kóbi
PAGE = 30               # bir bette neshe fermer túymesi
ROWS_TTL = 300          # kesteni neshe sekundta qayta oqıw
SESSION_TTL = 3 * 3600  # juwapsız qalǵan sessiya neshe waqıttan keyin óshiriledi

_tg = None
_token = ""
_owner = 0
STAFF = set()           # monitoring isley alatuǵınlardıń barlıǵı (adminler + vakiller)
ADMINS = set()          # barlıq rayonlardı kóredi
ACCESS = {}             # vakil ID -> {norm(rayon)}
OPEN_DISTRICTS = set()  # vakillar.csv'de ID ornında "*" turǵan rayonlar
REPORT_CHATS = []

INN = {}          # INN -> [(rayon, fermer)]
INN_BY_KEY = {}   # norm(rayon)|norm(fermer) -> INN

S = {}            # chat -> monitoring sessiyası
L = {}            # chat -> /blanka hám /qaldi ushın rayonlar dizimi

_rows = []
_rows_ts = 0.0
_daily = {"day": "", "next_try": 0.0}


# ---------------------------------------------------------------- tiykarǵı

def setup(tg, token, owner_id):
    global _tg, _token, _owner, STAFF, ADMINS, REPORT_CHATS
    _tg, _token, _owner = tg, token, owner_id
    ADMINS = {owner_id} | {int(x) for x in re.findall(r"-?\d+", os.environ.get("MON_STAFF_IDS", ""))}
    load_access()
    STAFF = ADMINS | set(ACCESS)
    REPORT_CHATS = [int(x) for x in re.findall(r"-?\d+", os.environ.get("MON_REPORT_CHATS", ""))] or [owner_id]
    load_inn()
    print("Monitoring:", "qosılǵan" if enabled() else "óshirili (MON_URL joq)",
          f"| adminler: {len(ADMINS)} | rayon vakilleri: {len(ACCESS)} | INN: {len(INN)}", flush=True)


def load_access():
    ACCESS.clear()
    OPEN_DISTRICTS.clear()
    try:
        with open("vakillar.csv", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                d = norm(r.get("Rayon") or "")
                ids = r.get("ID") or ""
                if not d:
                    continue
                if "*" in ids:
                    OPEN_DISTRICTS.add(d)
                for x in re.findall(r"\d+", ids):
                    ACCESS.setdefault(int(x), set()).add(d)
    except FileNotFoundError:
        print("Monitoring: vakillar.csv tabılmadı, tek adminler isley aladı", flush=True)


def enabled():
    return bool(MON_URL and MON_KEY)


def is_staff(uid):
    return uid in STAFF


def is_admin(uid):
    return uid in ADMINS


def allowed(uid, d):
    """Bul adam usı rayonnıń monitoringin isley ala ma?"""
    if uid in ADMINS:
        return True
    nd = norm(d)
    return nd in ACCESS.get(uid, ()) or (uid in ACCESS and nd in OPEN_DISTRICTS)


def my_districts(uid):
    return [d for d in districts() if allowed(uid, d)]


def safe(e):
    """Qáte tekstinen bot tokenin alıp taslaydı (fayl júklew URL'inde token boladı)."""
    s = f"{type(e).__name__}: {e}"
    return s.replace(_token, "***") if _token else s


def norm(s):
    s = str(s).upper()
    for ch in "'`‘’ʻʼ\"":
        s = s.replace(ch, "")
    s = re.sub(r"[^0-9A-ZА-ЯЁЎҚҒҲӘӨҮҢ]+", " ", s)
    return " ".join(s.split())


def short(d):
    return d.replace(" tumani", "").replace(" shahri", " sh.")


def load_inn():
    INN.clear()
    INN_BY_KEY.clear()
    try:
        with open("inn.csv", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                inn = (r.get("INN") or "").strip()
                d, o = (r.get("Район") or "").strip(), (r.get("Владелец") or "").strip()
                if not inn or not o:
                    continue
                INN.setdefault(inn, []).append((d, o))
                INN_BY_KEY[norm(d) + "|" + norm(o)] = inn
    except FileNotFoundError:
        print("Monitoring: inn.csv tabılmadı, INN boyınsha izlew islemeydi")


def inn_of(d, o):
    return INN_BY_KEY.get(norm(d) + "|" + norm(o), "")


def gas(action, **kw):
    r = requests.post(MON_URL, json={"key": MON_KEY, "action": action, **kw}, timeout=300)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Apps Script juwabı JSON emes (HTTP {r.status_code}). URL'di tekseriń.")
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "Apps Script qátesi")
    return data


def rows(force=False):
    global _rows, _rows_ts
    if force or not _rows or time.time() - _rows_ts > ROWS_TTL:
        _rows = gas("list")["rows"]
        _rows_ts = time.time()
    return _rows


def done(r):
    return r.get("m") == "Islendi"


def districts():
    return sorted({r["d"] for r in rows()})


def farmers(d):
    return sorted({r["o"] for r in rows() if r["d"] == d and not done(r)})


def techs(d, o):
    return [r for r in rows() if r["d"] == d and r["o"] == o and not done(r)]


def row_by(n):
    return next((r for r in rows() if r["r"] == n), None)


def tech_label(r):
    """Túyme jazıwı. Kóp danalı qatarlarda (Borona 'BZTX') dana kórsetilmeydi."""
    extra = ""
    for v in (r.get("g"), r.get("c")):
        if v and v != "-":
            extra = v
            break
    t = r["t"] if len(r["t"]) <= 40 else r["t"][:39] + "…"
    return f"{t} · {extra}" if extra else t


def letter(o):
    n = norm(o)
    return n[0] if n and n[0].isalpha() else "#"


# ---------------------------------------------------------------- Telegram

def btn(text, data):
    return {"text": text[:64], "callback_data": data}


def grid(buttons, cols):
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def send(chat, text, kb=None, **kw):
    p = {"chat_id": chat, "text": text, "link_preview_options": {"is_disabled": True}}
    if kb is not None:
        p["reply_markup"] = {"inline_keyboard": kb}
    p.update(kw)
    return _tg("sendMessage", **p)


def edit(chat, mid, text, kb=None):
    p = {"chat_id": chat, "message_id": mid, "text": text, "link_preview_options": {"is_disabled": True}}
    if kb is not None:
        p["reply_markup"] = {"inline_keyboard": kb}
    r = _tg("editMessageText", **p)
    if not r.get("ok") and "not modified" not in str(r.get("description", "")):
        return send(chat, text, kb)
    return r


def who_name(frm):
    n = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
    if frm.get("username"):
        n = f"{n} (@{frm['username']})".strip()
    return n or str(frm.get("id"))


def download(fid):
    f = _tg("getFile", file_id=fid)
    if not f.get("ok"):
        raise RuntimeError("Telegram faylın alıw múmkin bolmadı")
    r = requests.get(f"https://api.telegram.org/file/bot{_token}/{f['result']['file_path']}", timeout=120)
    r.raise_for_status()
    b = r.content
    mime = "image/png" if b[:4] == b"\x89PNG" else "image/jpeg"
    return {"b64": base64.b64encode(b).decode(), "mime": mime}


# ---------------------------------------------------------------- súwret

def photo_file(msg):
    """(file_id, qáte_teksti)"""
    if msg.get("photo"):
        sizes = sorted(msg["photo"], key=lambda p: p.get("width", 0) * p.get("height", 0))
        best = sizes[-1]
        for p in sizes:
            if max(p.get("width", 0), p.get("height", 0)) <= 1600:
                best = p
        return best["file_id"], None
    d = msg.get("document")
    if d and str(d.get("mime_type", "")).startswith("image/"):
        if d.get("mime_type") not in ("image/jpeg", "image/png"):
            return None, "Bul formattaǵı súwret qabıl etilmeydi. Súwretti «Foto» sıpatında jiberiń."
        if d.get("file_size", 0) > 15 * 1024 * 1024:
            return None, "Súwret júdá úlken. «Foto» sıpatında (qısılǵan) jiberiń."
        return d["file_id"], None
    return None, None


def who_text(s):
    return f"📷 Súwretler: {len(s['photos'])}\n\nBul kimdiki?"


def who_kb():
    return [[btn("📍 Rayon", "w:r"), btn("🔢 INN", "w:i")], [btn("❌ Biykar etiw", "x")]]


def handle_photo(msg):
    """Xızmetkerdiń jeke chattaǵı súwretin qabıl etedi. Qabıl etse True qaytaradı."""
    if not enabled() or msg.get("chat", {}).get("type") != "private":
        return False
    frm = msg.get("from") or {}
    if not is_staff(frm.get("id")):
        return False
    fid, err = photo_file(msg)
    chat = msg["chat"]["id"]
    if err:
        send(chat, err)
        return True
    if not fid:
        return False
    s = S.get(chat)
    if s and s.get("busy"):
        send(chat, "⏳ Aldıńǵı slayt saqlanıp atır. Ol tayar bolǵannan keyin jańa súwretlerdi jiberiń.")
        return True
    if not s or time.time() - s.get("t", 0) > SESSION_TTL:
        s = S[chat] = {"photos": [], "stage": "who", "t": time.time(), "uid": frm.get("id")}
    if len(s["photos"]) >= MAX_PHOTOS:
        if not s.get("warned"):
            s["warned"] = True
            send(chat, f"Bir slaytqa eń kóbi {MAX_PHOTOS} súwret. Artıqları qosılmadı.")
        return True
    s["photos"].append(fid)
    s["t"] = time.time()
    if not s.get("mid"):
        r = send(chat, who_text(s), who_kb())
        s["mid"] = r.get("result", {}).get("message_id")
    elif s.get("stage") == "who":
        edit(chat, s["mid"], who_text(s), who_kb())
    return True


# ---------------------------------------------------------------- ekranlar

def show_who(chat, s):
    s["stage"] = "who"
    edit(chat, s["mid"], who_text(s), who_kb())


def pick_district(chat, s, d):
    s["d"] = d
    fs = farmers(d)
    if len(fs) <= PAGE:
        s.pop("letters", None)
        show_farmers(chat, s, [(d, o) for o in fs])
    else:
        show_letters(chat, s)


def show_districts(chat, s):
    s["stage"] = "district"
    s["dl"] = my_districts(s.get("uid"))
    if not s["dl"]:
        edit(chat, s["mid"], "Sizge hesh bir rayon biriktirilmegen.", [[btn("❌ Biykar", "x")]])
        return
    if len(s["dl"]) == 1:          # vakilge tek óz rayonı: birden fermerlerge ótemiz
        s["single"] = True
        pick_district(chat, s, s["dl"][0])
        return
    s.pop("single", None)
    left = {}
    for r in rows():
        if not done(r):
            left[r["d"]] = left.get(r["d"], 0) + 1
    b = [btn(f"{short(d)} · {left.get(d, 0)}", f"d:{i}") for i, d in enumerate(s["dl"])]
    kb = grid(b, 2) + [[btn("⬅️ Artqa", "bk:w"), btn("❌ Biykar", "x")]]
    edit(chat, s["mid"], f"📷 Súwretler: {len(s['photos'])}\n\n📍 Rayondı tańlań (sanı: qalǵan texnika):", kb)


def show_letters(chat, s):
    s["stage"] = "letter"
    s["letters"] = True
    fs = farmers(s["d"])
    counts = {}
    for o in fs:
        counts[letter(o)] = counts.get(letter(o), 0) + 1
    b = [btn(f"{ch} ({n})", f"l:{ch}") for ch, n in sorted(counts.items())]
    kb = grid(b, 4) + [[btn("⬅️ Artqa", "bk:w" if s.get("single") else "bk:d"), btn("❌ Biykar", "x")]]
    edit(chat, s["mid"], f"📍 {s['d']}\n\nFermer atınıń birinshi háribin tańlań:", kb)


def show_farmers(chat, s, fl, page=0):
    s["stage"] = "farmer"
    s["fl"] = fl
    s["pg"] = page
    part = fl[page * PAGE:(page + 1) * PAGE]
    b = [[btn(o, f"f:{page * PAGE + i}")] for i, (d, o) in enumerate(part)]
    nav = []
    if page > 0:
        nav.append(btn("◀️ Aldınǵı", f"p:{page - 1}"))
    if (page + 1) * PAGE < len(fl):
        nav.append(btn("Keyingi ▶️", f"p:{page + 1}"))
    kb = b + ([nav] if nav else []) + [[btn("⬅️ Artqa", "bk:l"), btn("❌ Biykar", "x")]]
    head = f"📍 {s.get('d', '')}\n\n👤 Fermerdi tańlań ({len(fl)}):" if fl else "Fermer tabılmadı."
    edit(chat, s["mid"], head, kb)


def show_techs(chat, s):
    s["stage"] = "tech"
    d, o = s["d"], s["o"]
    ts = techs(d, o)
    inn = inn_of(d, o)
    head = f"📍 {d}\n👤 {o}" + (f"\n🔢 INN: {inn}" if inn else "")
    back = [[btn("⬅️ Artqa", "bk:f"), btn("❌ Biykar", "x")]]
    if not ts:
        edit(chat, s["mid"], head + "\n\n✅ Bul fermerdiń barlıq texnikası tekserilgen.", back)
        return
    kb = [[btn(tech_label(r), f"t:{r['r']}")] for r in ts] + back
    edit(chat, s["mid"], head + f"\n\n🚜 Texnikanı tańlań (qaldı: {len(ts)}):", kb)


def confirm_text(s, r):
    inn = inn_of(r["d"], r["o"])
    lines = ["Tekseriń, durıs pa?", "",
             f"📍 Rayon: {r['d']}",
             f"👤 Fermer: {r['o']}"]
    if inn:
        lines.append(f"🔢 INN: {inn}")
    lines.append(f"🚜 Texnika: {r['t']}")
    if r.get("g") and r["g"] != "-":
        lines.append(f"🔖 Gos nomer: {r['g']}")
    if r.get("c") and r["c"] != "-":
        lines.append(f"🔖 Shassi: {r['c']}")
    lines.append(f"📷 Súwretler: {len(s['photos'])}")
    return "\n".join(lines)


def show_confirm(chat, s):
    s["stage"] = "confirm"
    r = row_by(s["row"])
    if not r or done(r):
        edit(chat, s["mid"], "Bul texnika endi dizimde joq (basqa xızmetker islegen bolıwı múmkin).",
             [[btn("⬅️ Artqa", "bk:t"), btn("❌ Biykar", "x")]])
        return
    kb = [[btn("✅ Tastıyıqlaw", "ok"), btn("❌ Biykar etiw", "x")], [btn("⬅️ Artqa", "bk:t")]]
    edit(chat, s["mid"], confirm_text(s, r), kb)


def show_blanka_q(chat, s):
    s["stage"] = "blanka"
    kb = [[btn("✅ Awa", "b:1"), btn("❌ Yaq", "b:0")], [btn("⬅️ Artqa", "bk:c")]]
    edit(chat, s["mid"], "📄 Monitoring blankası basıldı ma?", kb)


def submit(chat, s, blanka, frm):
    r = row_by(s["row"])
    if not r:
        raise RuntimeError("Texnika dizimnen tabılmadı")
    s["busy"] = True
    edit(chat, s["mid"], f"⏳ Slayt jasalıp atır ({len(s['photos'])} súwret)...")
    try:
        photos = [download(fid) for fid in s["photos"]]
        res = gas("submit", row=r["r"], district=r["d"], owner=r["o"], tech=r["t"],
                  chassis=r.get("c", ""), plate=r.get("g", ""),
                  blanka="Awa" if blanka else "Yaq", who=who_name(frm), photos=photos)
    except Exception:
        s["busy"] = False
        raise
    r.update({"m": "Islendi", "md": res.get("date", ""), "b": "Awa" if blanka else "Yaq",
              "bd": res.get("date", "") if blanka else "", "s": res.get("url", "")})
    left = res.get("left", len(techs(r["d"], r["o"])))
    S.pop(chat, None)
    text = ("✅ Saqlandı\n\n"
            f"📍 {r['d']}\n👤 {r['o']}\n🚜 {r['t']}\n"
            f"📄 Blanka: {'Awa' if blanka else 'Yaq'}\n"
            f"🖼 Slayt: {res.get('url', '')}\n\n")
    text += (f"Bul fermerde qaldı: {left} texnika. Keyingi texnikanıń súwretlerin jiberiń."
             if left else "Bul fermerdiń barlıq texnikası tekserildi ✅")
    edit(chat, s["mid"], text)


# ---------------------------------------------------------------- túymeler

def handle_callback(cq):
    data = cq.get("data", "")
    frm = cq.get("from") or {}
    m = cq.get("message") or {}
    chat = (m.get("chat") or {}).get("id")
    mid = m.get("message_id")
    try:
        _tg("answerCallbackQuery", callback_query_id=cq["id"])
    except Exception:
        pass
    if not chat or not is_staff(frm.get("id")) or not enabled():
        return
    try:
        if data.startswith(("bd:", "bt:", "qd:")):
            return list_callback(chat, mid, data, frm.get("id"))
        s = S.get(chat)
        if not s or s.get("mid") != mid:
            edit(chat, mid, "Bul sorawdıń waqtı ótken. Súwretlerdi qaytadan jiberiń.")
            return
        if s.get("busy"):
            return
        s["t"] = time.time()
        if data == "x":
            S.pop(chat, None)
            edit(chat, mid, "❌ Biykar etildi. Súwretler óshirildi.")
        elif data == "w:r" or data == "bk:d":
            s.pop("letters", None)
            show_districts(chat, s)
        elif data == "w:i":
            s["stage"] = "inn"
            edit(chat, mid, "🔢 Fermerdiń INN nomerin jazıń (9 san):",
                 [[btn("⬅️ Artqa", "bk:w"), btn("❌ Biykar", "x")]])
        elif data == "bk:w":
            show_who(chat, s)
        elif data.startswith("d:"):
            pick_district(chat, s, s["dl"][int(data[2:])])
        elif data.startswith("l:"):
            ch = data[2:]
            show_farmers(chat, s, [(s["d"], o) for o in farmers(s["d"]) if letter(o) == ch])
        elif data == "bk:l":
            if s.get("inn_mode"):
                show_who(chat, s)
            elif s.get("letters"):
                show_letters(chat, s)
            elif s.get("single"):
                show_who(chat, s)
            else:
                show_districts(chat, s)
        elif data.startswith("p:"):
            show_farmers(chat, s, s.get("fl", []), int(data[2:]))
        elif data.startswith("f:"):
            s["d"], s["o"] = s["fl"][int(data[2:])]
            show_techs(chat, s)
        elif data == "bk:f":
            if s.get("fl"):
                show_farmers(chat, s, s["fl"], s.get("pg", 0))
            else:
                show_who(chat, s)
        elif data.startswith("t:"):
            r = row_by(int(data[2:]))
            if not r or not allowed(frm.get("id"), r["d"]):
                edit(chat, mid, "⛔ Bul texnika sizdiń rayonıńızǵa tiyisli emes.", [[btn("❌ Biykar", "x")]])
                return
            s["row"] = r["r"]
            show_confirm(chat, s)
        elif data == "bk:t":
            show_techs(chat, s)
        elif data == "ok":
            r = row_by(s.get("row"))
            if not r or done(r):
                show_confirm(chat, s)
            else:
                show_blanka_q(chat, s)
        elif data == "bk:c":
            show_confirm(chat, s)
        elif data in ("b:1", "b:0"):
            try:
                submit(chat, s, data == "b:1", frm)
            except Exception as e:
                print("Monitoring submit qáte:", safe(e))
                v = data
                edit(chat, mid, "⚠️ Saqlanbadı: " + safe(e)[:300],
                     [[btn("🔁 Qaytadan", v), btn("❌ Biykar", "x")]])
    except Exception as e:
        print("Monitoring qáte:", safe(e))
        send(chat, "⚠️ Qáte: " + safe(e)[:300])


# ---------------------------------------------------------------- tekst

def handle_text(msg):
    """Xızmetkerdiń monitoring buyrıqları hám INN kirgiziwi. Qabıl etse True."""
    if not enabled() or msg.get("chat", {}).get("type") != "private":
        return False
    frm = msg.get("from") or {}
    if not is_staff(frm.get("id")):
        return False
    chat = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    s = S.get(chat)
    try:
        if s and s.get("stage") == "inn" and not text.startswith("/"):
            find_by_inn(chat, s, text)
            return True
        cmd = text.split()[0].lower().split("@")[0] if text else ""
        arg = text[len(text.split()[0]):].strip() if text else ""
        if cmd == "/bekor":
            S.pop(chat, None)
            send(chat, "❌ Monitoring sessiyası biykar etildi.")
            return True
        uid = frm.get("id")
        if cmd == "/esabat":
            send(chat, "⏳ Esabat tayarlanıp atır...")
            only = None if is_admin(uid) else {norm(d) for d in my_districts(uid)}
            send_report([chat], daily=False, only=only)
            return True
        if cmd == "/qaldi":
            return list_command(chat, uid, arg, "q")
        if cmd == "/blanka":
            return list_command(chat, uid, arg, "b")
        if cmd in ("/monitoring", "/komek"):
            send(chat, HELP)
            return True
    except Exception as e:
        print("Monitoring qáte:", safe(e))
        send(chat, "⚠️ Qáte: " + safe(e)[:300])
        return True
    return False


HELP = ("📋 Monitoring\n\n"
        "1. Bir fermerdiń texnikasınıń súwretlerin jiberiń (eń kóbi 8).\n"
        "2. «Kimdiki?» → 📍 Rayon yamasa 🔢 INN.\n"
        "3. Fermerdi hám texnikanı tańlań, tastıyıqlań.\n"
        "4. «Blanka basıldı ma?» → Awa / Yaq.\n"
        "Slayt Google Diskke saqlanadı, kestede «Islendi» dep belgilenedi.\n\n"
        "Buyrıqlar:\n"
        "/esabat — rayonlar kesiminde svod + Excel dizim\n"
        "/qaldi [rayon] — ele islenbegen texnikalar\n"
        "/blanka [rayon] — blankası basılmaǵanlar (basıp belgilew)\n"
        "/bekor — baslanǵan monitoringti biykar etiw")


def find_by_inn(chat, s, text):
    inn = re.sub(r"\D", "", text)
    if len(inn) != 9:
        send(chat, "INN 9 sannan ibarat. Qaytadan jazıń yamasa /bekor.")
        return
    pairs = []
    for d, o in INN.get(inn, []):
        for r in rows():
            if norm(r["d"]) == norm(d) and norm(r["o"]) == norm(o):
                pairs.append((r["d"], r["o"]))
                break
    s["inn_mode"] = True
    mine = [p for p in pairs if allowed(s.get("uid"), p[0])]
    if pairs and not mine:
        send(chat, f"⛔ {inn}: bul fermer {pairs[0][0]} rayonına tiyisli. Siz tek óz rayonıńızdı isley alasız.\n"
                   "Basqa INN jazıń yamasa /bekor.")
        return
    pairs = mine
    if not pairs:
        r = send(chat, f"🔢 {inn} boyınsha fermer tabılmadı. Rayon arqalı izleń:",
                 [[btn("📍 Rayon", "w:r"), btn("❌ Biykar", "x")]])
        s["mid"] = r.get("result", {}).get("message_id")
        s["stage"] = "who"
        return
    r = send(chat, "⏳")
    s["mid"] = r.get("result", {}).get("message_id")
    if len(pairs) == 1:
        s["fl"] = None
        s["d"], s["o"] = pairs[0]
        show_techs(chat, s)
    else:
        show_farmers(chat, s, pairs)


def resolve_district(arg, dl):
    a = norm(arg)
    if not a:
        return None
    for d in dl:
        if norm(d).startswith(a) or norm(short(d)).startswith(a):
            return d
    return None


def list_command(chat, uid, arg, kind):
    dl = my_districts(uid)
    if not dl:
        send(chat, "Sizge hesh bir rayon biriktirilmegen.")
        return True
    d = resolve_district(arg, dl) or (dl[0] if len(dl) == 1 else None)
    if d:
        if kind == "q":
            send_left_list(chat, d)
        else:
            show_blanka_list(chat, None, d)
        return True
    L[chat] = dl
    if kind == "q":
        cnt = {x: sum(1 for r in rows() if r["d"] == x and not done(r)) for x in dl}
        title = "Qaysı rayon boyınsha qalǵan texnikalar kerek?"
    else:
        cnt = {x: sum(1 for r in rows() if r["d"] == x and done(r) and r.get("b") != "Awa") for x in dl}
        title = "Blankası basılmaǵanlar. Rayondı tańlań:"
    b = [btn(f"{short(x)} · {cnt[x]}", f"{kind}d:{i}") for i, x in enumerate(dl)]
    send(chat, title, grid(b, 2))
    return True


def send_left_list(chat, d):
    by = {}
    for r in rows():
        if r["d"] == d and not done(r):
            by.setdefault(r["o"], []).append(r)
    if not by:
        send(chat, f"📍 {d}\n\n✅ Barlıq texnika tekserilgen.")
        return
    total = sum(len(v) for v in by.values())
    lines = [f"📍 {d}: qaldı {total} texnika, {len(by)} fermer", ""]
    for o in sorted(by):
        lines.append(f"👤 {o} ({len(by[o])})")
        for r in by[o]:
            lines.append(f"   • {tech_label(r)}")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3800:
            send(chat, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        send(chat, chunk)


def show_blanka_list(chat, mid, d, note=""):
    need = [r for r in rows() if r["d"] == d and done(r) and r.get("b") != "Awa"]
    head = (note + "\n\n" if note else "") + f"📍 {d}\n📄 Blankası basılmaǵan: {len(need)}"
    if not need:
        kb = None
        text = head + "\n\n✅ Hámmesiniń blankası basılǵan."
    else:
        kb = [[btn(f"{r['o'][:28]} · {tech_label(r)[:30]}", f"bt:{r['r']}")] for r in need[:40]]
        text = head + "\n\nBlankası basılǵanın basıp belgileń:"
        if len(need) > 40:
            text += f"\n(tek birinshi 40 kórsetildi)"
    if mid:
        edit(chat, mid, text, kb)
    else:
        send(chat, text, kb)


def list_callback(chat, mid, data, uid):
    if data.startswith(("bd:", "qd:")):
        dl = L.get(chat) or my_districts(uid)
        d = dl[int(data[3:])]
        if not allowed(uid, d):
            return
        if data.startswith("qd:"):
            send_left_list(chat, d)
        else:
            show_blanka_list(chat, mid, d)
        return
    r = row_by(int(data[3:]))
    if not r or not allowed(uid, r["d"]):
        send(chat, "Texnika tabılmadı.")
        return
    gas("blanka", row=r["r"], owner=r["o"], tech=r["t"])
    r["b"] = "Awa"
    r["bd"] = datetime.datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    show_blanka_list(chat, mid, r["d"], note=f"✅ Belgilendi: {r['o']} · {r['t']}")


# ---------------------------------------------------------------- esabat

def pct(a, b):
    return f"{a * 100 / b:.1f}".replace(".", ",") if b else "0,0"


def table(svod, key):
    lines = [f"{'Rayon':<11}{'Jámi':>5}{'Isl.':>5}{'Qal.':>5}{'%':>6}"]
    T = [0, 0]
    for a in svod:
        x = a[key]
        T[0] += a["total"]
        T[1] += x
        lines.append(f"{short(a['d'])[:11]:<11}{a['total']:>5}{x:>5}{a['total'] - x:>5}{pct(x, a['total']):>6}")
    lines.append("-" * 32)
    lines.append(f"{'JÁMI':<11}{T[0]:>5}{T[1]:>5}{T[0] - T[1]:>5}{pct(T[1], T[0]):>6}")
    return html.escape("\n".join(lines))


def report_text(svod, date):
    my = sum(a.get("mon_y", 0) for a in svod)
    by = sum(a.get("bl_y", 0) for a in svod)
    mt = sum(a.get("mon_t", 0) for a in svod)
    bt = sum(a.get("bl_t", 0) for a in svod)
    return (f"<b>📊 Monitoring · {date}</b>\n<pre>{table(svod, 'mon')}</pre>\n"
            f"<b>📄 Blanka</b>\n<pre>{table(svod, 'bl')}</pre>\n"
            f"Keshe: +{my} monitoring, +{by} blanka\n"
            f"Búgin: +{mt} monitoring, +{bt} blanka")


def report_xlsx(svod, all_rows, date):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Svod"
    head = ["Rayon", "Jámi", "Monitoring islendi", "Monitoring qaldı", "Monitoring %",
            "Blanka basıldı", "Blanka qaldı", "Blanka %"]
    ws.append([f"Monitoring esabatı · {date}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(head)
    T = [0, 0, 0]
    for a in svod:
        t, m, b = a["total"], a["mon"], a["bl"]
        T[0] += t; T[1] += m; T[2] += b
        ws.append([a["d"], t, m, t - m, m / t if t else 0, b, t - b, b / t if t else 0])
    ws.append(["JÁMI", T[0], T[1], T[0] - T[1], T[1] / T[0] if T[0] else 0,
               T[2], T[0] - T[2], T[2] / T[0] if T[0] else 0])
    fill = PatternFill("solid", fgColor="D9E2F3")
    for c in ws[3]:
        c.font = Font(bold=True)
        c.fill = fill
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for row in ws.iter_rows(min_row=4, max_row=ws.max_row):
        row[4].number_format = "0.0%"
        row[7].number_format = "0.0%"
    for col, w in zip("ABCDEFGH", (22, 8, 18, 18, 14, 15, 13, 10)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A4"

    ws2 = wb.create_sheet("Dizim")
    ws2.append(["Rayon", "Fermer", "INN", "Texnika", "Soni", "Gos nomer", "Shassi",
                "Monitoring", "Monitoring sánesi", "Kim", "Blanka", "Blanka sánesi", "Slayt"])
    for c in ws2[1]:
        c.font = Font(bold=True)
        c.fill = fill
    for r in all_rows:
        ws2.append([r["d"], r["o"], inn_of(r["d"], r["o"]), r["t"], r["q"], r.get("g", ""), r.get("c", ""),
                    "Awa" if done(r) else "Yaq", r.get("md", ""), r.get("k", ""),
                    "Awa" if r.get("b") == "Awa" else "Yaq", r.get("bd", ""), r.get("s", "")])
    ws2.auto_filter.ref = ws2.dimensions
    ws2.freeze_panes = "A2"
    for col, w in zip("ABCDEFGHIJKLM", (18, 34, 12, 44, 6, 12, 20, 11, 17, 22, 8, 17, 30)):
        ws2.column_dimensions[col].width = w
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def send_report(chats, daily, only=None):
    """only: rayonlar (norm) — vakilge tek óz rayonları kórsetiledi; None = barlıǵı."""
    res = gas("report", daily=daily)
    if res.get("skip"):
        return False
    svod, date = res["svod"], res["date"]
    all_rows = res.get("rows", [])
    if only is not None:
        svod = [a for a in svod if norm(a["d"]) in only]
        all_rows = [r for r in all_rows if norm(r["d"]) in only]
    text = report_text(svod, date)
    xlsx = report_xlsx(svod, all_rows, date)
    for c in chats:
        _tg("sendMessage", chat_id=c, text=text, parse_mode="HTML")
        try:
            requests.post(f"https://api.telegram.org/bot{_token}/sendDocument",
                          data={"chat_id": c, "caption": f"📎 Monitoring dizimi · {date}"},
                          files={"document": (f"monitoring_{date}.xlsx", xlsx)}, timeout=120)
        except Exception as e:
            print("Esabat faylı jiberilmedi:", safe(e))
    return True


def tick():
    """Tiykarǵı ciklda shaqırıladı: saat REPORT_HOUR'da kúnlik esabat."""
    if not enabled():
        return
    now = datetime.datetime.now(TZ)
    day = now.strftime("%Y-%m-%d")
    if now.hour < REPORT_HOUR or _daily["day"] == day or time.time() < _daily["next_try"]:
        return
    try:
        if send_report(REPORT_CHATS, daily=True):
            gas("daily_done")
        _daily["day"] = day
    except Exception as e:
        print("Kúnlik esabat qáte:", safe(e))
        _daily["next_try"] = time.time() + 600
    # eski sessiyalardı tazalaw
    for chat in [c for c, s in S.items() if time.time() - s.get("t", 0) > SESSION_TTL and not s.get("busy")]:
        S.pop(chat, None)
