#!/usr/bin/env python3
"""Convert an EPUB into a folder of per-chapter Markdown files.

Output layout (matches the existing books/<name>_md convention):
  <out>/README.md
  <out>/chapters/NN_Title.md
  <out>/supporting_sections/NN_Title.md
  <out>/images/...

Usage:
  epub_to_md.py <input.epub> <output_dir>
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp"}

SUPPORTING_KEYWORDS = [
    "introduction", "foreword", "preface", "prologue", "epilogue",
    "afterword", "acknowledg", "notes", "endnotes", "footnotes",
    "references", "bibliography", "index", "appendix", "glossary",
    "about the author", "praise", "copyright", "contents",
    "title page", "cover", "dedication", "section ", "part ",
    "resources", "credits", "imprint", "errata", "colophon",
]

CHAPTER_PATTERNS = [
    re.compile(r"^chapter\s+\d", re.I),
    re.compile(r"^ch\.?\s*\d", re.I),
    re.compile(r"^\d{1,3}[\s.:)-]"),
]


def slugify(text: str, max_len: int = 80) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"_+", "_", text)
    return text[:max_len].strip("_") or "section"


def safe_xml_parse(data: bytes) -> ET.Element:
    return ET.fromstring(data)


def find_opf_path(z: zipfile.ZipFile) -> str:
    container = z.read("META-INF/container.xml")
    root = safe_xml_parse(container)
    rootfile = root.find(".//container:rootfile", NS)
    if rootfile is None:
        rootfile = root.find(".//{*}rootfile")
    return rootfile.get("full-path")


def read_opf(z: zipfile.ZipFile, opf_path: str):
    data = z.read(opf_path)
    root = safe_xml_parse(data)

    title_el = root.find(".//dc:title", NS)
    creator_el = root.find(".//dc:creator", NS)
    title = (title_el.text or "").strip() if title_el is not None else ""
    creator = (creator_el.text or "").strip() if creator_el is not None else ""

    manifest = {}
    for item in root.findall(".//opf:manifest/opf:item", NS):
        manifest[item.get("id")] = {
            "href": item.get("href"),
            "media-type": item.get("media-type"),
            "properties": item.get("properties", ""),
        }

    spine = []
    for itemref in root.findall(".//opf:spine/opf:itemref", NS):
        idref = itemref.get("idref")
        if idref in manifest:
            spine.append(idref)

    spine_attrib = root.find(".//opf:spine", NS)
    ncx_id = spine_attrib.get("toc") if spine_attrib is not None else None

    nav_id = None
    for mid, m in manifest.items():
        if "nav" in (m["properties"] or ""):
            nav_id = mid
            break

    return {
        "title": title,
        "creator": creator,
        "manifest": manifest,
        "spine": spine,
        "ncx_id": ncx_id,
        "nav_id": nav_id,
        "opf_dir": os.path.dirname(opf_path),
    }


def normalize_href(opf_dir: str, href: str) -> str:
    href = urllib.parse.unquote(href)
    href = href.split("#", 1)[0]
    if opf_dir:
        href = f"{opf_dir}/{href}"
    return os.path.normpath(href).replace("\\", "/")


def parse_nav_titles(z: zipfile.ZipFile, opf_data: dict) -> dict:
    titles_by_href = {}

    nav_id = opf_data["nav_id"]
    if nav_id:
        href = opf_data["manifest"][nav_id]["href"]
        nav_path = normalize_href(opf_data["opf_dir"], href)
        try:
            data = z.read(nav_path)
            root = safe_xml_parse(data)
            nav_dir = os.path.dirname(nav_path)
            for a in root.iter():
                if a.tag.endswith("}a") or a.tag == "a":
                    target = a.get("href")
                    if not target:
                        continue
                    target = target.split("#", 1)[0]
                    if not target:
                        continue
                    full = os.path.normpath(
                        f"{nav_dir}/{urllib.parse.unquote(target)}"
                        if nav_dir else urllib.parse.unquote(target)
                    ).replace("\\", "/")
                    text = "".join(a.itertext()).strip()
                    if text and full not in titles_by_href:
                        titles_by_href[full] = text
        except (KeyError, ET.ParseError):
            pass

    ncx_id = opf_data["ncx_id"]
    if ncx_id and ncx_id in opf_data["manifest"]:
        href = opf_data["manifest"][ncx_id]["href"]
        ncx_path = normalize_href(opf_data["opf_dir"], href)
        try:
            data = z.read(ncx_path)
            root = safe_xml_parse(data)
            ncx_dir = os.path.dirname(ncx_path)
            for nav_point in root.findall(".//ncx:navPoint", NS):
                label_el = nav_point.find(".//ncx:navLabel/ncx:text", NS)
                content_el = nav_point.find(".//ncx:content", NS)
                if label_el is None or content_el is None:
                    continue
                src = content_el.get("src", "").split("#", 1)[0]
                if not src:
                    continue
                full = os.path.normpath(
                    f"{ncx_dir}/{urllib.parse.unquote(src)}"
                    if ncx_dir else urllib.parse.unquote(src)
                ).replace("\\", "/")
                text = (label_el.text or "").strip()
                if text and full not in titles_by_href:
                    titles_by_href[full] = text
        except (KeyError, ET.ParseError):
            pass

    return titles_by_href


def looks_like_path(s: str) -> bool:
    """True if a title is actually a file path / filename, not a real title.

    Calibre's PDF->EPUB conversion sometimes sets nav labels (and the doc
    title) to the source file's absolute path, which then leaks in as a
    chapter heading. Reject those.
    """
    s = (s or "").strip()
    if not s:
        return True
    if s.startswith("/") or s.startswith("\\") or "/Users/" in s or ":\\" in s:
        return True
    if re.search(r"\.(pdf|epub|x?html?|opf|ncx)$", s, re.I):
        return True
    return False


def title_for_item(item_path: str, titles_by_href: dict, html_data: bytes,
                   fallback_idx: int) -> str:
    cand = titles_by_href.get(item_path)
    if cand and not looks_like_path(cand):
        return cand

    try:
        text = html_data.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    for tag in ("h1", "h2", "title"):
        m = re.search(
            rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.I | re.S
        )
        if m:
            inner = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if inner and not looks_like_path(inner):
                return inner

    return f"Section {fallback_idx}"


def classify(title: str) -> str:
    t = title.lower().strip()
    for kw in SUPPORTING_KEYWORDS:
        if kw in t:
            return "supporting"
    for pat in CHAPTER_PATTERNS:
        if pat.search(t):
            return "chapter"
    return "chapter"


def convert_html_to_md(html_path: Path, md_path: Path):
    cmd = [
        "pandoc",
        "--from=html",
        "--to=gfm",
        "--wrap=preserve",
        "--standalone=false",
        str(html_path),
        "-o",
        str(md_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def rewrite_image_paths(md_text: str, image_map: dict) -> str:
    def repl(m):
        prefix, target, suffix = m.group(1), m.group(2), m.group(3)
        clean = urllib.parse.unquote(target.split("#", 1)[0]).split("?", 1)[0]
        base = os.path.basename(clean)
        if base in image_map:
            return f"{prefix}{image_map[base]}{suffix}"
        return m.group(0)

    md_text = re.sub(r"(\!\[[^\]]*\]\()([^)]+)(\))", repl, md_text)
    md_text = re.sub(r'(<img[^>]*src=")([^"]+)(")', repl, md_text, flags=re.I)
    return md_text


def _scrub_prose(s: str) -> str:
    """Strip styling cruft from a chunk of prose (never code)."""
    s = re.sub(r"</?span[^>]*>", "", s)
    s = re.sub(r"</?div[^>]*>", "", s)
    s = re.sub(r'\s+class="[^"]*"', "", s)
    for _ in range(4):
        s = s.replace("****", "").replace("____", "")
    s = re.sub(r"[ \t]+\n", "\n", s)
    return s


def clean_markdown(md_text: str) -> str:
    """Strip Calibre/ebook styling cruft that pandoc passes through as raw HTML.

    Removes `<span class="calibreN">` / `<div ...>` wrappers (keeping their text),
    merges bold/italic runs those wrappers split apart, and tidies whitespace.
    Leaves images, links, tables, and headings intact.

    Code is protected: fenced ``` blocks and inline `code` spans are passed
    through untouched, so HTML/CSS examples keep their class attributes.
    """
    fence_re = re.compile(r"^\s*(```|~~~)")
    out, in_fence = [], False
    for line in md_text.split("\n"):
        if fence_re.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        # Outside code: protect inline `code` spans, scrub the rest.
        spans = []
        stashed = re.sub(
            r"`[^`]*`", lambda m: spans.append(m.group(0)) or f"\x00{len(spans) - 1}\x00", line
        )
        cleaned = _scrub_prose(stashed)
        cleaned = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], cleaned)
        out.append(cleaned)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def extract_images(z: zipfile.ZipFile, opf_data: dict, images_dir: Path) -> dict:
    image_map = {}
    images_dir.mkdir(parents=True, exist_ok=True)
    for mid, m in opf_data["manifest"].items():
        media = (m.get("media-type") or "").lower()
        href = m.get("href") or ""
        ext = os.path.splitext(href)[1].lower()
        if media.startswith("image/") or ext in IMAGE_EXTS:
            full = normalize_href(opf_data["opf_dir"], href)
            try:
                data = z.read(full)
            except KeyError:
                continue
            base = os.path.basename(full)
            target = images_dir / base
            n = 1
            while target.exists() and target.read_bytes() != data:
                stem, ext2 = os.path.splitext(base)
                target = images_dir / f"{stem}_{n}{ext2}"
                n += 1
            target.write_bytes(data)
            image_map[base] = f"../images/{target.name}"
    return image_map


