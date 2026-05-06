"""FastAPI server: article extraction via news-please (fhamborg/news-please).

API shape matches ``/home/tss/scraper`` so ``TssScraperClient`` can use the same
endpoints (``/health``, ``/crawl``, ``/source``).
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import pathlib
import re
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from newspaper.configuration import Configuration
import newspaper
from newsplease import NewsPlease
from newsplease.body_markdown import extract_body_rich, markdown_has_embedded_media
from pydantic import BaseModel, HttpUrl
from requests.adapters import HTTPAdapter
from starlette.concurrency import run_in_threadpool
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
}
_REQUEST_TIMEOUT = int(os.environ.get("SCRAPER_REQUEST_TIMEOUT", "30"))

_JS_COOKIE_CHALLENGE_RE = re.compile(
    r'document\.cookie\s*=\s*"([^=]+)=([a-f0-9A-F]+)"',
    re.IGNORECASE,
)

_MEDIA_DIR = pathlib.Path(os.environ.get("MEDIA_STORAGE_PATH", "./medias")).resolve()
_CDN_BASE_URL = os.environ.get("CDN_MEDIA_BASE_URL", "https://sapbao.local/cdn-medias").rstrip("/")
_MAX_MEDIA_SIZE = int(os.environ.get("MAX_MEDIA_SIZE_BYTES", str(50 * 1024 * 1024)))

_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "application/x-mpegURL": ".m3u8",
    "application/vnd.apple.mpegurl": ".m3u8",
}

_MD_IMAGE_RE = re.compile(r"(!\[[^\]]*\])\(([^)\s]+)\)")
_HTML_IMG_SRC_RE = re.compile(r'(<img\b[^>]*\bsrc=["\'])([^"\']+)(["\'])', re.IGNORECASE)
_PHOTO_CREDIT_RE = re.compile(
    r"(?:^|[\s\"'“”‘’(])(?:Ảnh|Photo)\s*:\s*([^\n.]{2,120})",
    re.IGNORECASE,
)
_MD_AUTHOR_RE = re.compile(r"^\s*(?:tác giả|author)\s*:\s*.+$", re.IGNORECASE)
_MD_DATETIME_RE = re.compile(
    r"^\s*\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2})?(?:\s*\(gmt\s*[+-]?\s*\d+\s*\))?\s*$",
    re.IGNORECASE,
)
_MD_SECTION_CUT_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:tag|tags|tin mới nhất|tin mới|đọc nhiều|xem thêm|tin liên quan|cùng chuyên mục|tin cùng chuyên mục)\s*:?\s*$",
    re.IGNORECASE,
)
_MD_SOURCE_PREFIX_RE = re.compile(
    r"^\s*(?:[A-Z0-9À-ỴĐ][A-Z0-9À-ỴĐ.\-]{1,30})\s*-\s*",
    re.IGNORECASE,
)
_MD_SOURCE_PREFIX_WITH_HEADING_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:[A-Z0-9À-ỴĐ][A-Z0-9À-ỴĐ.\-]{1,30})\s*-\s*",
    re.IGNORECASE,
)
_MD_UPPER_SECTION_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?[A-ZÀ-ỴĐ0-9][A-ZÀ-ỴĐ0-9\s/&\-]{2,40}\s*$",
)
_PLAIN_SOURCE_PREFIX_RE = re.compile(
    r"(?:^|\s)(?:[A-Z0-9À-ỴĐ][A-Z0-9À-ỴĐ.\-]{1,30})\s*-\s*",
    re.IGNORECASE,
)
_PLAIN_CUT_RE = re.compile(
    r"(?:\bTin liên quan\b|\bTag\s*:|\bMời quý độc giả theo dõi\b)",
    re.IGNORECASE,
)

_http_adapter = HTTPAdapter(
    pool_connections=20,
    pool_maxsize=50,
    max_retries=Retry(total=0),
)
_media_session = requests.Session()
_media_session.headers.update(_HEADERS)
_media_session.mount("http://", _http_adapter)
_media_session.mount("https://", _http_adapter)

# --- source heuristics (aligned with ``scraper/main.py``) ---
_SRC_NON_ARTICLE = re.compile(
    r"/(tag|tags|tu-khoa|label|labels"
    r"|topic|topics|chu-de|chuyen-de"
    r"|category|cat|danh-muc|chuyen-muc"
    r"|author|authors|tac-gia|user|profile"
    r"|search|tim-kiem|keyword"
    r"|page|trang"
    r"|epaper|e-paper|bao-in|archive|luu-tru"
    r"|gallery|video|photo|anh|infographic"
    r"|rss|feed|amp)"
    r"(/|$|\?|#)",
    re.IGNORECASE,
)
_SRC_ASSET_EXT = re.compile(
    r"\.(css|js|png|jpg|jpeg|gif|webp|svg|ico|pdf|zip|tar|gz|xml|json|rss|atom)(\?|$)",
    re.IGNORECASE,
)
_SRC_ARTICLE_ID = re.compile(r"\d{4,}")
_SRC_ARTICLE_SIGNAL = re.compile(
    r"(post\d+|\d{6,}\.html?|\d{6,}\.tpo|\d{5,}\.ldo|\d{4}/\d{2}/\d{2})",
    re.IGNORECASE,
)
_SRC_HEX_SUFFIX = re.compile(r"-[0-9a-f]{8,}(\.html?|\.tpo)?$", re.IGNORECASE)
_SRC_FILE_EXT = re.compile(r"\.(html?|tpo|aspx|ldo)$", re.IGNORECASE)


def _registered_domain(netloc: str) -> str:
    host = netloc.lower().lstrip("www.")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _guess_ext(url: str, content_type: str | None) -> str:
    path = urlparse(url).path.split("?")[0]
    ext = os.path.splitext(path)[1].lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".mp4", ".webm", ".ogv", ".mov", ".avi", ".m3u8"}:
        return ext
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in _MIME_TO_EXT:
            return _MIME_TO_EXT[ct]
        guessed = mimetypes.guess_extension(ct)
        if guessed:
            return guessed
    return ".bin"


def _download_media(media_url: str, ref_date: datetime | None = None) -> str | None:
    try:
        url_hash = hashlib.sha256(media_url.encode()).hexdigest()[:20]
        now = ref_date or datetime.now()
        subdir = now.strftime("%Y-%m") + "/" + now.strftime("%d")
        dest_dir = _MEDIA_DIR / subdir
        dest_dir.mkdir(parents=True, exist_ok=True)

        resp = _media_session.get(
            media_url,
            timeout=_REQUEST_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        content_length = int(resp.headers.get("content-length", 0) or 0)
        if content_length and content_length > _MAX_MEDIA_SIZE:
            log.warning("Skipping large media (%d bytes): %s", content_length, media_url)
            return None

        ext = _guess_ext(media_url, content_type)
        filename = f"{url_hash}{ext}"
        filepath = dest_dir / filename
        relative = f"{subdir}/{filename}"

        if not filepath.exists():
            downloaded = 0
            with open(filepath, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65_536):
                    if chunk:
                        downloaded += len(chunk)
                        if downloaded > _MAX_MEDIA_SIZE:
                            fh.close()
                            filepath.unlink(missing_ok=True)
                            log.warning("Aborted oversized download: %s", media_url)
                            return None
                        fh.write(chunk)
            log.info("Downloaded media → %s", relative)

        return f"{_CDN_BASE_URL}/{relative}"

    except Exception as exc:
        log.warning("Failed to download media %s: %s", media_url, exc)
        return None


def _rewrite_markdown_images(
    text_markdown: str,
    ref_date: datetime | None = None,
    base_url: str = "",
    url_cache: dict | None = None,
) -> str:
    def _abs_url(u: str) -> str:
        u = u.strip()
        if u.startswith("//"):
            return "https:" + u
        if u.startswith(("http://", "https://")):
            return u
        if base_url:
            return urljoin(base_url, u)
        return u

    def _replace(m: re.Match) -> str:
        prefix = m.group(1)
        img_url = _abs_url(m.group(2))
        if not img_url.startswith(("http://", "https://")):
            return m.group(0)
        local_url = url_cache.get(img_url) if url_cache is not None else _download_media(img_url, ref_date)
        if local_url:
            return f"{prefix}({local_url})"
        return m.group(0)

    return _MD_IMAGE_RE.sub(_replace, text_markdown)


def _rewrite_html_image_sources(
    text_markdown: str,
    ref_date: datetime | None = None,
    base_url: str = "",
    url_cache: dict | None = None,
) -> str:
    def _abs_url(u: str) -> str:
        u = u.strip()
        if u.startswith("//"):
            return "https:" + u
        if u.startswith(("http://", "https://")):
            return u
        if base_url:
            return urljoin(base_url, u)
        return u

    def _replace(m: re.Match) -> str:
        prefix, img_url, quote = m.group(1), m.group(2), m.group(3)
        abs_url = _abs_url(img_url)
        if not abs_url.startswith(("http://", "https://")):
            return m.group(0)
        local_url = url_cache.get(abs_url) if url_cache is not None else _download_media(abs_url, ref_date)
        if local_url:
            return f"{prefix}{local_url}{quote}"
        return m.group(0)

    return _HTML_IMG_SRC_RE.sub(_replace, text_markdown)


def _fetch_html(url: str, extra_headers: dict | None = None) -> tuple[str, str]:
    session = requests.Session()
    headers = dict(_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    session.headers.update(headers)
    session.mount("http://", _http_adapter)
    session.mount("https://", _http_adapter)

    def _do_get(target: str, **kwargs) -> requests.Response:
        try:
            r = session.get(target, timeout=_REQUEST_TIMEOUT, allow_redirects=True, **kwargs)
        except requests.exceptions.SSLError:
            log.warning("SSL error for %s — retrying with verify=False", target)
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)
                r = session.get(
                    target, timeout=_REQUEST_TIMEOUT, allow_redirects=True, verify=False, **kwargs
                )
        r.raise_for_status()
        r.encoding = "utf-8"
        return r

    resp = _do_get(url)
    if "window.location.reload" in resp.text:
        m = _JS_COOKIE_CHALLENGE_RE.search(resp.text)
        if m:
            key, val = m.group(1), m.group(2)
            log.info("JS cookie challenge for %s — setting %s=%s", url, key, val)
            session.cookies.set(key, val)
            resp = _do_get(str(resp.url))

    return resp.text, str(resp.url)


def _norm_authors(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if x]
    return [str(raw)]


def _publish_date_str(article) -> str | None:
    d = article.date_publish
    if not d:
        return None
    if hasattr(d, "isoformat"):
        return d.isoformat()
    return str(d)


def _build_markdown(description: str | None, maintext: str | None) -> str:
    def _looks_like_subheading(line: str) -> bool:
        if not line:
            return False
        if len(line) > 90:
            return False
        if line[-1] in ".!?;:":
            return False
        if line.count(" ") > 10:
            return False
        return True

    def _maintext_to_md_blocks(raw_text: str) -> str:
        lines = [ln.strip() for ln in raw_text.splitlines()]
        blocks: list[str] = []
        for line in lines:
            if not line:
                continue
            if _looks_like_subheading(line):
                blocks.append(f"## {line}")
            else:
                blocks.append(line)
        return "\n\n".join(blocks)

    del description  # kept for API compatibility; markdown body excludes sapo to avoid duplication in UI.
    m = (maintext or "").strip()
    return _maintext_to_md_blocks(m) if m else ""


def _extract_photo_caption(text: str | None) -> str:
    if not text:
        return ""
    matches = _PHOTO_CREDIT_RE.findall(text)
    if not matches:
        return ""
    cap = matches[-1].strip(" \t\n\r\"'“”‘’()[]")
    return f"Ảnh: {cap}" if cap else ""


def _prepend_description_if_needed(description: str | None, markdown_body: str) -> str:
    del description
    return (markdown_body or "").strip()


def _inject_top_image_near_top(text_markdown: str, top_image: str | None) -> str:
    if not top_image:
        return text_markdown
    body = (text_markdown or "").strip()
    if not body:
        return f'<figure class="image"><img src="{top_image}" alt="" /></figure>'
    if top_image in body:
        return body

    img_block = f'<figure class="image"><img src="{top_image}" alt="" /></figure>'
    lines = body.splitlines()
    insert_at = 0
    if lines and lines[0].strip().startswith("*") and lines[0].strip().endswith("*"):
        insert_at = 1
        while insert_at < len(lines) and not lines[insert_at].strip():
            insert_at += 1

    prefix = "\n".join(lines[:insert_at]).strip()
    suffix = "\n".join(lines[insert_at:]).strip()
    if prefix:
        return f"{prefix}\n\n{img_block}\n\n{suffix}" if suffix else f"{prefix}\n\n{img_block}"
    return f"{img_block}\n\n{suffix}" if suffix else img_block


def _dedupe_description_paragraphs(text_markdown: str, description: str | None) -> str:
    body = (text_markdown or "").strip()
    desc = (description or "").strip()
    if not body or not desc:
        return body

    def _norm_para(p: str) -> str:
        cleaned = p.strip()
        cleaned = _MD_SOURCE_PREFIX_WITH_HEADING_RE.sub("", cleaned)
        cleaned = re.sub(r"^[*_`>\-\s]+", "", cleaned)
        cleaned = re.sub(r"[*_`]+$", "", cleaned)
        cleaned = re.sub(r"\W+", "", cleaned.lower(), flags=re.UNICODE)
        return cleaned

    desc_norm = _norm_para(desc)
    if not desc_norm:
        return body

    parts = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    kept: list[str] = []
    for part in parts:
        if _norm_para(part) == desc_norm:
            continue
        kept.append(part.strip())
    return "\n\n".join(kept).strip()


def _clean_markdown_noise(text_markdown: str) -> str:
    body = (text_markdown or "").strip()
    if not body:
        return body

    parts = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    cleaned: list[str] = []
    i = 0
    while i < len(parts):
        p = parts[i]
        p_stripped = p.strip()
        if _MD_AUTHOR_RE.match(p) or _MD_DATETIME_RE.match(p):
            i += 1
            continue
        if _MD_SECTION_CUT_RE.match(p):
            break
        if _MD_UPPER_SECTION_RE.match(p_stripped) and len(p_stripped.split()) <= 5:
            i += 1
            continue
        p_no_prefix = _MD_SOURCE_PREFIX_WITH_HEADING_RE.sub("", p_stripped)
        if p_no_prefix != p_stripped:
            p = p_no_prefix
            p_stripped = p_no_prefix
            if not p_stripped:
                i += 1
                continue
            if _MD_SECTION_CUT_RE.match(p_stripped):
                break
        # Common "related cards" tail: markdown image followed by heading teaser.
        if p.startswith("![") and i + 1 < len(parts) and parts[i + 1].startswith("#"):
            title = re.sub(r"^#{1,6}\s*", "", parts[i + 1]).strip().lower()
            alt_match = re.match(r"!\[([^\]]+)\]\(", p)
            alt = (alt_match.group(1).strip().lower() if alt_match else "")
            if title and alt and (title in alt or alt in title):
                break
        # HTML figure + following heading is usually "related article" tail on VOV.
        if "<figure" in p.lower() and i + 1 < len(parts) and re.match(r"^#{3,6}\s+", parts[i + 1]):
            break
        cleaned.append(p)
        i += 1
    return "\n\n".join(cleaned).strip()


def _clean_plain_text_noise(text: str | None) -> str:
    body = (text or "").strip()
    if not body:
        return body
    body = re.sub(r"\s+", " ", body).strip()
    body = re.sub(r"^\s*[A-ZÀ-ỴĐ0-9][A-ZÀ-ỴĐ0-9\s/&\-]{2,40}\s+", "", body)
    body = _PLAIN_SOURCE_PREFIX_RE.sub(" ", body)
    cut = _PLAIN_CUT_RE.search(body)
    if cut:
        body = body[:cut.start()]
    return re.sub(r"\s+", " ", body).strip()


def _looks_like_image_url(url: str | None, page_url: str = "") -> bool:
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return False
    if page_url and u.rstrip("/") == page_url.rstrip("/"):
        return False
    lower = u.lower().split("?")[0]
    if re.search(r"\.(jpg|jpeg|png|gif|webp|avif|svg)$", lower):
        return True
    return any(token in lower for token in ("/images/", "/image/", "/photo/", "cdn", "/upload", "media"))


def _remove_trailing_figure_if_any(md: str) -> str:
    text = (md or "").rstrip(" \t\r\n\u200b\u200c\u200d\ufeff")
    if "<figure" not in text:
        return text
    matches = list(re.finditer(r"<figure\b[^>]*>.*?</figure>", text, re.IGNORECASE | re.DOTALL))
    if not matches:
        return text
    m = matches[-1]
    after = text[m.end():].strip()
    if after:
        return text
    before = text[:m.start()].rstrip()
    # Keep a single trailing figure when the article is effectively media-only.
    if not before or len(before) < 200:
        return text
    return before


class ArticleResponse(BaseModel):
    url: str
    title: str
    authors: list[str]
    publish_date: Optional[str]
    text: str
    text_markdown: str
    top_image: Optional[str]
    images: list[str]
    movies: list[str]
    meta_keywords: list[str]
    tags: list[str]
    meta_description: Optional[str]
    meta_lang: Optional[str]
    source_url: Optional[str]
    article_type: str


class CrawlRequest(BaseModel):
    url: HttpUrl
    language: Optional[str] = None
    follow_meta_refresh: bool = False
    keep_article_html: bool = False
    download_media: bool = False


class SourceResponse(BaseModel):
    url: str
    domain: str
    article_urls: list[str]
    total: int


def _crawl_np(
    url: str,
    language: Optional[str] = None,
    follow_meta_refresh: bool = False,
    keep_article_html: bool = False,
    download_media: bool = False,
) -> ArticleResponse:
    del follow_meta_refresh, keep_article_html  # API parity with scraper; unused here.

    # Single HTTP GET via ``_fetch_html``, then ``NewsPlease.from_html`` (fork adds
    # ``maintext_markdown`` / ``body_image_urls`` inside from_html — no duplicate DOM walk here).
    extra_headers: dict[str, str] = {}
    if language:
        extra_headers["Accept-Language"] = f"{language},en;q=0.8"

    request_args: dict = {"timeout": _REQUEST_TIMEOUT}
    if language:
        request_args.setdefault("headers", {})["Accept-Language"] = f"{language},en;q=0.8"

    html = ""
    final_page_url = url
    try:
        html, final_page_url = _fetch_html(
            url, extra_headers=extra_headers if extra_headers else None
        )
    except Exception as exc:
        log.warning("HTML fetch failed for %s: %s", url, exc)

    download_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    article = None
    if html:
        r = NewsPlease.from_html(
            html, url=final_page_url, download_date=download_date, fetch_images=True
        )
        if not isinstance(r, dict):
            article = r

    if article is None:
        article = NewsPlease.from_url(url, request_args=request_args, fetch_images=True)
        if article is not None and not isinstance(article, dict):
            final_page_url = article.url or url
            html = ""

    if article is None or isinstance(article, dict):
        raise HTTPException(status_code=422, detail="news-please could not extract this URL")

    title = (article.title or "").strip()
    maintext = (article.maintext or "").strip()
    np_md = (getattr(article, "maintext_markdown", None) or "").strip()
    if not title and not maintext and not (np_md and markdown_has_embedded_media(np_md)):
        raise HTTPException(
            status_code=422,
            detail="Empty extraction (no title, body, or rich maintext_markdown)",
        )

    authors = _norm_authors(article.authors)
    publish_date = _publish_date_str(article)
    description = article.description
    text = article.maintext or ""
    text_markdown = _build_markdown(description, article.maintext)
    np_plain = (getattr(article, "maintext_body_plain", None) or "").strip()
    body_imgs = list(getattr(article, "body_image_urls", None) or [])
    html_plain = ""
    html_md = ""
    html_imgs: list[str] = []
    if html:
        try:
            html_plain, html_md, html_imgs = extract_body_rich(html, base_url=final_page_url)
        except Exception as exc:
            log.warning("extract_body_rich failed for %s: %s", final_page_url, exc)

    if np_md and markdown_has_embedded_media(np_md):
        text_markdown = _prepend_description_if_needed(description, np_md)
        if np_plain:
            text = np_plain
    elif np_plain and not (article.maintext or "").strip():
        text = np_plain
        if np_md:
            head = _build_markdown(description, None)
            text_markdown = (head + "\n\n" + np_md) if head else np_md

    # Fallback for pages where extractor markdown is polluted or misses article media.
    if html_md and markdown_has_embedded_media(html_md):
        no_md_media = not (np_md and markdown_has_embedded_media(np_md))
        no_body_imgs = not body_imgs
        if no_md_media or no_body_imgs:
            text_markdown = _prepend_description_if_needed(description, html_md)
            if html_plain and (not text or len(html_plain) >= max(300, len(text) // 2)):
                text = html_plain
            if html_imgs:
                body_imgs = html_imgs

    top_image = article.image_url or None
    if top_image and not _looks_like_image_url(top_image, final_page_url):
        top_image = None
    images: list[str] = []
    if top_image:
        images.append(top_image)
    movies: list[str] = []

    for u in body_imgs:
        if u and _looks_like_image_url(u, final_page_url) and u not in images:
            images.append(u)

    if not markdown_has_embedded_media(text_markdown):
        text_markdown = _inject_top_image_near_top(text_markdown, top_image)
    text_markdown = _dedupe_description_paragraphs(text_markdown, description)
    text_markdown = _clean_markdown_noise(text_markdown)
    text_markdown = _remove_trailing_figure_if_any(text_markdown)
    text = _clean_plain_text_noise(text)

    media_date = article.date_publish or datetime.now()
    if download_media:
        _to_download: set[str] = set()
        if top_image and top_image.startswith(("http://", "https://")):
            _to_download.add(top_image)
        if text_markdown:
            def _abs_md(u: str) -> str:
                u = u.strip()
                if u.startswith("//"):
                    return "https:" + u
                if u.startswith(("http://", "https://")):
                    return u
                return urljoin(final_page_url, u)

            for _m in _MD_IMAGE_RE.finditer(text_markdown):
                _u = _abs_md(_m.group(2))
                if _u.startswith(("http://", "https://")):
                    _to_download.add(_u)
            for _m in _HTML_IMG_SRC_RE.finditer(text_markdown):
                _u = _abs_md(_m.group(2))
                if _u.startswith(("http://", "https://")):
                    _to_download.add(_u)

        url_cache: dict[str, str | None] = {}
        if _to_download:
            _workers = min(len(_to_download), int(os.environ.get("MEDIA_WORKERS", "8")))
            with ThreadPoolExecutor(max_workers=_workers) as ex:
                futs = {ex.submit(_download_media, u, media_date): u for u in _to_download}
                for fut in as_completed(futs):
                    url_cache[futs[fut]] = fut.result()

        if top_image and url_cache.get(top_image):
            top_image = url_cache[top_image]
        if text_markdown:
            text_markdown = _rewrite_markdown_images(
                text_markdown, media_date, base_url=final_page_url, url_cache=url_cache
            )
            text_markdown = _rewrite_html_image_sources(
                text_markdown, media_date, base_url=final_page_url, url_cache=url_cache
            )
        images = [
            (url_cache.get(img) or img) if img and str(img).startswith(("http://", "https://")) else img
            for img in images
        ]

    return ArticleResponse(
        url=final_page_url,
        title=title,
        authors=authors,
        publish_date=publish_date,
        text=text,
        text_markdown=text_markdown,
        top_image=top_image,
        images=[x for x in images if x],
        movies=movies,
        meta_keywords=[],
        tags=[],
        meta_description=description,
        meta_lang=article.language,
        source_url=article.url,
        article_type="news-please",
    )


app = FastAPI(
    title="Article Crawler API (news-please)",
    description="Extract news articles using fhamborg/news-please",
    version="1.0.0",
)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/crawl", response_model=ArticleResponse, summary="Crawl article from URL (news-please)")
async def crawl_get(
    url: str = Query(..., description="URL of the article to crawl"),
    language: Optional[str] = Query(None, description="Language hint for HTTP Accept-Language"),
    follow_meta_refresh: bool = Query(False, description="Ignored (API parity with tss-scraper)"),
    keep_article_html: bool = Query(False, description="Ignored (API parity with tss-scraper)"),
    download_media: bool = Query(False, description="Download media to local CDN and rewrite URLs"),
):
    return await run_in_threadpool(
        _crawl_np,
        url,
        language=language,
        follow_meta_refresh=follow_meta_refresh,
        keep_article_html=keep_article_html,
        download_media=download_media,
    )


@app.post("/crawl", response_model=ArticleResponse, summary="Crawl article from URL (news-please)")
async def crawl_post(body: CrawlRequest):
    return await run_in_threadpool(
        _crawl_np,
        str(body.url),
        language=body.language,
        follow_meta_refresh=body.follow_meta_refresh,
        keep_article_html=body.keep_article_html,
        download_media=body.download_media,
    )


@app.get("/source", response_model=SourceResponse, summary="Discover article URLs from a homepage")
def scrape_source(
    url: str = Query(..., description="Homepage or source URL to discover articles from"),
    language: Optional[str] = Query(None, description="Language code, e.g. 'vi', 'en'"),
    only_in_path: bool = Query(False, description="Only include article URLs within the same URL path"),
):
    def is_article_url(candidate: str, base_reg_domain: str) -> bool:
        try:
            p = urlparse(candidate)
        except Exception:
            return False
        if p.scheme not in ("http", "https"):
            return False
        if _registered_domain(p.netloc) != base_reg_domain:
            return False
        path = p.path.rstrip("/")
        if not path or path == "/":
            return False
        if path.count("/") < 1 or len(path) < 10:
            return False
        if _SRC_ASSET_EXT.search(path):
            return False
        if _SRC_NON_ARTICLE.search(path):
            return False
        if _SRC_ARTICLE_SIGNAL.search(path) or _SRC_HEX_SUFFIX.search(path):
            return True
        if _SRC_FILE_EXT.search(path) and len(path) >= 30:
            return True
        if path.count("/") >= 2 and _SRC_ARTICLE_ID.search(path):
            return True
        return False

    try:
        config = Configuration()
        config.browser_user_agent = _BROWSER_UA
        config.headers = _HEADERS
        if language:
            config.language = language
        config.memoize_articles = False

        source = newspaper.build(
            url,
            config=config,
            only_homepage=True,
            only_in_path=only_in_path,
        )

        parsed_base = urlparse(url)
        base_reg = _registered_domain(parsed_base.netloc)

        seen: set[str] = set()
        article_urls: list[str] = []

        def _add(u: str) -> None:
            clean = u.split("#")[0].rstrip("/")
            if clean and clean not in seen and is_article_url(clean, base_reg):
                seen.add(clean)
                article_urls.append(clean)

        for a in source.articles:
            if a.url:
                _add(a.url)

        html = getattr(source, "html", "") or ""
        if len(html) < 500:
            log.info("Minimal HTML (%d bytes) for %s — _fetch_html fallback", len(html), url)
            try:
                html, _ = _fetch_html(url)
            except Exception as e:
                log.warning("_fetch_html fallback failed for %s: %s", url, e)
                html = ""
        if html:
            soup = BeautifulSoup(html, "lxml")
            for tag in soup.find_all("a", href=True):
                href = (tag.get("href") or "").strip()
                if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    continue
                _add(urljoin(url, href))

        return SourceResponse(
            url=url,
            domain=parsed_base.netloc,
            article_urls=article_urls,
            total=len(article_urls),
        )
    except Exception as exc:
        log.exception("Failed to scrape source %s", url)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True)
