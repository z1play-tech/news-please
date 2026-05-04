# -*- coding: utf-8 -*-
"""
Rich article body as markdown/HTML snippets (figures with captions, inline images).

Aligned with TSS ``scraper/main.py`` heuristics (e.g. VTC ``.edittor-content``).
Used from ``NewsPlease.from_html`` after the extractor pipeline — single HTML pass.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

_NOISE_CLASS_RE = re.compile(r"related|sidebar|footer|widget|ad|social|comment", re.I)


def _img_resolved_url(img_tag, base_url: str) -> Optional[str]:
    def _pick_from_srcset(srcset_value: str) -> Optional[str]:
        for part in srcset_value.split(","):
            token = (part or "").strip().split(" ")[0].strip()
            if token and not token.lower().startswith("data:"):
                return token
        return None

    attrs_order = (
        "data-src",
        "data-original",
        "data-lazy-src",
        "data-url",
        "data-srcset",
        "srcset",
        "src",
    )
    raw: Optional[str] = None
    for key in attrs_order:
        val = (img_tag.get(key) or "").strip()
        if not val or val.lower().startswith("data:"):
            continue
        if key in ("data-srcset", "srcset"):
            picked = _pick_from_srcset(val)
            if not picked:
                continue
            raw = picked
            break
        raw = val
        break
    if not raw:
        picture = img_tag.find_parent("picture")
        if picture:
            for source in picture.find_all("source"):
                srcset = (source.get("srcset") or source.get("data-srcset") or "").strip()
                if not srcset:
                    continue
                picked = _pick_from_srcset(srcset)
                if picked:
                    raw = picked
                    break
    if not raw:
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    elif not raw.startswith(("http://", "https://")):
        raw = urljoin(base_url, raw)
    return raw


def extract_body_rich(html: str, base_url: str = "") -> Tuple[str, str, List[str]]:
    """
    :return: (plain_text_from_body_blocks, markdown_with_figures_and_images, http_image_urls_in_order)
    """
    soup = BeautifulSoup(html, "lxml")
    parts: List[str] = []
    body_images: List[str] = []
    seen_img: set = set()

    def _note_img(u: Optional[str]) -> None:
        if u and u.startswith(("http://", "https://")) and u not in seen_img:
            seen_img.add(u)
            body_images.append(u)

    lead = soup.find(class_="chappeau") or soup.find(class_="article__sapo") or soup.find(class_="sapo")
    if lead:
        parts.append(lead.get_text(" ", strip=True))

    body = (
        soup.find(class_="article__body")
        or soup.find(class_="article-body")
        or soup.find(class_="detail__content")
        or soup.find(class_="article__content")
        or soup.find(class_="edittor-content")
        or soup.find(class_="editor-content")
        or soup.find(class_="fck_detail")
        or soup.find(class_="entry-content")
        or soup.find(class_="post-content")
        or soup.find(class_="content-wrapper")
    )

    text = ""
    text_markdown = ""
    if body:
        for noise in body.find_all(class_=_NOISE_CLASS_RE):
            noise.decompose()
        body_text = body.get_text(" ", strip=True)
        if body_text:
            parts.append(body_text)
        text = "\n\n".join(parts)

        md_parts: List[str] = []
        if lead:
            md_parts.append(f"_{lead.get_text(' ', strip=True)}_")

        body_md_parts: List[str] = []
        for el in body.find_all(
            ["p", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "figure", "img"],
            recursive=True,
        ):
            name = el.name.lower() if el.name else ""

            if name == "figure":
                img = el.find("img")
                if img:
                    src = _img_resolved_url(img, base_url)
                    if src:
                        _note_img(src)
                        alt = html_lib.escape((img.get("alt") or "").strip(), quote=True)
                        caption_tag = el.find("figcaption")
                        caption_text = caption_tag.get_text(" ", strip=True) if caption_tag else ""
                        caption_html = (
                            f"<figcaption>{html_lib.escape(caption_text)}</figcaption>"
                            if caption_text else ""
                        )
                        body_md_parts.append(
                            f'<figure class="image"><img src="{html_lib.escape(src, quote=True)}" alt="{alt}" />{caption_html}</figure>'
                        )
                continue

            if name == "img":
                if el.find_parent("figure"):
                    continue
                src = _img_resolved_url(el, base_url)
                if src:
                    _note_img(src)
                    alt = (el.get("alt") or "").strip()
                    body_md_parts.append(f"![{alt}]({src})")
                continue

            if el.find_parent(["p", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "figure"]):
                continue

            text_line = el.get_text(" ", strip=True)
            if text_line:
                body_md_parts.append(text_line)

        if body_md_parts:
            md_parts.append("\n\n".join(body_md_parts))
        else:
            body_md = body.get_text(" ", strip=True)
            if body_md:
                md_parts.append(body_md)
        if not md_parts and parts:
            md_parts = parts[:]
        text_markdown = "\n\n".join(md_parts)
        for img in body.find_all("img", recursive=True):
            _note_img(_img_resolved_url(img, base_url))
    else:
        text = "\n\n".join(parts)
        if lead:
            text_markdown = f"_{lead.get_text(' ', strip=True)}_"

    return text, text_markdown, body_images


def markdown_has_embedded_media(md: str) -> bool:
    if not md:
        return False
    md_lower = md.lower()
    return (
        "<figure" in md
        or ("![" in md and "](" in md)
        or ("<img" in md_lower and "src=" in md_lower)
    )
