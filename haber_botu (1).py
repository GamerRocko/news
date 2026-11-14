#!/usr/bin/env python3
"""Oyun haberleri botu: RSS -> popülerlik -> Türkçe özet (ücretsiz API) -> statik site.

Kurulum:   pip install feedparser trafilatura requests
Çalıştır:  export LLM_API_KEY="..." ; python haber_botu.py
Çıktı:     data/haberler.json  ve  site/index.html
"""
import os, re, sys, json, time, html, hashlib, difflib, datetime as dt, pathlib
import socket
import feedparser, requests, trafilatura

socket.setdefaulttimeout(20)  # yanıt vermeyen siteler en fazla 20 sn bekletsin

# ---------- AYARLAR ----------
FEEDS = [  # (sitede görünecek kaynak adı, RSS adresi) -> adı istediğin gibi yazabilirsin
    ("Destructoid", "http://www.destructoid.com/?mode=atom"),
    ("Eurogamer", "https://www.eurogamer.net/feed/news"),
    ("Game Informer", "https://gameinformer.com/news.xml"),
    ("Gameguru Mania", "http://www.ggmania.com/rss/index.xml"),
    ("GamesIndustry.biz", "http://www.gamesindustry.biz/rss/gamesindustry_news_feed.rss"),
    ("GameSpot", "https://www.gamespot.com/feeds/mashup/"),
    ("GamesRadar", "http://www.gamesradar.com/all-platforms/news/rss/"),
    ("IGN", "http://feeds.feedburner.com/ign/news"),
    ("Kotaku", "http://kotaku.com/index.xml"),
    ("PCGamesN", "http://www.pcgamesn.com/mainrss.xml"),
    ("Polygon", "http://www.polygon.com/rss/index.xml"),
    ("RockPaperShotgun", "http://feeds.feedburner.com/RockPaperShotgun"),
]
# Sağlayıcıyı değiştirmek için sadece bu üç satır yeter (hepsi OpenAI uyumlu):
#   Groq:    https://api.groq.com/openai/v1               model: llama-3.3-70b-versatile
#   Gemini:  https://generativelanguage.googleapis.com/v1beta/openai   model: gemini-3.5-flash-lite
#   Ollama:  http://localhost:11434/v1                    model: qwen2.5:14b
API_BASE = os.getenv("LLM_API_BASE", "https://generativelanguage.googleapis.com/v1beta/openai")
MODEL = os.getenv("LLM_MODEL", "gemini-3.5-flash-lite")   # 404 alırsan: python haber_botu.py --modeller
# API anahtarı koda YAZILMAZ. Sırasıyla şuralardan okunur:
#  1) LLM_API_KEY ortam değişkeni (GitHub Actions'ta secret'tan gelir)
#  2) bu klasördeki anahtar.txt dosyası (sadece kendi bilgisayarın için; GitHub'a yükleme!)
_dosya = pathlib.Path("anahtar.txt")
API_KEY = os.getenv("LLM_API_KEY") or (_dosya.read_text(encoding="utf-8").strip() if _dosya.exists() else "")
HABER_SAYISI = 8          # her çalışmada en fazla kaç haber
SAAT_SINIRI = 36          # kaç saatten eski haberler alınmasın
SAKLANAN = 200            # sitede tutulacak toplam haber
ILK_GOSTERIM = 15         # sayfa ilk açıldığında görünen haber sayısı
ADIM = 10                 # "Daha fazla" düğmesine her basışta açılacak haber sayısı
TR = dt.timezone(dt.timedelta(hours=3))   # Türkiye saati (UTC+3)
GORSEL_ZORUNLU = {"GGMania"}   # Bu kaynaklardan görseli bulunamayan haberler yayınlanmaz (kaynak adları FEEDS'tekiyle aynı olmalı)
MAKS_ISTEK = 20           # her çalışmada yapay zekaya en fazla kaç istek gönderilsin (kota koruması)
ONAR_LIMIT = 30           # her çalışmada eski haberlerden en fazla kaçının başlığı yeniden onarılsın (bir kerelik)
SON_KONTROL_SAAT = 72     # yeni haber, son kaç saatteki haberlerle konu benzerliği için karşılaştırılsın

