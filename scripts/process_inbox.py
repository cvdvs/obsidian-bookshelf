#!/usr/bin/env python3
"""obsidian-bookshelf — process every book sitting in a library's _inbox.

One shared engine for all of your vault libraries. The library is chosen by a
command-line argument; everything else comes from config.ini.

Routing by type:
  - .epub                     -> split into per-chapter Markdown directly.
  - .pdf with text            -> Calibre -> EPUB -> split.
  - .pdf that's a scan        -> ocrmypdf adds a text layer, pdftotext pulls the
                                 text, Markdown written directly (Calibre's PDF
                                 input discards the OCR layer). Falls back to OCR
                                 if Calibre times out on a "text" PDF too.
  - .djvu                     -> ddjvu -> PDF -> the scan path above.
  - .mobi/.azw3/.fb2/.doc/... -> Calibre -> EPUB -> split.

The <slug>_md folder (built in staging, moved in atomically) goes to the vault's
books dir with a `.source` provenance marker. The original source is filed to
<library>/<ext>/ (a Calibre conversion also leaves its EPUB in epub/_generated/).
Two different books that normalize to the same slug are kept apart with a _N
suffix. Scans OCR can't read are parked in <library>/pdf/_scanned-needs-ocr/.

Idempotent and serialized via a lock file so concurrent runs don't race.

Usage:
  process_inbox.py <library>                 # process the inbox once
  process_inbox.py <library> --watch         # poll the inbox forever
  process_inbox.py <library> --config PATH    # use a specific config.ini

Config is searched in this order:
  1. --config PATH
  2. $OBSIDIAN_BOOKSHELF_CONFIG
  3. config.ini next to the repo root (parent of this scripts/ folder)
  4. ~/.config/obsidian-bookshelf/config.ini
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import epub_to_md  # noqa: E402

SETTLE_INTERVAL = 3
SETTLE_RETRIES = 5
CONVERT_TIMEOUT = 1800          # Calibre conversions
OCR_TIMEOUT = 3600              # OCR of a big scan can take a long time
PDFTOTEXT_FULL_TIMEOUT = 1800
WATCH_INTERVAL = 15

# Scan detection: near-zero text in the first pages, or a big file with little
# text, means it's an image scan and needs OCR before conversion.
SCANNED_SAMPLE_PAGES = 10
SCANNED_NEAR_ZERO_BYTES = 200
SCANNED_LARGE_PDF_BYTES = 20 * 1024 * 1024
SCANNED_LARGE_TEXT_MAX = 2000
MIN_OCR_WORDS = 300            # below this, OCR basically failed -> park
OCR_CHUNK_PAGES = 25           # pages of OCR'd text per markdown file

# Non-epub formats Calibre turns into EPUB directly (text-based; no OCR).
# PDF and DJVU are handled specially (scan detection + OCR).
CALIBRE_EBOOK_FORMATS = {
    ".mobi", ".azw", ".azw3", ".azw4", ".prc", ".fb2", ".lit",
    ".pdb", ".rtf", ".docx", ".doc", ".htmlz", ".lrf", ".tcr", ".snb",
}
SUPPORTED = {".epub", ".pdf", ".djvu"} | CALIBRE_EBOOK_FORMATS


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p.strip())))


def find_config(explicit: str | None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("OBSIDIAN_BOOKSHELF_CONFIG"):
        candidates.append(Path(os.environ["OBSIDIAN_BOOKSHELF_CONFIG"]))
    candidates.append(HERE.parent / "config.ini")
    candidates.append(Path.home() / ".config/obsidian-bookshelf/config.ini")
    for c in candidates:
        if c and c.expanduser().is_file():
            return c.expanduser()
    raise SystemExit(
        "No config.ini found. Copy config.example.ini to config.ini and edit it,\n"
        "or pass --config PATH. Looked in:\n  " + "\n  ".join(str(c) for c in candidates)
    )


def load_config(explicit: str | None):
    path = find_config(explicit)
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")

    paths = cp["paths"] if cp.has_section("paths") else {}

    def tool(key: str, default: str) -> str:
        v = (paths.get(key, "") or "").strip()
        return v or default

    libraries = {}
    for section in cp.sections():
        if not section.startswith("library:"):
            continue
        name = section.split(":", 1)[1].strip()
        s = cp[section]
        libraries[name] = {
            "vault_books_dir": s.get("vault_books_dir", "").strip(),
            "unsorted": s.getboolean("unsorted", fallback=False),
        }
    if not libraries:
        raise SystemExit(f"No [library:NAME] sections in {path}")

    return {
        "config_path": path,
        "library_root": _expand(paths.get("library_root", "~/Documents/Library")),
        "vault_root": _expand(paths.get("vault_root", "~/Documents/Obsidian")),
        # Tools: blank ebook_convert disables Calibre formats + PDF/djvu→epub.
        # The others fall back to a PATH lookup of the bare command name.
        "ebook_convert": (paths.get("ebook_convert", "") or "").strip(),
        "ocrmypdf": tool("ocrmypdf", "ocrmypdf"),
        "ddjvu": tool("ddjvu", "ddjvu"),
        "pdftotext": tool("pdftotext", "pdftotext"),
        "ocr_lang": tool("ocr_lang", "eng"),
        "libraries": libraries,
    }


def cfg_for(library: str, conf: dict) -> dict:
    if library not in conf["libraries"]:
        raise SystemExit(
            f"unknown library '{library}'. Defined: {', '.join(conf['libraries'])}"
        )
    lib = conf["libraries"][library]
    base = conf["library_root"] / f"{library}-library"
    c = {
        "name": library,
        "base": base,
        "libraries_unsorted": lib["unsorted"],
        "ebook_convert": conf["ebook_convert"],
        "ocrmypdf": conf["ocrmypdf"],
        "ddjvu": conf["ddjvu"],
        "pdftotext": conf["pdftotext"],
        "ocr_lang": conf["ocr_lang"],
        "books_dest": conf["vault_root"] / lib["vault_books_dir"],
        "inbox": base / "_inbox",
        "log": conf["library_root"] / ".logs" / f"{library}-library.log",
        "lock": Path(tempfile.gettempdir()) / f"obsidian-bookshelf-{library}.lock",
        "scanned_dest": base / "pdf" / "_scanned-needs-ocr",
    }
    if lib["unsorted"]:
        c["epub_dest"] = base / "epub" / "_unsorted"
        c["pdf_dest"] = base / "pdf" / "_unsorted"
    else:
        c["epub_dest"] = base / "epub"
        c["pdf_dest"] = base / "pdf"
    return c


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def log(cfg: dict, msg: str) -> None:
    p = Path(cfg["log"])
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"{ts}  {msg}\n")
    print(msg, flush=True)


def folder_name(book: Path) -> str:
    """Derive a snake_case <slug>_md folder name from the source filename."""
    stem = book.stem
    stem = re.sub(r",?\s*\d+(?:st|nd|rd|th)?\s*edition\b", "", stem, flags=re.I)
    stem = re.sub(
        r"\b(?:Apr|Aug|Dec|Feb|Jan|Jul|Jun|Mar|May|Nov|Oct|Sep)\.?\s*\d{1,2}\,?\s*\d{4}\b",
        "", stem, flags=re.I,
    )
    stem = re.sub(r",?\s+\d{4}\b", "", stem)
    stem = re.sub(r"\bebook\b", "", stem, flags=re.I)
    stem = re.sub(r"[^\w\s-]", " ", stem)
    stem = re.sub(r"\s+", "_", stem.strip()).lower()
    stem = re.sub(r"_+", "_", stem).strip("_")
    return f"{stem}_md"


def unique_dest(directory: Path, name: str) -> Path:
    """A non-colliding path in `directory` for `name` (append _2, _3, … if taken)."""
    cand = directory / name
    if not cand.exists():
        return cand
    stem, ext = Path(name).stem, Path(name).suffix
    i = 2
    while (directory / f"{stem}_{i}{ext}").exists():
        i += 1
    return directory / f"{stem}_{i}{ext}"


def source_dir(cfg: dict, ext: str) -> Path:
    """Where an original source of a given extension is filed (mobi/, djvu/, …)."""
    d = cfg["base"] / ext
    if cfg["libraries_unsorted"]:
        d = d / "_unsorted"
    return d


def resolve_book_dest(original: Path, cfg: dict) -> tuple[Path, str]:
    """Resolve the vault <slug>_md folder, distinguishing a re-drop of the SAME
    book from a slug COLLISION with a different one (via a `.source` marker).
    Returns (dest, 'done'|'new'). A markerless legacy folder is treated as 'done'
    so pre-existing libraries aren't duplicated."""
    base = folder_name(original)
    stem = base[:-3].rstrip("_")
    i = 1
    while True:
        name = base if i == 1 else f"{stem}_{i}_md"
        dest = cfg["books_dest"] / name
        if not dest.exists():
            return dest, "new"
        marker = dest / ".source"
        if not marker.exists():
            return dest, "done"
        if marker.read_text(encoding="utf-8").strip() == original.name:
            return dest, "done"
        i += 1


