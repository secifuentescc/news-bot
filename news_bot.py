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
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_KEY         = os.getenv("GEMINI_API_KEY", "").strip()

STATE_PATH = pathlib.Path("state_sent.json")

# Fuentes RSS (incluye Colombia, Tech, Mundial y Medicina)
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
        # Blogs core IA/Cloud
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

# --- (1) Agregamos fuentes nuevas sin borrar las existentes ---
NEWS_SOURCES["tecnologia"] += [
    "https://www.apple.com/newsroom/rss-feed.rss",            # Apple Newsroom
    "https://developer.apple.com/news/releases/rss.xml",      # Apple Developer releases
    "https://iosdevweekly.com/issues.rss",                    # iOS Dev Weekly
    "https://android-developers.googleblog.com/atom.xml",     # Android Developers Blog
    "https://news.ycombinator.com/rss",                       # Hacker News
    "http://export.arxiv.org/rss/cs.AI",                      # arXiv AI
    "https://github.blog/changelog/feed/",                    # GitHub Changelog
]

NEWS_SOURCES["medicina"] += [
    "https://www.fda.gov/about-fda/newsroom/press-announcements/rss.xml",  # FDA PR
    "https://www.nih.gov/news-events/news-releases.xml",                   # NIH
    "https://www.who.int/rss-feeds/news-english.xml",                      # WHO (inglés)
    "https://www.medrxiv.org/rss/latest.xml",                              # medRxiv preprints
]

def quotas_for_today():
    # Cupos por categoría (ajustables). Garantizamos mínimo 1 más abajo.
    return {"tecnologia": 6, "medicina": 3, "colombia": 2, "mundial": 2}

# ================== MARKDOWNV2 ESCAPES ==================
def escape_md2(text: str) -> str:
    """Escapa caracteres especiales para MDV2 (solo contenido)."""
    if text is None:
        return ""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', text)

def escape_md2_url(url: str) -> str:
    """Escapa URL para usarla en texto MDV2 (sin [] para evitar roturas)."""
    if not url:
        return ""
    return re.sub(r'([_*$begin:math:display$$end:math:display$()~`>#+\-=|{}.!\\])', r'\\\1', url)

# ================== UTIL ==================
def chunk(text: str, limit: int = 4000):
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
    """
    Intenta hallar una imagen del RSS:
    - media:content / media:thumbnail
    - enclosure url con tipo imagen
    - primer <img> en summary/detail
    """
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

