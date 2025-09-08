import os
import re
import json
import hashlib
import logging
import argparse
import time
from datetime import datetime
import pathlib
import requests
import feedparser
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from urllib.parse import quote
import html

# Gemini opcional
import google.generativeai as genai

# ================== CONFIG ==================
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_KEY         = os.getenv("GEMINI_API_KEY", "").strip()

STATE_PATH = pathlib.Path("state_sent.json")

# Fuentes RSS (incluye Colombia, Tech, Mundial y Medicina) + Apple/dev extra
NEWS_SOURCES = {
    "tecnologia": [
        # Core tech
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://arstechnica.com/feed/",
        "https://www.wired.com/feed",
        "https://feeds.feedburner.com/Techmeme",
        "https://spectrum.ieee.org/rss/fulltext",
        "https://www.technologyreview.com/feed/",
        "https://www.engadget.com/rss.xml",
        # IA/Cloud
        "https://ai.googleblog.com/feeds/posts/default",
        "https://openai.com/blog/rss.xml",
        "https://blogs.nvidia.com/feed/",
        # Apple / iOS
        "https://www.apple.com/newsroom/rss-feed.rss",
        "https://developer.apple.com/news/releases/rss.xml",
        "https://9to5mac.com/feed/",
        "https://www.macrumors.com/macrumors.xml",
        "https://appleinsider.com/rss",
        "https://iosdevweekly.com/issues.rss",
        # Android / Dev general
        "https://android-developers.googleblog.com/atom.xml",
        "https://github.blog/changelog/feed/",
        "https://news.ycombinator.com/rss",
        "http://export.arxiv.org/rss/cs.AI",
        # Tech en español
        "https://www.xataka.com/tag/feeds/rss2.xml",
        "https://www.hipertextual.com/feed",
        "https://www.genbeta.com/feed",
    ],
    "colombia": [
        "https://www.eltiempo.com/rss.xml",
        "https://www.semana.com/rss.xml",
        "https://www.elespectador.com/rss.xml",
        "https://www.bluradio.com/rss",
        "https://www.elcolombiano.com/rss/portada.xml",
        "https://www.larepublica.co/rss",
        "https://www.portafolio.co/rss",
    ],
    "mundial": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://rss.cnn.com/rss/edition.rss",
        "https://www.reuters.com/rssFeed/worldNews",
    ],
    "medicina": [
        "https://www.nejm.org/action/showFeed?type=etoc&feed=rss&jc=nejm",
        "https://www.thelancet.com/rssfeed/lancet_current.xml",
        "https://jamanetwork.com/rss/site_6/mostRecent.xml",
        "https://www.nature.com/subjects/medicine.rss",
        "https://www.medscape.com/rss/all",
        # extra fuertes
        "https://www.nih.gov/news-events/news-releases.xml",
        "https://www.who.int/rss-feeds/news-english.xml",
        "https://www.medrxiv.org/rss/latest.xml",
        "https://www.fda.gov/about-fda/newsroom/press-announcements/rss.xml",
    ],
}

def quotas_for_today():
    return {"tecnologia": 8, "medicina": 4, "colombia": 3, "mundial": 3}

# ================== ESCAPES (HTML) ==================
def html_escape(text: str) -> str:
    """Escapa texto para parse_mode=HTML de Telegram."""
    if text is None:
        return ""
    # Escapa &, <, > y comillas
    return html.escape(text, quote=True)

def url_safe(url: str) -> str:
    """Percent-encode sin romper esquemas/domino/path; sin escapado HTML adicional."""
    if not url:
        return ""
    return quote(url, safe=":/?&=%#@+.,-~")

# ================== UTIL ==================
def chunk_text(text: str, limit: int = 4000):
    parts = []
    t = text
    while len(t) > limit:
        cut = t.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(t[:cut])
        t = t[cut:]
    if t:
        parts.append(t)
    return parts

def article_uid(a) -> str:
    base = f"{a.get('title','')}|{a.get('link','')}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]

def load_state():
    if STATE_PATH.exists():
        try:
            return set(json.loads(STATE_PATH.read_text(encoding="utf-8") or "[]"))
        except Exception:
            return set()
    return set()

def save_state(sent_ids: set):
    try:
        STATE_PATH.write_text(json.dumps(list(sent_ids)), encoding="utf-8")
    except Exception as e:
        logger.warning(f"No se pudo guardar el estado: {e}")

