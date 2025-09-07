# news_bot.py
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

REQUESTS_TIMEOUT = 10  # segundos
STATE_PATH = pathlib.Path("state_sent.json")

# Fallbacks públicos de traducción
LIBRE_ENDPOINTS = [
    (os.getenv("LIBRETRANSLATE_URL", "") or "").strip() or None,  # instancia propia (opcional)
    "https://translate.argosopentech.com/translate",
    "https://libretranslate.com/translate",
    "https://translate.astian.org/translate",
]
MYMEMORY_URL = "https://api.mymemory.translated.net/get"

# Fuentes RSS
NEWS_SOURCES = {
    "mundial": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://rss.cnn.com/rss/edition.rss",
        "https://www.reuters.com/rssFeed/worldNews",
    ],
    "colombia": [
        "https://www.eltiempo.com/rss.xml",
        "https://www.semana.com/rss.xml",
        "https://www.elespectador.com/rss.xml",
        "https://www.portafolio.co/rss/portada.xml",
        "https://www.bluradio.com/rss/colombia.xml",
        "https://caracol.com.co/rss/colombia.xml",
        "https://www.wradio.com.co/rss/colombia.xml",
        "https://www.elcolombiano.com/rss/colombia",
    ],
    "tecnologia": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://arstechnica.com/feed/",
        "https://www.wired.com/feed",
        "https://feeds.feedburner.com/Techmeme",
        "https://spectrum.ieee.org/rss/fulltext",
        "https://www.technologyreview.com/feed/",
        "https://www.engadget.com/rss.xml",
        # Blogs core
        "https://ai.googleblog.com/feeds/posts/default",
        "https://openai.com/blog/rss.xml",
        "https://blogs.nvidia.com/feed/",
    ],
}

def quotas_for_today():
    # Fines de semana: un poco menos de tech
    is_weekend = datetime.utcnow().weekday() >= 5
    return {"tecnologia": (7 if not is_weekend else 5), "colombia": 2, "mundial": 2}

# ================== UTIL ==================
def chunk(text: str, limit: int = 4096):
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
    return hashlib.sha256(base.encode()).hexdigest()[:16]

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

# ================== DETECCIÓN Y TRADUCCIÓN ==================
def detect_lang_simple(text: str) -> str:
    """
    Heurística ligera: devuelve 'es' o 'en'.
    """
    if not text:
        return "en"
    t = text.lower()

    es_hits = 0
    es_words = [" el ", " la ", " los ", " las ", " de ", " del ", " una ", " un ", " y ", " que ", " como ", " en ", " por ", " más ", " aún ", " también ", " según "]
    for w in es_words:
        if w in " " + t + " ":
            es_hits += 1
    if re.search(r"[áéíóúñü]", t):
        es_hits += 2

    en_hits = 0
    en_words = [" the ", " and ", " for ", " with ", " on ", " at ", " from ", " by ", " about ", " into ", " over ", " after ", " before ", " between ", " as "]
    for w in en_words:
        if w in " " + t + " ":
            en_hits += 1

    return "es" if es_hits >= en_hits else "en"

def translate_via_libre(text: str) -> str:
    """
    Intenta traducir con varias instancias de LibreTranslate.
    """
    if not text:
        return text
    for url in LIBRE_ENDPOINTS:
        if not url:
            continue
        try:
            r = requests.post(
                url,
                data={"q": text, "source": "auto", "target": "es", "format": "text"},
                timeout=REQUESTS_TIMEOUT,
            )
            if r.ok:
                out = (r.json().get("translatedText") or "").strip()
                if out:
                    logger.info(f"Traducción vía LibreTranslate OK: {url}")
                    return out
            else:
                logger.debug(f"LibreTranslate {url} status {r.status_code}: {r.text[:120]}")
        except Exception as e:
            logger.debug(f"LibreTranslate falló {url}: {e}")
    return ""

def translate_via_mymemory(text: str) -> str:
    """
    Traduce con MyMemory usando langpair correcto (no 'auto|es').
    Filtra mensajes de error devolviendo "" para forzar otros fallbacks.
    """
    if not text:
        return text
    try:
        src = detect_lang_simple(text)  # 'en' o 'es'
        if src == "es":
            return text  # ya en español
        r = requests.get(
            MYMEMORY_URL,
            params={"q": text, "langpair": f"{src}|es"},
            timeout=REQUESTS_TIMEOUT,
        )
        if r.ok:
            data = r.json()
            out = (data.get("responseData", {}).get("translatedText") or "").strip()
            # MyMemory a veces devuelve mensajes de error como "AUTO IS AN INVALID SOURCE LANGUAGE..."
            if not out or "INVALID SOURCE LANGUAGE" in out.upper():
                return ""
            logger.info("Traducción vía MyMemory OK.")
            return out
    except Exception as e:
        logger.debug(f"MyMemory falló: {e}")
    return ""