# Başlıklarda HER ZAMAN bu yazımla görünecek özel isimler ve kısaltmalar.
# Başlıkta yanlış küçük harfle çıkan bir isim görürsen buraya ekle (büyük/küçük harfi doğru yaz).
SABIT_OZEL_ISIMLER = [
    "Nintendo", "Zelda", "The Legend of Zelda", "Breath of the Wild", "Tears of the Kingdom", "Mario", "Pokémon",
    "Xbox", "PlayStation", "Steam", "Steam Deck", "Switch", "Sony", "Microsoft", "Valve", "Rockstar", "Rockstar Games",
    "Ubisoft", "Capcom", "Sega", "Bandai Namco", "Square Enix", "Bethesda", "Activision", "Blizzard", "Epic Games",
    "Nvidia", "AMD", "Intel", "Google", "Apple", "Amazon", "Netflix", "Discord", "Twitch", "YouTube",
    "GTA", "PS5", "PS4", "DLC", "PC", "VR", "AI", "RPG", "FPS", "MMO", "IGN",
    "The Witcher", "Elden Ring", "Call of Duty", "Fortnite", "Minecraft", "Diablo", "Overwatch", "Halo", "Starfield",
    "Cyberpunk 2077", "Hollow Knight", "Silksong", "Half-Life", "Counter-Strike", "Valorant", "League of Legends",
    "Final Fantasy", "Resident Evil", "Metal Gear", "Persona", "Fallout", "Skyrim", "The Elder Scrolls",
]
# ------------------------------

DATA = pathlib.Path("data/haberler.json")
OGRENILEN = pathlib.Path("data/ozel_isimler.json")     # başlıklarda görülüp öğrenilen özel isimler
REDDEDILEN = pathlib.Path("data/reddedilen.json")   # oyun dışı / görselsiz diye elenen haberler (tekrar sorulmaz)
SITE = pathlib.Path("site/index.html")


def gorsel_bul(e):
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for l in e.get("links", []):
        if l.get("type", "").startswith("image/"):
            return l.get("href")
    return None


def kaynaklari_oku():
    simdi = time.time()
    liste = []
    for kaynak, url in FEEDS:
        print(f"RSS okunuyor: {kaynak} ({url})", flush=True)
        try:
            d = feedparser.parse(url)
        except Exception as ex:
            print("  RSS hatası:", ex); continue
        if not d.entries:
            print("  -> haber alınamadı (engelli ya da adres değişmiş olabilir)"); continue
        for e in d.entries:
            if not e.get("link") or not e.get("title"):
                continue
            t = e.get("published_parsed") or e.get("updated_parsed")
            ts = time.mktime(t) if t else simdi
            if simdi - ts > SAAT_SINIRI * 3600:
                continue
            liste.append(dict(baslik=e.get("title", "").strip(), link=e.get("link"),
                              kaynak=kaynak, ts=ts, gorsel=gorsel_bul(e)))
    return liste


_DURAK = {"the", "and", "for", "with", "its", "are", "has", "have", "from", "this", "that", "will", "after",
          "new", "how", "why", "what", "game", "games", "gets", "get", "now", "out", "all", "over"}


def _kelimeler(t):
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if len(w) >= 3 and w not in _DURAK}


def benzer(a, b):
    """İki başlık aynı haberi mi anlatıyor? (yazım benzerliği ya da ortak anahtar kelimeler)"""
    if difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.6:
        return True
    ka, kb = _kelimeler(a), _kelimeler(b)
    ortak = ka & kb
    return len(ortak) >= 3 and len(ortak) / max(1, len(ka | kb)) >= 0.5


def grupla_ve_puanla(liste):
    """Benzer başlıkları aynı konu say; konuyu yazan kaynak sayısı + tazelik = puan."""
    gruplar = []
    for h in sorted(liste, key=lambda x: -x["ts"]):
        for g in gruplar:
            if any(benzer(h["baslik"], x["baslik"]) for x in g):
                g.append(h); break
        else:
            gruplar.append([h])
    sonuc = []
    for g in gruplar:
        kaynaklar = {x["kaynak"] for x in g}
        yas_saat = (time.time() - g[0]["ts"]) / 3600
        puan = len(kaynaklar) * 3 + max(0, 24 - yas_saat) / 8
        ana = dict(g[0]); ana["puan"] = round(puan, 2); ana["uyeler"] = g
        ana["diger_kaynaklar"] = [{"ad": x["kaynak"], "link": x["link"]} for x in g[1:] if x["kaynak"] != g[0]["kaynak"]]
        ana["gorsel"] = next((x["gorsel"] for x in g if x["gorsel"]), None)
        sonuc.append(ana)
    return sorted(sonuc, key=lambda x: -x["puan"])


