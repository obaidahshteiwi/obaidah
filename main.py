import html
import json
import os
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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")
OPENAI_URL = "https://api.openai.com/v1/responses"

SYRIA_KEYWORDS = [
    "سوريا", "سورية", "السوري", "السورية", "سوري", "سوريّة",
    "دمشق", "ريف دمشق", "حلب", "حمص", "حماة", "إدلب",
    "اللاذقية", "طرطوس", "درعا", "السويداء", "القنيطرة",
    "دير الزور", "الرقة", "الحسكة", "ريف حلب", "ريف حمص",
    "ريف حماة", "ريف إدلب",
]


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def load_feeds() -> list[str]:
    if not FEEDS_FILE.exists():
        return []
    return [
        line.strip()
        for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


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
    for parsed in (
        getattr(entry, "published_parsed", None),
        getattr(entry, "updated_parsed", None),
    ):
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc).timestamp()
            except (TypeError, ValueError):
                pass
    return 0.0


def is_syria_news(title: str, summary: str) -> bool:
    # Google News often appends the publisher name to the title
    # (for example: "عنوان الخبر - وكالة الأنباء السورية – سانا").
    # Remove that publisher suffix so the source name does not make
    # an unrelated article look like Syria news.
    core_title = re.split(r"\s[–—-]\s", title, maxsplit=1)[0]
    clean_summary = re.sub(
        r"(?i)(وكالة الأنباء السورية|سانا|تلفزيون سوريا|سوريا تي في|عنب بلدي)",
        " ",
        summary,
    )
    text = f"{core_title} {clean_summary}".lower()
    return any(keyword.lower() in text for keyword in SYRIA_KEYWORDS)


def rewrite_with_ai(article: dict[str, Any]) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("[ERROR] OPENAI_API_KEY is not configured in GitHub Secrets.")
        return None

    prompt = f"""أنت محرر أخبار سوري محترف.
أعد صياغة الخبر التالي باللغة العربية بصياغة صحفية واضحة ومحايدة.
التزم حصراً بالمعلومات الموجودة في النص، ولا تضف أي معلومة أو استنتاج غير موجود.
لا تذكر أنك ذكاء اصطناعي.

العنوان الأصلي:
{article["title"]}

ملخص/نص المصدر:
{article["summary"]}

أعد النتيجة بهذا الشكل فقط:
العنوان: عنوان مختصر وجذاب
النص: فقرة خبرية من 2 إلى 4 جمل
"""

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "max_output_tokens": 350,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            OPENAI_URL,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        output_text = str(data.get("output_text", "")).strip()
        if not output_text:
            print("[WARN] OpenAI returned an empty response.")
            return None

        rewritten = dict(article)
        rewritten["ai_text"] = output_text
        rewritten["ai_model"] = OPENAI_MODEL
        return rewritten
    except requests.RequestException as exc:
        print(f"[ERROR] OpenAI request failed: {exc}")
        return None


def collect_candidates(feeds: list[str]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    headers = {"User-Agent": "Syria-News-AI-Radar/1.0"}

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
            if article_id in seen_ids or not is_syria_news(title, summary):
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

    save_json(LATEST_FILE, new_articles)

    if not new_articles:
        print("[INFO] No new Syria news found.")
        return

    processed_articles = []
    for index, article in enumerate(new_articles, start=1):
        print(f"\n[{index}] {article['title']}")
        print(f"Published: {article['published']}")
        print(f"Link: {article['link']}")

        rewritten = rewrite_with_ai(article)
        if rewritten is None:
            print("[WARN] Article was not marked as seen so it can be retried next cycle.")
            continue

        processed_articles.append(rewritten)
        seen.add(article["link"] or article["title"])

    save_json(LATEST_FILE, processed_articles)
    save_json(SEEN_FILE, list(seen)[-500:])
    print(f"\n[OK] Cycle complete. AI processed: {len(processed_articles)} article(s).")


if __name__ == "__main__":
    main()
