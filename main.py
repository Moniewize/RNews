import calendar
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup

ID_INSTANCE = os.getenv("GREEN_API_ID_INSTANCE")
API_TOKEN = os.getenv("GREEN_API_TOKEN")
PHONE_NUMBERS = os.getenv("PHONE_NUMBER")  # one or more, separated by commas

# Optional: the "apiUrl" shown in your Green API console
GREEN_API_URL = (os.getenv("GREEN_API_URL") or "").strip().rstrip("/")
API_HOSTS = [h for h in dict.fromkeys([
    GREEN_API_URL,
    "https://api.greenapi.com",    # Green API's universal host
    "https://api.green-api.com",   # the old host
]) if h]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CUSTOM_FOOTER = os.getenv(
    "CUSTOM_FOOTER",
    "*Source:* rotary.org and other affiliated sources\n*Brought by:* RAC-FUTO Editorial Team",
)
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}
MAX_ITEMS = 10
RECENT_HOURS = 336.0  # two weeks


def safe(text):
    """Hides secret tokens in anything that is printed."""
    text = str(text)
    for secret in (API_TOKEN, TELEGRAM_BOT_TOKEN):
        if secret:
            text = text.replace(secret, "***")
    return text


def whatsapp_ready():
    return bool(ID_INSTANCE and API_TOKEN and PHONE_NUMBERS)


def telegram_ready():
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def clean_url(raw_url):
    """Strips query parameters to ensure clean, direct links."""
    if not raw_url:
        return ""
    parsed = urlparse(raw_url.strip())
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def parse_feed(url):
    """Downloads with a time limit, so a slow site cannot hang the run."""
    response = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
    response.raise_for_status()
    return feedparser.parse(response.content)


def entry_date(entry, now_utc):
    """Feed times are in Greenwich time, so timegm is used, not mktime."""
    for field in ("published_parsed", "updated_parsed"):
        value = getattr(entry, field, None)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return now_utc


def feed_articles(url, priority, now_utc, must_contain=None):
    articles = []
    for entry in parse_feed(url).entries:
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        if not title or not link:
            continue
        if must_contain and must_contain not in link:
            continue
        published = entry_date(entry, now_utc)
        hours_old = (now_utc - published).total_seconds() / 3600.0
        articles.append({
            "title": title,
            "url": clean_url(link),
            "published_at": published,
            "within_2_weeks": hours_old <= RECENT_HOURS,
            "priority": priority,
        })
    return articles


def fetch_primary_rotary_org(now_utc):
    """Official news from rotary.org: its news feed first, then its news page."""
    articles = []
    try:
        articles += feed_articles("https://www.rotary.org/rss.xml", 1, now_utc, "rotary.org")
        print(f"rotary.org feed: {len(articles)} stories.")
    except Exception as error:
        print(f"Error reading the rotary.org feed: {type(error).__name__}")

    try:
        response = requests.get("https://www.rotary.org/en/news-and-stories",
                                headers=BROWSER_HEADERS, timeout=20)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            found = 0
            for tag in soup.find_all("a", href=True):
                if "/en/articles/" not in tag["href"]:
                    continue
                title = tag.get_text(strip=True)
                if (not title or len(title) < 15
                        or title.lower() in ["read more", "news and stories", "all news and stories"]):
                    continue
                articles.append({
                    "title": title,
                    "url": clean_url(urljoin("https://www.rotary.org", tag["href"])),
                    "published_at": now_utc - timedelta(days=1),
                    "within_2_weeks": True,
                    "priority": 1,
                })
                found += 1
            print(f"rotary.org news page: {found} stories.")
        else:
            print(f"rotary.org news page answered {response.status_code}.")
    except Exception as error:
        print(f"Error reading the rotary.org news page: {type(error).__name__}")
    return articles


def fetch_secondary_rotary_feeds(now_utc):
    articles = []
    for name, url, priority in (
        ("Rotary Intercountry Committees", "https://rotary-icc.org/feed/", 2),
        ("Rotary News Online", "https://rotarynewsonline.org/feed/", 3),
    ):
        try:
            found = feed_articles(url, priority, now_utc)
            articles += found
            print(f"{name}: {len(found)} stories.")
        except Exception as error:
            print(f"Error reading {name}: {type(error).__name__}")
    return articles


def get_top_stories():
    """Official site first, then recency, with duplicates removed."""
    now_utc = datetime.now(timezone.utc)
    candidates = fetch_primary_rotary_org(now_utc) + fetch_secondary_rotary_feeds(now_utc)

    recent, older = [], []
    seen_links, seen_titles = set(), set()
    for item in candidates:
        title_key = item["title"].lower()[:30]
        if item["url"] in seen_links or title_key in seen_titles:
            continue
        seen_links.add(item["url"])
        seen_titles.add(title_key)
        (recent if item["within_2_weeks"] else older).append(item)

    order = lambda x: (x["priority"], -x["published_at"].timestamp())
    recent.sort(key=order)
    older.sort(key=order)
    chosen = recent[:MAX_ITEMS]
    if len(chosen) < MAX_ITEMS:
        chosen += older[:MAX_ITEMS - len(chosen)]
    return chosen


