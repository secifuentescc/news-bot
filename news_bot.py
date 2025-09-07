import os
import sys
import json
import hashlib
import logging
import argparse
from datetime import datetime
import pathlib
import re
import requests
import feedparser
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import google.generativeai as genai

# ================== CONFIG ==================
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID   = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
GEMINI_KEY         = (os.getenv("GEMINI_API_KEY") or "").strip()

STATE_PATH = pathlib.Path("state_sent.json")

# Fuentes RSS (añadimos MEDICINA, priorizamos tech + salud)
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
        "https://ai.googleblog.com/feeds/posts/default",
        "https://openai.com/blog/rss.xml",
        "https://blogs.nvidia.com/feed/",
    ],
    "medicina": [
        # Journals y editoriales
        "https://www.nejm.org/rss.xml",                             # NEJM
        "https://www.thelancet.com/rssfeed/lancet_current.rss",     # The Lancet (current)
        "https://www.bmj.com/rss/bmj_latest.xml",                   # BMJ
        "https://www.nature.com/nm.rss",                            # Nature Medicine
        "https://www.science.org/rss/channel/health",               # Science (Health)
        # Agencias/organismos (elige los que te funcionen mejor)
        "https://tools.cdc.gov/api/v2/resources/media/132608.rss",  # CDC Newsroom
        # Regional
        "https://www.paho.org/en/rss.xml",                          # OPS/PAHO (EN)
        "https://www.paho.org/es/rss.xml",                          # OPS/PAHO (ES)
        # Medios
        "https://www.statnews.com/feed/",                           # STAT News (global feed)
    ],
}

def quotas_for_today():
    """Cupoes por categoría; fin de semana reducimos tech un poco."""
    is_weekend = datetime.utcnow().weekday() >= 5
    return {
        "tecnologia": (7 if not is_weekend else 5),
        "medicina": 6,
        "colombia": 3,
        "mundial": 3,
    }

# ================== ESCAPES MarkdownV2 (Telegram) ==================
_MD2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"

def escape_md2(text: str) -> str:
    if text is None:
        return ""
    out = []
    for ch in text:
        if ch in _MD2_SPECIALS:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)

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

# ================== Heurística idioma ==================
_ENG_HINTS = {
    "the","and","or","but","in","on","at","to","for","of","with","by","from",
    "as","is","are","be","this","that","it","they","you","we","an","a"
}
def probably_english(s: str) -> bool:
    if not s:
        return False
    words = re.findall(r"[a-zA-Z']+", s.lower())
    if not words:
        return False
    hits = sum(1 for w in words if w in _ENG_HINTS)
    return hits >= max(3, int(len(words) * 0.2))

