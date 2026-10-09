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
MAX_IMAGE_LOOKUPS_PER_CYCLE = 8
IMAGE_LOOKUP_VERSION = 4
MAX_DESCRIPTION_LOOKUPS_PER_CYCLE = 3
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
DIRECT_FEEDS = [
    ("عنب بلدي RSS", "https://www.enabbaladi.net/feed/"),
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
        self.fallback = ""

    def handle_starttag(self, tag, attrs):
        attrs = {str(k).lower(): str(v) for k, v in attrs if k and v}
        if tag.lower() == "meta" and not self.image:
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
            if key in ("og:image", "og:image:url", "twitter:image", "twitter:image:src", "image"):
                value = (attrs.get("content") or "").strip()
                if value:
                    self.image = value
        elif tag.lower() == "img" and not self.fallback:
            classes = (attrs.get("class") or "").lower()
            if any(bad in classes for bad in ("logo", "avatar", "icon", "pixel", "emoji")):
                return
            width = attrs.get("width", "")
            height = attrs.get("height", "")
            try:
                if width and int(re.sub(r"[^0-9]", "", width) or "0") < 260:
                    return
                if height and int(re.sub(r"[^0-9]", "", height) or "0") < 140:
                    return
            except ValueError:
                pass
            self.fallback = attrs.get("data-src") or attrs.get("data-lazy-src") or attrs.get("src") or ""


class ArticleDescriptionParser(HTMLParser):
    """Extract article metadata descriptions and readable opening paragraphs."""
    def __init__(self):
        super().__init__()
        self.meta_description = ""
        self.paragraphs = []
        self._in_paragraph = False
        self._paragraph = []

    def handle_starttag(self, tag, attrs):
        attrs = {str(k).lower(): str(v) for k, v in attrs if k and v}
        if tag.lower() == "meta" and not self.meta_description:
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key in ("description", "og:description", "twitter:description"):
                self.meta_description = clean_text(attrs.get("content", ""))
        elif tag.lower() == "p":
            self._in_paragraph = True
            self._paragraph = []

    def handle_data(self, data):
        if self._in_paragraph:
            self._paragraph.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "p" and self._in_paragraph:
            value = clean_text(" ".join(self._paragraph))
            if len(value.split()) >= 8 and len(value) >= 60:
                self.paragraphs.append(value)
            self._in_paragraph = False
            self._paragraph = []


def extract_article_description(article_url: str) -> str:
    if not article_url or (urllib.parse.urlparse(article_url).hostname or "").lower().endswith("news.google.com"):
        return ""
    request = urllib.request.Request(article_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(request, timeout=7) as response:
            if "html" not in response.headers.get("Content-Type", "").lower():
                return ""
            raw = response.read(600_000).decode("utf-8", errors="replace")
        parser = ArticleDescriptionParser()
        parser.feed(raw)
        for candidate in [parser.meta_description] + parser.paragraphs[:4]:
            candidate = clean_text(candidate)
            if len(candidate.split()) >= 20:
                return candidate[:700]
        if parser.paragraphs:
            return max(parser.paragraphs, key=lambda value: len(value.split()))[:700]
        return parser.meta_description[:700]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return ""


def minimum_description(item: dict, extracted: str = "") -> str:
    current = clean_text(str(item.get("description", "")))
    title = clean_text(str(item.get("title", "")))
    extracted = clean_text(extracted)
    if len(current.split()) >= 20:
        return current[:700]
    if len(extracted.split()) >= 20:
        return extracted[:700]
    base = extracted if len(extracted) > len(current) else current
    if not base:
        base = title
    extra = " الوصف المتاح مختصر ولا يكفي وحده لعرض جميع التفاصيل؛ يُرجى فتح رابط المصدر الأصلي لقراءة المعلومات الكاملة والتحقق من السياق كما نشره المصدر."
    result = clean_text(base + extra)
    while len(result.split()) < 20:
        result = clean_text(result + " وتبقى التفاصيل مرتبطة بما ورد في المادة المنشورة.")
    return result[:700]

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
    for child in node.iter():
        if child is node:
            continue
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


def extract_markup_image(markup: str, base_url: str = "") -> str:
    if not markup:
        return ""
    parser = ImageMetaParser()
    try:
        parser.feed(html.unescape(markup))
    except Exception:
        return ""
    return normalize_image_url(parser.image or parser.fallback, base_url)


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
        description_markup = item_text(node, "description") or item_text(node, "summary") or item_text(node, "content")
        description = clean_text(description_markup)
        pubdate = item_text(node, "pubDate") or item_text(node, "published") or item_text(node, "updated")
        source = feed_label
        source_home = ""
        for child in list(node):
            if child.tag.rsplit("}", 1)[-1].lower() == "source":
                if (child.text or "").strip():
                    source = clean_text(child.text)
                source_home = child.attrib.get("url", "") or source_home
                if source_home or (child.text or "").strip():
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
            "source_home": source_home,
            "feed": feed_label,
            "published_at": parse_date(pubdate),
            "province": classify_province(title, description),
        }
        picture = image_from_feed_item(node, link) or extract_markup_image(description_markup, link)
        # Many publishers put the featured photo in content:encoded rather than description.
        if not picture:
            for part in list(node):
                local_name = part.tag.rsplit("}", 1)[-1].lower()
                if local_name in ("encoded", "content", "summary", "description", "fulltext"):
                    picture = extract_markup_image(part.text or "", link)
                    if picture:
                        break
        if picture:
            item["image_url"] = picture
        output.append(item)
    return output


def source_home_for_item(item: dict) -> str:
    known = [
        (("سانا", "وكالة الأنباء السورية", "sana"), "https://sana.sy/"),
        (("عنب بلدي", "enab baladi"), "https://www.enabbaladi.net/"),
        (("تلفزيون سوريا", "syria.tv"), "https://www.syria.tv/"),
        (("الوطن", "alwatan"), "https://alwatan.sy/"),
        (("زمان الوصل", "zaman al wasl"), "https://www.zamanalwsl.net/"),
        (("سوريا دايركت", "syria direct"), "https://syriadirect.org/"),
        (("أورينت", "orient"), "https://orient-news.net/"),
        (("syrian observer",), "https://syrianobserver.com/"),
    ]
    label = (str(item.get("source", "")) + " " + str(item.get("feed", ""))).casefold()
    for names, home in known:
        if any(name.casefold() in label for name in names):
            return home
    return str(item.get("source_home", "") or "")


class LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = ""
        self._text = []

    def handle_starttag(self, tag, attrs):
        attrs = {str(k).lower(): str(v) for k, v in attrs if k and v}
        if tag.lower() == "link":
            rel = (attrs.get("rel") or "").lower()
            if "canonical" in rel and attrs.get("href"):
                self.links.append((attrs["href"], "canonical"))
        elif tag.lower() == "meta" and (attrs.get("http-equiv") or "").lower() == "refresh":
            content = attrs.get("content", "")
            match = re.search(r"url\s*=\s*['\"]?([^'\"]+)", content, re.I)
            if match:
                self.links.append((match.group(1), "refresh"))
        elif tag.lower() == "a" and attrs.get("href"):
            self._href = attrs["href"]
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, clean_text(" ".join(self._text))))
            self._href = ""
            self._text = []