def sayfa_cek(link):
    ham = trafilatura.fetch_url(link)
    if not ham:
        return None, None
    metin = trafilatura.extract(ham, include_comments=False)
    meta = trafilatura.extract_metadata(ham)
    return metin, (meta.image if meta else None)


_BAGLAC = {"of", "the", "and", "for", "to", "in", "on", "at", "vs", "de", "la"}
_BASLATAN = {"The", "A", "An", "In", "On", "At", "It", "This", "That", "These", "Those", "After", "Before", "While",
             "When", "As", "For", "And", "But", "With", "From", "According", "However", "Also", "If", "We", "They",
             "He", "She", "Its", "His", "Her", "There", "Here", "What", "How", "Why", "Who", "One", "Some", "Many",
             "Most", "During", "Since", "Following", "You", "Your", "Our", "Their", "Now", "Today", "Meanwhile",
             "Still", "Of", "To", "By", "Not", "No", "Yes", "Just", "So", "Or", "Is", "Are", "Was", "Were", "Be",
             "Will", "Can", "Has", "Have", "Had", "Do", "Does", "Did", "Over", "Under", "More", "Less", "New",
             "All", "Both", "I"}
_RUN_BAGLAC = _BAGLAC - {"and"}      # "and" iki ayrı ismi birleştirmez (OpenAI and Anthropic)
_KISALTMA_DISI = {"ON", "IN", "AT", "OR", "IF", "IS", "IT", "AS", "BY", "TO", "OF", "SO", "NO", "AN", "BE", "DO",
                  "GO", "UP", "US", "WE", "ME", "MY", "HE", "AM"}
_TOKEN = re.compile(r"[A-Za-zÀ-ÿ0-9]+(?:['’\-][A-Za-zÀ-ÿ0-9]+)*|[.!?:\n]")


def _tr_kucuk(s):
    return s.replace("İ", "i").lower()


def _tr_buyuk_ilk(s):
    if not s:
        return s
    return {"i": "İ", "ı": "I"}.get(s[0], s[0].upper()) + s[1:]


def _buyuk(t):
    return t[:1].isupper()


def _tokenleri_birlestir(tokenler):
    s = ""
    for t in tokenler:
        s += t if t == ":" else ((" " + t) if s else t)
    return s


def _baglac_kucult(isim):
    t = isim.split(" ")
    for k in range(1, len(t)):
        if t[k].lower() in _BAGLAC and not t[k - 1].endswith(":"):
            t[k] = t[k].lower()
    return " ".join(t)


