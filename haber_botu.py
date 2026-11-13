#!/usr/bin/env python3
"""Oyun haberleri botu: RSS -> popülerlik -> Türkçe özet (ücretsiz API) -> statik site.

Kurulum:   pip install feedparser trafilatura requests
Çalıştır:  export LLM_API_KEY="..." ; python haber_botu.py
Çıktı:     data/haberler.json  ve  site/index.html
"""
import os, sys, json, time, html, hashlib, difflib, datetime as dt, pathlib
import socket
import feedparser, requests, trafilatura

socket.setdefaulttimeout(20)  # yanıt vermeyen siteler en fazla 20 sn bekletsin

# ---------- AYARLAR ----------
FEEDS = [  # Kendi RSS adreslerini buraya yaz
    "http://www.destructoid.com/?mode=atom",
    "http://www.eurogamer.net/rss/eurogamer_news_feed.rss",
    "http://www.gameinformer.com/feeds/thefeedrss.aspx",
    "http://www.ggmania.com/rss/index.xml",
    "http://www.gamesindustry.biz/rss/gamesindustry_news_feed.rss",
    "http://www.gamespot.com/rss/game_updates.php?type=3",
    "http://www.gamesradar.com/all-platforms/news/rss/",
    "http://feeds.feedburner.com/ign/news",
    "http://kotaku.com/index.xml",
    "http://www.pcgamesn.com/mainrss.xml",
    "http://www.polygon.com/rss/index.xml",
    "http://feeds.feedburner.com/RockPaperShotgun",
]
# Sağlayıcıyı değiştirmek için sadece bu üç satır yeter (hepsi OpenAI uyumlu):
#   Groq:    https://api.groq.com/openai/v1               model: llama-3.3-70b-versatile
#   Gemini:  https://generativelanguage.googleapis.com/v1beta/openai   model: gemini-2.5-flash
#   Ollama:  http://localhost:11434/v1                    model: qwen2.5:14b
API_BASE = os.getenv("LLM_API_BASE", "https://generativelanguage.googleapis.com/v1beta/openai")
MODEL = os.getenv("LLM_MODEL", "gemini-3.5-flash-lite")   # 404 alırsan: python haber_botu.py --modeller
# >>> Google AI Studio API anahtarını aşağıdaki tırnakların içine yapıştır <<<
API_KEY = os.getenv("LLM_API_KEY", "AQ.Ab8RN6ISv9xAc0OnfDTqaIlaU9CcVb7BWpzNoWGkn5x2hAt_Dw")
HABER_SAYISI = 8          # her çalışmada en fazla kaç haber
SAAT_SINIRI = 36          # kaç saatten eski haberler alınmasın
SAKLANAN = 200            # sitede tutulacak toplam haber
# ------------------------------

DATA = pathlib.Path("data/haberler.json")
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
    for url in FEEDS:
        print("RSS okunuyor:", url, flush=True)
        try:
            d = feedparser.parse(url)
        except Exception as ex:
            print("  RSS hatası:", ex); continue
        if not d.entries:
            print("  -> haber alınamadı (engelli ya da adres değişmiş olabilir)"); continue
        kaynak = d.feed.get("title", url)
        for e in d.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            ts = time.mktime(t) if t else simdi
            if simdi - ts > SAAT_SINIRI * 3600:
                continue
            liste.append(dict(baslik=e.get("title", "").strip(), link=e.get("link"),
                              kaynak=kaynak, ts=ts, gorsel=gorsel_bul(e)))
    return liste


