import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests

MAX_ARTICLES = 2
SEEN_FILE = Path("seen.json")
LATEST_FILE = Path("latest_news.json")
FEEDS_FILE = Path("feeds.txt")
REQUEST_TIMEOUT = 25

SYRIA_KEYWORDS = [
    "سوريا",
    "سورية",
    "السوري",
    "السورية",
    "سوري",
    "سوريّة",
    "دمشق",
    "ريف دمشق",
    "حلب",
    "حمص",
    "حماة",
    "إدلب",
    "اللاذقية",
    "طرطوس",
    "درعا",
    "السويداء",
    "القنيطرة",
    "دير الزور",
    "الرقة",
    "الحسكة",
    "ريف حلب",
    "ريف حمص",
    "ريف حماة",
    "ريف إدلب",
]


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def load_feeds() -> list[str]:
    if not FEEDS_FILE.exists():
        return []

    feeds = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            feeds.append(line)
    return feeds


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()

    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except (json.JSONDecodeError, OSError):
        return set()


def save_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def entry_time(entry: Any) -> float:
    published = getattr(entry, "published_parsed", None)
    updated = getattr(entry, "updated_parsed", None)

    for parsed in (published, updated):
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc).timestamp()
            except (TypeError, ValueError):
                pass

    return 0.0


def is_syria_news(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(keyword.lower() in text for keyword in SYRIA_KEYWORDS)


def collect_candidates(feeds: list[str]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    headers = {
        "User-Agent": "Syria-News-AI-Radar/1.0 (+https://github.com/obaidahshteiwi/obaidah)"
    }

    for feed_url in feeds:
        try:
            response = requests.get(
                feed_url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)
        except requests.RequestException as exc:
            print(f"[WARN] Feed request failed: {feed_url} -> {exc}")
            continue

        for entry in feed.entries:
            title = clean_text(getattr(entry, "title", ""))
            summary = clean_text(
                getattr(entry, "summary", "")
                or getattr(entry, "description", "")
            )
            link = str(getattr(entry, "link", "") or "").strip()

            if not title or not link:
                continue

            article_id = link or title
            if article_id in seen_ids:
                continue

            if not is_syria_news(title, summary):
                continue

            seen_ids.add(article_id)
            candidates.append(
                {
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "published": str(
                        getattr(entry, "published", "")
                        or getattr(entry, "updated", "")
                        or ""
                    ).strip(),
                    "timestamp": entry_time(entry),
                    "source_feed": feed_url,
                }
            )

    candidates.sort(key=lambda item: item["timestamp"], reverse=True)
    return candidates


import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests

MAX_ARTICLES = 2
SEEN_FILE = Path("seen.json")
LATEST_FILE = Path("latest_news.json")
FEEDS_FILE = Path("feeds.txt")
REQUEST_TIMEOUT = 25

SYRIA_KEYWORDS = [
    "سوريا",
    "سورية",
    "السوري",
    "السورية",
    "سوري",
    "سوريّة",
    "دمشق",
    "ريف دمشق",
    "حلب",
    "حمص",
    "حماة",
    "إدلب",
    "اللاذقية",
    "طرطوس",
    "درعا",
    "السويداء",
    "القنيطرة",
    "دير الزور",
    "الرقة",
    "الحسكة",
    "ريف حلب",
    "ريف حمص",
    "ريف حماة",
    "ريف إدلب",
]


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\\s+", " ", text)
    return text.strip()


def load_feeds() -> list[str]:
    if not FEEDS_FILE.exists():
        return []

    feeds = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            feeds.append(line)
    return feeds


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()

    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except (json.JSONDecodeError, OSError):
        return set()


def save_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def entry_time(entry: Any) -> float:
    published = getattr(entry, "published_parsed", None)
    updated = getattr(entry, "updated_parsed", None)

    for parsed in (published, updated):
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc).timestamp()
            except (TypeError, ValueError):
                pass

    return 0.0


def is_syria_news(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(keyword.lower() in text for keyword in SYRIA_KEYWORDS)


def collect_candidates(feeds: list[str]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    headers = {
        "User-Agent": "Syria-News-AI-Radar/1.0 (+https://github.com/obaidahshteiwi/obaidah)"
    }

    for feed_url in feeds:
        try:
            response = requests.get(
                feed_url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)
        except requests.RequestException as exc:
            print(f"[WARN] Feed request failed: {feed_url} -> {exc}")
            continue

        for entry in feed.entries:
            title = clean_text(getattr(entry, "title", ""))
            summary = clean_text(
                getattr(entry, "summary", "")
                or getattr(entry, "description", "")
            )
            link = str(getattr(entry, "link", "") or "").strip()

            if not title or not link:
                continue

            article_id = link or title
            if article_id in seen_ids:
                continue

            if not is_syria_news(title, summary):
                continue

            seen_ids.add(article_id)
            candidates.append(
                {
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "published": str(
                        getattr(entry, "published", "")
                        or getattr(entry, "updated", "")
                        or ""
                    ).strip(),
                    "timestamp": entry_time(entry),
                    "source_feed": feed_url,
                }
            )

    candidates.sort(key=lambda item: item["timestamp"], reverse=True)
    return candidates


def deliver_to_make(articles: list[dict[str, Any]]) -> bool:
    webhook_url = os.getenv("MAKE_WEBHOOK_URL", "").strip()

    if not webhook_url:
        print("[INFO] MAKE_WEBHOOK_URL is not configured. Test mode only.")
        return True

    payload = {
        "source": "syria-news-github",
        "count": len(articles),
        "articles": articles,
    }

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"User-Agent": "Syria-News-AI-Radar/1.0"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        print(f"[OK] Sent {len(articles)} article(s) to Make.")
        return True
    except requests.RequestException as exc:
        print(f"[ERROR] Make webhook failed: {exc}")
        return False


def main() -> None:
    feeds = load_feeds()
    if not feeds:
        raise SystemExit("No RSS feeds found in feeds.txt")

    seen = load_seen()
    candidates = collect_candidates(feeds)

    new_articles = []
    for article in candidates:
        article_id = article["link"] or article["title"]
        if article_id not in seen:
            new_articles.append(article)
        if len(new_articles) >= MAX_ARTICLES:
            break

    if not new_articles:
        print("[INFO] No new Syria news found.")
        save_json(LATEST_FILE, [])
        return

    for index, article in enumerate(new_articles, start=1):
        print(f"\\n[{index}] {article['title']}")
        print(f"Source: {article['source_feed']}")
        print(f"Link: {article['link']}")

    # Mark selected articles as seen after processing.
    for article in new_articles:
        seen.add(article["link"] or article["title"])

    # Keep the state file small.
    seen_list = list(seen)[-500:]
    save_json(SEEN_FILE, seen_list)

    # Keep a simple record of what the current cycle selected.
    save_json(LATEST_FILE, new_articles)

    print(f"\\n[OK] Cycle complete. Selected: {len(new_articles)} article(s).")


if __name__ == "__main__":
    main()