def _adaylar(metin, baslik_mi=False):
    """İngilizce metindeki özel isimleri bulur: büyük harfle başlayan kelime dizileri (Call of Duty, Grand Theft Auto 6).
    Döner: (çok kelimeli isimler, cümle ortasında büyük harfle geçen tek kelimeli isimler)."""
    tokenler = _TOKEN.findall(metin)
    n = len(tokenler)
    basta, onceki_sinir = [], True
    for t in tokenler:
        basta.append(onceki_sinir)
        onceki_sinir = t in (".", "!", "?", "\n")
    if baslik_mi:
        basta = [i == 0 for i in range(n)]
    coklu, ortada = set(), set()
    i = 0
    while i < n:
        if not _buyuk(tokenler[i]):
            i += 1; continue
        j = i + 1
        while j < n:
            u = tokenler[j]
            if _buyuk(u) or u[:1].isdigit():
                j += 1
            elif u.lower() in _RUN_BAGLAC and j + 1 < n and _buyuk(tokenler[j + 1]):
                j += 2
            elif u == ":" and j + 1 < n and _buyuk(tokenler[j + 1]):
                j += 2
            else:
                break
        run, bastan = tokenler[i:j], basta[i]
        i = j
        if bastan:                                       # cümle başındaki "The", "In" gibi kelimeleri at
            while run and run[0] in _BASLATAN:
                run = run[1:]
        while run and not _buyuk(run[0]):
            run = run[1:]
        if not run:
            continue
        run[-1] = re.sub(r"['’]s$", "", run[-1])           # Nintendo's -> Nintendo
        anlamli = [t for t in run if t != ":" and t.lower() not in _BAGLAC]
        if len(anlamli) >= 2:
            coklu.add(_tokenleri_birlestir(run))
            seg, segmentler = [], []
            for t in run + [":"]:
                if t == ":":
                    segmentler.append(seg); seg = []
                else:
                    seg.append(t)
            for sg in segmentler:                        # alt ifadeler de aday (Grand Theft Auto 6 -> Grand Theft Auto)
                for x in range(len(sg)):
                    for y in range(x + 2, min(len(sg), x + 5) + 1):
                        alt = sg[x:y]
                        if _buyuk(alt[0]) and alt[0].lower() not in _BAGLAC and \
                           (_buyuk(alt[-1]) or alt[-1][:1].isdigit()) and alt[-1].lower() not in _BAGLAC:
                            coklu.add(_tokenleri_birlestir(alt))
        elif len(anlamli) == 1:
            tek = anlamli[0]
            if not bastan and not baslik_mi and tek not in _BASLATAN and len(tek) >= 2 and not tek.isdigit():
                ortada.add(tek)
    return coklu, ortada


def _kisaltmalar(metin):
    """GTA, PS5, DLSS gibi kısaltmalar ve PlayStation, OpenAI, iPhone gibi iç büyük harfli yazımlar."""
    k = set(re.findall(r"\b[A-Za-z]*[a-z][A-Z][A-Za-z0-9]*\b", metin))
    k |= {x for x in re.findall(r"\b(?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,}\b", metin) if x not in _KISALTMA_DISI}
    return k


def isim_sozlugu(basliklar, metin, model_isimleri=(), ogrenilen=()):
    """Bir haber için {küçük harfli isim: doğru yazımı} sözlüğü. Kaynaklar (öncelik sırasıyla):
    elle yazılan liste, daha önce öğrenilenler, haber metni, modelin bildirdikleri, İngilizce başlık."""
    sozluk = {}

    def ekle(isim):
        isim = " ".join(str(isim).split())
        if len(isim) >= 2:
            sozluk.setdefault(isim.lower(), _baglac_kucult(isim))

    for i in SABIT_OZEL_ISIMLER:
        ekle(i)
    for i in ogrenilen:
        ekle(i)
    g_coklu, g_ortada = _adaylar(metin or "")
    for i in sorted(g_coklu):
        ekle(i)
    for i in sorted(g_ortada):
        ekle(i)
    for i in sorted(_kisaltmalar(metin or "")):
        ekle(i)
    for b in basliklar:                                  # başlık çoğunlukla büyük harfliyse kısaltma sayma
        harfler = [c for c in b if c.isalpha()]
        if harfler and sum(c.isupper() for c in harfler) / len(harfler) <= 0.6:
            for i in sorted(_kisaltmalar(b)):
                ekle(i)
    kaynak_kucuk = ((metin or "") + " " + " ".join(basliklar)).lower()
    for x in model_isimleri:                             # modelin uydurduğu isimleri ele: metinde geçmeli
        x = " ".join(str(x).split())
        if x and any(c.isupper() for c in x) and len(x.split()) <= 6 and x.lower() in kaynak_kucuk:
            ekle(x)
    if len(metin or "") < 300:                           # metin alınamadıysa İngilizce başlıktaki isimleri yedek kullan
        for b in basliklar:
            t_coklu, _ = _adaylar(b, baslik_mi=True)
            for i in sorted(t_coklu):
                ekle(i)
    return sozluk


def _kalip(isim):
    p = ""
    for c in isim:
        p += "['’]" if c in "'’" else (r"\s+" if c.isspace() else re.escape(c))
    return re.compile(r"(?<!\w)" + p + r"(?!\w)", re.IGNORECASE)


def _isimleri_uygula(metin, sozluk):
    for anahtar in sorted(sozluk, key=len, reverse=True):
        metin = _kalip(anahtar).sub(lambda m, v=sozluk[anahtar]: v, metin)
    return metin