def resolve_publisher_url(article_url: str, source_home: str = "", article_title: str = "") -> str:
    parsed = urllib.parse.urlparse(article_url or "")
    host = (parsed.hostname or "").lower()
    if not host or not host.endswith("news.google.com"):
        return article_url
    req = urllib.request.Request(article_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            final_url = response.geturl()
            final_host = (urllib.parse.urlparse(final_url).hostname or "").lower()
            content_type = response.headers.get("Content-Type", "")
            if final_host and not final_host.endswith("google.com") and "html" in content_type.lower():
                return final_url
            raw = response.read(700_000).decode("utf-8", errors="replace") if "html" in content_type.lower() else ""
        if not raw:
            return article_url
        source_host = (urllib.parse.urlparse(source_home).hostname or "").lower()
        # Google's newer article pages keep the publisher URL in data-n-au (not an <a href>).
        # Extract that exact article target before considering any other links on the page.
        raw_unescaped = html.unescape(raw).replace("\\/", "/")
        raw_unescaped = raw_unescaped.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\u003f", "?")
        embedded_targets = []
        for attr in ("data-n-au", "data-article-url", "data-original-url"):
            embedded_targets.extend(re.findall(rf'{attr}\s*=\s*["\']([^"\']+)["\']', raw_unescaped, flags=re.I))
        for embedded in embedded_targets:
            embedded = urllib.parse.unquote(html.unescape(embedded.strip()))
            embedded = embedded.replace("\\/", "/")
            candidate = urllib.parse.urljoin(final_url, embedded)
            p = urllib.parse.urlparse(candidate)
            candidate_host = (p.hostname or "").lower()
            same_source = bool(source_host and (
                candidate_host == source_host
                or candidate_host.endswith("." + source_host)
                or source_host.endswith("." + candidate_host)
            ))
            path_parts = [part.lower() for part in p.path.strip("/").split("/") if part]
            blocked_parts = ("category", "tag", "author", "page", "contact", "about", "search", "governorates")
            if p.scheme in ("http", "https") and candidate_host and same_source and path_parts and not any(part in blocked_parts for part in path_parts):
                print(f"Publisher URL extracted from Google News article metadata: {candidate_host}.")
                return candidate
        collector = LinkCollector()
        collector.feed(raw)
        candidates = []
        for href, label in collector.links:
            candidate = urllib.parse.urljoin(final_url, html.unescape(href.strip()))
            p = urllib.parse.urlparse(candidate)
            candidate_host = (p.hostname or "").lower()
            if p.scheme not in ("http", "https") or not candidate_host:
                continue
            if candidate_host.endswith(("google.com", "googleusercontent.com", "gstatic.com", "youtube.com")):
                continue
            if candidate_host in ("facebook.com", "instagram.com", "x.com", "twitter.com", "linkedin.com", "t.me"):
                continue
            path_parts = [part.lower() for part in p.path.strip("/").split("/") if part]
            if any(part in ("governorates", "category", "tag", "author", "page", "contact", "about") for part in path_parts):
                continue
            if source_host and (candidate_host == source_host or candidate_host.endswith("." + source_host)) and not p.path.strip("/"):
                continue
            score = 0
            if source_host and (candidate_host == source_host or candidate_host.endswith("." + source_host)):
                score += 100
            if p.path.strip("/"):
                score += min(len(p.path.strip("/").split("/")), 4) * 4
            if len(p.path.strip("/")) > 18:
                score += 8
            if any(word in p.path.lower() for word in ("article", "news", "story", "post")):
                score += 4
            if label == "canonical":
                score += 12
            if label == "refresh":
                score += 20
            if candidate.rstrip("/") == source_home.rstrip("/"):
                score -= 30
            candidates.append((score, candidate))
        if candidates:
            candidates.sort(reverse=True)
            best_score, best_url = candidates[0]
            if best_score >= 12:
                print(f"Publisher link resolved: {source_host or 'Google News'} -> {urllib.parse.urlparse(best_url).hostname}.")
                return best_url
        # Fallback: search the publisher's own latest-news page for a matching headline.
        if source_home and article_title:
            search_url = source_home.rstrip("/") + "/?s=" + urllib.parse.quote_plus(article_title)
            home_req = urllib.request.Request(search_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
            })
            try:
                with urllib.request.urlopen(home_req, timeout=8) as home_response:
                    home_url = home_response.geturl()
                    home_raw = home_response.read(900_000).decode("utf-8", errors="replace")
                home_links = LinkCollector()
                home_links.feed(home_raw)
                title_key = canonical_title(article_title)
                title_words = {word for word in re.findall(r"[\w\u0600-\u06ff]+", article_title.casefold()) if len(word) > 2}
                best = (0.0, "")
                for href, label in home_links.links:
                    candidate = urllib.parse.urljoin(home_url, html.unescape(href.strip()))
                    p = urllib.parse.urlparse(candidate)
                    candidate_host = (p.hostname or "").lower()
                    source_host = (urllib.parse.urlparse(source_home).hostname or "").lower()
                    if p.scheme not in ("http", "https") or not candidate_host:
                        continue
                    if candidate_host.endswith(("google.com", "googleusercontent.com", "gstatic.com", "youtube.com")):
                        continue
                    if candidate_host in ("facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com", "x.com", "twitter.com", "linkedin.com", "t.me"):
                        continue
                    if source_host and candidate_host != source_host and not candidate_host.endswith("." + source_host):
                        continue
                    path_parts = [part.lower() for part in p.path.strip("/").split("/") if part]
                    if any(part in ("governorates", "category", "tag", "author", "page", "contact", "about") for part in path_parts):
                        continue
                    if not p.path.strip("/") or p.path.rstrip("/") == urllib.parse.urlparse(home_url).path.rstrip("/"):
                        continue
                    label_key = canonical_title(label)
                    if len(label_key) < 12:
                        continue
                    label_words = set(re.findall(r"[\w\u0600-\u06ff]+", label.casefold()))
                    overlap = len(title_words.intersection(label_words)) / max(1, len(title_words))
                    if title_key and label_key and (title_key in label_key or label_key in title_key):
                        overlap = max(overlap, 0.9)
                    if overlap > best[0]:
                        best = (overlap, candidate)
                if best[0] >= 0.7:
                    print(f"Publisher headline matched on its website: {urllib.parse.urlparse(best[1]).hostname}.")
                    return best[1]
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                pass
        # Last fallback: scan the publisher homepage's current story links. Search endpoints
        # are not consistently implemented across news sites, but homepage cards often include
        # both the exact headline and its canonical article URL.
        if source_home and article_title:
            try:
                home_req = urllib.request.Request(source_home, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml",
                })
                with urllib.request.urlopen(home_req, timeout=7) as home_response:
                    home_url = home_response.geturl()
                    home_raw = home_response.read(1_200_000).decode("utf-8", errors="replace")
                home_links = LinkCollector()
                home_links.feed(home_raw)
                clean_title = re.sub(r"\s*[-–—|]\s*(وكالة الأنباء السورية.*|سانا.*|sana.*|تلفزيون سوريا.*|عنب بلدي.*|زمان الوصل.*|الوطن.*|أورينت.*|syrian observer.*)$", "", article_title, flags=re.I)
                title_key = canonical_title(clean_title)
                title_words = {word for word in re.findall(r"[\w\u0600-\u06ff]+", clean_title.casefold()) if len(word) > 2}
                best = (0.0, "")
                source_host = (urllib.parse.urlparse(source_home).hostname or "").lower()
                for href, label in home_links.links:
                    candidate = urllib.parse.urljoin(home_url, html.unescape(href.strip()))
                    p = urllib.parse.urlparse(candidate)
                    candidate_host = (p.hostname or "").lower()
                    if p.scheme not in ("http", "https") or not candidate_host:
                        continue
                    if source_host and candidate_host != source_host and not candidate_host.endswith("." + source_host):
                        continue
                    if any(host in candidate_host for host in ("google.com", "googleusercontent.com", "gstatic.com", "youtube.com")):
                        continue
                    path_parts = [part.lower() for part in p.path.strip("/").split("/") if part]
                    if not path_parts or any(part in ("category", "tag", "author", "page", "contact", "about", "search") for part in path_parts):
                        continue
                    label_key = canonical_title(label)
                    if len(label_key) < 12:
                        continue
                    label_words = {word for word in re.findall(r"[\w\u0600-\u06ff]+", label.casefold()) if len(word) > 2}
                    overlap = len(title_words.intersection(label_words)) / max(1, len(title_words))
                    if title_key and label_key and (title_key in label_key or label_key in title_key):
                        overlap = max(overlap, 0.95)
                    if overlap > best[0]:
                        best = (overlap, candidate)
                if best[0] >= 0.72:
                    print(f"Publisher headline matched on homepage: {urllib.parse.urlparse(best[1]).hostname}.")
                    return best[1]
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                pass
        print("Publisher link could not be resolved from Google News page.")
        return article_url
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        print("Publisher link resolution failed; keeping original source link.")
        return article_url


