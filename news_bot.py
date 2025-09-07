import os
import logging
import requests
import feedparser
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import google.generativeai as genai

# ============== CONFIG ==============
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

NEWS_SOURCES = {
    "mundial": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://rss.cnn.com/rss/edition.rss",
    ],
    "colombia": [
        "https://www.eltiempo.com/rss.xml",
        "https://www.semana.com/rss.xml",
        "https://www.elespectador.com/rss.xml",
    ],
    "tecnologia": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://arstechnica.com/feed/",
        "https://www.wired.com/feed",
    ],
}

# ============== UTILIDADES ==============
def is_spanish(text: str) -> bool:
    if not text:
        return False
    tl = f" {text.lower()} "
    hits = 0
    for w in (" el ", " la ", " los ", " las ", " de ", " y ", " que ", " en ", " para ", " con ", " por "):
        if w in tl:
            hits += 1
    if any(c in text for c in "áéíóúñÁÉÍÓÚÑ"):
        hits += 2
    return hits >= 2

def escape_markdown(s: str) -> str:
    if not s:
        return s
    return (
        s.replace("_", "\\_")
         .replace("*", "\\*")
         .replace("[", "\\[")
         .replace("`", "\\`")
    )

# ============== BOT ==============
class NewsBot:
    def __init__(self):
        self.processed = set()
        self.model = None

        # Log: confirmar que llegó la key (enmascarada)
        if GEMINI_KEY:
            masked = GEMINI_KEY[:4] + "..." + GEMINI_KEY[-4:]
            logger.info(f"GEMINI_API_KEY detectada: {masked}")
            try:
                genai.configure(api_key=GEMINI_KEY)
                # Modelo recomendado actual
                self.model = genai.GenerativeModel("gemini-1.5-flash")
                logger.info("Gemini inicializado correctamente (gemini-1.5-flash).")
            except Exception as e:
                logger.warning(f"No se pudo inicializar Gemini: {e}")
        else:
            logger.warning("GEMINI_API_KEY no está definida. No habrá traducción.")

    def send_message(self, text: str):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        }
        try:
            r = requests.post(url, data=payload, timeout=20)
            if not r.ok:
                logger.error(f"Telegram error: {r.text}")
        except Exception as e:
            logger.error(f"Error enviando a Telegram: {e}")

    def translate(self, text: str) -> str:
        if not text:
            return text
        if is_spanish(text):
            logger.debug("Texto detectado como español; no se traduce.")
            return text
        if not self.model:
            logger.warning("translate(): sin modelo Gemini, devolviendo original.")
            return f"[NO_GEMINI] {text}"
        try:
            prompt = (
                "Traduce al español de forma natural, breve y clara. "
                "No agregues comentarios ni comillas, solo el texto traducido.\n\n"
                f"{text}"
            )
            response = self.model.generate_content(prompt)
            out = (getattr(response, "text", "") or "").strip()
            if not out:
                logger.warning("Gemini devolvió respuesta vacía; regreso original.")
                return text
            logger.debug("Texto traducido correctamente.")
            return out
        except Exception as e:
            logger.error(f"Error traduciendo con Gemini: {e}")
            return text

    def get_rss(self, category: str):
        arts = []
        for rss in NEWS_SOURCES.get(category, []):
            try:
                feed = feedparser.parse(rss)
                for entry in feed.entries[:5]:
                    raw_desc = entry.get("description", "") or entry.get("summary", "")
                    desc = BeautifulSoup(raw_desc, "html.parser").get_text(" ").strip()
                    uid = f"{entry.title}|{entry.link}"
                    if uid not in self.processed:
                        arts.append({
                            "title": entry.title,
                            "desc": desc,
                            "link": entry.link,
                            "cat": category
                        })
                        self.processed.add(uid)
            except Exception as e:
                logger.error(f"Error con RSS {rss}: {e}")
        return arts

    def create_summary(self, articles):
        if not articles:
            return "No hay noticias nuevas."
        text = f"📰 *Resumen de noticias* - {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        for a in articles:
            title_es = escape_markdown(self.translate(a["title"]))
            desc_es = escape_markdown(self.translate(a["desc"])[:200])
            text += f"• *{title_es}*\n{desc_es}...\n[Leer más]({a['link']})\n\n"
        return text

    def run(self):
        all_articles = []
        for c in ["colombia", "mundial", "tecnologia"]:
            all_articles.extend(self.get_rss(c))
        summary = self.create_summary(all_articles[:9])
        self.send_message(summary)

# ============== MAIN ==============
def main():
    bot = NewsBot()
    # Smoke test: verifica en logs si se traduce
    prueba_src = "Breaking: Apple unveils a new AI feature for iPhone."
    prueba_out = bot.translate(prueba_src)
    logger.info(f"Traducción de prueba: {prueba_out}")
    logger.info("Ejecutando envío único...")
    bot.run()
    logger.info("Bot finalizado.")

if __name__ == "__main__":
    main()
