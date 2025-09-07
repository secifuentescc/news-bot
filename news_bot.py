import os
import sys
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

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
    is_weekend = datetime.utcnow().weekday() >= 5
    return {"tecnologia": (7 if not is_weekend else 5), "colombia": 2, "mundial": 2}

STATE_PATH = pathlib.Path("state_sent.json")

# ================== ESCAPE MARKDOWN ==================
def escape_markdown(text: str) -> str:
    """Escape mínimo para Markdown clásico de Telegram (mantiene *negrita*)."""
    if not text:
        return ""
    return (
        text.replace("_", "\\_")
            .replace("[", "\\[")
            .replace("]", "\\]")
            .replace("`", "\\`")
    )

# ================== UTILIDADES ==================
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

# ================== BOT ==================
class NewsBot:
    def __init__(self, only_tech: bool = False):
        self.only_tech = only_tech
        self.processed = set()
        self.sent_ids = load_state()

        # Inicializa Gemini (modelo PRO)
        self.model = None
        if GEMINI_KEY:
            try:
                genai.configure(api_key=GEMINI_KEY)
                self.model = genai.GenerativeModel("gemini-1.5-pro")
                masked = GEMINI_KEY[:4] + "..." + GEMINI_KEY[-4:]
                logger.info(f"Gemini listo (key {masked}).")
            except Exception as e:
                logger.warning(f"No se pudo inicializar Gemini: {e}")
        else:
            logger.warning("GEMINI_API_KEY no definido: no habrá traducción ni resúmenes IA.")

    # ------------ Transporte ------------
    def send_message(self, text: str):
        """Envía mensaje a Telegram con Markdown clásico."""
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

    def send_article(self, title: str, resumen: str, url: str, section_title: str = None, icon: str = ""):
        """Envía una noticia con preview (URL en texto plano)."""
        lines = []
        if section_title:
            lines.append(f"{icon} *{escape_markdown(section_title)}*")
        lines.append(f"• *{escape_markdown(title)}*")
        if resumen:
            lines.append(escape_markdown(resumen))
        lines.append(url)
        text = "\n".join(lines)
        self.send_message(text)

    def send_long(self, text: str):
        for p in chunk(text):
            self.send_message(p)

    # ------------ IA helpers ------------
    def translate_force_es(self, text: str) -> str:
        """Siempre reescribe el texto en español con Gemini."""
        if not text:
            return text
        if not self.model:
            logger.warning("Gemini no inicializado: envío sin traducir.")
            return text
        prompt = (
            "Reescribe este texto ÍNTEGRAMENTE en ESPAÑOL neutro, claro y natural. "
            "Si ya está en español, simplemente reescríbelo mejorado. "
            "No dejes nada en inglés. No agregues comillas ni comentarios.\n\n"
            f"{text}"
        )
        try:
            resp = self.model.generate_content(prompt)
            out = (getattr(resp, "text", "") or "").strip()
            return out or text
        except Exception as e:
            logger.error(f"Error traduciendo: {e}")
            return text

    def summarize_extended(self, title_es: str, description_es: str, category: str) -> str:
        """Resumen 3–5 frases siempre en español."""
        base = (description_es or title_es or "").strip()
        if not self.model:
            return base[:600]
        try:
            prompt = (
                "Redacta un resumen informativo en ESPAÑOL (3 a 5 frases, "
                "máximo ~100 palabras) sobre la noticia. Explica qué pasó, "
                "por qué importa y da contexto. NO dejes nada en inglés. "
                "No agregues comentarios ni comillas.\n\n"
                f"Título: {title_es}\n"
                f"Descripción: {description_es}\n"
                f"Categoría: {category.upper()}\n"
            )
            resp = self.model.generate_content(prompt)
            out = (getattr(resp, "text", "") or "").strip()
            return out or base[:600]
        except Exception as e:
            logger.error(f"Error resumiendo: {e}")
            return base[:600]

    def rank_with_gemini(self, articles):
        """Ranking básico: prioridad a tecnología si falla IA."""
        if not self.model or not articles:
            return sorted(articles, key=lambda a: (a["cat"] != "tecnologia",))
        try:
            packed = "\n".join(
                [f"{i+1}. [{a['cat']}] {a['title']}" for i, a in enumerate(articles)]
            )
            prompt = (
                "Eres editor. Puntúa cada noticia del 1 al 10 según impacto, "
                "novedad y relevancia para lectores hispanohablantes. "
                "Da preferencia a TECNOLOGÍA si el impacto es similar. "
                "Devuelve JSON: [{\"idx\": <n>, \"score\": <0-10>}].\n\n"
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

    # ------------ Pipeline ------------
    def collect_all(self):
        all_articles = []
        cats = ["tecnologia"] if self.only_tech else ["tecnologia", "colombia", "mundial"]
        for cat in cats:
            all_articles.extend(self.get_rss(cat))
        logger.info(f"Recolectadas {len(all_articles)} noticias.")
        return all_articles

    def select_top_by_quota(self, articles):
        """Aplica cupos fijos por categoría (si hay artículos)."""
        if self.only_tech:
            tech = [a for a in articles if a["cat"] == "tecnologia"]
            return self.rank_with_gemini(tech)[:7]

        if not articles:
            return []
        q = quotas_for_today()
        ranked = self.rank_with_gemini(articles)

        by_cat = {"tecnologia": [], "colombia": [], "mundial": []}
        for a in ranked:
            if a["cat"] in by_cat:
                by_cat[a["cat"]].append(a)

        selected = []
        for cat in ["tecnologia", "colombia", "mundial"]:
            selected.extend(by_cat[cat][: q.get(cat, 0)])

        order = {"tecnologia": 0, "colombia": 1, "mundial": 2}
        selected.sort(key=lambda a: order.get(a["cat"], 9))
        return selected

    def run(self):
        icons = {"tecnologia": "💻", "colombia": "🇨🇴", "mundial": "🌍"}
        titles = {"tecnologia": "TECNOLOGÍA", "colombia": "COLOMBIA", "mundial": "MUNDIAL"}

        all_articles = self.collect_all()
        all_articles = [a for a in all_articles if article_uid(a) not in self.sent_ids]
        selected = self.select_top_by_quota(all_articles)

        # Cabecera
        header = f"📰 *{escape_markdown('Boletín de noticias')}* — {escape_markdown(datetime.now().strftime('%d/%m/%Y %H:%M'))}"
        self.send_message(header)

        current_cat = None
        for a in selected:
            section_title = None
            icon = ""
            if a["cat"] != current_cat:
                current_cat = a["cat"]
                section_title = titles[current_cat]
                icon = icons[current_cat]

            title_es = self.translate_force_es(a["title"])
            desc_src = a["desc"] if a["desc"] else a["title"]
            resumen = self.summarize_extended(title_es, self.translate_force_es(desc_src), a["cat"])

            self.send_article(title_es, resumen, a["link"], section_title=section_title, icon=icon)

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
    prueba = bot.translate_force_es("Breaking: Apple unveils a new AI feature for iPhone.")
    logger.info(f"Traducción de prueba: {prueba}")
    bot.run()

if __name__ == "__main__":
    main()
