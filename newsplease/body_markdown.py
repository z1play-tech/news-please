# -*- coding: utf-8 -*-
"""
Rich article body as markdown/HTML snippets (figures with captions, inline images).

Targets common Vietnamese / CMS layouts; preserves ``<strong>`` / ``<em>`` as markdown.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import List, Optional, Set, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

_NOISE_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(related|sidebar|footer|widget|social|comment|advert|ads?|banner)(?:$|[\s_-])",
    re.I,
)

# CSS ``select_one`` order: more specific first.
_BODY_ROOT_SELECTORS: Tuple[str, ...] = (
    "article .the-article-body",
    ".the-article-body",
    "article .the-article-content",
    ".the-article-content",
    "article .edittor-content",
    ".edittor-content",
    "article .article__body",
    ".article__body",
    "article .detail__content",
    ".detail__content",
    "article .article__content",
    ".article__content",
    "article .fck_detail",
    ".fck_detail",
    "article .entry-content",
    ".entry-content",
    "article .post-content",
    ".post-content",
    "article .detail-content",
    ".detail-content",
    "article .content-detail",
    ".content-detail",
    "article .detail_cms",
    ".detail_cms",
    "article .cms-body",
    ".cms-body",
    "#divNewsContent",
    ".knc-content",
    ".tm-article-body",
    "article .content-wrapper",
    ".content-wrapper",
    "article[itemtype*='Article']",
    "article",
)

_CAPTION_CLASS_RE = re.compile(r"caption|imageinfo|image-info|credit|photo__caption|mmcaption|pic__", re.I)
_IMG_NOISE_RE = re.compile(
    r"(logo|icon|sprite|avatar|tracking|google-news|google_news|1x1|pixel)",
    re.I,
)
_SECTION_STOP_RE = re.compile(
    r"^(tin liên quan|xem thêm|đọc thêm|bạn có thể quan tâm|cùng chuyên mục|tin cùng chuyên mục)$",
    re.I,
)


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
        "data-lazy",
        "data-url",
        "data-image",
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


def _inline_md_fragment(node) -> str:
    if isinstance(node, NavigableString):
        return str(node).replace("\xa0", " ")
    if not isinstance(node, Tag):
        return ""
    name = (node.name or "").lower()
    if name in ("script", "style", "noscript"):
        return ""
    if name == "br":
        return "\n"
    if name in ("strong", "b"):
        inner = "".join(_inline_md_fragment(c) for c in node.children).strip()
        return f"**{inner}**" if inner else ""
    if name in ("em", "i"):
        inner = "".join(_inline_md_fragment(c) for c in node.children).strip()
        return f"*{inner}*" if inner else ""
    if name == "a":
        return "".join(_inline_md_fragment(c) for c in node.children)
    return "".join(_inline_md_fragment(c) for c in node.children)


def _p_to_md(p: Tag) -> str:
    raw = "".join(_inline_md_fragment(c) for c in p.children)
    return " ".join(raw.split()).strip()


def _heading_to_md(el: Tag) -> str:
    name = (el.name or "h2").lower()
    level = int(name[1]) if len(name) > 1 and name[0] == "h" and name[1].isdigit() else 2
    level = max(2, min(level, 6))
    hashes = "#" * level
    t = el.get_text(" ", strip=True)
    return f"{hashes} {t}".strip() if t else ""


def _table_to_md(table: Tag) -> str:
    rows: List[List[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        row: List[str] = []
        for cell in cells:
            txt = cell.get_text(" ", strip=True)
            txt = " ".join(txt.split())
            txt = txt.replace("|", "\\|")
            row.append(txt)
        if any(c for c in row):
            rows.append(row)

    if not rows:
        return ""

    max_cols = max(len(r) for r in rows)
    for r in rows:
        if len(r) < max_cols:
            r.extend([""] * (max_cols - len(r)))

    first_row_has_th = bool(rows and table.find("tr") and table.find("tr").find("th"))
    header = rows[0] if first_row_has_th else []
    data_rows = rows[1:] if first_row_has_th else rows

    # Heuristic: many news tables use row 1 as title and row 2 as real header.
    if not first_row_has_th and len(rows) >= 2:
        second = [c.strip().lower() for c in rows[1]]
        if any("thời gian" in c for c in second) and any("trận" in c for c in second):
            header = rows[1]
            data_rows = rows[2:]
    if not header:
        header = [f"Cột {i+1}" for i in range(max_cols)]

    # Emit HTML table instead of Markdown table because some downstream renderers
    # do not enable GFM table extension and would show raw pipes as plain text.
    thead_cells = "".join(f"<th>{html_lib.escape(c)}</th>" for c in header)
    body_rows = []
    for row in data_rows:
        tds = "".join(f"<td>{html_lib.escape(c)}</td>" for c in row)
        body_rows.append(f"<tr>{tds}</tr>")
    tbody = "".join(body_rows)
    return f"<table><thead><tr>{thead_cells}</tr></thead><tbody>{tbody}</tbody></table>"


def _find_body_root(soup: BeautifulSoup):
    def _looks_like_cookie_box(el: Tag) -> bool:
        txt = el.get_text(" ", strip=True)
        if not txt:
            return True
        low = txt.lower()
        return ("chúng tôi sử dụng cookie" in low and len(txt) < 500)

    for sel in _BODY_ROOT_SELECTORS:
        try:
            el = soup.select_one(sel)
        except ValueError:
            continue
        if el and el.get_text(strip=True) and not _looks_like_cookie_box(el):
            return el
    return (
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


def _div_is_media_wrapper(div: Tag, body_root: Tag) -> bool:
    if div is body_root:
        return False
    cls = " ".join(div.get("class") or [])
    if not div.find("img"):
        return False
    # Narrow: avoid treating the whole article column as a "media" div.
    if re.search(
        r"photo|picture|zoom|VCSortable|align|embed|thumb|wp-caption|MMC|Image|VNE|cms-img|img-b",
        cls,
        re.I,
    ):
        return True
    if "sortable" in cls.lower():
        return True
    return False


def _caption_for_media_div(div: Tag) -> str:
    cap_el = div.find(class_=_CAPTION_CLASS_RE)
    if cap_el:
        return cap_el.get_text(" ", strip=True)
    for child in div.children:
        if isinstance(child, Tag) and child.name in ("p", "span", "div"):
            cl = " ".join(child.get("class") or [])
            if _CAPTION_CLASS_RE.search(cl) or "caption" in cl.lower():
                return child.get_text(" ", strip=True)
    ns = div.find_next_sibling()
    if isinstance(ns, Tag) and ns.name == "p":
        txt = ns.get_text(" ", strip=True)
        if txt and len(txt) < 500:
            return txt
    return ""


def extract_body_rich(html: str, base_url: str = "") -> Tuple[str, str, List[str]]:
    """
    :return: (plain_text_from_body_blocks, markdown_with_figures_and_images, http_image_urls_in_order)
    """
    soup = BeautifulSoup(html, "lxml")
    parts: List[str] = []
    body_images: List[str] = []
    seen_src: Set[str] = set()

    def _note_img(u: Optional[str]) -> None:
        if u and u.startswith(("http://", "https://")) and u not in seen_src:
            seen_src.add(u)
            body_images.append(u)

    def _is_content_image_url(u: str) -> bool:
        low = (u or "").lower()
        if not low.startswith(("http://", "https://")):
            return False
        if _IMG_NOISE_RE.search(low):
            return False
        return True

    def _norm_soft_text(s: str) -> str:
        return re.sub(r"\W+", "", (s or "").lower(), flags=re.UNICODE)

    lead = soup.find(class_="chappeau") or soup.find(class_="article__sapo") or soup.find(class_="sapo")
    if lead:
        parts.append(lead.get_text(" ", strip=True))

    body = _find_body_root(soup)
    text = ""
    text_markdown = ""

    if not body:
        text = "\n\n".join(parts)
        if lead:
            text_markdown = _p_to_md(lead) if lead.name == "p" else f"_{lead.get_text(' ', strip=True)}_"
        return text, text_markdown, body_images

    for noise in body.find_all(class_=_NOISE_CLASS_RE):
        noise.decompose()

    body_text = body.get_text(" ", strip=True)
    if body_text:
        parts.append(body_text)
    text = "\n\n".join(parts)

    lead_inside = bool(lead and lead in body.descendants)
    md_parts: List[str] = []
    lead_norm = ""
    lead_consumed_in_body = False
    if lead:
        if lead.name == "p":
            lead_norm = " ".join(_p_to_md(lead).lower().split())
        else:
            lead_norm = " ".join(lead.get_text(" ", strip=True).lower().split())
    lead_norm_soft = _norm_soft_text(lead_norm)
    if lead and not lead_inside:
        if lead.name == "p":
            lm = _p_to_md(lead)
            if lm:
                md_parts.append(lm)
        else:
            lt = lead.get_text(" ", strip=True)
            if lt:
                md_parts.append(f"_{lt}_")

    body_md_parts: List[str] = []
    emitted_img_src: Set[str] = set()

    def _emit_figure_from_img(img_tag: Tag, caption_text: str = "") -> None:
        src = _img_resolved_url(img_tag, base_url)
        if not src or src in emitted_img_src or not _is_content_image_url(src):
            return
        emitted_img_src.add(src)
        _note_img(src)
        alt = html_lib.escape((img_tag.get("alt") or "").strip(), quote=True)
        cap = caption_text.strip()
        caption_html = f"<figcaption>{html_lib.escape(cap)}</figcaption>" if cap else ""
        body_md_parts.append(
            f'<figure class="image"><img src="{html_lib.escape(src, quote=True)}" alt="{alt}" />{caption_html}</figure>'
        )

    walk_tags = (
        "p",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "blockquote",
        "table",
        "figure",
        "img",
        "div",
    )
    for el in body.find_all(walk_tags, recursive=True):
        name = (el.name or "").lower()
        el_classes = " ".join(el.get("class") or [])

        # Embedded related-story cards frequently appear in-body on Nhandan.
        if "story__thumb" in el_classes or "story__heading" in el_classes:
            continue

        if name == "figure":
            img = el.find("img")
            if img:
                cap_tag = el.find("figcaption")
                cap = cap_tag.get_text(" ", strip=True) if cap_tag else ""
                _emit_figure_from_img(img, cap)
            continue

        if name == "table":
            if el.find_parent("figure"):
                continue
            md_table = _table_to_md(el)
            if md_table:
                body_md_parts.append(md_table)
            continue

        if name == "img":
            if el.find_parent("figure"):
                continue
            pw = el.find_parent("div")
            if pw is not None and _div_is_media_wrapper(pw, body):
                continue
            src = _img_resolved_url(el, base_url)
            if not src:
                continue
            if src in emitted_img_src or not _is_content_image_url(src):
                continue
            emitted_img_src.add(src)
            _note_img(src)
            alt = (el.get("alt") or "").strip()
            body_md_parts.append(f"![{alt}]({src})")
            continue

        if name == "div" and _div_is_media_wrapper(el, body):
            imgs = el.find_all("img", limit=4)
            cap = _caption_for_media_div(el)
            for im in imgs:
                _emit_figure_from_img(im, cap if len(imgs) == 1 else "")
            continue

        if name in ("p", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"):
            if el.find_parent("table"):
                continue
            if el.find_parent("figure"):
                continue
            if el.find_parent(["p", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "figure"]):
                continue
            if name == "p":
                line = _p_to_md(el)
                if line and lead_norm and (not lead_consumed_in_body):
                    line_norm = " ".join(line.lower().split())
                    if line_norm == lead_norm or _norm_soft_text(line_norm) == lead_norm_soft:
                        lead_consumed_in_body = True
                        continue
            elif name.startswith("h"):
                line = _heading_to_md(el)
            elif name == "blockquote":
                raw_quote = _p_to_md(el)
                if raw_quote:
                    q_lines = [q.strip() for q in raw_quote.splitlines() if q.strip()]
                    line = "\n".join(f"> {q}" for q in q_lines)
                else:
                    line = ""
            else:
                line = el.get_text(" ", strip=True)
            if line:
                if name in ("h2", "h3", "h4", "h5", "h6") and _SECTION_STOP_RE.match(line.strip()):
                    break
                body_md_parts.append(line)

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
        u = _img_resolved_url(img, base_url)
        if u and _is_content_image_url(u):
            _note_img(u)

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