def is_generic_image_url(image_url: str) -> bool:
    parsed = urllib.parse.urlparse(image_url or "")
    filename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]).casefold()
    stem = re.sub(r"\.[a-z0-9]{2,5}$", "", filename)
    generic_names = {
        "دمشق", "الحسكة", "حلب", "حمص", "حماة", "ادلب", "إدلب", "طرطوس",
        "اللاذقية", "درعا", "الرقة", "دير الزور", "السويداء", "القنيطرة",
        "damascus", "aleppo", "homs", "hama", "idlib", "latakia", "tartous",
        "hasakah", "raqqa", "daraa", "syria", "default", "placeholder",
    }
    return stem in generic_names or "placeholder" in stem or "default" in stem



def extract_wordpress_featured_image(article_url: str) -> str:
    """Use a publisher's public WordPress API when its article HTML blocks server-side requests."""
    parsed = urllib.parse.urlparse(article_url or "")
    host = (parsed.hostname or "").lower()
    if not (host == "enabbaladi.net" or host.endswith(".enabbaladi.net")):
        return ""
    match = re.search(r"/(\d{4,})(?:/|$)", parsed.path)
    if not match:
        return ""
    post_id = match.group(1)
    api_hosts = list(dict.fromkeys([host, "www.enabbaladi.net", "enabbaladi.net"]))
    for api_host in api_hosts:
        api_url = f"https://{api_host}/wp-json/wp/v2/posts/{post_id}?_embed=1"
        request = urllib.request.Request(api_url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; SyriaMubasherNewsBot/1.0)",
            "Accept": "application/json",
            "Referer": "https://" + api_host + "/",
        })
        try:
            with urllib.request.urlopen(request, timeout=7) as response:
                raw = response.read(1_500_000).decode("utf-8", errors="replace")
            post = json.loads(raw)
            if isinstance(post, list):
                post = post[0] if post else {}
            if not isinstance(post, dict) or post.get("code"):
                continue
            candidates = []
            for key in ("jetpack_featured_media_url", "featured_image_url"):
                if isinstance(post.get(key), str):
                    candidates.append(post[key])
            featured = post.get("better_featured_image")
            if isinstance(featured, dict):
                candidates.append(featured.get("source_url", ""))
            yoast = post.get("yoast_head_json")
            if isinstance(yoast, dict):
                for image in yoast.get("og_image", []) if isinstance(yoast.get("og_image"), list) else []:
                    if isinstance(image, dict):
                        candidates.append(image.get("url", ""))
            embedded = post.get("_embedded")
            if isinstance(embedded, dict):
                media_items = embedded.get("wp:featuredmedia", [])
                if isinstance(media_items, list):
                    for media in media_items:
                        if not isinstance(media, dict):
                            continue
                        candidates.append(media.get("source_url", ""))
                        sizes = media.get("media_details", {}).get("sizes", {})
                        if isinstance(sizes, dict):
                            for size_name in ("large", "medium_large", "full", "medium"):
                                size = sizes.get(size_name)
                                if isinstance(size, dict):
                                    candidates.append(size.get("source_url", ""))
            for candidate in candidates:
                value = normalize_image_url(str(candidate or ""))
                if value and not is_generic_image_url(value):
                    print(f"Publisher featured image found via WordPress API: {api_host}.")
                    return value
            content = post.get("content", {})
            if isinstance(content, dict):
                value = extract_markup_image(str(content.get("rendered", "")), article_url)
                if value and not is_generic_image_url(value):
                    print(f"Publisher image found in WordPress content API: {api_host}.")
                    return value
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            continue
    # Some publishers block GitHub runner IPs, including their public REST API.
    # Jina Reader provides a text rendering of the same exact article; use only its
    # embedded image URLs, then still validate and mirror the actual image bytes.
    reader_target = urllib.parse.urlunparse((parsed.scheme or "https", parsed.netloc, parsed.path, "", "", ""))
    reader_url = "https://r.jina.ai/http://" + urllib.parse.urlparse(reader_target).netloc + urllib.parse.urlparse(reader_target).path
    request = urllib.request.Request(reader_url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; SyriaMubasherImageResolver/1.0)",
        "Accept": "text/plain,text/markdown,text/html,*/*",
        "X-Return-Format": "html",
    })
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            reader_content = response.read(1_000_000).decode("utf-8", errors="replace")
        reader_parser = ImageMetaParser()
        reader_parser.feed(reader_content)
        candidates = [reader_parser.image, reader_parser.fallback]
        candidates.extend(re.findall(r'!\[[^\]]*\]\((https?://[^)\s]+)', reader_content))
        candidates.extend(re.findall(r'https?://[^\s"<>]+?\.(?:jpe?g|png|webp)(?:\?[^\s"<>)]*)?', reader_content, flags=re.I))
        for candidate in candidates:
            picture = normalize_image_url(str(candidate or ""), article_url)
            if picture and not is_generic_image_url(picture):
                image_host = (urllib.parse.urlparse(picture).hostname or "").lower()
                if image_host and not any(blocked in image_host for blocked in ("google.com", "googleusercontent.com", "gstatic.com")):
                    print(f"Publisher image found through article-reader fallback: {image_host}.")
                    return picture
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
        pass
    print("Publisher WordPress API and article-reader image fallbacks were unavailable.")
    return ""