def is_settled(p: Path) -> bool:
    last = -1
    for _ in range(SETTLE_RETRIES):
        try:
            size = p.stat().st_size
        except FileNotFoundError:
            return False
        if size > 0 and size == last:
            return True
        last = size
        time.sleep(SETTLE_INTERVAL)
    return False


def is_valid_epub(p: Path) -> bool:
    try:
        with zipfile.ZipFile(p) as z:
            return "META-INF/container.xml" in z.namelist()
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# External tools
# --------------------------------------------------------------------------- #
def calibre_to_epub(src: Path, out_epub: Path, cfg: dict) -> str:
    """Convert any Calibre-supported file to EPUB. Returns ok|timeout|failed|no-tool."""
    convert = cfg.get("ebook_convert")
    if not convert:
        log(cfg, f"skip (no ebook_convert configured — set it in config.ini): {src.name}")
        return "no-tool"
    try:
        res = subprocess.run([convert, str(src), str(out_epub)],
                             capture_output=True, text=True, timeout=CONVERT_TIMEOUT)
    except FileNotFoundError:
        log(cfg, f"skip (ebook-convert not found at {convert}): {src.name}")
        return "no-tool"
    except subprocess.TimeoutExpired:
        log(cfg, f"convert TIMED OUT: {src.name}")
        return "timeout"
    if res.returncode != 0 or not out_epub.exists():
        tail = (res.stderr or res.stdout or "").strip()[-300:]
        log(cfg, f"convert FAILED for {src.name}: {tail}")
        return "failed"
    return "ok"


