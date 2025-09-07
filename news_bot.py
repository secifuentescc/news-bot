import os
import re
import json
import hashlib
import logging
import argparse
from datetime import datetime
import pathlib
import requests
import feedparser
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import google.generativeai as genai

# ================== CONFIG ==================
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()

STATE_PATH = pathlib.Path("state_sent.json")

NEWS_SOURCES = {
    "tecnologia": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://arstechnica.com/feed/",
        "https://www.wired.com/feed",
        "https://feeds.feedburner.com/Techmeme",
        "https://spectrum.ieee.org/rss/fulltext",
        "https://www.technologyreview.com/feed/",
        "https://www.engadget.com/rss.xml",
        "https://ai.googleblog.com/feeds/posts/default",
        "https://openai.com/blog/rss.xml",
        "https://blogs.nvidia.com/feed/",
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
    ],
}

def quotas_for_today():
    # Cupos objetivo por categoría (ajustables). Luego garantizamos mínimo 1 por categoría.
    return {"tecnologia": 6, "medicina": 3, "colombia": 2, "mundial": 2}

# ================== MARKDOWNV2 ==================
def escape_md2(text: str) -> str:
    if text is None:
        return ""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', text)

def escape_md2_url(url: str) -> str:
    if not url:
        return ""
    return re.sub(r'([_*$begin:math:display$$end:math:display$()~`>#+\-=|{}.!\\])', r'\\\1', url)

def chunk(text: str, limit: int = 4000):
    parts, t = [], text
    while len(t) > limit:
        cut = t.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(t[:cut])
        t = t[cut:]
    if t:
        parts.append(t)
    return parts

# ================== STATE / DEDUP ==================
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

# ================== LENGUA & TRADUCCIÓN ==================
EN_STOP = {"the","and","for","from","with","that","this","on","in","to","by","of","is","are","as","at","be","or","an","it"}

def looks_english(text: str) -> bool:
    if not text:
        return False
    t = re.sub(r"[^a-zA-Z\s]", " ", text).lower()
    toks = [w for w in t.split() if len(w) > 2]
    if not toks:
        return False
    hits = sum(1 for w in toks[:40] if w in EN_STOP)
    return hits >= max(2, len(toks[:40]) // 6)

def translate_mymemory(text: str) -> str:
    try:
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": "en|es"},
            timeout=12,
        )
        if r.ok:
            data = r.json()
            out = (data.get("responseData") or {}).get("translatedText") or ""
            return out.strip() or text
        return text
    except Exception as e:
        logger.warning(f"MyMemory falló: {e}")
        return text

def translate_libretranslate(text: str) -> str:
    try:
        r = requests.post(
            "https://libretranslate.com/translate",
            data={"q": text, "source": "en", "target": "es", "format": "text"},
            timeout=12,
        )
        if r.ok:
            out = r.json().get("translatedText") or ""
            return out.strip() or text
        return text
    except Exception as e:
        logger.warning(f"LibreTranslate falló: {e}")
        return text

def translate_googletrans(text: str) -> str:
    try:
        # Import tardío para evitar costos de import si no se usa
        from googletrans import Translator
        translator = Translator()
        out = translator.translate(text, src="en", dest="es").text
        return (out or "").strip() or text
    except Exception as e:
        logger.warning(f"googletrans falló: {e}")
        return text

# ================== IMÁGENES ==================
def extract_feed_image(entry) -> str | None:
    # RSS / media content
    try:
        # media_content
        media = entry.get("media_content")
        if isinstance(media, list) and media:
            url = media[0].get("url")
            if url:
                return url
        # media_thumbnail
        thumb = entry.get("media_thumbnail")
        if isinstance(thumb, list) and thumb:
            url = thumb[0].get("url")
            if url:
                return url
        # itunes:image
        if "image" in entry and isinstance(entry["image"], dict):
            url = entry["image"].get("href") or entry["image"].get("url")
            if url:
                return url
    except Exception:
        pass
    return None

def fetch_og_image(link: str) -> str | None:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (NewsBot/1.0)"}
        r = requests.get(link, headers=headers, timeout=10)
        if not r.ok:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for prop in ["og:image", "twitter:image", "og:image:secure_url"]:
            tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
            if tag:
                url = tag.get("content") or ""
                if url.startswith("//"):
                    url = "https:" + url
                if url.startswith("http"):
                    return url
    except Exception:
        return None
    return None

def get_image_for_entry(entry, link: str) -> str | None:
    return extract_feed_image(entry) or fetch_og_image(link)