def build_message(articles):
    if not articles:
        return ("📌 *Rotary & Rotaract Global News Update*\n\n"
                "There are currently no new posts or updates available.\n\n"
                + CUSTOM_FOOTER.strip())
    lines = [
        "📌 *Rotary & Rotaract Global News Update*\n",
        "Here are today's top official reports and featured initiatives:\n",
    ]
    for number, article in enumerate(articles, 1):
        # characters that break bold text or links in chat apps are removed
        title = re.sub(r"[*_`\[\]]", "", article["title"])
        lines.append(f"{number}. *{title}*")
        lines.append(f"🔗 {article['url']}\n")
    if CUSTOM_FOOTER.strip():
        lines.append(CUSTOM_FOOTER.strip())
    return "\n".join(lines)


def chat_identifier(raw):
    cleaned = raw.strip().replace("+", "").replace(" ", "")
    return cleaned if ("@g.us" in cleaned or "@c.us" in cleaned) else f"{cleaned}@c.us"


def green_post(endpoint, **kwargs):
    """Tries each Green API host. Moves on only when no connection was made;
    a server error or a timeout after connecting stops here, because the
    message may already have been accepted and a retry could send it twice."""
    last_error = None
    for host in API_HOSTS:
        name = host.split("//")[1]
        link = f"{host}/waInstance{ID_INSTANCE}/{endpoint}/{API_TOKEN}"
        try:
            response = requests.post(link, timeout=(10, 60), **kwargs)
        except requests.ConnectionError as error:
            last_error = error
            print(f"Green API host not reachable: {name}")
            continue
        except requests.Timeout:
            raise RuntimeError(f"no answer from {name}; it may or may not have been sent")
        if response.status_code in (401, 403, 404) and host != API_HOSTS[-1]:
            last_error = requests.HTTPError(f"{response.status_code} from {name}")
            print(f"{name} refused this instance ({response.status_code}). Trying the next host.")
            continue
        if response.status_code >= 500:
            raise RuntimeError(f"{response.status_code} from {name}; it may or may not have been sent")
        response.raise_for_status()
        print(f"Green API host used: {name}")
        return response.json()
    raise last_error or ConnectionError("no Green API host reachable")


def deliver_whatsapp(message):
    """Returns (recipients_reached, problems)."""
    if not whatsapp_ready():
        print("WhatsApp skipped: credentials not set.")
        return 0, []
    sent, problems = 0, []
    for raw in (p for p in PHONE_NUMBERS.split(",") if p.strip()):
        chat = chat_identifier(raw)
        try:
            green_post("sendMessage", json={"chatId": chat, "message": message})
            sent += 1
            print(f"Delivered via WhatsApp to {chat}")
        except Exception as error:
            problems.append(f"WhatsApp {chat}: {safe(error)}")
            print(f"FAILED for WhatsApp {chat}: {safe(error)}")
        time.sleep(3)
    return sent, problems


def split_for_telegram(text, limit=4000):
    """Telegram accepts at most 4096 characters in one message."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip("\n"))
    return chunks


def deliver_telegram(message):
    """Returns (messages_sent, problems)."""
    if not telegram_ready():
        print("Telegram skipped: credentials not set.")
        return 0, []
    link = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in split_for_telegram(message):
        sent = False
        for extra in ({"parse_mode": "Markdown"}, {}):  # plain text if Markdown is rejected
            try:
                response = requests.post(
                    link, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, **extra}, timeout=30)
            except Exception as error:
                print(f"Telegram not reachable: {safe(error)}")
                return 0, [f"Telegram: {safe(error)}"]
            if response.status_code == 400 and extra:
                print(f"Telegram rejected the formatting ({response.text[:120]}). Retrying as plain text.")
                continue
            if response.ok:
                sent = True
            else:
                print(f"Telegram refused ({response.status_code}): {response.text[:200]}")
            break
        if not sent:
            return 0, ["Telegram: delivery failed"]
    print("Delivered to Telegram.")
    return 1, []


def main():
    if not (whatsapp_ready() or telegram_ready()):
        print("No delivery channel is configured (WhatsApp or Telegram secrets missing).")
        sys.exit(1)

    articles = get_top_stories()
    print(f"Stories chosen: {len(articles)}")
    message = build_message(articles)
    print("\n===== MESSAGE =====\n" + message + "\n===================")

    delivered, problems = 0, []
    for deliver in (deliver_whatsapp, deliver_telegram):
        sent, issues = deliver(message)
        delivered += sent
        problems += issues

    if delivered == 0:
        print("NOT DELIVERED: no channel accepted the message.")
        sys.exit(1)
    if problems:
        print("PROBLEMS (the message reached some recipients):", *problems, sep="\n- ")
        sys.exit(1)  # marks the run as failed so GitHub emails you


if __name__ == "__main__":
    main()