def ocr_pdf(src: Path, out_pdf: Path, cfg: dict) -> bool:
    """Add a searchable text layer to a scanned PDF via ocrmypdf."""
    try:
        res = subprocess.run(
            [cfg["ocrmypdf"], "--skip-text", "--rotate-pages", "--deskew",
             "-l", cfg["ocr_lang"], str(src), str(out_pdf)],
            capture_output=True, text=True, timeout=OCR_TIMEOUT)
    except FileNotFoundError:
        log(cfg, f"OCR unavailable (ocrmypdf not found: {cfg['ocrmypdf']}): {src.name}")
        return False
    except subprocess.TimeoutExpired:
        log(cfg, f"OCR TIMED OUT: {src.name}")
        return False
    if res.returncode != 0 or not out_pdf.exists():
        tail = (res.stderr or res.stdout or "").strip()[-300:]
        log(cfg, f"OCR error for {src.name}: {tail}")
        return False
    return True


def djvu_to_pdf(src: Path, out_pdf: Path, cfg: dict) -> bool:
    """Convert a DjVu document to a (multi-page, image) PDF via ddjvu."""
    try:
        res = subprocess.run([cfg["ddjvu"], "-format=pdf", "-quality=85",
                              str(src), str(out_pdf)],
                             capture_output=True, text=True, timeout=OCR_TIMEOUT)
    except FileNotFoundError:
        log(cfg, f"djvu skip (ddjvu not found: {cfg['ddjvu']}): {src.name}")
        return False
    except subprocess.TimeoutExpired:
        log(cfg, f"djvu->pdf TIMED OUT: {src.name}")
        return False
    if res.returncode != 0 or not out_pdf.exists():
        tail = (res.stderr or res.stdout or "").strip()[-300:]
        log(cfg, f"djvu->pdf FAILED for {src.name}: {tail}")
        return False
    return True