def _ilk_harf(sonuc, sozluk):
    if any(v[:1].islower() and sonuc.startswith(v) for v in sozluk.values()):   # "iPhone ..." gibi markalar
        return sonuc
    return _tr_buyuk_ilk(sonuc)


def baslik_duzelt(baslik, sozluk):
    """Cümle düzeni: ilk harf ve özel isimler büyük, geri kalan her şey küçük."""
    baslik = " ".join(str(baslik).split())
    return _ilk_harf(_isimleri_uygula(_tr_kucuk(baslik), sozluk), sozluk)


def basligi_onar(baslik, sozluk):
    """Eski başlıklar için: mevcut harflere dokunmadan eksik büyük harfleri tamamlar."""
    baslik = re.sub(r"(:\s+)(\w)", lambda m: m.group(1) + _tr_kucuk(m.group(2)), baslik)   # iki noktadan sonra küçük harf
    return _ilk_harf(_isimleri_uygula(baslik, sozluk), sozluk)


PROMPT = """Sen bir Türk oyun haberleri editörüsün. Aşağıdaki İngilizce haber metnine DAYANARAK
özgün bir Türkçe haber yaz. Kurallar:
- Metinde olmayan hiçbir bilgiyi ekleme, tahmin yürütme.
- Cümleleri çevirme; olayı kendi cümlelerinle anlat (en fazla 120 kelime).
- Oyun, şirket ve kişi adlarını orijinal yaz.
- Başlık normal Türkçe cümle yazımıyla olsun: ilk kelimenin ilk harfi büyük; özel isimler (oyun, şirket, kişi, platform ve kısaltma adları, ör. Nintendo, Zelda, Breath of the Wild, GTA 6, PlayStation) orijinal büyük/küçük harfleriyle yazılsın; diğer tüm kelimeler küçük harfle yazılsın. "Başlıktaki Her Kelime Büyük" biçimini KULLANMA. Özel isimleri asla tamamen küçük harfle yazma.
- "ozel_isimler" alanına başlıkta geçen özel isimleri ve kısaltmaları, başlıktaki yazımlarıyla listele.
- Haber video oyunları, oyun şirketleri veya oyun donanımıyla ilgili değilse (film, dizi, LEGO, genel teknoloji vb.) sadece {"uygun": false} döndür.
- Aksi halde sadece geçerli JSON döndür: {"uygun": true, "baslik": "...", "ozel_isimler": ["...", "..."], "ozet": "..."}
Başlık: {baslik}
Metin:
{metin}"""


# Ana model yoğunsa sırayla yedek modeller denenir (adları virgülle değiştirebilirsin)
YEDEK_MODELLER = [m for m in os.getenv("LLM_YEDEK_MODELLER", "gemini-3.1-flash-lite,gemini-3.6-flash,gemini-3.8-flash").split(",") if m]


def yapay_zeka(baslik, metin):
    istek = PROMPT.replace("{baslik}", baslik).replace("{metin}", metin[:4500])
    for model in [MODEL] + [m for m in YEDEK_MODELLER if m != MODEL]:
        for deneme in range(3):
            try:
                r = requests.post(f"{API_BASE}/chat/completions", timeout=(10, 120),
                                  headers={"Authorization": f"Bearer {API_KEY}"},
                                  json={"model": model, "temperature": 0.3,
                                        "messages": [{"role": "user", "content": istek}]})
            except (requests.Timeout, requests.ConnectionError):
                print(f"  zaman aşımı ({model}), tekrar deneniyor...", flush=True)
                time.sleep(5 * (deneme + 1)); continue
            if r.status_code in (429, 500, 502, 503, 504):   # kota ya da sunucu yoğunluğu: bekle, tekrar dene
                print(f"  {r.status_code} ({model}), {10 * (deneme + 1)} sn bekleniyor...", flush=True)
                time.sleep(10 * (deneme + 1)); continue
            if r.status_code == 404:                           # model yok: sıradaki modele geç
                print(f"  model bulunamadı: {model}", flush=True)
                break
            r.raise_for_status()
            if model != MODEL:
                print(f"  (yedek model kullanıldı: {model})", flush=True)
            t = r.json()["choices"][0]["message"]["content"].strip()
            t = t.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return json.loads(t)
    raise RuntimeError("Hiçbir model yanıt veremedi")