# ================== BOT ==================
class NewsBot:
    def __init__(self, only_tech: bool = False, only_medicine: bool = False):
        self.only_tech = only_tech
        self.only_medicine = only_medicine
        self.processed = set()
        self.sent_ids = load_state()

        # Gemini opcional
        self.model = None
        if GEMINI_KEY:
            try:
                genai.configure(api_key=GEMINI_KEY)
                # 1.5-pro (mejor) o 1.5-flash (más barato/rápido). Cambia si quieres.
                self.model = genai.GenerativeModel("gemini-1.5-pro")
                masked = GEMINI_KEY[:4] + "..." + GEMINI_KEY[-4:]
                logger.info(f"Gemini listo (key {masked}).")
            except Exception as e:
                logger.warning(f"No se pudo inicializar Gemini: {e}")
        else:
            logger.warning("GEMINI_API_KEY no definido: sin traducción/resúmenes de IA.")

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

    # ------------ Traducción ------------
    def translate_gemini(self, text: str) -> str | None:
        """Traduce con Gemini; devuelve None si falla (cuota/timeout)."""
        if not text or not self.model:
            return None
        try:
            prompt = (
                "Traduce al español de forma natural y clara. "
                "No agregues comentarios ni comillas. Solo el texto traducido.\n\n"
                f"{text}"
            )
            resp = self.model.generate_content(prompt)
            out = (getattr(resp, "text", "") or "").strip()
            return out or None
        except Exception as e:
            logger.warning(f"Gemini generate_content falló: {e}")
            return None

    def translate_mymemory(self, text: str, assume_en: bool) -> str | None:
        """
        Fallback gratuito: MyMemory (límite comunitario).
        Si assume_en=True usamos langpair=en|es; si no, intentamos auto|es.
        """
        if not text:
            return None
        try:
            src = "en" if assume_en else "auto"
            params = {
                "q": text[:4500],     # límite prudente
                "langpair": f"{src}|es"
            }
            r = requests.get("https://api.mymemory.translated.net/get", params=params, timeout=12)
            if not r.ok:
                return None
            data = r.json()
            t = data.get("responseData", {}).get("translatedText")
            if t:
                logger.info("Traducción vía MyMemory OK.")
                return t
        except Exception as e:
            logger.warning(f"Fallback MyMemory falló: {e}")
        return None

    def translate_force_es(self, text: str) -> str:
        """Siempre intenta dejar el texto en español (Gemini -> MyMemory -> original)."""
        if not text:
            return text
        # 1) Gemini
        out = self.translate_gemini(text)
        if out:
            return out
        logger.info("Gemini sin salida; usaré fallback de traducción.")
        # 2) Fallback MyMemory (si parece inglés, forzamos en|es)
        out = self.translate_mymemory(text, assume_en=probably_english(text))
        return out or text

    # ------------ Resúmenes ------------
    def summarize_extended(self, title_es: str, description_es: str, category: str) -> str:
        """Resumen 3–5 frases; si no hay IA, devuelve la descripción truncada."""
        base = (description_es or title_es or "").strip()
        if not self.model:
            return base[:600]
        try:
            prompt = (
                "Redacta un resumen informativo en español (3 a 5 frases, "
                "máximo ~100 palabras) sobre la noticia. Explica qué pasó, "
                "por qué importa y da contexto. No incluyas opiniones.\n\n"
                f"Título: {title_es}\n"
                f"Descripción/Extracto: {description_es}\n"
                f"Categoría: {category.upper()}\n"
            )
            resp = self.model.generate_content(prompt)
            out = (getattr(resp, "text", "") or "").strip()
            if not out:
                return base[:600]
            return out
        except Exception as e:
            logger.warning(f"Error resumiendo con Gemini: {e}")
            return base[:600]

    def summarize_batch(self, articles):
        """Boletín completo en una sola llamada (si falla, devolvemos None y usamos fallback por ítem)."""
        if not self.model or not articles:
            return None
        try:
            lines = []
            for i, a in enumerate(articles, 1):
                lines.append(
                    f"{i}. [{a['cat']}]\n"
                    f"TÍTULO: {a['title']}\n"
                    f"DESC: {a['desc'][:900]}\n"
                    f"LINK: {a['link']}\n---"
                )
            block = "\n".join(lines)
            prompt = (
                "Redacta un boletín en español, claro y profesional, con SECCIONES "
                "en este orden: TECNOLOGÍA, MEDICINA, COLOMBIA, MUNDIAL. Para cada noticia, escribe "
                "3–5 frases (qué pasó, contexto, por qué importa). Traduce todo al español. "
                "Cierra con 3 viñetas de 'Qué vigilar'. No agregues disclaimers.\n\n"
                f"NOTICIAS:\n{block}"
            )
            resp = self.model.generate_content(prompt)
            out = (getattr(resp, "text", "") or "").strip()
            return out or None
        except Exception as e:
            logger.warning(f"Error en summarize_batch: {e}")
            return None

    # ------------ Ranking ------------
    def rank_with_gemini(self, articles):
        """Puntúa importancia (0-10) priorizando tecnología/medicina; fallback heurístico."""
        if not self.model or not articles:
            return sorted(
                articles,
                key=lambda a: (a["cat"] not in ("tecnologia","medicina"),),
            )
        try:
            packed = "\n".join(
                [f"{i+1}. [{a['cat']}] {a['title']}\n{(a['desc'] or '')[:280]}" for i, a in enumerate(articles)]
            )
            prompt = (
                "Eres editor senior. Puntúa cada ítem del 1 al 10 según impacto, novedad "
                "y relevancia para lectores hispanohablantes. Da preferencia a TECNOLOGÍA y MEDICINA "
                "si el impacto es similar. Devuelve JSON: [{\"idx\": <n>, \"score\": <0-10>}].\n\n"
                f"NOTICIAS:\n{packed}"
            )
            resp = self.model.generate_content(prompt)
            txt = (getattr(resp, "text", "") or "").strip()
            scores = json.loads(txt)
            score_map = {int(it["idx"]) - 1: float(it["score"]) for it in scores if "idx" in it and "score" in it}
            ranked = sorted(
                articles,
                key=lambda a: score_map.get(a["_i"], 0.0) + (1.2 if a["cat"] in ("tecnologia","medicina") else 0.0),
                reverse=True,
            )
            return ranked
        except Exception as e:
            logger.warning(f"No se pudo rankear con IA: {e}")
            return sorted(
                articles,
                key=lambda a: (a["cat"] not in ("tecnologia","medicina"),),
            )

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
        if self.only_tech:
            cats = ["tecnologia"]
        elif self.only_medicine:
            cats = ["medicina"]
        else:
            cats = ["tecnologia", "medicina", "colombia", "mundial"]
        all_articles = []
        for cat in cats:
            all_articles.extend(self.get_rss(cat))
        logger.info(f"Recolectadas {len(all_articles)} noticias (only_tech={self.only_tech}, only_medicine={self.only_medicine}).")
        return all_articles

    def select_top_by_quota(self, articles):
        """Ordena por importancia y aplica cupos por categoría."""
        if self.only_tech:
            articles = [a for a in articles if a["cat"] == "tecnologia"]
        if self.only_medicine:
            articles = [a for a in articles if a["cat"] == "medicina"]

        if not articles:
            return []

        q = quotas_for_today()
        ranked = self.rank_with_gemini(articles)
        counts = {k: 0 for k in q}
        selected = []
        for a in ranked:
            if counts.get(a["cat"], 0) < q.get(a["cat"], 0):
                selected.append(a)
                counts[a["cat"]] += 1
            if sum(counts.values()) >= sum(q.values()):
                break

        # Orden de presentación (tech, medicina, colombia, mundial)
        order = {"tecnologia": 0, "medicina": 1, "colombia": 2, "mundial": 3}
        selected.sort(key=lambda a: order.get(a["cat"], 9))
        logger.info(f"Seleccionadas por cuota: {counts}")
        return selected

    def create_digest_fallback(self, selected):
        """Fallback por ítem: traduce y resume cada noticia (MarkdownV2)."""
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
                text += f"{icons[current_cat]} *{escape_md2(titles[current_cat])}*\n"

            # Traducción forzada de título/desc (si están en inglés u otro idioma)
            title_es = self.translate_force_es(a["title"]) if probably_english(a["title"]) else a["title"]
            desc_src = a["desc"] if a["desc"] else a["title"]
            desc_es  = self.translate_force_es(desc_src) if probably_english(desc_src) else desc_src

            resumen = self.summarize_extended(title_es, desc_es, a["cat"])

            title_bold = f"*{escape_md2(title_es)}*"
            resumen_md = escape_md2(resumen)

            # En MarkdownV2, el URL dentro de [texto](url) no requiere escapado especial
            text += f"• {title_bold}\n{resumen_md}\n[Leer más]({a['link']})\n\n"

        text += escape_md2("---") + "\n" + "_Resumen automatizado con IA (prioridad tecnología/medicina)_"
        return text

    def save_report(self, text: str):
        reports = pathlib.Path("reports")
        reports.mkdir(exist_ok=True)
        fname = reports / f"boletin_{datetime.now().strftime('%Y-%m-%d_%H%M')}.md"
        try:
            fname.write_text(text, encoding="utf-8")
            return str(fname)
        except Exception as e:
            logger.warning(f"No se pudo guardar el reporte: {e}")
            return None

    def run(self):
        start = datetime.now()
        all_articles = self.collect_all()
        # filtra artículos ya enviados
        all_articles = [a for a in all_articles if article_uid(a) not in self.sent_ids]

        selected = self.select_top_by_quota(all_articles)

        # Intento 1: resumen batch pro (una sola llamada IA)
        digest = self.summarize_batch(selected)
        if digest:
            digest = escape_md2(digest)
        else:
            # Fallback por ítem
            digest = self.create_digest_fallback(selected)

        # Enviar (en trozos si es largo)
        self.send_long(digest)

        # Persistir IDs enviados
        for a in selected:
            self.sent_ids.add(article_uid(a))
        save_state(self.sent_ids)

        # Guardar reporte en repo
        saved = self.save_report(digest)
        if saved:
            logger.info(f"Reporte guardado en: {saved}")

        elapsed = (datetime.now() - start).seconds
        logger.info(f"Boletín enviado. {len(selected)} noticias. Tiempo total: {elapsed}s.")

# ================== MAIN ==================
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--only-tech", action="store_true", help="Enviar solo tecnología")
    p.add_argument("--only-medicine", action="store_true", help="Enviar solo medicina")
    return p.parse_args()

def main():
    args = parse_args()
    bot = NewsBot(only_tech=args.only_tech, only_medicine=args.only_medicine)

    # Prueba visible en logs (debe salir traducido si la key/quotas están OK o usar fallback)
    sample = "Breaking: Apple unveils a new AI feature for iPhone."
    test = bot.translate_force_es(sample)
    logger.info(f"Traducción de prueba: {test}")

    logger.info("Generando y enviando boletín...")
    bot.run()
    logger.info("Listo.")

if __name__ == "__main__":
    main()