def pdf_text_bytes(pdf: Path, cfg: dict) -> int | None:
    """Non-whitespace text bytes in the first pages of a PDF (None if no tool)."""
    try:
        res = subprocess.run(
            [cfg["pdftotext"], "-f", "1", "-l", str(SCANNED_SAMPLE_PAGES), str(pdf), "-"],
            capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return len("".join(res.stdout.split()))


def looks_scanned(pdf: Path, cfg: dict) -> bool | None:
    """True if the PDF reads as an image scan, False if it has text, None if we
    can't tell (pdftotext missing / timed out / errored)."""
    nbytes = pdf_text_bytes(pdf, cfg)
    if nbytes is None:
        return None
    size = pdf.stat().st_size
    return nbytes < SCANNED_NEAR_ZERO_BYTES or (
        size > SCANNED_LARGE_PDF_BYTES and nbytes < SCANNED_LARGE_TEXT_MAX)


def pdftotext_full(pdf: Path, cfg: dict) -> str | None:
    """Full reading-order text of a PDF (None on tool-missing / timeout / error)."""
    try:
        res = subprocess.run([cfg["pdftotext"], str(pdf), "-"],
                             capture_output=True, text=True, timeout=PDFTOTEXT_FULL_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


# --------------------------------------------------------------------------- #
# Filing
# --------------------------------------------------------------------------- #
def park_source(src: Path, cfg: dict, reason: str) -> None:
    cfg["scanned_dest"].mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(unique_dest(cfg["scanned_dest"], src.name)))
    log(cfg, f"parked for manual OCR ({reason}): {src.name}")


def file_failed(original: Path, cfg: dict, reason: str) -> None:
    """Conversion failed but the file is fine — move it out of the inbox into its
    <ext>/ folder so it isn't retried forever (no markdown produced)."""
    oext = original.suffix.lower().lstrip(".")
    sdir = source_dir(cfg, oext)
    sdir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(original), str(unique_dest(sdir, original.name)))
    log(cfg, f"conversion failed ({reason}); filed {oext} source only, no markdown: {original.name}")


def file_source_only(original: Path, cfg: dict) -> None:
    oext = original.suffix.lower().lstrip(".")
    sdir = source_dir(cfg, oext)
    sdir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(original), str(unique_dest(sdir, original.name)))
    log(cfg, f"filed {oext} {original.name} -> {oext}/ (OCR text-only; no epub)")


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #
def scanned_pdf_to_vault(ocr_pdf_path: Path, original: Path, cfg: dict) -> bool:
    """OCR'd scan -> Markdown written directly (Calibre drops the OCR layer).
    Returns True if written/already-done, False if too sparse to keep (park).
    Page numbers track the real PDF pages."""
    dest, state = resolve_book_dest(original, cfg)
    if state == "done":
        log(cfg, f"skip (already converted): {dest.name}")
        return True
    text = pdftotext_full(ocr_pdf_path, cfg)
    if text is None:
        log(cfg, f"pdftotext could not read OCR output: {original.name}")
        return False
    if len(text.split()) < MIN_OCR_WORDS:
        return False
    pages = [p.strip() for p in text.split("\f")]   # index i == real PDF page i+1
    if pages and pages[-1] == "":
        pages.pop()   # poppler emits a trailing form-feed; drop the phantom page
    total_words = len(text.split())
    nonblank = sum(1 for p in pages if p)
    with tempfile.TemporaryDirectory(prefix="ocr_md_") as tmp:
        staging = Path(tmp) / dest.name
        chapters = staging / "chapters"
        chapters.mkdir(parents=True)
        fileno = 0
        for start in range(0, len(pages), OCR_CHUNK_PAGES):
            group = pages[start:start + OCR_CHUNK_PAGES]
            a, b = start + 1, start + len(group)
            sections = [f"## Page {start + i + 1}\n\n{p}"
                        for i, p in enumerate(group) if p]
            if not sections:
                continue
            fileno += 1
            (chapters / f"{fileno:02d}_Pages_{a}-{b}.md").write_text(
                f"# {original.stem} — pages {a}–{b}\n\n" + "\n\n".join(sections) + "\n",
                encoding="utf-8")
        (staging / ".source").write_text(original.name + "\n", encoding="utf-8")
        (staging / "README.md").write_text(
            f"# {original.stem} — OCR export\n\n"
            f"OCR'd from a scanned {original.suffix.lstrip('.').upper()} "
            f"(ocrmypdf + pdftotext). {len(pages)} pages ({nonblank} with text), "
            f"~{total_words} words, {fileno} file(s). No native chapter structure — "
            f"files are page ranges and page numbers match the source PDF. "
            f"OCR text may contain recognition errors.\n", encoding="utf-8")
        cfg["books_dest"].mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(dest))
    log(cfg, f"OCR->md {original.name} -> {dest.name} ({len(pages)} pages, ~{total_words} words)")
    return True


