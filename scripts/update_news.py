#!/usr/bin/env python3
"""Fetch Syrian news RSS items, deduplicate them, publish a JSON feed, and optionally post to WhatsApp."""
from __future__ import annotations

import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
NEWS_FILE = DATA / "news.json"
STATE_FILE = DATA / "state.json"
MAX_NEW_PER_CYCLE = 2
MAX_SITE_NEWS = 100
MAX_HISTORY = 1000

FEEDS = [
    ("سانا", "site:sana.sy سوريا"),
    ("تلفزيون سوريا", "site:syria.tv سوريا"),
    ("عنب بلدي", "site:enabbaladi.net سوريا"),
    ("الوطن", "site:alwatan.sy سوريا"),
    ("زمان الوصل", "site:zamanalwsl.net سوريا"),
    ("سوريا دايركت", "site:syriadirect.org سوريا"),
    ("The Syrian Observer", "site:syrianobserver.com Syria"),
    ("أورينت", "site:orient-news.net سوريا"),
]

SYRIA_TERMS = (
    "سوريا", "السوري", "السورية", "دمشق", "حلب", "حمص", "حماة", "إدلب",
    "اللاذقية", "طرطوس", "الحسكة", "دير الزور", "الرقة", "القامشلي",
    "السويداء", "درعا", "القنيطرة", "ريف دمشق", "سوريون", "سوريين",
    "syria", "syrian", "damascus", "aleppo", "homs", "hama", "idlib",
    "hasakah", "raqqa", "deir ez zor", "daraa", "latakia", "tartous",
    "sweida", "quneitra",
)


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self):
        return re.sub(r"\\s+", " ", " ".join(self.parts)).strip()


def clean_text(value: str) -> str:
    parser = TextOnly()
    try:
        parser.feed(html.unescape(value or ""))
        return parser.get_text()
    except Exception:
        return re.sub(r"\\s+", " ", html.unescape(value or "")).strip()


def parse_date(value: str) -> str:
    if value:
        try:
            dt = email.utils.parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError):
            pass
    return ""


def item_text(item: ET.Element, name: str) -> str:
    for child in list(item):
        if child.tag.rsplit("}", 1)[-1].lower() == name.lower():
            return (child.text or "").strip()
    return ""


def parse_feed(xml_bytes: bytes, feed_label: str) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    container = root
    for child in list(root):
        if child.tag.rsplit("}", 1)[-1].lower() == "channel":
            container = child
            break
    output = []
    for node in list(container):
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag not in ("item", "entry"):
            continue
        title = clean_text(item_text(node, "title"))
        link = item_text(node, "link")
        if not link:
            for child in list(node):
                if child.tag.rsplit("}", 1)[-1].lower() == "link":
                    link = child.attrib.get("href", "")
                    if link:
                        break
        description = clean_text(item_text(node, "description") or item_text(node, "summary") or item_text(node, "content"))
        pubdate = item_text(node, "pubDate") or item_text(node, "published") or item_text(node, "updated")
        source = feed_label
        for child in list(node):
            if child.tag.rsplit("}", 1)[-1].lower() == "source" and (child.text or "").strip():
                source = clean_text(child.text)
                break
        if not title or not link.startswith(("http://", "https://")):
            continue
        combined = (title + " " + description).casefold()
        if not any(term.casefold() in combined for term in SYRIA_TERMS):
            continue
        output.append({
            "title": title[:300],
            "description": description[:700],
            "url": link,
            "source": source[:100],
            "feed": feed_label,
            "published_at": parse_date(pubdate),
        })
    return output


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def canonical_title(text: str) -> str:
    return re.sub(r"[^\\w\\u0600-\\u06ff]+", "", (text or "").casefold())


