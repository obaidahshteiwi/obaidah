#!/usr/bin/env python3
"""Collect Syria-focused RSS news, classify by governorate, extract article images, and track WhatsApp delivery."""
from __future__ import annotations

import email.utils
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
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
NEWS_FILE = DATA / "news.json"
STATE_FILE = DATA / "state.json"
MAX_NEW_PER_CYCLE = 2
MAX_SITE_NEWS = 100
MAX_HISTORY = 1000
MAX_IMAGE_LOOKUPS_PER_CYCLE = 3
REQUEST_TIMEOUT = 12
WHATSAPP_TRACKING_VERSION = 2

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

PROVINCES = [
    ("ريف دمشق", ("ريف دمشق", "غوطة دمشق", "الغوطة الشرقية", "الغوطة الغربية", "دوما", "داريا", "جرمانا", "يبرود", "النبك", "الزبداني", "حرستا", "القلمون", "Rif Dimashq", "Rural Damascus")),
    ("دمشق", ("دمشق", "دمشقي", "دمشقية", "Damascus")),
    ("حلب", ("حلب", "عفرين", "الباب", "منبج", "جرابلس", "اعزاز", "أعزاز", "دابق", "Aleppo", "Afrin", "Manbij")),
    ("حمص", ("حمص", "تدمر", "القصير", "الرستن", "تلبيسة", "تلكلخ", "Homs", "Palmyra")),
    ("حماة", ("حماة", "مصياف", "سلمية", "محردة", "السقيلبية", "Hama", "Masyaf")),
    ("اللاذقية", ("اللاذقية", "جبلة", "القرداحة", "Latakia", "Jableh")),
    ("طرطوس", ("طرطوس", "بانياس", "صافيتا", "الدريكيش", "Tartous", "Baniyas")),
    ("إدلب", ("إدلب", "جسر الشغور", "معرة النعمان", "أريحا", "سراقب", "Idlib", "Jisr al-Shughur")),
    ("دير الزور", ("دير الزور", "ديرالزور", "الميادين", "البوكمال", "Deir ez-Zor", "Deir Ezzor", "Mayadin", "Bukamal")),
    ("الرقة", ("الرقة", "تل أبيض", "عين عيسى", "Raqqa", "Tell Abyad")),
    ("الحسكة", ("الحسكة", "القامشلي", "قامشلي", "المالكية", "رأس العين", "رأس العين", "Hasakah", "Qamishli", "Qamishle")),
    ("درعا", ("درعا", "بصرى الشام", "نوى", "طفس", "درعا البلد", "Daraa", "Dera'a", "Bosra")),
    ("السويداء", ("السويداء", "شهبا", "صلخد", "جبل العرب", "Suwayda", "Sweida", "As-Suwayda")),
    ("القنيطرة", ("القنيطرة", "الجولان", "Quneitra", "Golan")),
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
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


class ImageMetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image = ""

    def handle_starttag(self, tag, attrs):
        if self.image or tag.lower() != "meta":
            return
        attrs = {str(k).lower(): str(v) for k, v in attrs if k and v}
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        if key in ("og:image", "og:image:url", "twitter:image", "twitter:image:src", "image"):
            value = (attrs.get("content") or "").strip()
            if value:
                self.image = value


def clean_text(value: str) -> str:
    parser = TextOnly()
    try:
        parser.feed(html.unescape(value or ""))
        return parser.get_text()
    except Exception:
        return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def parse_date(value: str) -> str:
    if value:
        try:
            dt = email.utils.parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
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


def normalize_image_url(value: str, base_url: str = "") -> str:
    value = html.unescape((value or "").strip())
    if not value:
        return ""
    resolved = urllib.parse.urljoin(base_url, value)
    parsed = urllib.parse.urlparse(resolved)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    if parsed.netloc.lower().endswith("google.com") and "/images/" in parsed.path.lower():
        return ""
    return resolved


def image_from_feed_item(node: ET.Element, base_url: str = "") -> str:
    for child in list(node):
        local = child.tag.rsplit("}", 1)[-1].lower()
        candidate = ""
        if local in ("thumbnail", "content", "image"):
            candidate = child.attrib.get("url", "") or child.attrib.get("href", "")
        elif local == "enclosure" and "image" in child.attrib.get("type", "").lower():
            candidate = child.attrib.get("url", "")
        if candidate:
            result = normalize_image_url(candidate, base_url)
            if result:
                return result
    return ""


def classify_province(title: str, description: str = "") -> str:
    text = (title + " " + description).casefold()
    for province, aliases in PROVINCES:
        if any(alias.casefold() in text for alias in aliases):
            return province
    return "أخبار عامة"


def parse_feed(xml_bytes: bytes, feed_label: str) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    container = root
    for child in list(root):
        if child.tag.rsplit("}", 1)[-1].lower() == "channel":
            container = child
            break
    output = []
    for node in list(container):
        if node.tag.rsplit("}", 1)[-1].lower() not in ("item", "entry"):
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
        item = {
            "title": title[:300],
            "description": description[:700],
            "url": link,
            "source": source[:100],
            "feed": feed_label,
            "published_at": parse_date(pubdate),
            "province": classify_province(title, description),
        }
        picture = image_from_feed_item(node, link)
        if picture:
            item["image_url"] = picture
        output.append(item)
    return output


def extract_article_image(article_url: str) -> str:
    if not article_url.startswith(("https://", "http://")):
        return ""
    req = urllib.request.Request(article_url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; SyriaMubasherNewsBot/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                return ""
            raw = response.read(400_000).decode("utf-8", errors="replace")
        parser = ImageMetaParser()
        parser.feed(raw)
        return normalize_image_url(parser.image, final_url)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return ""


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def canonical_title(text: str) -> str:
    return re.sub(r"[^\w\u0600-\u06ff]+", "", (text or "").casefold())


def parse_iso_datetime(value: str):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def green_api_config():
    base = os.getenv("GREEN_API_URL", "").strip().rstrip("/")
    instance = os.getenv("GREEN_API_INSTANCE", "").strip()
    token = os.getenv("GREEN_API_TOKEN", "").strip()
    chat_id = os.getenv("WHATSAPP_GROUP_ID", "").strip()
    if base and not base.startswith(("https://", "http://")):
        base = "https://" + base
    if instance and not instance.startswith("waInstance"):
        instance = "waInstance" + instance
    if chat_id and not chat_id.endswith(("@g.us", "@c.us")):
        chat_id += "@g.us"
    return base, instance, token, chat_id


def green_request(method_name: str, payload: dict | None = None, http_method: str = "POST"):
    base, instance, token, _ = green_api_config()
    if not all((base, instance, token)):
        return None, "not_configured"
    endpoint = f"{base}/{instance}/{method_name}/{token}"
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "SyriaMubasher/1.1"},
        method=http_method,
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            parsed = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
        return parsed, ""
    except urllib.error.HTTPError as exc:
        # Do not log the raw response or URL: both can contain provider-specific sensitive data.
        try:
            error_body = json.loads(exc.read(2000).decode("utf-8", errors="replace"))
        except Exception:
            error_body = {}
        kind = str(error_body.get("error", "") or error_body.get("message", "") or "")
        kind = re.sub(r"[^A-Za-z0-9 _.-]", "", kind)[:80]
        return None, f"http_{exc.code}" + (f":{kind}" if kind else "")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None, "connection_error"


def get_instance_state() -> str:
    result, error = green_request("getStateInstance", http_method="GET")
    if error or not isinstance(result, dict):
        return "unknown"
    return str(result.get("stateInstance", "unknown"))


def read_message_status(chat_id: str, message_id: str) -> tuple[str, str]:
    result, error = green_request("getMessage", {"chatId": chat_id, "idMessage": message_id})
    if error or not isinstance(result, dict):
        return "unknown", error or "invalid_response"
    return str(result.get("statusMessage", "unknown")), ""


def build_message(item: dict) -> str:
    description = clean_text(item.get("description", "")) or "اضغط على الرابط لقراءة التفاصيل من المصدر الأصلي."
    title = clean_text(item.get("title", "خبر من سوريا"))
    source = clean_text(item.get("source") or item.get("feed") or "المصدر الأصلي")
    url = item.get("url", "")
    return (
        "🇸🇾 *سوريا مباشر*\n\n"
        f"*{title}*\n\n"
        f"{description[:800]}\n\n"
        f"🗺️ المحافظة: {item.get('province') or classify_province(title, description)}\n"
        f"📰 المصدر: {source}\n"
        f"🔗 {url}"
    )[:5000]


def post_whatsapp(item: dict) -> tuple[str, str, str]:
    base, instance, token, chat_id = green_api_config()
    if not all((base, instance, token, chat_id)):
        return "", "", "not_configured"
    title = clean_text(item.get("title", "خبر من سوريا"))
    message = build_message(item)
    image_url = normalize_image_url(item.get("image_url", ""))
    if image_url:
        path = urllib.parse.urlparse(image_url).path.lower()
        suffix = next((ext for ext in (".jpg", ".jpeg", ".png", ".webp") if path.endswith(ext)), ".jpg")
        caption = (
            "🇸🇾 *سوريا مباشر*\n\n"
            f"*{title[:250]}*\n\n"
            f"{clean_text(item.get('description', ''))[:300]}\n\n"
            f"🗺️ المحافظة: {item.get('province') or classify_province(title)}\n"
            f"📰 المصدر: {clean_text(item.get('source') or item.get('feed') or 'المصدر الأصلي')[:80]}\n"
            f"🔗 {item.get('url', '')}"
        )[:1000]
        result, error = green_request("sendFileByUrl", {
            "chatId": chat_id,
            "urlFile": image_url,
            "fileName": "syria-mubasher" + suffix,
            "caption": caption,
        })
        if isinstance(result, dict) and isinstance(result.get("idMessage"), str) and result["idMessage"]:
            return result["idMessage"], "image", ""
        print(f"WhatsApp image send failed; falling back to text. Reason: {error or 'invalid_response'}", file=sys.stderr)
    result, error = green_request("sendMessage", {"chatId": chat_id, "message": message, "linkPreview": True})
    if isinstance(result, dict) and isinstance(result.get("idMessage"), str) and result["idMessage"]:
        return result["idMessage"], "text", ""
    return "", "", error or "invalid_response"


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    old_news = load_json(NEWS_FILE, {"updated_at": "", "items": []})
    state = load_json(STATE_FILE, {"published_urls": [], "whatsapp_sent_urls": [], "feed_errors": []})
    published_urls_list = list(state.get("published_urls", []))
    wa_sent_urls_list = list(state.get("whatsapp_sent_urls", []))
    # Earlier script versions treated queue acceptance as successful delivery. Re-check those URLs once.
    if int(state.get("whatsapp_tracking_version", 0) or 0) < WHATSAPP_TRACKING_VERSION:
        wa_sent_urls_list = []
    published_urls = set(published_urls_list)
    wa_sent_urls = set(wa_sent_urls_list)
    pending = state.get("whatsapp_pending", {})
    if not isinstance(pending, dict):
        pending = {}
    old_items = old_news.get("items", []) if isinstance(old_news, dict) else []
    errors = []
    candidates = []
    user_agent = "Mozilla/5.0 (compatible; SyriaMubasherNewsBot/1.1)"

    for feed_label, query in FEEDS:
        feed_url = "https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(query) + "&hl=ar&gl=SY&ceid=SY%3Aar"
        request = urllib.request.Request(feed_url, headers={"User-Agent": user_agent, "Accept": "application/rss+xml, application/xml, text/xml"})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read(2_000_000)
            candidates.extend(parse_feed(raw, feed_label))
            time.sleep(0.12)
        except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as exc:
            errors.append(f"{feed_label}: {type(exc).__name__}")

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
    run_now = datetime.now(timezone.utc)
    previous_seen_dt = parse_iso_datetime(state.get("newest_seen_at", ""))
    cutoff_dt = previous_seen_dt - timedelta(minutes=30) if previous_seen_dt else run_now - timedelta(hours=6)
    published_dates = [parse_iso_datetime(item.get("published_at", "")) for item in unique]
    published_dates = [value for value in published_dates if value is not None]
    latest_feed_date = max(published_dates) if published_dates else previous_seen_dt
    newest_seen_dt = max([value for value in (previous_seen_dt, latest_feed_date) if value is not None], default=None)
    fresh = []
    old_titles = {canonical_title(item.get("title", "")) for item in old_items}
    for item in unique:
        item_dt = parse_iso_datetime(item.get("published_at", ""))
        if item_dt is None or item_dt < cutoff_dt:
            continue
        if item["url"] in published_urls or canonical_title(item["title"]) in old_titles:
            continue
        fresh.append(item)
        if len(fresh) >= MAX_NEW_PER_CYCLE:
            break

    changed_news = False
    if fresh:
        existing_by_url = {item.get("url"): item for item in old_items if item.get("url")}
        for item in fresh:
            existing_by_url[item["url"]] = item
            if item["url"] not in published_urls:
                published_urls.add(item["url"])
                published_urls_list.append(item["url"])
        new_items = list(existing_by_url.values())
        new_items.sort(key=lambda item: item.get("published_at", ""), reverse=True)
        old_news["items"] = new_items[:MAX_SITE_NEWS]
        changed_news = True

    # Backfill governorate categories and article photos for older items already on the website.
    image_lookups = 0
    for item in old_news.get("items", [])[:MAX_SITE_NEWS]:
        if not item.get("province"):
            item["province"] = classify_province(item.get("title", ""), item.get("description", ""))
            changed_news = True
        if not item.get("image_url") and image_lookups < MAX_IMAGE_LOOKUPS_PER_CYCLE:
            image_lookups += 1
            picture = extract_article_image(item.get("url", ""))
            if picture:
                item["image_url"] = picture
                changed_news = True

    if changed_news:
        old_news["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old_news.setdefault("items", [])
    old_news.setdefault("updated_at", "")

    base, instance, token, chat_id = green_api_config()
    configured = all((base, instance, token, chat_id))
    instance_status = get_instance_state() if configured else "not_configured"
    delivered_count = 0
    accepted_count = 0
    failed_count = 0

    if configured and instance_status == "authorized":
        # Verify queue IDs on later runs instead of falsely equating API acceptance with delivery.
        for item_url, record in list(pending.items()):
            message_id = str(record.get("id_message", ""))
            record_chat = str(record.get("chat_id", chat_id))
            if not message_id:
                pending.pop(item_url, None)
                continue
            status_message, error = read_message_status(record_chat, message_id)
            if status_message in ("delivered", "read"):
                wa_sent_urls.add(item_url)
                wa_sent_urls_list.append(item_url)
                pending.pop(item_url, None)
                delivered_count += 1
            elif status_message in ("sent", "pending"):
                record["status"] = status_message
                record["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            elif status_message == "failed":
                pending.pop(item_url, None)
                failed_count += 1
                print("WhatsApp delivery status: failed; item will be retried.")
            elif error not in ("", "http_400"):
                print(f"WhatsApp status check failed: {error}")

        attempts_this_run = 0
        for item in old_news.get("items", [])[:MAX_SITE_NEWS]:
            item_url = item.get("url", "")
            if not item_url or item_url in wa_sent_urls or item_url in pending:
                continue
            message_id, kind, error = post_whatsapp(item)
            attempts_this_run += 1
            if message_id:
                pending[item_url] = {
                    "id_message": message_id,
                    "chat_id": chat_id,
                    "title": clean_text(item.get("title", ""))[:200],
                    "kind": kind,
                    "status": "pending",
                    "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                accepted_count += 1
                print(f"WhatsApp API accepted one {kind} message; waiting for delivery confirmation.")
            else:
                failed_count += 1
                print(f"WhatsApp send failed: {error}")
            if attempts_this_run >= MAX_NEW_PER_CYCLE:
                break
    elif configured:
        print(f"WhatsApp sending skipped because Green-API instance state is {instance_status!r}; no message marked as delivered.")
    else:
        print("WhatsApp sending skipped: one or more required GitHub Actions secrets are missing.")

    state_out = {
        "published_urls": list(dict.fromkeys(published_urls_list))[-MAX_HISTORY:],
        "whatsapp_sent_urls": list(dict.fromkeys(wa_sent_urls_list))[-MAX_HISTORY:],
        "whatsapp_pending": pending,
        "whatsapp_tracking_version": WHATSAPP_TRACKING_VERSION,
        "newest_seen_at": newest_seen_dt.isoformat(timespec="seconds") if newest_seen_dt else state.get("newest_seen_at", ""),
        "feed_errors": errors,
    }
    NEWS_FILE.write_text(json.dumps(old_news, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STATE_FILE.write_text(json.dumps(state_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Sources checked: {len(FEEDS)}; accepted feed entries: {len(candidates)}; "
        f"new items: {len(fresh)}; website total: {len(old_news.get('items', []))}; "
        f"images looked up: {image_lookups}; WhatsApp instance: {instance_status}; "
        f"API accepted: {accepted_count}; delivered/read confirmed: {delivered_count}; "
        f"delivery pending: {len(pending)}; send/status errors: {failed_count}; feed errors: {len(errors)}"
    )


if __name__ == "__main__":
    main()