def split_epub_to_vault(epub: Path, cfg: dict, original: Path | None = None) -> bool:
    """Split an EPUB into per-chapter Markdown in the vault. Returns True if
    converted/already-done; raises on conversion failure."""
    original = original or epub
    dest, state = resolve_book_dest(original, cfg)
    if state == "done":
        log(cfg, f"skip (already converted): {dest.name}")
        return True
    with tempfile.TemporaryDirectory(prefix="epub_md_") as tmp:
        staging = Path(tmp) / dest.name
        result = epub_to_md.convert_epub(epub, staging)   # raises if nothing converts
        (staging / ".source").write_text(original.name + "\n", encoding="utf-8")
        cfg["books_dest"].mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(dest))
    log(cfg, f"converted {original.name} -> {dest.name} "
             f"(chapters={result['chapters']}, supporting={result['supporting']})")
    return True


def _file_epub_and_source(tmp_epub: Path, original: Path, cfg: dict) -> None:
    split_epub_to_vault(tmp_epub, cfg, original)
    oext = original.suffix.lower().lstrip(".")
    gen_dir = cfg["epub_dest"] / "_generated"     # isolated so it can't mask native .epub
    gen_dir.mkdir(parents=True, exist_ok=True)
    gen_final = unique_dest(gen_dir, f"{original.stem}.epub")
    shutil.move(str(tmp_epub), str(gen_final))
    sdir = source_dir(cfg, oext)
    sdir.mkdir(parents=True, exist_ok=True)
    src_final = unique_dest(sdir, original.name)
    shutil.move(str(original), str(src_final))
    log(cfg, f"filed {oext} {original.name} -> {oext}/ (+ epub/_generated/{gen_final.name})")


# --------------------------------------------------------------------------- #
# Per-format processors
# --------------------------------------------------------------------------- #
def process_epub(epub: Path, cfg: dict) -> None:
    if (cfg["epub_dest"] / epub.name).exists():
        log(cfg, f"skip (epub already filed): {epub.name}")
        return
    try:
        split_epub_to_vault(epub, cfg, epub)
    except Exception as e:
        log(cfg, f"convert FAILED for {epub.name}: {e}")
        log(cfg, traceback.format_exc())
        file_failed(epub, cfg, "epub split failed")
        return
    cfg["epub_dest"].mkdir(parents=True, exist_ok=True)
    shutil.move(str(epub), str(unique_dest(cfg["epub_dest"], epub.name)))


def process_pdf(pdf: Path, cfg: dict, original: Path | None = None) -> None:
    original = original or pdf
    oext = original.suffix.lower().lstrip(".")
    if (source_dir(cfg, oext) / original.name).exists():
        log(cfg, f"skip ({oext} already filed): {original.name}")
        return
    if resolve_book_dest(original, cfg)[1] == "done":
        sdir = source_dir(cfg, oext)
        sdir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(original), str(unique_dest(sdir, original.name)))
        log(cfg, f"already converted: {original.name}; filed {oext} source")
        return

    scanned = looks_scanned(pdf, cfg)
    if scanned is None:
        park_source(original, cfg, "scan detection failed (pdftotext unavailable/errored)")
        return

    with tempfile.TemporaryDirectory(prefix="pdf_") as tmp:
        if scanned:
            ocr_out = Path(tmp) / f"{pdf.stem}.ocr.pdf"
            if not ocr_pdf(pdf, ocr_out, cfg):
                park_source(original, cfg, "scan; OCR failed")
                return
            log(cfg, f"OCR added a text layer: {original.name}")
            try:
                ok = scanned_pdf_to_vault(ocr_out, original, cfg)
            except Exception as e:
                log(cfg, f"OCR->md FAILED for {original.name}: {e}")
                log(cfg, traceback.format_exc())
                park_source(original, cfg, "OCR->md crashed")
                return
            if not ok:
                park_source(original, cfg, "OCR text too sparse")
                return
            file_source_only(original, cfg)
            return
        # Text PDF: Calibre -> EPUB -> split.
        tmp_epub = Path(tmp) / f"{original.stem}.epub"
        status = calibre_to_epub(pdf, tmp_epub, cfg)
        if status == "no-tool":
            return
        if status != "ok":
            # Calibre choked on a "text" PDF — often a big scan with sparse early
            # text. Try OCR before giving up.
            log(cfg, f"Calibre {status}; trying OCR fallback: {original.name}")
            ocr_out = Path(tmp) / f"{pdf.stem}.ocr.pdf"
            if ocr_pdf(pdf, ocr_out, cfg):
                try:
                    if scanned_pdf_to_vault(ocr_out, original, cfg):
                        file_source_only(original, cfg)
                        return
                except Exception as e:
                    log(cfg, f"OCR->md FAILED for {original.name}: {e}")
                    log(cfg, traceback.format_exc())
                    park_source(original, cfg, "OCR->md crashed")
                    return
            park_source(original, cfg, f"Calibre {status}; OCR fallback also failed")
            return
        try:
            _file_epub_and_source(tmp_epub, original, cfg)
        except Exception as e:
            log(cfg, f"convert FAILED for {original.name} (via epub): {e}")
            log(cfg, traceback.format_exc())
            file_failed(original, cfg, "epub split failed after conversion")