def convert_epub(epub_path: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    chapters_dir = out_dir / "chapters"
    supporting_dir = out_dir / "supporting_sections"
    images_dir = out_dir / "images"
    chapters_dir.mkdir(exist_ok=True)
    supporting_dir.mkdir(exist_ok=True)

    with zipfile.ZipFile(epub_path) as z:
        opf_path = find_opf_path(z)
        opf_data = read_opf(z, opf_path)
        titles_by_href = parse_nav_titles(z, opf_data)
        image_map = extract_images(z, opf_data, images_dir)

        readme_lines = []
        chapter_n = 0
        supporting_n = 0
        written_ch = 0
        written_sup = 0
        log = []

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            for spine_idx, idref in enumerate(opf_data["spine"], start=1):
                manifest_item = opf_data["manifest"][idref]
                href = manifest_item["href"]
                full = normalize_href(opf_data["opf_dir"], href)
                try:
                    data = z.read(full)
                except KeyError:
                    log.append(f"missing in zip: {full}")
                    continue

                title = title_for_item(full, titles_by_href, data, spine_idx)
                kind = classify(title)
                slug = slugify(title)

                if kind == "chapter":
                    chapter_n += 1
                    fname = f"{chapter_n:02d}_{slug}.md"
                    md_target = chapters_dir / fname
                    rel = f"chapters/{fname}"
                else:
                    supporting_n += 1
                    fname = f"{supporting_n:02d}_{slug}.md"
                    md_target = supporting_dir / fname
                    rel = f"supporting_sections/{fname}"

                tmp_html = tmpdir / f"{spine_idx:03d}.html"
                tmp_html.write_bytes(data)
                tmp_md = tmpdir / f"{spine_idx:03d}.md"
                try:
                    convert_html_to_md(tmp_html, tmp_md)
                except subprocess.CalledProcessError as e:
                    log.append(f"pandoc failed for {full}: {e.stderr.decode(errors='replace')[:200]}")
                    continue

                md_text = tmp_md.read_text(encoding="utf-8")
                md_text = rewrite_image_paths(md_text, image_map)
                md_text = clean_markdown(md_text)
                md_text = re.sub(r"\n{3,}", "\n\n", md_text).strip() + "\n"

                front = f"# {title}\n\n"
                if not md_text.lstrip().startswith("#"):
                    md_text = front + md_text
                md_target.write_text(md_text, encoding="utf-8")
                if kind == "chapter":
                    written_ch += 1
                else:
                    written_sup += 1
                readme_lines.append(f"- `{rel}` — {title}")

        if written_ch + written_sup == 0:
            raise RuntimeError(
                f"no sections converted from {epub_path.name} "
                f"(pandoc produced nothing for all {len(opf_data['spine'])} spine items)"
            )

        if not any(chapters_dir.iterdir()):
            chapters_dir.rmdir()
        if not any(supporting_dir.iterdir()):
            supporting_dir.rmdir()
        if not any(images_dir.iterdir()):
            images_dir.rmdir()

        title = opf_data["title"] or epub_path.stem
        creator = opf_data["creator"]
        readme = [f"# {title} — Markdown export", ""]
        if creator:
            readme.append(f"_by {creator}_")
            readme.append("")
        readme.append(
            "Generated from the EPUB. Files appear below in reading (spine) order."
        )
        readme.append("")
        readme.extend(readme_lines)
        if log:
            readme.append("")
            readme.append("## Conversion notes")
            readme.extend(f"- {x}" for x in log)
        (out_dir / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")

    return {
        "chapters": written_ch,
        "supporting": written_sup,
        "title": title,
    }


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    src = Path(sys.argv[1]).resolve()
    dst = Path(sys.argv[2]).resolve()
    if not src.is_file():
        print(f"not a file: {src}", file=sys.stderr)
        sys.exit(1)
    result = convert_epub(src, dst)
    print(
        f"OK: {result['title']} -> {dst} "
        f"(chapters={result['chapters']}, supporting={result['supporting']})"
    )


if __name__ == "__main__":
    main()
