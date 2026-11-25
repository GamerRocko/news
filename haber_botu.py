#!/usr/bin/env python3
"""Oyun haberleri botu: RSS -> popülerlik -> Türkçe özet (ücretsiz API) -> statik site.

Kurulum:   pip install feedparser trafilatura requests
Çalıştır:  export LLM_API_KEY="..." ; python haber_botu.py
Çıktı:     data/haberler.json  ve  site/index.html
"""
import os, re, sys, json, time, html, hashlib, difflib, datetime as dt, pathlib
import socket
from urllib.parse import urljoin
import feedparser, requests, trafilatura

socket.setdefaulttimeout(20)  # yanıt vermeyen siteler en fazla 20 sn bekletsin

# ---------- AYARLAR ----------
FEEDS = [  # (sitede görünecek kaynak adı, RSS adresi) -> adı istediğin gibi yazabilirsin
    ("Destructoid", "http://www.destructoid.com/?mode=atom"),
    ("Eurogamer", "https://www.eurogamer.net/feed/news"),
    ("Game Developer", "https://www.gamedeveloper.com/rss.xml"),
    ("Game Informer", "https://gameinformer.com/news.xml"),
    ("GamesIndustry.biz", "https://www.gamesindustry.biz/feed/news"),
    ("GameSpot", "https://www.gamespot.com/feeds/game-news/"),
    ("GamesRadar", "https://www.gamesradar.com/feeds/articletype/news/"),
    ("IGN", "https://feeds.feedburner.com/ign/news"),
    ("Kotaku", "https://kotaku.com/games/feed"),
    ("PC Gamer", "https://www.pcgamer.com/feeds/articletype/news/"),
    ("PCGamesN", "http://www.pcgamesn.com/mainrss.xml"),
    ("Polygon", "https://www.polygon.com/rss/news/index.xml"),
    ("Rock Paper Shotgun", "https://www.rockpapershotgun.com/feed/news"),
    ("VG247", "https://www.vg247.com/feed/news"),
    ("VGC", "https://www.videogameschronicle.com/category/news/feed/"),
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
GORSEL_ZORUNLU = set()    # Buraya yazılan kaynaklardan görseli bulunamayan haberler yayınlanmaz. Ad, FEEDS'teki adla BİREBİR aynı olmalı
KALDIRILAN_KAYNAKLAR = {"Gameguru Mania", "GGMania"}   # Çıkarılan kaynaklar: bunlardan gelen görselsiz eski haberler sayfadan silinir
SURUM = "2026-11-24-b"   # günlükte görünür; GitHub'daki dosyanın güncel olup olmadığını anlamak için
MAKS_ISTEK = 20           # her çalışmada yapay zekaya en fazla kaç istek gönderilsin (kota koruması)
SON_LISTE = 40            # yapay zekaya, "aynı haber mi?" kontrolü için gösterilecek son haber sayısı
ONAR_LIMIT = 30           # her çalışmada eski haberlerden en fazla kaçının başlığı cümle düzenine çevrilsin (sayfa okunur, istek harcamaz)
ASCII_ONAR_LIMIT = 5      # her çalışmada Türkçe karakteri eksik en fazla kaç eski haber yapay zekayla düzeltilsin
SON_KONTROL_SAAT = 72     # yeni haber, son kaç saatteki haberlerle konu benzerliği için karşılaştırılsın

# Başlıkta HER ZAMAN bu yazımla görünecek isimler ve kısaltmalar (GTA, PS5, PlayStation gibi).
# Yanlış yazılan bir kısaltma görürsen buraya doğru yazımıyla ekle.
SABIT_OZEL_ISIMLER = [
    "Nintendo", "Zelda", "The Legend of Zelda", "Breath of the Wild", "Tears of the Kingdom", "Mario", "Pokémon",
    "Xbox", "PlayStation", "Steam", "Steam Deck", "Switch", "Sony", "Microsoft", "Valve", "Rockstar", "Rockstar Games",
    "Ubisoft", "Capcom", "Sega", "Bandai Namco", "Square Enix", "Bethesda", "Activision", "Blizzard", "Epic Games",
    "Nvidia", "AMD", "Intel", "Google", "Apple", "Amazon", "Netflix", "Discord", "Twitch", "YouTube",
    "GTA", "PS5", "PS4", "DLC", "PC", "VR", "AI", "RPG", "FPS", "MMO", "IGN", "CEO", "CFO", "OpenAI", "ChatGPT", "iPhone",
    "The Witcher", "Elden Ring", "Call of Duty", "Fortnite", "Minecraft", "Diablo", "Overwatch", "Halo", "Starfield",
    "Cyberpunk 2077", "Hollow Knight", "Silksong", "Half-Life", "Counter-Strike", "Valorant", "League of Legends",
    "Final Fantasy", "Resident Evil", "The Last of Us", "Slay the Spire", "Metal Gear", "Persona", "Fallout", "Skyrim", "The Elder Scrolls",
]
# ------------------------------

DATA = pathlib.Path("data/haberler.json")
OGRENILEN = pathlib.Path("data/ozel_isimler.json")     # başlıklarda görülüp öğrenilen özel isimler
REDDEDILEN = pathlib.Path("data/reddedilen.json")   # oyun dışı / görselsiz diye elenen haberler (tekrar sorulmaz)
SITE = pathlib.Path("site/index.html")


_IMG_RE = re.compile(r"""<img[^>]+?src=["']([^"']+)["']""", re.I)
_REKLAM_IMG = ("pixel", "1x1", "spacer", "emoji", "gravatar", "feedburner", "doubleclick", "tracking")
_META_GORSEL = [re.compile(k, re.I) for k in (
    r"""<meta[^>]+(?:property|name)=["'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["'][^>]*?content=["']([^"']+)""",
    r"""<meta[^>]+content=["']([^"']+)["'][^>]*?(?:property|name)=["'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["']""",
    r"""<link[^>]+rel=["']image_src["'][^>]*?href=["']([^"']+)""")]


def _html_gorsel(metin):
    for u in _IMG_RE.findall(metin or ""):
        if u.startswith(("http://", "https://")) and not any(k in u.lower() for k in _REKLAM_IMG):
            return html.unescape(u)
    return None


def gorsel_bul(e):
    """Feed girdisindeki görseli bulur: media etiketleri, ek dosyalar, sonra içerikteki <img>."""
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for l in e.get("links", []):
        if l.get("type", "").startswith("image/"):
            return l.get("href")
    for c in e.get("content", []) or []:
        u = _html_gorsel(c.get("value", ""))
        if u:
            return u
    return _html_gorsel(e.get("summary", ""))



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
    """Haber sayfasından metni ve görseli alır. Görsel: trafilatura, olmazsa og:image / twitter:image / image_src."""
    ham = trafilatura.fetch_url(link)
    if not ham:
        return None, None
    metin = trafilatura.extract(ham, include_comments=False)
    meta = trafilatura.extract_metadata(ham)
    gorsel = meta.image if meta and meta.image else None
    if not gorsel:
        for kalip in _META_GORSEL:
            m = kalip.search(ham)
            if m:
                gorsel = html.unescape(m.group(1)); break
    return metin, (urljoin(link, gorsel) if gorsel else None)



_KUCUK_KELIME = {"ve", "ile", "için", "de", "da", "mi", "mı", "mu", "mü", "veya", "ya", "ki",
                 "of", "the", "and", "for", "to", "in", "on", "at", "vs", "a", "an"}
_BAGLAC = {"of", "the", "and", "for", "to", "in", "on", "at", "vs", "de", "la"}
_RUN_BAGLAC = _BAGLAC - {"and"}      # "and" iki ayrı ismi birleştirmez (OpenAI and Anthropic)
_BASLATAN = {"The", "A", "An", "In", "On", "At", "It", "This", "That", "These", "Those", "After", "Before", "While",
             "When", "As", "For", "And", "But", "With", "From", "According", "However", "Also", "If", "We", "They",
             "He", "She", "Its", "His", "Her", "There", "Here", "What", "How", "Why", "Who", "One", "Some", "Many",
             "Most", "During", "Since", "Following", "You", "Your", "Our", "Their", "Now", "Today", "Meanwhile",
             "Still", "Of", "To", "By", "Not", "No", "Yes", "Just", "So", "Or", "Is", "Are", "Was", "Were", "Be",
             "Will", "Can", "Has", "Have", "Had", "Do", "Does", "Did", "Over", "Under", "More", "Less", "New",
             "All", "Both", "I"}
_KISALTMA_DISI = {"ON", "IN", "AT", "OR", "IF", "IS", "IT", "AS", "BY", "TO", "OF", "SO", "NO", "AN", "BE", "DO",
                  "GO", "UP", "US", "WE", "ME", "MY", "HE", "AM"}
_TOKEN = re.compile(r"[A-Za-zÀ-ÿ0-9]+(?:['’\-][A-Za-zÀ-ÿ0-9]+)*|[.!?:\n]")
_TR_HARFLER = set("çğıöşüÇĞİÖŞÜ")
_TR_STOP = {"ve", "de", "da", "bir", "bu", "su", "şu", "o", "ya", "ki", "mi", "mı", "mu", "mü", "ama", "ise", "en", "daha",
            "gibi", "kadar", "her", "hem", "ne", "göre", "gore", "ancak", "fakat", "veya", "yani", "ayrıca", "ayrica",
            "sonra", "önce", "once", "yeni", "eski", "iki", "üç", "uc", "ilk", "son", "oyun", "için", "icin", "ile",
            "çok", "cok", "olan", "olarak"}
_IKI_HARFLI = {"PC", "VR", "AI", "TV", "UI", "XP", "HP"}   # 2 harfli isimlerde yalnızca bunlar kabul edilir


def _isim_gecerli(isim):
    """Tek kelimelik 'isim' sıradan bir Türkçe kelime (ve, de, bir...) ya da rastgele 2 harfli kısaltma olamaz."""
    isim = isim.strip()
    if " " in isim:
        return len(isim) >= 2
    return (len(isim) >= 2 and isim.lower() not in _TR_STOP and isim.lower() not in _ING_STOP
            and isim.lower() not in _BAGLAC and (len(isim) > 2 or isim in _IKI_HARFLI)
            and not (isim.isupper() and len(isim) >= 4 and isim not in _BILINEN_KISALTMA))   # FINAL, FANTASY: yazı stili, isim değil


_BILINEN_KISALTMA = {i for i in SABIT_OZEL_ISIMLER if i.isupper()} | {
    "OLED", "HDMI", "FIFA", "UEFA", "NASA", "NATO", "ESRB", "PEGI", "NBA", "NFL", "UFC", "SSD", "HDR", "GPU", "CPU", "RAM", "USB",
    "RTX", "GTX", "RX", "DLSS", "FSR", "VRR", "NPC", "HUD", "API", "SDK", "DRM", "VPN", "LED", "LCD", "UHD", "FHD", "QHD"}
_ROMEN = re.compile(r"^(?=[IVXLCDM])M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")


def _isim_normalle(isim):
    """Haber metninde TAMAMI BÜYÜK HARFLE yazılmış isimleri düzeltir: FINAL FANTASY VII -> Final Fantasy VII,
    SLAY THE SPIRE -> Slay the Spire, THE LAST OF US -> The Last of Us. Gerçek kısaltmalar (GTA, PS5, DLC) ve roma rakamları (VII) korunur."""
    t = isim.split(" ")
    alfa = [w.rstrip(":") for w in t if w.rstrip(":").isalpha()]
    hepsi_buyuk = len(alfa) >= 2 and all(w.isupper() for w in alfa)     # "THE LAST OF US": tüm isim büyük harfle yazılmış, yazı stili
    for k, w in enumerate(t):
        govde = w.rstrip(":")
        ek = w[len(govde):]
        if not (govde.isalpha() and govde.isupper() and len(govde) >= 2):
            continue
        if govde in _BILINEN_KISALTMA or (len(govde) <= 4 and _ROMEN.match(govde)):
            continue
        basta = k == 0 or t[k - 1].endswith(":")
        if govde.lower() in _BAGLAC or govde.lower() in _ING_STOP:     # THE, OF, AND ...: baştaysa "The", ortadaysa "the"
            yeni = govde.capitalize() if basta else govde.lower()
        elif hepsi_buyuk or (len(govde) >= 4 and re.search(r"[AEIOU]", govde)):   # FINAL, FANTASY, US, WAR: yazı stili, kısaltma değil
            yeni = govde.capitalize()
        else:
            continue
        t[k] = yeni + ek
    return " ".join(t)


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
    kelimeler = set(re.findall(r"[A-Za-z]+", metin))
    for x in re.findall(r"\b(?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,}\b", metin):
        if x in _KISALTMA_DISI or x.lower() in _ING_STOP or x.lower() in _BAGLAC:
            continue
        if x.isalpha() and x not in _BILINEN_KISALTMA:
            if len(x) >= 4 and re.search(r"[AEIOU]", x):                   # FINAL, FANTASY, DOOM: yazı stili, kısaltma değil
                continue
            if x.capitalize() in kelimeler or x.lower() in kelimeler:       # metnin başka yerinde normal yazılmış
                continue
        k.add(x)
    return k


def isim_sozlugu(basliklar, metin, model_isimleri=(), ogrenilen=()):
    """Bir haber için {küçük harfli isim: doğru yazımı} sözlüğü. Kaynaklar (öncelik sırasıyla):
    elle yazılan liste, daha önce öğrenilenler, haber metni, modelin bildirdikleri, İngilizce başlık."""
    sozluk = {}

    def ekle(isim):
        isim = " ".join(str(isim).split())
        if len(isim) >= 2 and _isim_gecerli(isim):
            sozluk.setdefault(isim.lower(), _baglac_kucult(_isim_normalle(isim)))

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
    """Özel isimleri doğru yazımlarıyla yerleştirir. Uzun isimler önce işlenir ve yerleştirilen bölgeye
    sonraki (kısa) kayıtlar dokunamaz: "Slay the Spire" içindeki "the", "THE" kaydıyla bozulmaz."""
    kaplanan, degisimler = [], []
    for anahtar in sorted(sozluk, key=len, reverse=True):
        for m in _kalip(anahtar).finditer(metin):
            if any(m.start() < e and bas < m.end() for bas, e in kaplanan):
                continue
            kaplanan.append((m.start(), m.end())); degisimler.append((m.start(), m.end(), sozluk[anahtar]))
    for bas, e, v in sorted(degisimler, reverse=True):
        metin = metin[:bas] + v + metin[e:]
    return metin


def _ilk_harf(sonuc, sozluk):
    if any(v[:1].islower() and sonuc.startswith(v) for v in sozluk.values()):   # "iPhone ..." gibi markalar
        return sonuc
    return _tr_buyuk_ilk(sonuc)


def _her_kelime_buyuk_mu(baslik):
    """Başlık "Her Kelime Büyük" biçiminde mi? (ilk kelime, ekler ve küçük bağlaçlar sayılmaz)"""
    kelimeler = [(m.start(), m.group(0)) for m in re.finditer(r"\w+", baslik)][1:]
    sayilan = [k for p, k in kelimeler if not baslik[p - 1] in "'’" and k.lower() not in _KUCUK_KELIME and k.isalpha()]
    return len(sayilan) >= 3 and sum(k[0].isupper() for k in sayilan) / len(sayilan) >= 0.75


def baslik_duzelt(baslik, sozluk):
    """Cümle düzeni: ilk harf ve özel isimler büyük, geri kalan kelimeler küçük.
    Özel isimler haber metninden ve listelerden gelir. Model başlığı zaten cümle düzeninde yazdıysa,
    onun büyük harfle yazdığı kelimelere de güvenilir. "Her Kelime Büyük" yazdıysa güvenilmez."""
    baslik = " ".join(str(baslik).split())
    yerel = dict(sozluk)
    if not _her_kelime_buyuk_mu(baslik):
        for m in re.finditer(r"\w+", baslik):
            k, p = m.group(0), m.start()
            onceki = baslik[:p].rstrip()
            if (p and baslik[p - 1] in "'’") or not onceki or onceki.endswith(":"):
                continue
            if k[0].isupper() and k.lower() not in _KUCUK_KELIME:
                yerel.setdefault(k.lower(), _isim_normalle(k))
    return _ilk_harf(_isimleri_uygula(_tr_kucuk(baslik), yerel), yerel)


_NOKTALI_KISALTMA = {"vb", "vs", "örn", "yak", "dr", "bkz", "sn", "no", "mr", "mrs", "ms"}


def ozet_duzelt(ozet, sozluk):
    """Özet: cümle başlarını büyütür ve bilinen özel isimlerin yazımını düzeltir. Hiçbir şeyi küçültmez."""
    ozet = _isimleri_uygula(" ".join(str(ozet).split()), sozluk)

    def cumle_basi(m):
        onceki = re.search(r"(\w+)$", m.string[:m.start()])
        if onceki and onceki.group(1).lower() in _NOKTALI_KISALTMA:
            return m.group(0)
        return m.group(1) + m.group(2) + _tr_buyuk_ilk(m.group(3))

    ozet = re.sub(r"([.!?…])(\s+)([a-zçğıöşü])", cumle_basi, ozet)
    birimler = {"hz": "Hz", "ghz": "GHz", "mhz": "MHz", "gb": "GB", "tb": "TB", "mb": "MB", "kb": "KB", "mah": "mAh"}
    ozet = re.sub(r"(?<=\d)(\s?)(hz|ghz|mhz|gb|tb|mb|kb|mah)\b", lambda m: m.group(1) + birimler[m.group(2).lower()], ozet, flags=re.I)
    return _tr_buyuk_ilk(ozet)


_ING_STOP = {"the", "and", "of", "to", "in", "is", "that", "for", "with", "on", "as", "are", "was", "this", "it", "by",
             "from", "has", "have", "will", "be", "at", "an", "or", "their", "its", "which", "who", "said"}


def ingilizce_supheli(metin):
    """Metin Türkçeye çevrilmemiş, İngilizce kalmış gibi mi? (Türkçe karakter yok + sık İngilizce kelimeler var)"""
    metin = str(metin)
    kel = re.findall(r"[a-zA-Z']+", metin.lower())
    if len(kel) < 8 or any(c in _TR_HARFLER for c in metin):
        return False
    return sum(k in _ING_STOP for k in kel) / len(kel) >= 0.15


def dil_sorunlu(baslik, ozet=""):
    return ascii_supheli(baslik, ozet) or ingilizce_supheli(ozet)


def ascii_supheli(baslik, ozet=""):
    """Türkçe metin, Türkçe karakterler olmadan (ı yerine i, ş yerine s...) yazılmış gibi mi?"""
    harf = lambda t: sum(c.isalpha() for c in t)
    tum = f"{baslik} {ozet}"
    if harf(tum) >= 60 and not any(c in _TR_HARFLER for c in tum):
        return True
    return harf(baslik) >= 35 and not any(c in _TR_HARFLER for c in baslik)


FIX_PROMPT = """Aşağıdaki başlık ve özet Türkçe olmalı, ama şu sorunlardan biri olabilir: (1) Türkçe karakterler (ç, ğ, ı, İ, ö, ş, ü) eksik,
yani ASCII harflerle yazılmış (örn. "ı" yerine "i", "ş" yerine "s"); (2) metin İngilizce kalmış.
Eksik karakterleri düzelt, İngilizce kalan kısmı doğal ve doğru bir Türkçeyle çevir. Anlamı DEĞİŞTİRME, bilgi ekleme, bilgi çıkarma.
Özel isimleri (oyun, şirket, kişi, ürün) orijinal yaz; özet cümleleri büyük harfle başlasın. Zaten doğruysa aynen döndür.
Sadece geçerli JSON döndür: {"baslik": "...", "ozet": "..."}
Başlık: {baslik}
Özet: {ozet}"""


PROMPT = """Sen bir Türk oyun haberleri editörüsün. Aşağıdaki İngilizce haber metnine DAYANARAK
özgün bir Türkçe haber yaz. Kurallar:
- Metinde olmayan hiçbir bilgiyi ekleme, tahmin yürütme.
- Cümleleri çevirme; olayı kendi cümlelerinle anlat (en fazla 120 kelime).
- Oyun, şirket ve kişi adlarını orijinal yaz.
- Başlık normal Türkçe cümle yazımıyla olsun: yalnızca ilk kelimenin ilk harfi büyük; özel isimler (oyun, şirket, kişi, platform adları ve kısaltmalar; ör. Nintendo, Zelda, Breath of the Wild, GTA 6) orijinal büyük/küçük harfleriyle yazılsın; diğer tüm kelimeler küçük harfle başlasın. "Başlıktaki Her Kelime Büyük" biçimini KULLANMA.
- Özet normal Türkçe yazımıyla olsun: her cümle büyük harfle başlasın; özel isimler (oyun, şirket, ürün, kişi adları) ve kısaltmalar (RTX, OLED, GB, PS5) orijinal büyük/küçük harfleriyle yazılsın. Küçük harf kuralı SADECE başlık içindir, özette her şeyi küçük harfle yazma.
- "ozel_isimler" alanına başlıkta geçen özel isimleri ve kısaltmaları, başlıktaki yazımlarıyla listele.
- Türkçe karakterleri (ç, ğ, ı, İ, ö, ş, ü) eksiksiz ve doğru kullan; asla ASCII'ye çevirme (ı yerine i, ş yerine s yazma).
- Haber video oyunları, oyun şirketleri veya oyun donanımıyla ilgili değilse (film, dizi, LEGO, genel teknoloji vb.) sadece {"uygun": false} döndür.
- Aşağıda "Son yayınlanan haberler" listesi varsa ve bu haber onlardan biriyle AYNI olayı ya da duyuruyu anlatıyorsa (aynı oyun veya şirketle ilgili FARKLI bir gelişme aynı sayılmaz), başka hiçbir şey yazmadan {"uygun": true, "ayni_haber": N} döndür (N: listedeki numara).
- Aksi halde sadece geçerli JSON döndür: {"uygun": true, "baslik": "...", "ozel_isimler": ["...", "..."], "ozet": "..."}
{son_haberler}Başlık: {baslik}
Metin:
{metin}"""


# Ana model yoğunsa sırayla yedek modeller denenir (adları virgülle değiştirebilirsin)
YEDEK_MODELLER = [m for m in os.getenv("LLM_YEDEK_MODELLER", "gemini-3.1-flash-lite,gemini-3.6-flash,gemini-3.8-flash").split(",") if m]


def yapay_zeka(baslik, metin, son_liste=()):
    liste = ("Son yayınlanan haberler:\n" + "\n".join(f"{i + 1}) {h['baslik_tr']}" for i, h in enumerate(son_liste)) + "\n\n") if son_liste else ""
    istek = PROMPT.replace("{son_haberler}", liste).replace("{baslik}", baslik).replace("{metin}", metin[:4500])
    return _llm_json(istek)


def _llm_json(istek):
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


def turkce_karakter_duzelt(baslik, ozet):
    """Türkçe karakteri eksik başlık/özeti yapay zekaya düzelttirir."""
    out = _llm_json(FIX_PROMPT.replace("{baslik}", baslik).replace("{ozet}", ozet))
    return str(out.get("baslik") or baslik), str(out.get("ozet") or ozet)


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
.pop{display:inline-block;background:var(--accent);color:#fff;border-radius:4px;padding:0 8px;font-size:12px;margin-bottom:8px}
small{color:var(--muted)}
a{color:var(--accent)}
[hidden]{display:none!important}
#daha{display:block;margin:28px auto 0;background:var(--accent);color:#fff;border:0;border-radius:99px;padding:10px 26px;font:600 15px "Segoe UI",system-ui,sans-serif;cursor:pointer}
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
    SITE.write_text(SAYFA.replace("__TARIH__", dt.datetime.now(TR).strftime("%d.%m.%Y %H:%M"))
                    .replace("__ICERIK__", icerik).replace("__ADIM__", str(ADIM)), encoding="utf-8")


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
    print("haber_botu sürümü:", SURUM, flush=True)
    for ad in GORSEL_ZORUNLU - {k for k, _ in FEEDS}:
        print(f"UYARI: GORSEL_ZORUNLU içindeki '{ad}' adı FEEDS'te yok, kural çalışmaz.", flush=True)
    DATA.parent.mkdir(exist_ok=True)
    arsiv = json.loads(DATA.read_text("utf-8")) if DATA.exists() else []
    for h in arsiv:
        h.setdefault("diger_kaynaklar", [])
        h.setdefault("linkler", [h["link"]] + [d["link"] for d in h["diger_kaynaklar"]])
        h.setdefault("basliklar", [])
    once = len(arsiv)                                # çıkarılan kaynakları temizle
    kalan = []
    for h in arsiv:
        h["diger_kaynaklar"] = [d for d in h["diger_kaynaklar"] if d["ad"] not in KALDIRILAN_KAYNAKLAR]
        if h["kaynak"] in KALDIRILAN_KAYNAKLAR:
            if not h["diger_kaynaklar"]:
                continue                             # tek kaynağı çıkarılan bir kaynaksa haber silinir
            d = h["diger_kaynaklar"].pop(0)          # başka kaynağı varsa o ana kaynak olur
            h["kaynak"], h["link"] = d["ad"], d["link"]
        kalan.append(h)
    arsiv = kalan
    if once != len(arsiv):
        print(f"{once - len(arsiv)} haber, çıkarılan kaynak (Gameguru Mania) yüzünden silindi.", flush=True)
    ogrenilen = set(json.loads(OGRENILEN.read_text("utf-8"))) if OGRENILEN.exists() else set()
    ogrenilen = {_isim_normalle(x) for x in ogrenilen if _isim_gecerli(x)}      # önceki hatalı öğrenmeleri ("VE", "FINAL FANTASY") düzelt
    ascii_say = 0
    for h in [x for x in arsiv if not x.get("bd6")][:ONAR_LIMIT]:   # eski kayıtları sayfa metnine bakarak onar (başlık, özet, dil, eksik görsel)
        try:
            metin, og = sayfa_cek(h["link"])
        except Exception:
            metin, og = None, None
        if not h.get("gorsel") and og:
            h["gorsel"] = og
        if ascii_say < ASCII_ONAR_LIMIT and dil_sorunlu(h["baslik_tr"], h["ozet"]):
            try:
                h["baslik_tr"], h["ozet"] = turkce_karakter_duzelt(h["baslik_tr"], h["ozet"]); ascii_say += 1
            except Exception as ex:
                print("Türkçe karakter düzeltmesi yapılamadı:", ex)
        sozluk = isim_sozlugu(h["basliklar"], metin or "", (), ogrenilen)
        h["baslik_tr"] = baslik_duzelt(h["baslik_tr"], sozluk)
        h["ozet"] = ozet_duzelt(h["ozet"], sozluk)
        h["onar_deneme"] = h.get("onar_deneme", 0) + 1
        if not dil_sorunlu(h["baslik_tr"], h["ozet"]) or h["onar_deneme"] >= 3:
            h["bd6"] = True
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
    say = dict(oyun_disi=0, kisa=0, gorselsiz=0, hata=0, model_birlesen=0, ingilizce=0)
    son_liste = [h for h in arsiv[:60] if not h.get("ts") or simdi - h["ts"] < SON_KONTROL_SAAT * 3600][:SON_LISTE]
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
            out = yapay_zeka(a["baslik"], metin, son_liste)
            if not out.get("uygun", True):
                say["oyun_disi"] += 1; reddedilen.update(idle(l) for l in linkler)
                print("Oyun dışı, atlandı:", a["baslik"]); time.sleep(7); continue
            try:
                n = int(out.get("ayni_haber"))
            except (TypeError, ValueError):
                n = None
            if n and 1 <= n <= len(son_liste):          # model: bu haber zaten yayında -> yeni haber açma, kaynak olarak ekle
                birlestir(son_liste[n - 1], a["uyeler"]); say["model_birlesen"] += 1
                print(f"Aynı haber, kaynak olarak eklendi: {a['baslik']} -> {son_liste[n - 1]['baslik_tr']}")
                time.sleep(7); continue
            if dil_sorunlu(out["baslik"], out["ozet"]):   # Türkçe karakter eksik ya da metin İngilizce kalmışsa bir kez düzelttir
                istek += 1
                out["baslik"], out["ozet"] = turkce_karakter_duzelt(out["baslik"], out["ozet"])
                if ingilizce_supheli(out["ozet"]):
                    say["ingilizce"] += 1
                    print("İngilizce kaldı, bu çalışmada atlandı:", a["baslik"]); continue
            sozluk = isim_sozlugu([m["baslik"] for m in a["uyeler"]], metin, out.get("ozel_isimler", []), ogrenilen)
            baslik_tr = baslik_duzelt(out["baslik"], sozluk)
            ozet = ozet_duzelt(out["ozet"], sozluk)
            kullanilan = {v for v in sozluk.values() if v in baslik_tr and v not in SABIT_OZEL_ISIMLER}
            ogrenilen.update(v for v in kullanilan if _isim_gecerli(v) and not any(v != w and v in w for w in kullanilan))   # alt parçaları değil, tam ismi öğren
            haber = dict(id=idle(a["link"]), baslik_tr=baslik_tr, bd6=True, ozet=ozet, link=a["link"],
                             kaynak=a["kaynak"], diger_kaynaklar=a["diger_kaynaklar"], gorsel=gorsel, puan=a["puan"],
                             linkler=linkler, basliklar=[m["baslik"] for m in a["uyeler"]], ts=a["ts"],
                             tarih=dt.datetime.now(TR).strftime("%d.%m.%Y %H:%M"))
            yeni.append(haber); son_liste.insert(0, haber); del son_liste[SON_LISTE:]
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
          f"modelin birleştirdiği: {say['model_birlesen']} | İngilizce kalan: {say['ingilizce']} | görselsiz: {say['gorselsiz']} | hata: {say['hata']} | sıra bekleyen: {len(adaylar) - islenen}")


if __name__ == "__main__":
    main()
