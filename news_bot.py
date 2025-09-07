import os
import time
import logging
import requests
import feedparser
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ============== CONFIG ==============
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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
                    desc = BeautifulSoup(entry.get("description",""), "html.parser").get_text(" ")
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
                logger.error(f"Error con {rss}: {e}")
        return arts

    def create_summary(self, articles):
        if not articles:
            return "No hay noticias nuevas."
        text = f"📰 *Resumen de noticias* - {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        for a in articles:
            text += f"• *{a['title']}*\n{a['desc'][:120]}...\n[Leer más]({a['link']})\n\n"
        return text

    def run(self):
        all_articles = []
        for c in ["colombia","mundial","tecnologia"]:
            all_articles.extend(self.get_rss(c))
        summary = self.create_summary(all_articles[:9])  # máx 9 noticias
        self.send_message(summary)

# ============== MAIN ==============
def main():
    bot = NewsBot()
    logger.info("Bot iniciado. Ejecutando una sola vez...")
    bot.run()
    logger.info("Bot finalizado.")

if __name__ == "__main__":
    main()