# ================== BOT ==================
class NewsBot:
    def __init__(self, only_tech: bool = False, only_medicine: bool = False):
        self.only_tech = only_tech
        self.only_medicine = only_medicine
        self.processed = set()
        self.sent_ids = load_state()

        # Inicializa Gemini (opcional)
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
            logger.warning("GEMINI_API_KEY no definido: no habrá traducción/resumen IA.")

    # ------------ Transporte ------------
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
            r = requests.post(url, data=payload, timeout=25)
            if not r.ok:
                logger.error(f"Telegram error: {r.text}")
        except Exception as e:
            logger.error(f"Error enviando a Telegram (texto): {e}")

    def send_photo(self, photo_url: str, caption: str) -> bool:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
            return False
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "photo": photo_url,
            "caption": caption[:1024],   # límite de caption Telegram
            "parse_mode": "MarkdownV2",
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

    def send_long(self, text: str):
        for p in chunk(text):
            self.send_text(p)

    # ------------ Traducción / Resumen ------------
    def translate_force_es(self, text: str) -> str:
        """Fuerza traducción al español. Gemini primero; si falla, MyMemory; si falla, original."""
        if not text:
            return text

        # 1) Gemini si está disponible
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

        # 2) Fallback: MyMemory (en->es)
        try:
            r = requests.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text, "langpair": "en|es"},
                timeout=12,
            )
            if r.ok:
                data = r.json()
                out = data.get("responseData", {}).get("translatedText", "")
                if out:
                    logger.info("Traducción vía MyMemory OK.")
                    return out
        except Exception as e:
            logger.warning(f"MyMemory falló: {e}")

        # 3) Último recurso: original
        return text

    def summarize_extended(self, title_es: str, description_es: str, category: str) -> str:
        """
        Resumen extendido (~90–130 palabras) con:
        - Qué pasó (quién/qué/cuándo/dónde)
        - Contexto/antecedentes
        - Por qué importa (impacto)
        - Qué viene después
        Fallback: trozo de la descripción.
        """
        base = (description_es or title_es or "").strip()
        if not self.model:
            # Sin IA: devolvemos descripción un poco más larga, limitada para caption.
            return base[:700]

        try:
            prompt = (
                "Escribe un resumen extendido en español, claro y profesional, de ~100–130 palabras. "
                "Incluye: qué pasó (quién/qué/cuándo/dónde), contexto breve, por qué importa (impacto) "
                "y qué viene después. Evita opiniones, emojis y listas; un solo párrafo fluido. "
                "No repitas el título.\n\n"
                f"CATEGORÍA: {category.upper()}\n"
                f"TÍTULO: {title_es}\n"
                f"TEXTO/EXTRACTO:\n{description_es}\n"
            )
            resp = self.model.generate_content(prompt)
            out = (getattr(resp, "text", "") or "").strip()
            # Seguridad de longitud para caption de foto (dejamos margen)
            if len(out) > 700:
                out = out[:700]
            return out or base[:700]
        except Exception as e:
            logger.warning(f"Gemini resumen extendido falló: {e}")
            return base[:700]

    def rank_with_gemini(self, articles):
        """Puntúa importancia (0-10) priorizando tecnología; fallback heurístico simple."""
        if not self.model or not articles:
            # Heurística mínima: tecnología primero
            return sorted(articles, key=lambda a: (a["cat"] != "tecnologia",))
        try:
            packed = "\n".join(
                [f"{i+1}. [{a['cat']}] {a['title']}\n{(a['desc'] or '')[:280]}" for i, a in enumerate(articles)]
            )
            prompt = (
                "Eres editor senior. Puntúa cada ítem del 1 al 10 según impacto, novedad "
                "y relevancia para lectores hispanohablantes (da preferencia a TECNOLOGÍA "
                "si el impacto es similar). Devuelve JSON: [{\"idx\": <n>, \"score\": <0-10>}].\n\n"
                f"NOTICIAS:\n{packed}"
            )
            resp = self.model.generate_content(prompt)
            txt = (getattr(resp, "text", "") or "").strip()
            scores = json.loads(txt)
            score_map = {int(it["idx"]) - 1: float(it["score"]) for it in scores if "idx" in it and "score" in it}
            ranked = sorted(
                articles,
                key=lambda a: score_map.get(a["_i"], 0.0) + (1.0 if a["cat"] == "tecnologia" else 0.0),
                reverse=True,
            )
            return ranked
        except Exception as e:
            logger.warning(f"No se pudo rankear con IA: {e}")
            return sorted(articles, key=lambda a: (a["cat"] != "tecnologia",))

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
                        "_entry": entry,   # guardo entry para imagen
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
        """Ordena por importancia y aplica cupos + mínimo 1 de cada categoría disponible."""
        if not articles:
            return []

        ranked = self.rank_with_gemini(articles)

        # Cupos
        q = quotas_for_today()
        counts = {k: 0 for k in q}
        selected = []

        for a in ranked:
            if counts.get(a["cat"], 0) < q.get(a["cat"], 0):
                selected.append(a)
                counts[a["cat"]] += 1
            if sum(counts.values()) >= sum(q.values()):
                break

        # Garantías mínimas por categoría
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
            "Seleccionadas (mínimos): "
            f"tech={sum(1 for x in selected if x['cat']=='tecnologia')}, "
            f"med={sum(1 for x in selected if x['cat']=='medicina')}, "
            f"col={sum(1 for x in selected if x['cat']=='colombia')}, "
            f"mun={sum(1 for x in selected if x['cat']=='mundial')}"
        )
        return selected

    def run(self):
        # ---- Encabezado del boletín (texto) ----
        header = f"📰 *{escape_md2('Boletín de noticias')}* — {escape_md2(datetime.now().strftime('%d/%m/%Y %H:%M'))}"
        self.send_text(header)

        all_articles = self.collect_all()
        all_articles = [a for a in all_articles if article_uid(a) not in self.sent_ids]
        selected = self.select_top_by_quota(all_articles)

        if not selected:
            self.send_text("_No hay noticias nuevas_")
            return

        # Secciones por categoría
        icons  = {"tecnologia": "💻", "colombia": "🇨🇴", "mundial": "🌍", "medicina": "🩺"}
        titles = {"tecnologia": "TECNOLOGÍA", "colombia": "COLOMBIA", "mundial": "MUNDIAL", "medicina": "MEDICINA"}

        categories_order = ["tecnologia", "medicina", "colombia", "mundial"]
        for cat in categories_order:
            cat_articles = [a for a in selected if a["cat"] == cat]
            if not cat_articles:
                continue

            # Título de categoría
            self.send_text(f"{icons.get(cat,'📰')} *{escape_md2(titles[cat])}*")

            for a in cat_articles:
                # Traducimos título SIEMPRE antes de escapar
                raw_title = a["title"]
                title_es  = self.translate_force_es(raw_title)

                desc_src = a["desc"] if a["desc"] else raw_title
                resumen  = self.summarize_extended(title_es, self.translate_force_es(desc_src), a["cat"])

                caption = (
                    f"*{escape_md2(title_es)}*\n"
                    f"{escape_md2(resumen)}\n"
                    f"{escape_md2_url(a['link'])}"
                )

                img_url = get_image_for_entry(a.get("_entry", {}), a["link"])
                sent_ok = False
                if img_url:
                    sent_ok = self.send_photo(img_url, caption)
                if not sent_ok:
                    self.send_text(caption)

                # Marcar como enviada
                self.sent_ids.add(article_uid(a))

        # Footer
        self.send_text(escape_md2("---") + "\n" + "_Resumen automatizado con IA (prioridad tecnología)_")

        # Persistir estado
        save_state(self.sent_ids)

        # Guardar reporte en repo
        reports = pathlib.Path("reports")
        reports.mkdir(exist_ok=True)
        fname = reports / f"boletin_{datetime.now().strftime('%Y-%m-%d_%H%M')}.md"
        try:
            fname.write_text(f"{header}\n\nReporte enviado por Telegram.\n", encoding="utf-8")
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
    # Prueba visible en logs (traducción)
    prueba = bot.translate_force_es("Breaking: Apple unveils a new AI feature for iPhone.")
    logger.info(f"Traducción de prueba: {prueba}")
    logger.info("Generando y enviando boletín...")
    bot.run()
    logger.info("Listo.")

if __name__ == "__main__":
    main()