def post_whatsapp(item: dict) -> bool:
    base = os.getenv("GREEN_API_URL", "").strip().rstrip("/")
    instance = os.getenv("GREEN_API_INSTANCE", "").strip()
    token = os.getenv("GREEN_API_TOKEN", "").strip()
    chat_id = os.getenv("WHATSAPP_GROUP_ID", "").strip()
    if not all((base, instance, token, chat_id)):
        return False
    if not instance.startswith("waInstance"):
        instance = "waInstance" + instance
    if not chat_id.endswith("@g.us"):
        chat_id += "@g.us"
    description = item.get("description") or "اضغط على الرابط لقراءة التفاصيل من المصدر."
    message = (
        "🇸🇾 *سوريا مباشر*\\n\\n"
        f"*{item['title']}*\\n\\n"
        f"{description}\\n\\n"
        f"📰 المصدر: {item.get('source') or item.get('feed') or 'المصدر الأصلي'}\\n"
        f"🔗 {item['url']}"
    )
    url = f"{base}/{instance}/sendMessage/{token}"
    payload = json.dumps({"chatId": chat_id, "message": message}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", "User-Agent": "SyriaMubasher/1.0"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
        return isinstance(result.get("idMessage"), str) and bool(result["idMessage"])
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        print(f"WhatsApp send failed for one news item: {type(exc).__name__}", file=sys.stderr)
        return False


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    old_news = load_json(NEWS_FILE, {"updated_at": "", "items": []})
    state = load_json(STATE_FILE, {"published_urls": [], "whatsapp_sent_urls": [], "feed_errors": []})
    published_urls_list = list(state.get("published_urls", []))
    wa_sent_urls_list = list(state.get("whatsapp_sent_urls", []))
    published_urls = set(published_urls_list)
    wa_sent_urls = set(wa_sent_urls_list)
    old_items = old_news.get("items", []) if isinstance(old_news, dict) else []
    errors = []
    candidates = []
    user_agent = "Mozilla/5.0 (compatible; SyriaMubasherNewsBot/1.0)"

    for feed_label, query in FEEDS:
        feed_url = "https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(query) + "&hl=ar&gl=SY&ceid=SY%3Aar"
        request = urllib.request.Request(feed_url, headers={"User-Agent": user_agent, "Accept": "application/rss+xml, application/xml, text/xml"})
        try:
            with urllib.request.urlopen(request, timeout=18) as response:
                raw = response.read(2_000_000)
            candidates.extend(parse_feed(raw, feed_label))
            time.sleep(0.15)
        except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as exc:
            errors.append(f"{feed_label}: {type(exc).__name__}")

    # Deduplicate across multiple feed queries by URL and normalized title.
    unique = []
    used_urls = set()
    used_titles = set()
    for item in candidates:
        url = item["url"].split("&oc=5", 1)[0]
        title_key = canonical_title(item["title"])
        if url in used_urls or (title_key and title_key in used_titles):
            continue
        used_urls.add(url)
        if title_key:
            used_titles.add(title_key)
        item["url"] = url
        unique.append(item)

    unique.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    fresh = []
    old_titles = {canonical_title(item.get("title", "")) for item in old_items}
    for item in unique:
        if item["url"] in published_urls or canonical_title(item["title"]) in old_titles:
            continue
        fresh.append(item)
        if len(fresh) >= MAX_NEW_PER_CYCLE:
            break

    if fresh:
        existing_by_url = {item.get("url"): item for item in old_items if item.get("url")}
        for item in fresh:
            existing_by_url[item["url"]] = item
            if item["url"] not in published_urls:
                published_urls.add(item["url"])
                published_urls_list.append(item["url"])
        new_items = list(existing_by_url.values())
        new_items.sort(key=lambda item: item.get("published_at", ""), reverse=True)
        old_news = {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "items": new_items[:MAX_SITE_NEWS],
        }
    else:
        old_news.setdefault("items", [])
        old_news.setdefault("updated_at", "")

    # Retry recent unsent items, but attempt at most two posts per workflow run.
    attempted_this_run = 0
    whatsapp_configured = all(os.getenv(key, "").strip() for key in ("GREEN_API_URL", "GREEN_API_INSTANCE", "GREEN_API_TOKEN", "WHATSAPP_GROUP_ID"))
    if whatsapp_configured:
        for item in old_news.get("items", [])[:MAX_SITE_NEWS]:
            url = item.get("url", "")
            if not url or url in wa_sent_urls:
                continue
            if post_whatsapp(item):
                wa_sent_urls.add(url)
                wa_sent_urls_list.append(url)
            attempted_this_run += 1
            if attempted_this_run >= MAX_NEW_PER_CYCLE:
                break

    state_out = {
        "published_urls": list(dict.fromkeys(published_urls_list))[-MAX_HISTORY:],
        "whatsapp_sent_urls": list(dict.fromkeys(wa_sent_urls_list))[-MAX_HISTORY:],
        "feed_errors": errors,
    }
    NEWS_FILE.write_text(json.dumps(old_news, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    STATE_FILE.write_text(json.dumps(state_out, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    print(f"Sources checked: {len(FEEDS)}; accepted feed entries: {len(candidates)}; new items: {len(fresh)}; website total: {len(old_news.get('items', []))}; WhatsApp configured: {all(os.getenv(key, '').strip() for key in ('GREEN_API_URL', 'GREEN_API_INSTANCE', 'GREEN_API_TOKEN', 'WHATSAPP_GROUP_ID'))}; feed errors: {len(errors)}")


if __name__ == "__main__":
    main()