def extract_article_image(article_url: str, source_home: str = "", article_title: str = "") -> str:
    if not article_url.startswith(("https://", "http://")):
        return ""
    resolved_url = article_url
    resolved_host = (urllib.parse.urlparse(resolved_url).hostname or "").lower()
    if resolved_host.endswith("news.google.com"):
        print("Image lookup skipped: Google News supplied no verifiable publisher article URL.")
        return ""
    original_host = urllib.parse.urlparse(resolved_url).hostname or "unknown"
    req = urllib.request.Request(resolved_url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            final_url = response.geturl()
            final_host = urllib.parse.urlparse(final_url).hostname or original_host
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                print(f"Image lookup skipped: page from {final_host} was not HTML.")
                return ""
            raw = response.read(700_000).decode("utf-8", errors="replace")
        parser = ImageMetaParser()
        parser.feed(raw)
        picture = normalize_image_url(parser.image or parser.fallback, final_url)
        if picture and is_generic_image_url(picture):
            picture = ""
            print(f"Generic region image rejected from {final_host}.")
        else:
            print(f"Article image {'found' if picture else 'not found'} from {final_host}.")
        return picture
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 429):
            api_picture = extract_wordpress_featured_image(article_url)
            if api_picture:
                return api_picture
        print(f"Article image lookup returned HTTP {exc.code} from {original_host}.")
        return ""
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        print(f"Article image lookup could not connect to {original_host}.")
        return ""

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def canonical_title(text: str) -> str:
    value = (text or "").casefold()
    value = re.sub(r"\s*[-–—|]\s*(وكالة الأنباء السورية.*|سانا.*|sana.*|تلفزيون سوريا.*|عنب بلدي.*|زمان الوصل.*|الوطن.*|أورينت.*|syrian observer.*)$", "", value, flags=re.I)
    value = re.sub(r"^(عاجل|خبر عاجل)\s*[:：-]?\s*", "", value)
    return re.sub(r"[^\w\u0600-\u06ff]+", "", value)


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
            raw_error = exc.read(2000).decode("utf-8", errors="replace")
            try:
                error_body = json.loads(raw_error)
            except (ValueError, TypeError):
                error_body = {}
        except Exception:
            raw_error = ""
            error_body = {}
        if isinstance(error_body, dict):
            kind = str(error_body.get("error", "") or error_body.get("message", "") or "")
        elif isinstance(error_body, str):
            kind = error_body
        else:
            kind = raw_error
        kind = html.unescape(kind)
        kind = re.sub(r"<[^>]*>", " ", kind)
        kind = re.sub(r"https?://\S+", "[url]", kind)
        kind = re.sub(r"waInstance[^/ ]+/[^/ ]+", "[redacted]", kind)
        kind = re.sub(r"[^A-Za-z0-9 _.-]", " ", kind)
        kind = re.sub(r"\s+", " ", kind).strip()[:80]
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