def benzer(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.6


def grupla_ve_puanla(liste):
    """Benzer başlıkları aynı konu say; konuyu yazan kaynak sayısı + tazelik = puan."""
    gruplar = []
    for h in sorted(liste, key=lambda x: -x["ts"]):
        for g in gruplar:
            if benzer(h["baslik"], g[0]["baslik"]):
                g.append(h); break
        else:
            gruplar.append([h])
    sonuc = []
    for g in gruplar:
        kaynaklar = {x["kaynak"] for x in g}
        yas_saat = (time.time() - g[0]["ts"]) / 3600
        puan = len(kaynaklar) * 3 + max(0, 24 - yas_saat) / 8
        ana = dict(g[0]); ana["puan"] = round(puan, 2)
        ana["diger_kaynaklar"] = [{"ad": x["kaynak"], "link": x["link"]} for x in g[1:]]
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


PROMPT = """Sen bir Türk oyun haberleri editörüsün. Aşağıdaki İngilizce haber metnine DAYANARAK
özgün bir Türkçe haber yaz. Kurallar:
- Metinde olmayan hiçbir bilgiyi ekleme, tahmin yürütme.
- Cümleleri çevirme; olayı kendi cümlelerinle anlat (en fazla 120 kelime).
- Oyun, şirket ve kişi adlarını orijinal yaz.
- Haber video oyunları, oyun şirketleri veya oyun donanımıyla ilgili değilse (film, dizi, LEGO, genel teknoloji vb.) sadece {"uygun": false} döndür.
- Aksi halde sadece geçerli JSON döndür: {"uygun": true, "baslik": "...", "ozet": "...", "etiketler": ["...", "..."]}
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
<style>
:root{--bg:#eef1f7;--card:#fff;--ink:#141a2e;--muted:#5d6580;--accent:#3a4bff;--line:#d9deea;--tag:#e6e9ff}
@media (prefers-color-scheme:dark){:root{--bg:#0e1222;--card:#171d33;--ink:#e8ebf7;--muted:#99a2c4;--accent:#8c99ff;--line:#262e4d;--tag:#222a52}}
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
.tags span{display:inline-block;background:var(--tag);color:var(--accent);border-radius:99px;padding:1px 10px;font-size:13px;margin:0 6px 4px 0}
.pop{display:inline-block;background:var(--accent);color:#fff;border-radius:4px;padding:0 8px;font-size:12px;margin-bottom:8px}
small{color:var(--muted)}
a{color:var(--accent)}
@media(max-width:640px){.lead{grid-template-columns:1fr}.liste article{grid-template-columns:1fr}.g{min-height:170px}}
</style>
</head>
<body>
<div class="wrap">
<header><h1>Oyun Haberleri</h1><small>Son güncelleme: __TARIH__</small></header>
__ICERIK__
</div>
</body>
</html>"""


def _kart(h, lead=False):
    e = html.escape
    a, b = GRADYANLAR[int(h["id"][:2], 16) % len(GRADYANLAR)]
    img = (f'<img src="{e(h["gorsel"])}" alt="" loading="lazy" referrerpolicy="no-referrer" '
           f'onerror="this.remove()">') if h.get("gorsel") else ""
    gorsel = f'<div class="g" style="--a:{a};--b:{b}">{img}</div>'
    diger = h.get("diger_kaynaklar", [])
    rozet = f'<span class="pop">{1 + len(diger)} kaynak</span>' if diger else ""
    etiket = "".join(f"<span>#{e(t)}</span>" for t in h.get("etiketler", []))
    kaynaklar = f'<a href="{e(h["link"])}" rel="nofollow noopener">{e(h["kaynak"])}</a>' + "".join(
        f', <a href="{e(d["link"])}" rel="nofollow noopener">{e(d["ad"])}</a>' for d in diger)
    govde = (f'{rozet}<{"h2" if lead else "h3"}>{e(h["baslik_tr"])}</{"h2" if lead else "h3"}>'
             f'<p>{e(h["ozet"])}</p><div class="tags">{etiket}</div>'
             f'<small>Kaynak: {kaynaklar} · {h["tarih"]}</small>')
    if lead:
        return f'<section class="lead">{gorsel}<div class="b">{govde}</div></section>'
    return f'<article>{gorsel}<div>{govde}</div></article>'


def site_uret(haberler):
    SITE.parent.mkdir(exist_ok=True)
    if haberler:
        icerik = _kart(haberler[0], lead=True)
        icerik += '<section class="liste">' + "".join(_kart(h) for h in haberler[1:]) + "</section>"
    else:
        icerik = "<p>Henüz haber yok.</p>"
    sayfa = SAYFA.replace("__TARIH__", dt.datetime.now().strftime("%d.%m.%Y %H:%M")).replace("__ICERIK__", icerik)
    SITE.write_text(sayfa, encoding="utf-8")


def modelleri_listele():
    r = requests.get(f"{API_BASE}/models", headers={"Authorization": f"Bearer {API_KEY}"}, timeout=30)
    r.raise_for_status()
    print("Anahtarınla kullanılabilen modeller (MODEL satırına bu adlardan birini yaz):")
    for m in r.json().get("data", []):
        print(" ", m["id"].replace("models/", ""))


def main():
    if not API_KEY or API_KEY.startswith("BURAYA"):
        raise SystemExit("API anahtarı girilmemiş. Dosyanın başındaki API_KEY satırına anahtarını yapıştır.")
    if "--modeller" in sys.argv:
        return modelleri_listele()
    DATA.parent.mkdir(exist_ok=True)
    arsiv = json.loads(DATA.read_text("utf-8")) if DATA.exists() else []
    gorulen = {h["id"] for h in arsiv}
    adaylar = [a for a in grupla_ve_puanla(kaynaklari_oku())
               if hashlib.md5(a["link"].encode()).hexdigest() not in gorulen]
    print(f"{len(adaylar)} aday haber bulundu, özetleniyor (her biri birkaç saniye sürer)...", flush=True)
    yeni = []
    for a in adaylar:
        if len(yeni) >= HABER_SAYISI:
            break
        try:
            metin, og = sayfa_cek(a["link"])
            if not metin or len(metin) < 300:
                continue
            out = yapay_zeka(a["baslik"], metin)
            if not out.get("uygun", True):
                print("Oyun dışı, atlandı:", a["baslik"])
                continue
            yeni.append(dict(id=hashlib.md5(a["link"].encode()).hexdigest(), baslik_tr=out["baslik"],
                             ozet=out["ozet"], etiketler=out.get("etiketler", []), link=a["link"],
                             kaynak=a["kaynak"], diger_kaynaklar=a["diger_kaynaklar"],
                             gorsel=a["gorsel"] or og, puan=a["puan"],
                             tarih=dt.datetime.now().strftime("%d.%m.%Y %H:%M")))
            print("OK:", out["baslik"])
            time.sleep(7)  # ücretsiz katmanda dakika limitine takılmamak için
        except Exception as ex:
            print("Atlandı:", a["baslik"], ex)
    arsiv = (yeni + arsiv)[:SAKLANAN]
    DATA.write_text(json.dumps(arsiv, ensure_ascii=False, indent=1), "utf-8")
    site_uret(arsiv)
    print(f"{len(yeni)} yeni haber eklendi.")


if __name__ == "__main__":
    main()