# ================== BOT ==================
class NewsBot:
    def __init__(self, only_tech: bool = False, only_medicine: bool = False):
        self.only_tech = only_tech
        self.only_medicine = only_medicine
        self.processed = set()
        self.sent_ids = load_state()

        # IA opcional
        self.model = None
        if GEMINI_KEY:
            try:
                genai.configure(api_key=GEMINI_KEY)
                self.model = genai.GenerativeModel("gemini-1.5-flash")
                masked = GEMINI_KEY[:4] + "..." + GEMINI_KEY[-4:]
                logger.info(f"Gemini listo (key {masked}).")
            except Exception as e:
                logger.warning(f"No se pudo inicializar Gemini: {e}")
        else:
            logger.warning("GEMINI_API_KEY no definido: no habrá traducción/resumen por Gemini.")

    # -------- Telegram --------
    def send_text(self, text: str):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": False,
        }
        try:
            r = requests.post(url, data=payload, timeout=20)
            if not r.ok:
                logger.error(f"Telegram sendMessage error: {r.text}")
        except Exception as e:
            logger.error(f"Error enviando texto: {e}")

    def send_long_text(self, text: str):
        for p in chunk(text):
            self.send_text(p)

    def send_photo(self, photo_url: str, caption_md2: str):
        if not photo_url:
            return False
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return False
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": photo_url,
            "caption": caption_md2[:1024],  # límite caption
            "parse_mode": "MarkdownV2",
        }
        try:
            r = requests.post(url, data=data, timeout=20)
            if not r.ok:
                logger.warning(f"Telegram sendPhoto error: {r.text}")
                return False
            return True
        except Exception as e:
            logger.warning(f"Error enviando foto: {e}")
            return False

    # -------- IA helpers --------
    def translate_force_es(self, text: str) -> str:
        if not text:
            return text

        # 0) si ya parece español, devuelve
        if not looks_english(text):
            return text

        # 1) Gemini
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

        # 2) MyMemory
        out = translate_mymemory(text)
        if out and out != text:
            return out

        # 3) LibreTranslate
        out = translate_libretranslate(text)
        if out and out != text:
            return out

        # 4) googletrans
        out = translate_googletrans(text)
        if out and out != text:
            return out

        # 5) original
        return text

    def summarize_extended(self, title_es: str, description_es: str, category: str) -> str:
        base = (description_es or title_es or "").strip()
        # IA
        if self.model:
            try:
                prompt = (
                    "Redacta un resumen informativo en español (3 a 5 frases, "
                    "máximo ~90 palabras) sobre la noticia. Explica qué pasó, "
                    "por qué importa y da contexto. Sin opiniones ni emojis.\n\n"
                    f"Título: {title_es}\n"
                    f"Descripción: {description_es}\n"
                    f"Categoría: {category.upper()}\n"
                )
                resp = self.model.generate_content(prompt)
                out = (getattr(resp, "text", "") or "").strip()
                if out:
                    return out
            except Exception as e:
                logger.warning(f"Gemini resumen falló: {e}")
        # Fallback
        return base[:600]

    def rank_techoriented(self, articles):
        # Heurístico: primero tecnología; luego resto como llegan.
        # (Si quieres IA ranking, puedes reactivar aquí con Gemini y JSON)
        return sorted(articles, key=lambda a: (a["cat"] != "tecnologia",))

    # -------- Recolección --------
    def get_rss(self, category: str):
        arts = []
        for rss in NEWS_SOURCES.get(category, []):
            try:
                feed = feedparser.parse(rss)
                for entry in feed.entries[:8]:
                    link = entry.get("link") or ""
                    title = entry.get("title") or ""
                    if not link or not title:
                        continue
                    raw_desc = entry.get("description", "") or entry.get("summary", "")
                    desc = BeautifulSoup(raw_desc, "html.parser").get_text(" ").strip()
                    a = {
                        "_i": len(self.processed),
                        "title": title,
                        "desc": desc,
                        "link": link,
                        "cat": category,
                        "_entry": entry,  # para intentar imagen
                    }
                    uid = article_uid(a)
                    if uid in self.processed or uid in self.sent_ids:
                        continue
                    arts.append(a)
                    self.processed.add(uid)
            except Exception as e:
                logger.error(f"Error con RSS {rss}: {e}")
        return arts

    # -------- Pipeline --------
    def collect_all(self):
        if self.only_tech:
            cats = ["tecnologia"]
        elif self.only_medicine:
            cats = ["medicina"]
        else:
            cats = ["tecnologia", "medicina", "colombia", "mundial"]

        all_articles = []
        for c in cats:
            all_articles.extend(self.get_rss(c))
        logger.info(f"Recolectadas {len(all_articles)} noticias (cats={cats}).")
        return all_articles

    def select_top_by_quota(self, articles):
        if not articles:
            return []

        ranked = self.rank_techoriented(articles)
        q = quotas_for_today()
        counts = {k: 0 for k in q}
        selected = []

        # 1) Llenar por cupos
        for a in ranked:
            if counts.get(a["cat"], 0) < q.get(a["cat"], 0):
                selected.append(a)
                counts[a["cat"]] += 1
            if sum(counts.values()) >= sum(q.values()):
                break

        # 2) Garantizar mínimo 1 por categoría (si existe material)
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

        # Orden final para presentación
        order = {"tecnologia": 0, "medicina": 1, "colombia": 2, "mundial": 3}
        selected.sort(key=lambda a: order.get(a["cat"], 9))
        logger.info(
            "Seleccionadas: "
            f"tech={sum(1 for x in selected if x['cat']=='tecnologia')}, "
            f"med={sum(1 for x in selected if x['cat']=='medicina')}, "
            f"col={sum(1 for x in selected if x['cat']=='colombia')}, "
            f"mun={sum(1 for x in selected if x['cat']=='mundial')}"
        )
        return selected

    def build_and_send_digest(self, selected):
        if not selected:
            self.send_text("*No hay noticias nuevas*")
            return

        icons  = {"tecnologia": "💻", "colombia": "🇨🇴", "mundial": "🌍", "medicina": "🩺"}
        titles = {"tecnologia": "TECNOLOGÍA", "colombia": "COLOMBIA", "mundial": "MUNDIAL", "medicina": "MEDICINA"}

        # Header general
        header = f"📰 *{escape_md2('Boletín de noticias')}* — {escape_md2(datetime.now().strftime('%d/%m/%Y %H:%M'))}\n\n"
        text_block = header

        # Enviar 1ª noticia de cada categoría como foto si hay imagen
        first_sent_photo_for = set()

        current_cat = None
        for a in selected:
            # Cambió de categoría ⇒ encabezado
            if a["cat"] != current_cat:
                current_cat = a["cat"]
                text_block += f"{icons.get(current_cat,'📰')} *{escape_md2(titles[current_cat])}*\n"

                # Intentar foto para el primer ítem de la categoría
                if current_cat not in first_sent_photo_for:
                    img = get_image_for_entry(a.get("_entry", {}), a["link"])
                    if img:
                        title_es = self.translate_force_es(a["title"])
                        desc_src = a["desc"] if a["desc"] else a["title"]
                        resumen = self.summarize_extended(title_es, self.translate_force_es(desc_src), a["cat"])
                        caption = (
                            f"*{escape_md2(title_es)}*\n"
                            f"{escape_md2(resumen)}\n"
                            f"{escape_md2_url(a['link'])}"
                        )
                        ok = self.send_photo(img, caption)
                        if ok:
                            first_sent_photo_for.add(current_cat)
                            # Marca como enviado este artículo con foto y no lo dupliques en texto
                            self.sent_ids.add(article_uid(a))
                            continue  # pasa al siguiente artículo (ya enviado como foto)

            # Envío normal (texto)
            title_es = self.translate_force_es(a["title"])
            desc_src = a["desc"] if a["desc"] else a["title"]
            resumen = self.summarize_extended(title_es, self.translate_force_es(desc_src), a["cat"])
            text_block += (
                f"• *{escape_md2(title_es)}*\n"
                f"{escape_md2(resumen)}\n"
                f"{escape_md2_url(a['link'])}\n\n"
            )

        text_block += escape_md2("---") + "\n" + "_Resumen automatizado con IA (prioridad tecnología)_"
        self.send_long_text(text_block)

    def run(self):
        all_articles = self.collect_all()
        all_articles = [a for a in all_articles if article_uid(a) not in self.sent_ids]
        selected = self.select_top_by_quota(all_articles)

        self.build_and_send_digest(selected)

        # Guardar estado de enviados
        for a in selected:
            self.sent_ids.add(article_uid(a))
        save_state(self.sent_ids)

        # Guardar reporte en repo
        reports = pathlib.Path("reports")
        reports.mkdir(exist_ok=True)
        fname = reports / f"boletin_{datetime.now().strftime('%Y-%m-%d_%H%M')}.md"
        try:
            # Guardamos SOLO el texto (no incluye las fotos ya enviadas)
            # Si quieres, puedes también persistir el bloque construido.
            fname.write_text("Enviado vía Telegram. Ver historial en el chat.", encoding="utf-8")
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
    # Prueba rápida de traducción
    prueba = bot.translate_force_es("Breaking: Apple unveils a new AI feature for iPhone.")
    logger.info(f"Traducción de prueba: {prueba}")
    logger.info("Generando y enviando boletín...")
    bot.run()
    logger.info("Listo.")

if __name__ == "__main__":
    main()