GRADYANLAR = [("#3a4bff", "#12163a"), ("#14a38b", "#07302c"), ("#7b4bd6", "#1d0f3d"),
              ("#e0457b", "#3b1236"), ("#f0a030", "#4a2400"), ("#0ea5e9", "#062a3d")]

SAYFA = """<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Oyun Haberleri</title>
<script>try{document.documentElement.dataset.theme=localStorage.getItem("tema")||"dark"}catch(e){document.documentElement.dataset.theme="dark"}</script>
<style>
:root{--bg:#eef1f7;--card:#fff;--ink:#141a2e;--muted:#5d6580;--accent:#3a4bff;--line:#d9deea;--tag:#e6e9ff}
:root{color-scheme:light}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0e1222;--card:#171d33;--ink:#e8ebf7;--muted:#99a2c4;--accent:#8c99ff;--line:#262e4d;--tag:#222a52}
#tema{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:99px;padding:4px 14px;font:14px "Segoe UI",system-ui,sans-serif;cursor:pointer}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 "Segoe UI",system-ui,-apple-system,sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;border-bottom:2px solid var(--ink);padding-bottom:12px}
h1{margin:0;font:800 30px/1.1 Georgia,"Times New Roman",serif;letter-spacing:-.5px}
.lead{display:grid;grid-template-columns:1.2fr 1fr;margin:22px 0;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.lead .b{padding:20px}
.lead h2{font:700 26px/1.25 Georgia,serif;margin:0 0 10px}
.liste article{display:grid;grid-template-columns:210px 1fr;gap:16px;padding:18px 0;border-bottom:1px solid var(--line)}
.liste h3{font:700 19px/1.3 Georgia,serif;margin:0 0 6px}
.g{position:relative;overflow:hidden;min-height:130px;border-radius:8px;background:linear-gradient(135deg,var(--a),var(--b))}
.g img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.lead .g{border-radius:0;min-height:250px}
p{margin:0 0 8px;max-width:64ch}
[hidden]{display:none!important}
#daha{display:block;margin:28px auto 0;background:var(--accent);color:#fff;border:0;border-radius:99px;padding:10px 26px;font:600 15px "Segoe UI",system-ui,sans-serif;cursor:pointer}
.tags span{display:inline-block;background:var(--tag);color:var(--accent);border-radius:99px;padding:1px 10px;font-size:13px;margin:0 6px 4px 0}
.pop{display:inline-block;background:var(--accent);color:#fff;border-radius:4px;padding:0 8px;font-size:12px;margin-bottom:8px}
small{color:var(--muted)}
a{color:var(--accent)}
@media(max-width:640px){.lead{grid-template-columns:1fr}.liste article{grid-template-columns:1fr}.g{min-height:170px}}
</style>
<noscript><style>.liste article[hidden]{display:grid!important}#daha{display:none}</style></noscript>
</head>
<body>
<div class="wrap">
<header><h1>Oyun Haberleri</h1><div><small>Son güncelleme: __TARIH__</small> <button id="tema" type="button">Açık/Koyu</button></div></header>
__ICERIK__
</div>
<script>document.getElementById("tema").onclick=function(){var d=document.documentElement,y=d.dataset.theme==="dark"?"light":"dark";d.dataset.theme=y;try{localStorage.setItem("tema",y)}catch(e){}}</script>
<script>(function(){var b=document.getElementById("daha");if(!b)return;b.onclick=function(){var g=document.querySelectorAll("article[hidden]");for(var i=0;i<g.length&&i<__ADIM__;i++)g[i].hidden=false;if(g.length<=__ADIM__)b.style.display="none"}})();</script>
</body>
</html>"""


