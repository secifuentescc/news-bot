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
NEWSAPI_KEY = (os.getenv("NEWSAPI_KEY") or "").strip()  # opcional

# Fuentes RSS (fuerte en tecnología y ampliado Colombia)
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
    # Fin de semana => un poco menos de tech
    is_weekend = datetime.utcnow().weekday() >= 5
    return {"tecnologia": (7 if not is_weekend else 5), "colombia": 2, "mundial": 2}

STATE_PATH = pathlib.Path("state_sent.json")

# ================== ESCAPE MARKDOWN (CLÁSICO) ==================
def escape_markdown(text: str) -> str:
    """
    Escape mínimo para Markdown clásico de Telegram.
    NO toca los asteriscos (*) para que *negrita* funcione.
    """
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

# ======= Heurística de idioma + refuerzo de español (Gemini) =======
def is_probably_english(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    common = [" the ", " and ", " for ", " with ", " from ", " in ", " on ", " at ",
              " to ", " of ", " by ", " is ", " are ", " was ", " were ", " as ",
              " it ", " this ", " that "]
    t_spaced = f" {t} "
    hits = sum(1 for w in common if w in t_spaced)
    return hits >= 2

def ensure_spanish(text: str, model) -> str:
    """
    Intenta 2 veces con Gemini: si detectamos inglés, reescribe forzando español.
    """
    if not text or not model:
        return text
    prompt = (
        "Traduce al ESPAÑOL neutro, claro y natural. "
        "No agregues comentarios, notas ni comillas. SOLO el texto final en español.\n\n"
        f"{text}"
    )
    try:
        r = model.generate_content(prompt)
        out = (getattr(r, "text", "") or "").strip()
        if out and not is_probably_english(out):
            return out
        # Reintento más estricto
        prompt2 = (
            "Reescribe ÍNTEGRAMENTE en ESPAÑOL neutro y natural. "
            "Prohibido dejar texto en inglés. No agregues comentarios.\n\n"
            f"{text}"
        )
        r2 = model.generate_content(prompt2)
        out2 = (getattr(r2, "text", "") or "").strip()
        return out2 or text
    except Exception:
        return text

# ================== BOT ==================
class NewsBot:
    def __init__(self, only_tech: bool = False):
        self.only_tech = only_tech
        self.processed = set()
        self.sent_ids = load_state()

        # Inicializa Gemini (modelo PRO + config estable)
        self.model = None
        if GEMINI_KEY:
            try:
                genai.configure(api_key=GEMINI_KEY)
                gen_cfg = {
                    "temperature": 0.2,
                    "top_p": 0.9,
                    "top_k": 40,
                }
                self.model = genai.GenerativeModel(
                    model_name="gemini-1.5-pro",
                    generation_config=gen_cfg
                )
                masked = GEMINI_KEY[:4] + "..." + GEMINI_KEY[-4:]
                logger.info(f"Gemini listo (key {masked}).")
            except Exception as e:
                logger.warning(f"No se pudo inicializar Gemini: {e}")
        else:
            logger.warning("GEMINI_API_KEY no definido: no habrá traducción ni resúmenes IA.")

    # ------------ Transporte ------------
    def send_message(self, text: str):
        """Envía un mensaje genérico (Markdown clásico)."""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",          # Markdown clásico
            "disable_web_page_preview": False, # Permite previews
        }
        try:
            r = requests.post(url, data=payload, timeout=25)
            if not r.ok:
                logger.error(f"Telegram error: {r.text}")
        except Exception as e:
            logger.error(f"Error enviando a Telegram: {e}")

    def send_article(self, title: str, resumen: str, url: str, section_title: str = None, icon: str = ""):
        """
        Envía UNA noticia por mensaje, con URL en texto plano para forzar preview con imagen.
        """
        lines = []
        if section_title:
            lines.append(f"{icon} *{escape_markdown(section_title)}*")
        lines.append(f"• *{escape_markdown(title)}*")
        if resumen:
            lines.append(escape_markdown(resumen))
        # URL en texto plano → Telegram genera la tarjeta con imagen
        lines.append(url)

        text = "\n".join(lines)
        self.send_message(text)

    def send_long(self, text: str):
        for p in chunk(text):
            self.send_message(p)

    # ------------ IA helpers ------------
    def translate_force_es(self, text: str) -> str:
        """Traduce al español con verificación y reintento."""
        if not text:
            return text
        if not self.model:
            logger.warning("Gemini no inicializado: envío sin traducir.")
            return text
        out = ensure_spanish(text, self.model)
        return out or text

    def summarize_extended(self, title_es: str, description_es: str, category: str) -> str:
        """Resumen 3–5 frases en español. Si sale en inglés, se reintenta forzando español."""
        base = (description_es or title_es or "").strip()
        if not self.model:
            return base[:600]
        try:
            prompt = (
                "Redacta un resumen informativo en ESPAÑOL (3 a 5 frases, "
                "máximo ~100 palabras) sobre la noticia. Explica qué pasó, "
                "por qué importa y da contexto. No incluyas opiniones.\n\n"
                f"Título (ES): {title_es}\n"
                f"Descripción/Extracto (ES): {description_es}\n"
                f"Categoría: {category.upper()}\n"
            )
            resp = self.model.generate_content(prompt)
            out = (getattr(resp, "text", "") or "").strip()
            if not out:
                return base[:600]
            if is_probably_english(out):
                out = ensure_spanish(out, self.model)
            return out or base[:600]
        except Exception as e:
            logger.error(f"Error resumiendo: {e}")
            return base[:600]

    def rank_with_gemini(self, articles):
        """Puntúa importancia (0-10) priorizando tecnología; fallback heurístico."""
        if not self.model or not articles:
            # Heurística: tecnología primero
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
        logger.info(f"Recolectadas {len(all_articles)} noticias (only_tech={self.only_tech}).")
        return all_articles

    def select_top_by_quota(self, articles):
        """
        Ordena por importancia y GARANTIZA cupos por categoría (si hay artículos).
        """
        if self.only_tech:
            tech = [a for a in articles if a["cat"] == "tecnologia"]
            ranked = self.rank_with_gemini(tech)
            return ranked[:7]

        if not articles:
            return []
        q = quotas_for_today()
        ranked = self.rank_with_gemini(articles)

        # Agrupa por categoría preservando el orden del ranking
        by_cat = {"tecnologia": [], "colombia": [], "mundial": []}
        for a in ranked:
            if a["cat"] in by_cat:
                by_cat[a["cat"]].append(a)

        # Toma hasta el cupo disponible por categoría (si hay)
        selected = []
        for cat in ["tecnologia", "colombia", "mundial"]:
            selected.extend(by_cat[cat][: q.get(cat, 0)])

        # Orden final para presentación
        order = {"tecnologia": 0, "colombia": 1, "mundial": 2}
        selected.sort(key=lambda a: order.get(a["cat"], 9))
        logger.info({
            "disponibles": {k: len(v) for k, v in by_cat.items()},
            "tomadas": {k: len([x for x in selected if x['cat']==k]) for k in by_cat}
        })
        return selected

    def run(self):
        start = datetime.now()
        icons = {"tecnologia": "💻", "colombia": "🇨🇴", "mundial": "🌍"}
        titles = {"tecnologia": "TECNOLOGÍA", "colombia": "COLOMBIA", "mundial": "MUNDIAL"}

        all_articles = self.collect_all()
        # Filtra artículos ya enviados
        all_articles = [a for a in all_articles if article_uid(a) not in self.sent_ids]
        selected = self.select_top_by_quota(all_articles)

        # Cabecera general (un solo mensaje)
        header = f"📰 *{escape_markdown('Boletín de noticias')}* — {escape_markdown(datetime.now().strftime('%d/%m/%Y %H:%M'))}"
        self.send_message(header)

        # Envía cada noticia por separado para que Telegram muestre la preview con imagen
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

        # Persistir IDs enviados
        for a in selected:
            self.sent_ids.add(article_uid(a))
        save_state(self.sent_ids)

        elapsed = (datetime.now() - start).seconds
        logger.info(f"Boletín enviado. {len(selected)} noticias. Tiempo total: {elapsed}s.")

# ================== MAIN ==================
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--only-tech", action="store_true", help="Enviar solo tecnología (ignora Colombia y Mundial)")
    return p.parse_args()

def main():
    args = parse_args()
    bot = NewsBot(only_tech=args.only_tech)
    # Prueba visible en logs (debe salir traducido si la key está bien)
    prueba = bot.translate_force_es("Breaking: Apple unveils a new AI feature for iPhone.")
    logger.info(f"Traducción de prueba: {prueba}")
    logger.info("Generando y enviando boletín...")
    bot.run()
    logger.info("Listo.")

if __name__ == "__main__":
    main()