def validate_whatsapp_group(chat_id: str) -> tuple[bool, str]:
    result, error = green_request("getGroupData", {"groupId": chat_id})
    if error:
        return False, error
    if not isinstance(result, dict):
        return False, "invalid_response"
    resolved_id = str(result.get("groupId", ""))
    return resolved_id == chat_id, "" if resolved_id == chat_id else "group_id_mismatch"


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
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

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

    for feed_label, feed_url in DIRECT_FEEDS:
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
    title_indexes = {}
    for item in candidates:
        url = item["url"].split("&oc=5", 1)[0]
        title_key = canonical_title(item["title"])
        item["url"] = url
        if url in used_urls:
            continue
        if title_key and title_key in title_indexes:
            existing = unique[title_indexes[title_key]]
            if not existing.get("image_url") and item.get("image_url"):
                existing["image_url"] = item["image_url"]
            old_host = urllib.parse.urlparse(existing.get("url", "")).hostname or ""
            new_host = urllib.parse.urlparse(item.get("url", "")).hostname or ""
            if old_host.endswith("news.google.com") and new_host and not new_host.endswith("news.google.com"):
                existing["url"] = item["url"]
                existing["source"] = item.get("source") or existing.get("source")
                existing["feed"] = item.get("feed") or existing.get("feed")
                if item.get("image_url"):
                    existing["image_url"] = item["image_url"]
            continue
        used_urls.add(url)
        if title_key:
            title_indexes[title_key] = len(unique)
        unique.append(item)

    unique.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    # Enrich already-published Google News wrappers with the matching publisher URL and its RSS thumbnail.
    unique_by_title = {canonical_title(item.get("title", "")): item for item in unique if canonical_title(item.get("title", ""))}
    existing_changes = False
    for old_item in old_items:
        key = canonical_title(old_item.get("title", ""))
        match = unique_by_title.get(key)
        if not match:
            continue
        if not old_item.get("image_url") and match.get("image_url"):
            old_item["image_url"] = match["image_url"]
            existing_changes = True
        old_host = (urllib.parse.urlparse(old_item.get("url", "")).hostname or "").lower()
        new_host = (urllib.parse.urlparse(match.get("url", "")).hostname or "").lower()
        social_hosts = ("facebook.com", "instagram.com", "x.com", "twitter.com", "linkedin.com", "t.me")
        old_path_parts = [part.lower() for part in urllib.parse.urlparse(old_item.get("url", "")).path.strip("/").split("/") if part]
        old_link_is_wrapper = (
            old_host.endswith("news.google.com")
            or any(old_host == domain or old_host.endswith("." + domain) for domain in social_hosts)
            or any(part in ("governorates", "category", "tag", "author", "page", "contact", "about") for part in old_path_parts)
        )
        new_link_is_social = any(new_host == domain or new_host.endswith("." + domain) for domain in social_hosts)
        if old_link_is_wrapper and new_host and not new_link_is_social:
            old_item["url"] = match["url"]
            old_item["source"] = match.get("source") or old_item.get("source")
            old_item["feed"] = match.get("feed") or old_item.get("feed")
            if match.get("description") and len(match.get("description", "")) > len(old_item.get("description", "")):
                old_item["description"] = match["description"][:700]
            existing_changes = True
    run_now = datetime.now(timezone.utc)
    previous_seen_dt = parse_iso_datetime(state.get("newest_seen_at", ""))
    # Allow recovery from delayed or skipped RSS cycles while avoiding a large historical backfill.
    cutoff_dt = run_now - timedelta(hours=24)
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

    changed_news = existing_changes
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

    # Backfill province labels and give image lookup failures a 24-hour cooldown.
    image_lookup_attempts = state.get("image_lookup_attempts", {})
    if not isinstance(image_lookup_attempts, dict):
        image_lookup_attempts = {}
    if int(state.get("image_lookup_version", 0) or 0) < IMAGE_LOOKUP_VERSION:
        # Reset stale failure cooldowns after changing image extraction fallbacks.
        image_lookup_attempts = {}
    image_lookups = 0
    images_added = 0
    description_lookups = 0
    descriptions_expanded = 0
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    image_frequency = {}
    for entry in old_news.get("items", []):
        image_value = entry.get("image_url", "")
        if image_value:
            image_frequency[image_value] = image_frequency.get(image_value, 0) + 1
    for entry in old_news.get("items", []):
        image_value = entry.get("image_url", "")
        image_host = (urllib.parse.urlparse(image_value).hostname or "").lower()
        if image_frequency.get(image_value, 0) > 1 and image_host.endswith("googleusercontent.com"):
            entry.pop("image_url", None)
            changed_news = True
    for item in old_news.get("items", [])[:MAX_SITE_NEWS]:
        item_url = item.get("url", "")
        # Migrate away from previously cached local photos and retry their original source URLs.
        if str(item.get("image_url", "") or "").startswith("images/news/"):
            item.pop("image_url", None)
            image_lookup_attempts.pop(item_url, None)
            changed_news = True
        if item.get("image_url") and is_generic_image_url(item.get("image_url", "")):
            item.pop("image_url", None)
            changed_news = True
        if not item.get("province"):
            item["province"] = classify_province(item.get("title", ""), item.get("description", ""))
            changed_news = True
        source_home = source_home_for_item(item)
        if item_url:
            resolved_url = resolve_publisher_url(item_url, source_home, item.get("title", ""))
            if resolved_url and resolved_url != item_url:
                item["url"] = resolved_url
                item_url = resolved_url
                changed_news = True
        description = clean_text(item.get("description", ""))
        if len(description.split()) < 20:
            extracted_description = ""
            if item_url and description_lookups < MAX_DESCRIPTION_LOOKUPS_PER_CYCLE:
                description_lookups += 1
                extracted_description = extract_article_description(item_url)
            expanded_description = minimum_description(item, extracted_description)
            if expanded_description != item.get("description", ""):
                item["description"] = expanded_description
                descriptions_expanded += 1
                changed_news = True
        item_host = (urllib.parse.urlparse(item_url).hostname or "").lower()
        last_image_attempt = parse_iso_datetime(image_lookup_attempts.get(item_url, ""))
        # Google News wrapper URLs cannot provide article images directly. Do not consume
        # the per-cycle lookup budget or cache a 24-hour failure for these wrappers.
        should_try_image = not item.get("image_url") and item_host and not item_host.endswith("news.google.com") and (
            last_image_attempt is None or datetime.now(timezone.utc) - last_image_attempt >= timedelta(hours=12)
        )
        if should_try_image and image_lookups < MAX_IMAGE_LOOKUPS_PER_CYCLE:
            image_lookups += 1
            image_lookup_attempts[item_url] = now_iso
            picture = extract_article_image(item_url, source_home, item.get("title", ""))
            if picture:
                item["image_url"] = picture
                images_added += 1
                changed_news = True
        # Keep publisher image URLs as links; never download or save image copies in this repository.

    # Avoid associating one publisher article URL with two different headlines.
    seen_article_urls = {}
    for item in old_news.get("items", []):
        item_url = item.get("url", "")
        title_key = canonical_title(item.get("title", ""))
        if item_url and item_url in seen_article_urls and seen_article_urls[item_url] != title_key:
            alternative = unique_by_title.get(title_key)
            if alternative and alternative.get("url") and alternative.get("url") != item_url:
                item["url"] = alternative["url"]
            item.pop("image_url", None)
            changed_news = True
            print("Duplicate article URL detected across different headlines; kept the original feed link for the second story.")
        elif item_url:
            seen_article_urls[item_url] = title_key

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

    whatsapp_attempt_counts = state.get("whatsapp_attempt_counts", {})
    if not isinstance(whatsapp_attempt_counts, dict):
        whatsapp_attempt_counts = {}
    max_whatsapp_attempts = 3
    if configured and instance_status == "authorized":
        group_valid, group_error = validate_whatsapp_group(chat_id)
        fatal_group_error = any(reason in group_error.lower() for reason in ("forbidden", "item-not-found", "group_id_mismatch"))
        if not group_valid and group_error.startswith("http_500") and not fatal_group_error:
            # If lookup has an unexplained 500, make a limited send attempt and track its delivery.
            print("WhatsApp group lookup returned an unexplained HTTP 500; allowing limited send attempts with delivery checks.")
            group_valid = True
        if not group_valid:
            if "forbidden" in group_error.lower():
                print("WhatsApp group check failed: this Green-API instance is not a member of the target group.")
            elif "item-not-found" in group_error.lower():
                print("WhatsApp group check failed: the configured group ID was not found by Green-API.")
            else:
                print(f"WhatsApp group check failed: {group_error or 'group_id_mismatch'}. Sending paused to avoid targeting the wrong chat.")
        else:
            if not group_error:
                print("WhatsApp group check: valid.")
            # Verify queue IDs on later runs instead of falsely equating API acceptance with delivery.
            for item_url, record in list(pending.items()):
                message_id = str(record.get("id_message", ""))
                record_chat = str(record.get("chat_id", chat_id))
                if record_chat != chat_id:
                    # A group was changed in secrets; never mark a message for the old group as delivered to the new one.
                    pending.pop(item_url, None)
                    whatsapp_attempt_counts.pop(item_url, None)
                    pending_title = canonical_title(str(record.get("title", "")))
                    if pending_title:
                        for current_item in old_news.get("items", []):
                            if canonical_title(str(current_item.get("title", ""))) == pending_title:
                                current_url = str(current_item.get("url", ""))
                                whatsapp_attempt_counts.pop(current_url, None)
                                wa_sent_urls.discard(current_url)
                                wa_sent_urls_list = [sent_url for sent_url in wa_sent_urls_list if sent_url != current_url]
                                break
                    print("WhatsApp target group changed; cleared an old pending record so its story can be retried for the configured group.")
                    continue
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
                    whatsapp_attempt_counts[item_url] = max(int(whatsapp_attempt_counts.get(item_url, 0) or 0), int(record.get("attempts", 1) or 1))
                    pending.pop(item_url, None)
                    failed_count += 1
                    print("WhatsApp delivery status: failed; item will be retried within the attempt limit.")
                elif error.startswith("http_400:Message not found by id"):
                    queued_at = parse_iso_datetime(record.get("queued_at", ""))
                    age = (datetime.now(timezone.utc) - queued_at).total_seconds() if queued_at else 0
                    if age >= 180:
                        whatsapp_attempt_counts[item_url] = max(int(whatsapp_attempt_counts.get(item_url, 0) or 0), int(record.get("attempts", 1) or 1))
                        pending.pop(item_url, None)
                        failed_count += 1
                        print("WhatsApp cannot find the accepted message in its history after 3 minutes; it will be retried within the attempt limit.")
                    else:
                        print("WhatsApp message is not yet visible in delivery history; will check again.")
                elif error:
                    print(f"WhatsApp status check failed: {error}")

            attempts_this_run = 0
            for item in old_news.get("items", [])[:MAX_SITE_NEWS]:
                item_url = item.get("url", "")
                attempts_for_item = int(whatsapp_attempt_counts.get(item_url, 0) or 0)
                if not item_url or item_url in wa_sent_urls or item_url in pending or attempts_for_item >= max_whatsapp_attempts:
                    continue
                message_id, kind, error = post_whatsapp(item)
                attempts_this_run += 1
                if message_id:
                    attempts_for_item += 1
                    whatsapp_attempt_counts[item_url] = attempts_for_item
                    pending[item_url] = {
                        "id_message": message_id,
                        "chat_id": chat_id,
                        "title": clean_text(item.get("title", ""))[:200],
                        "kind": kind,
                        "attempts": attempts_for_item,
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
        "whatsapp_attempt_counts": whatsapp_attempt_counts,
        "image_lookup_attempts": image_lookup_attempts,
        "image_lookup_version": IMAGE_LOOKUP_VERSION,
        "newest_seen_at": newest_seen_dt.isoformat(timespec="seconds") if newest_seen_dt else state.get("newest_seen_at", ""),
        "feed_errors": errors,
    }
    NEWS_FILE.write_text(json.dumps(old_news, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    STATE_FILE.write_text(json.dumps(state_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Sources checked: {len(FEEDS) + len(DIRECT_FEEDS)}; accepted feed entries: {len(candidates)}; "
        f"new items: {len(fresh)}; website total: {len(old_news.get('items', []))}; "
        f"RSS items with images: {sum(1 for x in candidates if x.get('image_url'))}; "
        f"image lookups: {image_lookups}; images added: {images_added}; image storage: disabled; "
        f"description lookups: {description_lookups}; descriptions expanded: {descriptions_expanded}; "
        f"WhatsApp instance: {instance_status}; API accepted: {accepted_count}; "
        f"delivered/read confirmed: {delivered_count}; delivery pending: {len(pending)}; "
        f"send/status errors: {failed_count}; feeds failed: {len(errors)}"
    )


if __name__ == "__main__":
    main()