def _kart(h, lead=False, gizli=False):
    e = html.escape
    a, b = GRADYANLAR[int(h["id"][:2], 16) % len(GRADYANLAR)]
    img = (f'<img src="{e(h["gorsel"])}" alt="" loading="lazy" referrerpolicy="no-referrer" '
           f'onerror="this.remove()">') if h.get("gorsel") else ""
    gorsel = f'<div class="g" style="--a:{a};--b:{b}">{img}</div>'
    diger = h.get("diger_kaynaklar", [])
    rozet = f'<span class="pop">{1 + len(diger)} kaynak</span>' if diger else ""
    kaynaklar = f'<a href="{e(h["link"])}" rel="nofollow noopener">{e(h["kaynak"])}</a>' + "".join(
        f', <a href="{e(d["link"])}" rel="nofollow noopener">{e(d["ad"])}</a>' for d in diger)
    govde = (f'{rozet}<{"h2" if lead else "h3"}>{e(h["baslik_tr"])}</{"h2" if lead else "h3"}>'
             f'<p>{e(h["ozet"])}</p>'
             f'<small>Kaynak: {kaynaklar} · {h["tarih"]}</small>')
    if lead:
        return f'<section class="lead">{gorsel}<div class="b">{govde}</div></section>'
    return f'<article{" hidden" if gizli else ""}>{gorsel}<div>{govde}</div></article>'


def site_uret(haberler):
    SITE.parent.mkdir(exist_ok=True)
    if haberler:
        icerik = _kart(haberler[0], lead=True)
        icerik += '<section class="liste">' + "".join(
            _kart(h, gizli=(i >= ILK_GOSTERIM)) for i, h in enumerate(haberler) if i > 0) + "</section>"
        if len(haberler) > ILK_GOSTERIM:
            icerik += '<button id="daha" type="button">Daha fazla haber göster</button>'
    else:
        icerik = "<p>Henüz haber yok.</p>"
    sayfa = SAYFA.replace("__TARIH__", dt.datetime.now(TR).strftime("%d.%m.%Y %H:%M")).replace("__ICERIK__", icerik).replace("__ADIM__", str(ADIM))
    SITE.write_text(sayfa, encoding="utf-8")


def modelleri_listele():
    r = requests.get(f"{API_BASE}/models", headers={"Authorization": f"Bearer {API_KEY}"}, timeout=30)
    r.raise_for_status()
    print("Anahtarınla kullanılabilen modeller (MODEL satırına bu adlardan birini yaz):")
    for m in r.json().get("data", []):
        print(" ", m["id"].replace("models/", ""))


def birlestir(h, uyeler):
    """Aynı konuyu başka bir kaynak geç yazdıysa yeni haber açmaz; mevcut habere kaynak olarak ekler."""
    for m in uyeler:
        if m["link"] not in h["linkler"]:
            h["linkler"].append(m["link"])
        if m["kaynak"] != h["kaynak"] and not any(d["ad"] == m["kaynak"] for d in h["diger_kaynaklar"]):
            h["diger_kaynaklar"].append({"ad": m["kaynak"], "link": m["link"]})
        if m["baslik"] not in h["basliklar"]:
            h["basliklar"].append(m["baslik"])
        if not h.get("gorsel") and m.get("gorsel"):
            h["gorsel"] = m["gorsel"]


