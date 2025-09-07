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

def quotas_for_today():
    # Cupos por categoría (puedes ajustar)
    # Garantizamos al menos 1 de cada categoría más abajo.
    return {"tecnologia": 6, "medicina": 3, "colombia": 2, "mundial": 2}

# ================== MARKDOWNV2 ESCAPES ==================
def escape_md2(text: str) -> str:
    """Escapa caracteres especiales para MarkdownV2 (solo contenido, no asteriscos envolventes)."""
    if text is None:
        return ""
    # Telegram MDV2 especiales: _ * [ ] ( ) ~ ` > # + - = | { } . !
    return re.sub(r'([_*$begin:math:display$$end:math:display$()~`>#+\-=|{}.!\\])', r'\\\1', text)

def escape_md2_url(url: str) -> str:
    """Escapa URL para usarse en MDV2 (sin []() para evitar roturas)."""
    if not url:
        return ""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', url)

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
                # Modelo rápido y barato
                self.model = genai.GenerativeModel("gemini-1.5-flash")
                masked = GEMINI_KEY[:4] + "..." + GEMINI_KEY[-4:]
                logger.info(f"Gemini listo (key {masked}).")
            except Exception as e:
                logger.warning(f"No se pudo inicializar Gemini: {e}")
        else:
            logger.warning("GEMINI_API_KEY no definido: no habrá traducción/resumen IA.")

    # ------------ Transporte ------------
    def send_message(self, text: str):
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
            logger.error(f"Error enviando a Telegram: {e}")

    def send_long(self, text: str):
        for p in chunk(text):
            self.send_message(p)

    # ------------ IA helpers ------------
    def translate_force_es(self, text: str) -> str:
        """Fuerza traducción al español. Si Gemini falla, devuelve original."""
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
            logger.warning(f"Gemini traducción falló: {e}")
            return text

    def summarize_extended(self, title_es: str, description_es: str, category: str) -> str:
        """Resumen 3–5 frases con contexto. Fallback: descripción limpia."""
        base = (description_es or title_es or "").strip()
        if not self.model:
            return base[:600]
        try:
            prompt = (
                "Redacta un resumen informativo en español (3 a 5 frases, "
                "máximo ~100 palabras) sobre la noticia. Explica qué pasó, "
                "por qué importa y da contexto. Sin opiniones ni emojis.\n\n"
                f"Título: {title_es}\n"
                f"Descripción/Extracto: {description_es}\n"
                f"Categoría: {category.upper()}\n"
            )
            resp = self.model.generate_content(prompt)
            out = (getattr(resp, "text", "") or "").strip()
            return out or base[:600]
        except Exception as e:
            logger.warning(f"Gemini resumen falló: {e}")
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
        if self.only_tech:
            cats = ["tecnologia"]
        elif self.only_medicine:
            cats = ["medicina"]
        else:
            cats = ["tecnologia", "medicina", "colombia", "mundial"]

        for cat in cats:
            all_articles.extend(self.get_rss(cat))
        logger.info(f"Recolectadas {len(all_articles)} noticias (cats={cats}).")
        return all_articles

    def select_top_by_quota(self, articles):
        """Ordena por importancia y aplica cupos + mínimo 1 de cada categoría disponible."""
        if not articles:
            return []

        # Ranking
        ranked = self.rank_with_gemini(articles)

        # Cupos base
        q = quotas_for_today()
        counts = {k: 0 for k in q}
        selected = []

        for a in ranked:
            if counts.get(a["cat"], 0) < q.get(a["cat"], 0):
                selected.append(a)
                counts[a["cat"]] += 1
            if sum(counts.values()) >= sum(q.values()):
                break

        # Garantías mínimas por categoría (si existen candidatos no seleccionados)
        minimums = {"tecnologia": 1, "medicina": 1, "colombia": 1, "mundial": 1}
        by_cat = {"tecnologia": [], "medicina": [], "colombia": [], "mundial": []}
        for a in ranked:
            by_cat[a["cat"]].append(a)

        sel_ids = {article_uid(x) for x in selected}
        for cat, min_needed in minimums.items():
            have = sum(1 for x in selected if x["cat"] == cat)
            if have >= min_needed:
                continue
            # Intenta sumar 1 del cat si existe alguno disponible
            for cand in by_cat.get(cat, []):
                uid = article_uid(cand)
                if uid not in sel_ids:
                    selected.append(cand)
                    sel_ids.add(uid)
                    break  # solo uno para cumplir mínimo

        # Orden final para presentación
        order = {"tecnologia": 0, "medicina": 1, "colombia": 2, "mundial": 3}
        selected.sort(key=lambda a: order.get(a["cat"], 9))
        logger.info(
            "Seleccionadas (garantizando mínimos): "
            f"tech={sum(1 for x in selected if x['cat']=='tecnologia')}, "
            f"med={sum(1 for x in selected if x['cat']=='medicina')}, "
            f"col={sum(1 for x in selected if x['cat']=='colombia')}, "
            f"mun={sum(1 for x in selected if x['cat']=='mundial')}"
        )
        return selected

    def create_digest_textblock(self, selected):
        """Construye el boletín en MarkdownV2 (texto puro)."""
        if not selected:
            return "*No hay noticias nuevas*"

        icons  = {"tecnologia": "💻", "colombia": "🇨🇴", "mundial": "🌍", "medicina": "🩺"}
        titles = {"tecnologia": "TECNOLOGÍA", "colombia": "COLOMBIA", "mundial": "MUNDIAL", "medicina": "MEDICINA"}

        header = f"📰 *{escape_md2('Boletín de noticias')}* — {escape_md2(datetime.now().strftime('%d/%m/%Y %H:%M'))}\n\n"
        text = header

        current_cat = None
        for a in selected:
            if a["cat"] != current_cat:
                current_cat = a["cat"]
                text += f"{icons.get(current_cat,'📰')} *{escape_md2(titles[current_cat])}*\n"

            # Traducción + resumen
            title_es = self.translate_force_es(a["title"])
            desc_src = a["desc"] if a["desc"] else a["title"]
            resumen = self.summarize_extended(title_es, self.translate_force_es(desc_src), a["cat"])

            # Armar bloque (negritas ok + URL escapada)
            text += (
                f"• *{escape_md2(title_es)}*\n"
                f"{escape_md2(resumen)}\n"
                f"{escape_md2_url(a['link'])}\n\n"
            )

        text += escape_md2("---") + "\n" + "_Resumen automatizado con IA (prioridad tecnología)_"
        return text

    def run(self):
        all_articles = self.collect_all()
        # filtra artículos ya enviados
        all_articles = [a for a in all_articles if article_uid(a) not in self.sent_ids]

        selected = self.select_top_by_quota(all_articles)

        digest = self.create_digest_textblock(selected)
        self.send_long(digest)

        # Persistir IDs enviados
        for a in selected:
            self.sent_ids.add(article_uid(a))
        save_state(self.sent_ids)

        # Guardar reporte en repo
        reports = pathlib.Path("reports")
        reports.mkdir(exist_ok=True)
        fname = reports / f"boletin_{datetime.now().strftime('%Y-%m-%d_%H%M')}.md"
        try:
            fname.write_text(digest, encoding="utf-8")
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
    # Prueba de traducción (si Gemini está activo, debería salir traducido)
    prueba = bot.translate_force_es("Breaking: Apple unveils a new AI feature for iPhone.")
    logger.info(f"Traducción de prueba: {prueba}")
    logger.info("Generando y enviando boletín...")
    bot.run()
    logger.info("Listo.")

if __name__ == "__main__":
    main()
