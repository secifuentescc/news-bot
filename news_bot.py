import os
import logging
import requests
import feedparser
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import google.generativeai as genai

# ================== CONFIG ==================
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
NEWSAPI_KEY = (os.getenv("NEWSAPI_KEY") or "").strip()  # opcional

# Fuentes RSS (más cobertura tech)
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
    ],
}

# Cupos por categoría (prioridad tecnología)
QUOTAS = {"tecnologia": 5, "colombia": 2, "mundial": 2}

# ================== UTILIDADES ==================
def escape_markdown(s: str) -> str:
    if not s:
        return s
    return (
        s.replace("_", "\\_")
         .replace("*", "\\*")
         .replace("[", "\\[")
         .replace("`", "\\`")
    )

# ================== BOT ==================
class NewsBot:
    def __init__(self):
        self.processed = set()
        self.model = None
        if GEMINI_KEY:
            try:
                genai.configure(api_key=GEMINI_KEY)
                # Rápido y actual
                self.model = genai.GenerativeModel("gemini-1.5-flash")
                masked = GEMINI_KEY[:4] + "..." + GEMINI_KEY[-4:]
                logger.info(f"Gemini listo (key {masked}).")
            except Exception as e:
                logger.warning(f"No se pudo inicializar Gemini: {e}")
        else:
            logger.warning("GEMINI_API_KEY no definido: no habrá traducciones ni resúmenes IA.")

    # ------------ Transporte ------------
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
            r = requests.post(url, data=payload, timeout=25)
            if not r.ok:
                logger.error(f"Telegram error: {r.text}")
        except Exception as e:
            logger.error(f"Error enviando a Telegram: {e}")

    # ------------ IA helpers ------------
    def translate_force_es(self, text: str) -> str:
        """Fuerza traducción al español (sin detector)."""
        if not text:
            return text
        if not self.model:
            return text
        try:
            prompt = (
                "Traduce al español de forma natural y clara. "
                "No agregues comentarios ni comillas. Solo el texto traducido.\n\n"
                f"{text}"
            )
            resp = self.model.generate_content(prompt)
            out = (getattr(resp, "text", "") or "").strip()
            return out or text
        except Exception as e:
            logger.error(f"Error traduciendo: {e}")
            return text

    def summarize_extended(self, title: str, description: str, category: str) -> str:
        """Resumen 3–5 frases con contexto. Fallback: descripción limpia."""
        if not self.model:
            # Fallback sin IA
            base = description if description else title
            return (base or "")[:500]
        try:
            prompt = (
                "Redacta un resumen informativo en español (3 a 5 frases, "
                "máximo ~90 palabras) sobre la noticia. Explica qué pasó, "
                "por qué importa y da contexto breve. No incluyas opiniones.\n\n"
                f"Título: {title}\n"
                f"Descripción/Extracto: {description}\n"
                f"Categoría: {category.upper()}\n"
            )
            resp = self.model.generate_content(prompt)
            out = (getattr(resp, "text", "") or "").strip()
            if not out:
                return (description or title)[:500]
            return out
        except Exception as e:
            logger.error(f"Error resumiendo: {e}")
            return (description or title)[:500]

    def rank_with_gemini(self, articles):
        """Pide a Gemini que puntúe importancia (0-10) y devuelve ordenado."""
        if not self.model or not articles:
            return articles
        try:
            packed = "\n".join(
                [
                    f"{i+1}. [{a['cat']}]: {a['title']}\n{(a['desc'] or '')[:280]}"
                    for i, a in enumerate(articles)
                ]
            )
            prompt = (
                "Eres editor senior. Puntúa cada ítem del 1 al 10 según "
                "impacto, novedad y relevancia para lectores hispanohablantes "
                "(da preferencia a TECNOLOGÍA si el impacto es similar). "
                "Responde en JSON: [{idx: <n>, score: <0-10>}].\n\n"
                f"NOTICIAS:\n{packed}"
            )
            resp = self.model.generate_content(prompt)
            txt = (getattr(resp, "text", "") or "").strip()
            import json
            scores = json.loads(txt)
            # map idx->score
            score_map = {int(item["idx"]) - 1: float(item["score"]) for item in scores if "idx" in item and "score" in item}
            # agrega pequeño boost a tecnología
            ordered = sorted(
                articles,
                key=lambda a: score_map.get(a["_i"], 0.0) + (1.0 if a["cat"] == "tecnologia" else 0.0),
                reverse=True,
            )
            return ordered
        except Exception as e:
            logger.warning(f"No se pudo rankear con IA: {e}")
            # Heurística: tecnología primero, luego resto por orden
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
                    uid = f"{entry.title}|{entry.link}"
                    if uid in self.processed:
                        continue
                    arts.append({
                        "_i": len(self.processed),  # índice interno para ranking IA
                        "title": entry.title or "",
                        "desc": desc or "",
                        "link": entry.link,
                        "cat": category
                    })
                    self.processed.add(uid)
            except Exception as e:
                logger.error(f"Error con RSS {rss}: {e}")
        return arts

    # ------------ Pipeline ------------
    def collect_all(self):
        all_articles = []
        for cat in ["tecnologia", "colombia", "mundial"]:  # recolecta tech primero
            all_articles.extend(self.get_rss(cat))
        logger.info(f"Recolectadas {len(all_articles)} noticias.")
        return all_articles

    def select_top_by_quota(self, articles):
        """Ordena por importancia y aplica cupos por categoría."""
        if not articles:
            return []
        # Ranking global con IA (o heurística)
        ranked = self.rank_with_gemini(articles)
        # Filtra por cupos
        counts = {k: 0 for k in QUOTAS}
        selected = []
        for a in ranked:
            if counts.get(a["cat"], 0) < QUOTAS.get(a["cat"], 0):
                selected.append(a)
                counts[a["cat"]] += 1
            # stop si ya llenamos todos los cupos
            if sum(counts.values()) >= sum(QUOTAS.values()):
                break
        logger.info(f"Seleccionadas: { {k: v for k, v in counts.items()} }")
        # Reordena para salida: Tecnología → Colombia → Mundial
        order = {"tecnologia": 0, "colombia": 1, "mundial": 2}
        selected.sort(key=lambda a: order.get(a["cat"], 9))
        return selected

    def create_digest(self, selected):
        if not selected:
            return "No hay noticias nuevas."

        icons = {"tecnologia": "💻", "colombia": "🇨🇴", "mundial": "🌍"}
        titles = {"tecnologia": "TECNOLOGÍA", "colombia": "COLOMBIA", "mundial": "MUNDIAL"}

        text = f"📰 *Boletín de noticias* — {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        current_cat = None
        for a in selected:
            if a["cat"] != current_cat:
                current_cat = a["cat"]
                text += f"{icons[current_cat]} *{titles[current_cat]}*\n"

            # Traducción forzada de título y descripción
            title_es = self.translate_force_es(a["title"])
            desc_src = a["desc"] if a["desc"] else a["title"]
            resumen = self.summarize_extended(title_es, self.translate_force_es(desc_src), a["cat"])

            title_es = escape_markdown(title_es)
            resumen = escape_markdown(resumen)

            text += f"• *{title_es}*\n{resumen}\n[Leer más]({a['link']})\n\n"

        text += "---\n🤖 _Resumen automatizado con IA (prioridad tecnología)_"
        return text

    def run(self):
        all_articles = self.collect_all()
        selected = self.select_top_by_quota(all_articles)
        digest = self.create_digest(selected)
        self.send_message(digest)

# ================== MAIN ==================
def main():
    bot = NewsBot()
    # Prueba visible en logs (debe salir traducido si la key está bien)
    prueba = bot.translate_force_es("Breaking: Apple unveils a new AI feature for iPhone.")
    logger.info(f"Traducción de prueba: {prueba}")
    logger.info("Generando y enviando boletín...")
    bot.run()
    logger.info("Listo.")

if __name__ == "__main__":
    main()