def main():
    if not API_KEY:
        raise SystemExit("API anahtarı bulunamadı. Bu klasörde anahtar.txt dosyası oluşturup anahtarı içine yapıştır "
                         "ya da LLM_API_KEY ortam değişkenini tanımla.")
    if "--modeller" in sys.argv:
        return modelleri_listele()
    DATA.parent.mkdir(exist_ok=True)
    arsiv = json.loads(DATA.read_text("utf-8")) if DATA.exists() else []
    ogrenilen = set(json.loads(OGRENILEN.read_text("utf-8"))) if OGRENILEN.exists() else set()
    for h in arsiv:
        h.setdefault("diger_kaynaklar", [])
        h.setdefault("linkler", [h["link"]] + [d["link"] for d in h["diger_kaynaklar"]])
        h.setdefault("basliklar", [])
    for h in [x for x in arsiv if not x.get("bd2")][:ONAR_LIMIT]:   # eski başlıkların harflerini, sayfa metnine bakarak bir kez onar
        try:
            metin, _ = sayfa_cek(h["link"])
        except Exception:
            metin = None
        h["baslik_tr"] = basligi_onar(h["baslik_tr"], isim_sozlugu(h["basliklar"], metin or "", (), ogrenilen))
        h["bd2"] = True
    reddedilen = set(json.loads(REDDEDILEN.read_text("utf-8"))) if REDDEDILEN.exists() else set()
    idle = lambda l: hashlib.md5(l.encode()).hexdigest()

    # 1) Bulunan konuları, daha önce yayınlanan haberlerle eşleştir (aynı konu = yeni haber değil, ek kaynak)
    link_haritasi = {l: h for h in arsiv for l in h["linkler"]}
    simdi = time.time()
    son_haberler = [h for h in arsiv if h.get("ts") and simdi - h["ts"] < SON_KONTROL_SAAT * 3600 and h["basliklar"]]
    gruplar = grupla_ve_puanla(kaynaklari_oku())
    adaylar, birlesen = [], 0
    for a in gruplar:
        linkler = [m["link"] for m in a["uyeler"]]
        if any(idle(l) in reddedilen for l in linkler):
            continue
        hedef = next((link_haritasi[l] for l in linkler if l in link_haritasi), None)
        if not hedef:
            hedef = next((h for h in son_haberler
                          if any(benzer(m["baslik"], o) for m in a["uyeler"] for o in h["basliklar"])), None)
        if hedef:
            birlestir(hedef, a["uyeler"]); birlesen += 1
            continue
        adaylar.append(a)
    print(f"{len(gruplar)} konu bulundu: {birlesen} tanesi zaten yayında (ek kaynak olarak eklendi), "
          f"{len(adaylar)} yeni aday. Özetleniyor (her biri birkaç saniye sürer)...", flush=True)

    # 2) En yüksek puanlı yeni adayları sırayla işle
    yeni, istek, islenen = [], 0, 0
    say = dict(oyun_disi=0, kisa=0, gorselsiz=0, hata=0)
    for a in adaylar:
        if len(yeni) >= HABER_SAYISI or istek >= MAKS_ISTEK:
            break
        islenen += 1
        linkler = [m["link"] for m in a["uyeler"]]
        try:
            metin, og = sayfa_cek(a["link"])
            if not metin or len(metin) < 300:
                say["kisa"] += 1; continue
            gorsel = a["gorsel"] or og
            if not gorsel and a["kaynak"] in GORSEL_ZORUNLU:
                say["gorselsiz"] += 1; reddedilen.update(idle(l) for l in linkler)
                print("Görselsiz, atlandı:", a["baslik"]); continue
            istek += 1
            out = yapay_zeka(a["baslik"], metin)
            if not out.get("uygun", True):
                say["oyun_disi"] += 1; reddedilen.update(idle(l) for l in linkler)
                print("Oyun dışı, atlandı:", a["baslik"]); time.sleep(7); continue
            sozluk = isim_sozlugu([m["baslik"] for m in a["uyeler"]], metin, out.get("ozel_isimler", []), ogrenilen)
            baslik_tr = baslik_duzelt(out["baslik"], sozluk)
            ogrenilen.update(v for v in sozluk.values() if v in baslik_tr and v not in SABIT_OZEL_ISIMLER)
            yeni.append(dict(id=idle(a["link"]), baslik_tr=baslik_tr, bd=True, bd2=True, ozet=out["ozet"], link=a["link"],
                             kaynak=a["kaynak"], diger_kaynaklar=a["diger_kaynaklar"], gorsel=gorsel, puan=a["puan"],
                             linkler=linkler, basliklar=[m["baslik"] for m in a["uyeler"]], ts=a["ts"],
                             tarih=dt.datetime.now(TR).strftime("%d.%m.%Y %H:%M")))
            print("OK:", baslik_tr)
            time.sleep(7)  # ücretsiz katmanda dakika limitine takılmamak için
        except Exception as ex:
            say["hata"] += 1; print("Atlandı:", a["baslik"], ex)

    arsiv = (yeni + arsiv)[:SAKLANAN]
    DATA.write_text(json.dumps(arsiv, ensure_ascii=False, indent=1), "utf-8")
    REDDEDILEN.write_text(json.dumps(list(reddedilen)[-5000:]), "utf-8")
    OGRENILEN.write_text(json.dumps(sorted(ogrenilen)[:3000], ensure_ascii=False), "utf-8")
    site_uret(arsiv)
    print(f"{len(yeni)} yeni haber eklendi. | oyun dışı: {say['oyun_disi']} | metni alınamayan/kısa: {say['kisa']} | "
          f"görselsiz: {say['gorselsiz']} | hata: {say['hata']} | sıra bekleyen: {len(adaylar) - islenen}")


if __name__ == "__main__":
    main()