# ================== EXTRACCIÓN DE IMAGEN ==================
def get_image_for_entry(entry: dict, fallback_link: str) -> str | None:
    media_content = entry.get("media_content") or entry.get("media:content")
    if isinstance(media_content, list) and media_content:
        url = media_content[0].get("url")
        if url:
            return url
    if isinstance(media_content, dict):
        url = media_content.get("url")
        if url:
            return url
    media_thumbnail = entry.get("media_thumbnail") or entry.get("media:thumbnail")
    if isinstance(media_thumbnail, list) and media_thumbnail:
        url = media_thumbnail[0].get("url")
        if url:
            return url
    if isinstance(media_thumbnail, dict):
        url = media_thumbnail.get("url")
        if url:
            return url
    enclosures = entry.get("enclosures") or []
    for enc in enclosures:
        typ = enc.get("type", "")
        url = enc.get("url")
        if url and ("image" in typ or url.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
            return url
    for key in ("summary", "summary_detail", "content", "description"):
        val = entry.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        if isinstance(val, list) and val:
            val = val[0].get("value")
        if isinstance(val, str) and "<img" in val:
            soup = BeautifulSoup(val, "html.parser")
            img = soup.find("img")
            if img and img.get("src"):
                return img.get("src")
    return None

# ================== ENRIQUECIMIENTO DE TEXTO (sin IA) ==================
def fetch_article_snippet(url: str, min_len: int = 300, max_len: int = 900) -> str | None:
    try:
        r = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        if not r.ok or not r.text:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        candidates = []
        for p in soup.find_all("p"):
            txt = p.get_text(" ", strip=True)
            if not txt:
                continue
            low = txt.lower()
            if "cookie" in low or "suscríbete" in low or "subscribe" in low:
                continue
            if len(txt) >= min_len:
                candidates.append(txt[:max_len])
        if not candidates:
            return None
        return sorted(candidates, key=len, reverse=True)[0]
    except Exception:
        return None

# ================== TRADUCCIÓN: ARGOS + GEMINI + MYMEMORY ==================
def init_argos():
    """Prepara Argos Translate y se asegura de que el modelo en→es esté instalado."""
    try:
        from argostranslate import package, translate as ar_translate

        installed = ar_translate.get_installed_languages()
        en = next((l for l in installed if getattr(l, "code", "") == "en"), None)
        es = next((l for l in installed if getattr(l, "code", "") == "es"), None)
        if en and es:
            tr = en.get_translation(es)
            if tr:
                logger.info("Argos en→es disponible (instalado).")
                return lambda txt: tr.translate(txt)

        try:
            package.update_package_index()
            available = package.get_available_packages()
            pkg = next((p for p in available if p.from_code == "en" and p.to_code == "es"), None)
            if pkg:
                logger.info("Descargando modelo Argos en→es...")
                path = pkg.download()
                package.install_from_path(path)
                installed = ar_translate.get_installed_languages()
                en = next((l for l in installed if getattr(l, "code", "") == "en"), None)
                es = next((l for l in installed if getattr(l, "code", "") == "es"), None)
                if en and es:
                    tr = en.get_translation(es)
                    if tr:
                        logger.info("Argos en→es instalado y listo.")
                        return lambda txt: tr.translate(txt)
        except Exception as ie:
            logger.warning(f"No pude instalar modelo Argos en→es: {ie}")

        logger.warning("Argos no disponible: modelo en→es ausente.")
        return None
    except Exception as e:
        logger.warning(f"Argos no disponible: {e}")
        return None

def split_for_mymemory(text: str, max_len: int = 450):
    text = text.strip()
    if len(text) <= max_len:
        return [text]
    sentences = re.split(r'(?<=[\.\!\?])\s+', text)
    chunks, cur = [], ""
    for s in sentences:
        if not s:
            continue
        if len(cur) + len(s) + 1 <= max_len:
            cur = (cur + " " + s).strip()
        else:
            if cur:
                chunks.append(cur)
            if len(s) <= max_len:
                cur = s
            else:
                while len(s) > max_len:
                    cut = s.rfind(" ", 0, max_len)
                    if cut == -1:
                        cut = max_len
                    chunks.append(s[:cut])
                    s = s[cut:].lstrip()
                cur = s
    if cur:
        chunks.append(cur)
    return chunks

def mymemory_translate_en_es(text: str, pause: float = 0.9) -> str:
    if not text:
        return text
    out_parts = []
    for part in split_for_mymemory(text, max_len=450):
        for _try in range(2):
            try:
                r = requests.get(
                    "https://api.mymemory.translated.net/get",
                    params={"q": part, "langpair": "en|es"},
                    timeout=12,
                )
                if r.ok:
                    data = r.json()
                    translated = data.get("responseData", {}).get("translatedText", "")
                    if translated:
                        out_parts.append(translated)
                        break
                    else:
                        if len(part) > 300:
                            subparts = split_for_mymemory(part, max_len=300)
                            out_parts.append(mymemory_translate_en_es(" ".join(subparts), pause))
                            break
                time.sleep(pause)
            except Exception:
                time.sleep(pause)
                continue
        else:
            out_parts.append(part)
        time.sleep(pause)
    return " ".join(out_parts).strip()

# ================== BOT ==================
class NewsBot:
    def __init__(self, only_tech: bool = False, only_medicine: bool = False):
        self.only_tech = only_tech
        self.only_medicine = only_medicine
        self.processed = set()
        self.sent_ids = load_state()

        # Argos primero (estable/offline)
        self.argos = init_argos()
        if self.argos:
            logger.info("Traductor principal: Argos Translate (offline)")

        # Gemini (opcional)
        self.model = None
        if GEMINI_KEY:
            try:
                genai.configure(api_key=GEMINI_KEY)
                self.model = genai.GenerativeModel("gemini-1.5-flash")
                masked = GEMINI_KEY[:4] + "..." + GEMINI_KEY[-4:]
                logger.info(f"Gemini listo (key {masked}).")
            except Exception as e:
                logger.warning(f"No se pudo inicializar Gemini: {e}")

    # ------------ Transporte (HTML) ------------
    def send_text(self, text_html: str):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        try:
            r = requests.post(url, data=payload, timeout=25)
            if not r.ok:
                logger.error(f"Telegram error: {r.text}")
        except Exception as e:
            logger.error(f"Error enviando a Telegram (texto): {e}")

    def send_photo(self, photo_url: str, caption_html: str) -> bool:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
            return False
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": photo_url,
            "caption": caption_html[:1024],   # límite caption Telegram
            "parse_mode": "HTML",
        }
        try:
            r = requests.post(url, data=payload, timeout=25)
            if not r.ok:
                logger.warning(f"sendPhoto falló, usaré texto. Respuesta: {r.text}")
                return False
            return True
        except Exception as e:
            logger.warning(f"Error sendPhoto: {e}")
            return False

    def send_long(self, text_html: str):
        for p in chunk_text(text_html):
            self.send_text(p)

    # ------------ Traducción / Resumen ------------
    def translate_force_es(self, text: str) -> str:
        if not text:
            return text

        # 1) Argos offline
        if self.argos:
            try:
                return self.argos(text)
            except Exception as e:
                logger.warning(f"Argos falló en tiempo de ejecución: {e}")

        # 2) Gemini
        if self.model:
            try:
                prompt = (
                    "Traduce al español de forma natural y clara. "
                    "No agregues comentarios ni comillas. Solo el texto traducido.\n\n"
                    f"{text}"
                )
                resp = self.model.generate_content(prompt)
                out = (getattr(resp, "text", "") or "").strip()
                if out:
                    return out
            except Exception as e:
                logger.warning(f"Gemini traducción falló: {e}")

        # 3) MyMemory chunked
        return mymemory_translate_en_es(text)

    def summarize_extended(self, title_es: str, base_es: str, category: str) -> str:
        if self.model:
            try:
                prompt = (
                    "Escribe un resumen extendido en español, claro y profesional, de ~120–160 palabras. "
                    "Incluye: qué pasó, contexto breve, por qué importa y qué viene después. "
                    "Evita opiniones y emojis; un solo párrafo fluido. No repitas el título.\n\n"
                    f"CATEGORÍA: {category.upper()}\n"
                    f"TÍTULO: {title_es}\n"
                    f"TEXTO/EXTRACTO:\n{base_es}\n"
                )
                resp = self.model.generate_content(prompt)
                out = (getattr(resp, "text", "") or "").strip()
                if out:
                    return out[:900]
            except Exception as e:
                logger.warning(f"Gemini resumen extendido falló: {e}")
        return base_es[:900]

    def rank_with_gemini(self, articles):
        KW_BOOST = {
            "apple": 2.2, "ios": 2.2, "iphone": 1.8, "ipad": 1.6, "mac": 1.4, "swift": 2.0, "xcode": 1.9, "wwdc": 2.2,
            "javascript": 2.0, "typescript": 1.8, "node": 1.8, "react": 1.8, "python": 2.0, "django": 1.5, "fastapi": 1.7,
            "docker": 1.6, "kubernetes": 1.6, "github": 1.7, "vscode": 1.6,
            "ai": 1.8, "machine learning": 1.8, "gemini": 1.6, "gpt": 1.6, "openai": 1.8, "llm": 1.8,
        }
        def manual_score(a):
            base = 1.0
            text = f"{a['title']} {a['desc']}".lower()
            for k, w in KW_BOOST.items():
                if k in text:
                    base += w
            if a["cat"] == "tecnologia":
                base += 0.8
            return base

        if not self.model or not articles:
            return sorted(articles, key=manual_score, reverse=True)

        try:
            packed = "\n".join(
                [f"{i+1}. [{a['cat']}] {a['title']}\n{(a['desc'] or '')[:280]}" for i, a in enumerate(articles)]
            )
            prompt = (
                "Eres editor senior. Puntúa cada ítem del 1 al 10 según impacto, novedad "
                "y relevancia para lectores hispanohablantes (prioriza TECNOLOGÍA si el impacto es similar). "
                "Devuelve JSON: [{\"idx\": <n>, \"score\": <0-10>}].\n\n"
                f"NOTICIAS:\n{packed}"
            )
            resp = self.model.generate_content(prompt)
            txt = (getattr(resp, "text", "") or "").strip()
            scores = json.loads(txt)
            score_map = {int(it["idx"]) - 1: float(it["score"]) for it in scores if "idx" in it and "score" in it}
            ranked = sorted(
                articles,
                key=lambda a: score_map.get(a["_i"], 0.0) + manual_score(a),
                reverse=True,
            )
            return ranked
        except Exception as e:
            logger.warning(f"No se pudo rankear con IA: {e}")
            return sorted(articles, key=manual_score, reverse=True)

    # ------------ Datos ------------
    def get_rss(self, category: str):
        arts = []
        for rss in NEWS_SOURCES.get(category, []):
            try:
                feed = feedparser.parse(rss)
                for entry in feed.entries[:8]:
                    raw_desc = entry.get("description", "") or entry.get("summary", "")
                    desc = BeautifulSoup(raw_desc, "html.parser").get_text(" ").strip()
                    link = entry.get("link") or ""
                    title = entry.get("title") or ""
                    if not link or not title:
                        continue
                    a = {
                        "_i": len(self.processed),
                        "title": title,
                        "desc": desc,
                        "link": link,
                        "cat": category,
                        "_entry": entry,
                    }
                    uid = article_uid(a)
                    if uid in self.processed or uid in self.sent_ids:
                        continue
                    arts.append(a)
                    self.processed.add(uid)
            except Exception as e:
                logger.error(f"Error con RSS {rss}: {e}")
        return arts

    # ------------ Pipeline ------------
    def collect_all(self):
        if self.only_tech:
            cats = ["tecnologia"]
        elif self.only_medicine:
            cats = ["medicina"]
        else:
            cats = ["tecnologia", "medicina", "colombia", "mundial"]

        all_articles = []
        for cat in cats:
            all_articles.extend(self.get_rss(cat))
        logger.info(f"Recolectadas {len(all_articles)} noticias (cats={cats}).")
        return all_articles

    def select_top_by_quota(self, articles):
        if not articles:
            return []
        ranked = self.rank_with_gemini(articles)
        q = quotas_for_today()
        counts = {k: 0 for k in q}
        selected = []
        for a in ranked:
            if counts.get(a["cat"], 0) < q.get(a["cat"], 0):
                selected.append(a)
                counts[a["cat"]] += 1
            if sum(counts.values()) >= sum(q.values()):
                break
        minimums = {"tecnologia": 1, "medicina": 1, "colombia": 1, "mundial": 1}
        by_cat = {"tecnologia": [], "medicina": [], "colombia": [], "mundial": []}
        for a in ranked:
            by_cat[a["cat"]].append(a)
        sel_ids = {article_uid(x) for x in selected}
        for cat, min_needed in minimums.items():
            have = sum(1 for x in selected if x["cat"] == cat)
            if have >= min_needed:
                continue
            for cand in by_cat.get(cat, []):
                uid = article_uid(cand)
                if uid not in sel_ids:
                    selected.append(cand)
                    sel_ids.add(uid)
                    break
        order = {"tecnologia": 0, "medicina": 1, "colombia": 2, "mundial": 3}
        selected.sort(key=lambda a: order.get(a["cat"], 9))
        logger.info(
            "Seleccionadas (mínimos): "
            f"tech={sum(1 for x in selected if x['cat']=='tecnologia')}, "
            f"med={sum(1 for x in selected if x['cat']=='medicina')}, "
            f"col={sum(1 for x in selected if x['cat']=='colombia')}, "
            f"mun={sum(1 for x in selected if x['cat']=='mundial')}"
        )
        return selected

    def run(self):
        # Encabezado (HTML)
        header = f"📰 <b>{html_escape('Boletín de noticias')}</b> — {html_escape(datetime.now().strftime('%d/%m/%Y %H:%M'))}"
        self.send_text(header)

        all_articles = self.collect_all()
        all_articles = [a for a in all_articles if article_uid(a) not in self.sent_ids]
        selected = self.select_top_by_quota(all_articles)

        if not selected:
            self.send_text("<i>No hay noticias nuevas</i>")
            return

        icons  = {"tecnologia": "💻", "colombia": "🇨🇴", "mundial": "🌍", "medicina": "🩺"}
        titles = {"tecnologia": "TECNOLOGÍA", "colombia": "COLOMBIA", "mundial": "MUNDIAL", "medicina": "MEDICINA"}
        categories_order = ["tecnologia", "medicina", "colombia", "mundial"]

        for cat in categories_order:
            cat_articles = [a for a in selected if a["cat"] == cat]
            if not cat_articles:
                continue

            # Título de categoría
            self.send_text(f"{icons.get(cat,'📰')} <b>{html_escape(titles[cat])}</b>")

            for a in cat_articles:
                # 1) Título → ES
                raw_title = a["title"]
                title_es  = self.translate_force_es(raw_title)

                # 2) Base: descripción RSS; si es corta, enriquecer leyendo la página
                base_text = a["desc"]
                if len(base_text) < 300:
                    extra = fetch_article_snippet(a["link"])
                    if extra:
                        base_text = (base_text + "\n\n" + extra).strip()

                # 3) Base → ES
                base_es = self.translate_force_es(base_text)

                # 4) Resumen extendido
                resumen  = self.summarize_extended(title_es, base_es, a["cat"])

                # 5) Caption con control de 1024, SIEMPRE con enlace inline HTML
                url_raw = a['link']
                url     = url_safe(url_raw)
                title_h = html_escape(title_es)
                resumen_h = html_escape(resumen)

                link_html = f'<a href="{url}">🔗 Leer la nota completa</a>'
                caption_full = f"<b>{title_h}</b>\n{resumen_h}\n{link_html}"

                def send_with_link(img_url: str | None):
                    if len(caption_full) <= 1024:
                        if img_url:
                            ok = self.send_photo(img_url, caption_full)
                            if not ok:
                                self.send_text(caption_full)
                        else:
                            self.send_text(caption_full)
                    else:
                        # recortar el resumen pero mantener título + enlace dentro del caption
                        max_caption = 1000
                        base_len = len(f"<b>{title_h}</b>\n") + len("\n") + len(link_html)
                        resto = max_caption - base_len
                        if resto < 0:
                            resto = 0
                        resumen_rec = html_escape(resumen[:resto].rstrip())
                        caption_short = f"<b>{title_h}</b>\n{resumen_rec}\n{link_html}"
                        if img_url:
                            ok = self.send_photo(img_url, caption_short)
                            if not ok:
                                self.send_text(caption_short)
                        else:
                            self.send_text(caption_short)

                img_url = get_image_for_entry(a.get("_entry", {}), a["link"])
                send_with_link(img_url)

                # Marcar como enviada
                self.sent_ids.add(article_uid(a))

        # Footer
        self.send_text(html_escape("---") + "\n" + "<i>Resumen automatizado con IA (prioridad tecnología)</i>")

        # Persistir estado
        save_state(self.sent_ids)

        # Guardar reporte simple
        reports = pathlib.Path("reports")
        reports.mkdir(exist_ok=True)
        fname = reports / f"boletin_{datetime.now().strftime('%Y-%m-%d_%H%M')}.md"
        try:
            fname.write_text(f"{html.unescape(header)}\n\nReporte enviado por Telegram.\n", encoding="utf-8")
            logger.info(f"Reporte guardado en: {fname}")
        except Exception as e:
            logger.warning(f"No se pudo guardar el reporte: {e}")

        logger.info(f"Boletín enviado. Total noticias: {len(selected)}.")

# ================== MAIN ==================
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--only-tech", action="store_true", help="Enviar solo tecnología")
    p.add_argument("--only-medicine", action="store_true", help="Enviar solo medicina")
    return p.parse_args()

def main():
    args = parse_args()
    bot = NewsBot(only_tech=args.only_tech, only_medicine=args.only_medicine)
    prueba = bot.translate_force_es("Breaking: Apple unveils a new AI feature for iPhone.")
    logger.info(f"Traducción de prueba: {prueba}")
    logger.info("Generando y enviando boletín...")
    bot.run()
    logger.info("Listo.")

if __name__ == "__main__":
    main()