def process_djvu(src: Path, cfg: dict) -> None:
    if (source_dir(cfg, "djvu") / src.name).exists():
        log(cfg, f"skip (djvu already filed): {src.name}")
        return
    with tempfile.TemporaryDirectory(prefix="djvu_") as tmp:
        tmp_pdf = Path(tmp) / f"{src.stem}.pdf"
        if not djvu_to_pdf(src, tmp_pdf, cfg):
            park_source(src, cfg, "djvu->pdf failed")
            return
        process_pdf(tmp_pdf, cfg, original=src)


def process_ebook(src: Path, cfg: dict) -> None:
    """mobi / azw3 / fb2 / doc / ... -> Calibre -> EPUB -> split."""
    oext = src.suffix.lower().lstrip(".")
    if (source_dir(cfg, oext) / src.name).exists():
        log(cfg, f"skip ({oext} already filed): {src.name}")
        return
    with tempfile.TemporaryDirectory(prefix="ebk_") as tmp:
        tmp_epub = Path(tmp) / f"{src.stem}.epub"
        status = calibre_to_epub(src, tmp_epub, cfg)
        if status == "no-tool":
            return
        if status != "ok":
            sdir = source_dir(cfg, oext)
            sdir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(unique_dest(sdir, src.name)))
            log(cfg, f"convert FAILED ({oext}, Calibre {status}); filed source only: {src.name}")
            return
        try:
            _file_epub_and_source(tmp_epub, src, cfg)
        except Exception as e:
            log(cfg, f"convert FAILED for {src.name} (via epub): {e}")
            log(cfg, traceback.format_exc())
            file_failed(src, cfg, "epub split failed after conversion")


def process_one(book: Path, cfg: dict) -> None:
    if not is_settled(book):
        log(cfg, f"skip (not settled yet): {book.name}")
        return
    ext = book.suffix.lower()
    if ext == ".epub":
        if not is_valid_epub(book):
            log(cfg, f"skip (not a valid EPUB): {book.name}")
            return
        process_epub(book, cfg)
    elif ext == ".pdf":
        process_pdf(book, cfg)
    elif ext == ".djvu":
        process_djvu(book, cfg)
    elif ext in CALIBRE_EBOOK_FORMATS:
        process_ebook(book, cfg)


def run_once(cfg: dict) -> int:
    cfg["inbox"].mkdir(parents=True, exist_ok=True)
    entries = [p for p in cfg["inbox"].iterdir()
               if p.is_file() and not p.name.startswith(".")]
    books = sorted(p for p in entries if p.suffix.lower() in SUPPORTED)
    others = sorted(p for p in entries if p.suffix.lower() not in SUPPORTED)
    if not books and not others:
        return 0
    lock = Path(cfg["lock"])
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        log(cfg, "another run is already in progress, exiting")
        return 0
    try:
        for other in others:
            log(cfg, f"skip (unsupported format '{other.suffix or 'none'}'; "
                     f"convert by hand): {other.name}")
        for book in books:
            try:
                process_one(book, cfg)
            except Exception as e:
                log(cfg, f"unhandled error processing {book.name}: {e}")
                log(cfg, traceback.format_exc())
    finally:
        os.close(fd)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
    return len(books)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit("usage: process_inbox.py <library> [--watch] [--config PATH]")
    library = args[0]
    watch = "--watch" in args
    explicit = None
    if "--config" in args:
        i = args.index("--config")
        explicit = args[i + 1] if i + 1 < len(args) else None

    conf = load_config(explicit)
    cfg = cfg_for(library, conf)

    if watch:
        print(f"watching {cfg['inbox']} (every {WATCH_INTERVAL}s) — Ctrl-C to stop")
        while True:
            run_once(cfg)
            time.sleep(WATCH_INTERVAL)
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
