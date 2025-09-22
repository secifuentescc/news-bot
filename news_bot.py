import os
import time
import logging
import requests
import schedule
import feedparser
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ============== CONFIG ==============
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ARTICLE_MAX_AGE_HOURS = 48
MAX_ARTICLES_PER_CATEGORY = 5
MESSAGE_TS_FORMAT = "%d/%m/%Y %H:%M"

NEWS_SOURCES = {
    "colombia": [
        "https://www.eltiempo.com/rss.xml",
        "https://www.semana.com/rss.xml",
        "https://www.elespectador.com/rss.xml",
    ],
    "internacionales": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://rss.cnn.com/rss/edition.rss",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
    "tecnologia": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://arstechnica.com/feed/",
        "https://www.wired.com/feed",
    ],
    "medicina": [
        "https://www.medicalnewstoday.com/rss",
        "https://www.nih.gov/news-events/rss.xml",
        "https://www.sciencedaily.com/rss/top/health.xml",
        "https://www.eurekalert.org/rss/health-medicine.xml",
    ],
}

CATEGORY_LABEL = {
    "colombia": "🇨🇴 Noticias Colombia",
    "internacionales": "🌎 Noticias Internacionales",
    "tecnologia": "💻 Tecnología",
    "medicina": "🩺 Medicina",
}

CATEGORY_ORDER = ["colombia", "internacionales", "tecnologia", "medicina"]

# ============== BOT ==============
class NewsBot:
    def __init__(self):
        self.processed = set()

    def send_message(self, text: str):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            r = requests.post(url, data=payload, timeout=15)
            if not r.ok:
                logger.error(r.text)
        except Exception as e:
            logger.error(f"Error enviando a Telegram: {e}")

    def get_rss(self, category: str):
        arts = []
        for rss in NEWS_SOURCES.get(category, []):
            try:
                feed = feedparser.parse(rss)
                for entry in feed.entries[:5]:
                    desc_html = entry.get("summary") or entry.get("description") or ""
                    desc = BeautifulSoup(desc_html, "html.parser").get_text(" ").strip()
                    uid = f"{entry.get('title','Sin título')}|{entry.get('link')}"
                    published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                    published_dt = None
                    if published_struct:
                        try:
                            published_dt = datetime.fromtimestamp(time.mktime(published_struct), tz=timezone.utc)
                        except Exception:
                            published_dt = None
                    if published_dt and published_dt < datetime.now(timezone.utc) - timedelta(hours=ARTICLE_MAX_AGE_HOURS):
                        continue

                    image_url = None
                    for media in entry.get("media_content", []) or []:
                        if isinstance(media, dict) and media.get("url"):
                            if not media.get("type") or media["type"].startswith("image"):
                                image_url = media["url"]
                                break
                    if not image_url:
                        for media in entry.get("media_thumbnail", []) or []:
                            if isinstance(media, dict) and media.get("url"):
                                image_url = media["url"]
                                break
                    if not image_url:
                        for link in entry.get("links", []) or []:
                            if link.get("rel") == "enclosure" and link.get("type", "").startswith("image"):
                                image_url = link.get("href")
                                break

                    if uid not in self.processed:
                        arts.append({
                            "title": entry.get("title", "Sin título").strip(),
                            "desc": desc,
                            "link": entry.get("link"),
                            "image": image_url,
                            "published": published_dt,
                            "cat": category,
                        })
                        self.processed.add(uid)
            except Exception as e:
                logger.error(f"Error con {rss}: {e}")
        return arts

    @staticmethod
    def _shorten(text: str, max_len: int = 240) -> str:
        text = (text or "").strip()
        if len(text) <= max_len:
            return text
        truncated = text[:max_len].rsplit(" ", 1)[0]
        return f"{truncated}…"

    def build_category_message(self, category: str, articles):
        header = CATEGORY_LABEL.get(category, category.title())
        timestamp = datetime.now().strftime(MESSAGE_TS_FORMAT)
        if not articles:
            return f"{header}\nNo hay noticias nuevas por ahora ({timestamp})."

        lines = [f"{header} | {timestamp}", ""]
        for art in articles:
            desc = self._shorten(art.get("desc") or "Sin descripción disponible.")
            link = art.get("link") or ""
            title = art.get("title") or "Sin título"
            segment = [f"• *{title}*"]
            if art.get("published"):
                try:
                    local_pub = art["published"].astimezone()
                    segment.append(f"🕒 {local_pub.strftime('%d/%m %H:%M')}")
                except Exception:
                    pass
            segment.append(desc or "Sin descripción disponible.")
            if link:
                segment.append(f"[Leer más]({link})")
            if art.get("image"):
                segment.append(f"🖼 {art['image']}")
            lines.append("\n".join(segment))
            lines.append("")
        return "\n".join(lines).strip()

    def run(self):
        for category in CATEGORY_ORDER:
            articles = self.get_rss(category)
            articles.sort(key=lambda a: a.get("published") or datetime.now(timezone.utc), reverse=True)
            summary = self.build_category_message(category, articles[:MAX_ARTICLES_PER_CATEGORY])
            self.send_message(summary)
            time.sleep(1)

def main():
    bot = NewsBot()
    schedule.every().day.at("08:00").do(bot.run)
    schedule.every().day.at("18:00").do(bot.run)

    logger.info("Bot iniciado. Ejecutando prueba inicial...")
    bot.run()

    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