# ================== BOT ==================
class NewsBot:
    def __init__(self, only_tech: bool = False):
        self.only_tech = only_tech
        self.processed = set()
        self.sent_ids = load_state()

        self.model = None
        self.gemini_disabled = False
        if GEMINI_KEY:
            try:
                genai.configure(api_key=GEMINI_KEY)
                # Intenta el más reciente; si falla, retrocede
                try:
                    self.model = genai.GenerativeModel("gemini-1.5-pro-latest")
                except Exception:
                    self.model = genai.GenerativeModel("gemini-1.5-pro")
                masked = GEMINI_KEY[:4] + "..." + GEMINI_KEY[-4:]
                logger.info(f"Gemini listo (key {masked}).")
            except Exception as e:
                logger.warning(f"No se pudo inicializar Gemini: {e}")
                self.gemini_disabled = True
        else:
            logger.warning("GEMINI_API_KEY no definido.")
            self.gemini_disabled = True

    # ---- Gemini helpers ----
    def _handle_gemini_error(self, e: Exception):
        msg = str(e)
        if "429" in msg or "quota" in msg.lower():
            logger.warning("Cuota Gemini excedida; desactivo Gemini para este run.")
            self.gemini_disabled = True

    @staticmethod
    def _extract_text(resp) -> str:
        try:
            if hasattr(resp, "text") and resp.text:
                return resp.text
            if hasattr(resp, "candidates") and resp.candidates:
                cand = resp.candidates[0]
                if hasattr(cand, "content") and hasattr(cand.content, "parts"):
                    texts = []
                    for p in cand.content.parts:
                        t = getattr(p, "text", None) or (p.get("text") if isinstance(p, dict) else None)
                        if t:
                            texts.append(t)
                    if texts:
                        return "".join(texts)
        except Exception:
            pass
        return ""

    def gemini_generate(self, prompt: str) -> str:
        if self.gemini_disabled or not self.model:
            return ""
        try:
            resp = self.model.generate_content(prompt)
            return (self._extract_text(resp) or "").strip()
        except Exception as e:
            logger.warning(f"Gemini generate_content falló: {e}")
            self._handle_gemini_error(e)
            return ""

    # ---- Transporte ----
    def send_message(self, text: str):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",  # simple y compatible
            "disable_web_page_preview": False,  # deja previews e imagen cuando posible
        }
        try:
            r = requests.post(url, data=payload, timeout=20)
            if not r.ok:
                logger.error(f"Telegram error: {r.text}")
        except Exception as e:
            logger.error(f"Error enviando a Telegram: {e}")

    def send_article(self, title: str, resumen: str, url: str, section_title: str = None, icon: str = ""):
        parts = []
        if section_title:
            parts.append(f"{icon} *{section_title}*")
        parts.append(f"• *{title}*")
        if resumen:
            parts.append(resumen)
        # URL en texto plano para que Telegram intente previsualizar
        parts.append(url)
        self.send_message("\n".join(parts))

    # ---- Traducción + Resumen ----
    def translate_force_es(self, text: str) -> str:
        if not text:
            return text

        # 1) Gemini si disponible
        if not self.gemini_disabled and self.model:
            prompt = (
                "Reescribe ÍNTEGRAMENTE en ESPAÑOL neutro, claro y natural. "
                "Si ya está en español, mejóralo ligeramente. "
                "No dejes nada en inglés. No agregues comillas ni comentarios.\n\n"
                f"{text}"
            )
            out = self.gemini_generate(prompt)
            if out:
                return out
            logger.info("Gemini sin salida; usaré fallbacks de traducción.")

        # 2) LibreTranslate (multi-endpoint)
        out = translate_via_libre(text)
        if out:
            return out

        # 3) MyMemory con langpair correcto
        out = translate_via_mymemory(text)
        if out:
            return out

        # 4) Último recurso: original
        return text

    def summarize_extended(self, title_es: str, description_es: str, category: str) -> str:
        base = (description_es or title_es or "").strip()
        # 1) Gemini para resumen si disponible
        if not self.gemini_disabled and self.model:
            prompt = (
                "Redacta un resumen informativo en ESPAÑOL (3 a 5 frases, ~100 palabras). "
                "Explica qué pasó, por qué importa y da contexto. "
                "NO dejes nada en inglés. No agregues comentarios ni comillas.\n\n"
                f"Título: {title_es}\n"
                f"Descripción: {description_es}\n"
                f"Categoría: {category.upper()}\n"
            )
            out = self.gemini_generate(prompt)
            if out:
                return out
            logger.info("Gemini sin salida en resumen; usaré fallback.")
        # 2) Fallback: traducir/cortar
        tr = self.translate_force_es(base)
        return tr[:600]

    # ---- Datos ----
    def get_rss(self, category: str):
        arts = []
        for rss in NEWS_SOURCES.get(category, []):
            try:
                feed = feedparser.parse(rss)
                for entry in feed.entries[:8]:
                    raw_desc = entry.get("description", "") or entry.get("summary", "")
                    desc = BeautifulSoup(raw_desc, "html.parser").get_text(" ").strip()
                    if not entry.link:
                        continue
                    a = {
                        "_i": len(self.processed),
                        "title": entry.title or "",
                        "desc": desc or "",
                        "link": entry.link,
                        "cat": category
                    }
                    uid = article_uid(a)
                    if uid in self.processed or uid in self.sent_ids:
                        continue
                    arts.append(a)
                    self.processed.add(uid)
            except Exception as e:
                logger.error(f"Error con RSS {rss}: {e}")
        return arts

    # ---- Selección ----
    def collect_all(self):
        cats = ["tecnologia"] if self.only_tech else ["tecnologia", "colombia", "mundial"]
        all_articles = []
        for cat in cats:
            all_articles.extend(self.get_rss(cat))
        logger.info(f"Recolectadas {len(all_articles)} noticias.")
        return all_articles

    def select_top_by_quota(self, articles):
        if self.only_tech:
            tech = [a for a in articles if a["cat"] == "tecnologia"]
            return tech[:7]
        q = quotas_for_today()
        by_cat = {"tecnologia": [], "colombia": [], "mundial": []}
        for a in articles:
            if a["cat"] in by_cat:
                by_cat[a["cat"]].append(a)
        selected = []
        for cat in ["tecnologia", "colombia", "mundial"]:
            selected.extend(by_cat[cat][: q.get(cat, 0)])
        order = {"tecnologia": 0, "colombia": 1, "mundial": 2}
        selected.sort(key=lambda a: order.get(a["cat"], 9))
        return selected

    # ---- Run ----
    def run(self):
        icons = {"tecnologia": "💻", "colombia": "🇨🇴", "mundial": "🌍"}
        titles = {"tecnologia": "TECNOLOGÍA", "colombia": "COLOMBIA", "mundial": "MUNDIAL"}

        all_articles = self.collect_all()
        all_articles = [a for a in all_articles if article_uid(a) not in self.sent_ids]
        selected = self.select_top_by_quota(all_articles)

        header = f"📰 *Boletín de noticias* — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        self.send_message(header)

        current_cat = None
        for a in selected:
            section_title = None
            icon = ""
            if a["cat"] != current_cat:
                current_cat = a["cat"]
                section_title = titles[current_cat]
                icon = icons[current_cat]

            # Traducción y resumen
            title_es = self.translate_force_es(a["title"])
            desc_src = a["desc"] if a["desc"] else a["title"]
            resumen = self.summarize_extended(title_es, self.translate_force_es(desc_src), a["cat"])

            # Enviar artículo
            self.send_article(title_es, resumen, a["link"], section_title=section_title, icon=icon)

            # Registrar como enviado
            self.sent_ids.add(article_uid(a))
        save_state(self.sent_ids)

# ================== MAIN ==================
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--only-tech", action="store_true", help="Solo noticias de tecnología")
    return p.parse_args()

def main():
    args = parse_args()
    bot = NewsBot(only_tech=args.only_tech)

    # Prueba rápida visible en logs (debe salir en español si todo ok)
    prueba = bot.translate_force_es("Breaking: Apple unveils a new AI feature for iPhone.")
    logger.info(f"Traducción de prueba: {prueba}")

    bot.run()

if __name__ == "__main__":
    main()
