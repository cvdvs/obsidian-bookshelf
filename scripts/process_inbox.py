#!/usr/bin/env python3
"""obsidian-bookshelf — process every book sitting in a library's _inbox.

One shared engine for all of your vault libraries. The library is chosen by a
command-line argument; everything else comes from config.ini.

For each *.epub or *.pdf in <library>/_inbox:
  1. Wait until the file is finished copying (size stable; valid file).
  2. EPUB  -> split into per-chapter Markdown via epub_to_md.
     PDF   -> convert to EPUB first (Calibre), store that EPUB, then split.
  3. Move the converted <slug>_md folder into the vault's books directory.
  4. File the source: EPUBs go to <library>/epub/, PDFs to <library>/pdf/
     (a PDF also leaves its generated EPUB in <library>/epub/).

Idempotent and serialized via a file lock so concurrent runs don't race.

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
PDF_CONVERT_TIMEOUT = 1800  # seconds; large PDFs can be slow
WATCH_INTERVAL = 15


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
    library_root = _expand(paths.get("library_root", "~/Documents/Library"))
    vault_root = _expand(paths.get("vault_root", "~/Documents/Obsidian"))
    ebook_convert = (paths.get("ebook_convert", "") or "").strip()

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
        "library_root": library_root,
        "vault_root": vault_root,
        "ebook_convert": ebook_convert,
        "libraries": libraries,
    }


def cfg_for(library: str, conf: dict) -> dict:
    if library not in conf["libraries"]:
        raise SystemExit(
            f"unknown library '{library}'. Defined: {', '.join(conf['libraries'])}"
        )
    lib = conf["libraries"][library]
    base = conf["library_root"] / f"{library}-library"
    logs_dir = conf["library_root"] / ".logs"
    c = {
        "name": library,
        "ebook_convert": conf["ebook_convert"],
        "books_dest": conf["vault_root"] / lib["vault_books_dir"],
        "inbox": base / "_inbox",
        "log": logs_dir / f"{library}-library.log",
        "lock": Path(tempfile.gettempdir()) / f"obsidian-bookshelf-{library}.lock",
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


def pdf_to_epub(pdf: Path, out_epub: Path, cfg: dict) -> bool:
    convert = cfg.get("ebook_convert")
    if not convert:
        log(cfg, f"PDF skip (no ebook_convert configured — set it in config.ini): {pdf.name}")
        return False
    try:
        res = subprocess.run(
            [convert, str(pdf), str(out_epub)],
            capture_output=True, text=True, timeout=PDF_CONVERT_TIMEOUT,
        )
    except FileNotFoundError:
        log(cfg, f"PDF skip (ebook-convert not found at {convert}): {pdf.name}")
        return False
    except subprocess.TimeoutExpired:
        log(cfg, f"PDF convert TIMED OUT: {pdf.name}")
        return False
    if res.returncode != 0 or not out_epub.exists():
        tail = (res.stderr or res.stdout or "").strip()[-300:]
        log(cfg, f"PDF convert FAILED for {pdf.name}: {tail}")
        return False
    return True


def split_epub_to_vault(epub: Path, cfg: dict) -> bool:
    name = folder_name(epub)
    final_md = cfg["books_dest"] / name
    if final_md.exists():
        log(cfg, f"skip (md folder already in vault): {name}")
        return False
    with tempfile.TemporaryDirectory(prefix="epub_md_") as tmp:
        staging = Path(tmp) / name
        result = epub_to_md.convert_epub(epub, staging)
        cfg["books_dest"].mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(final_md))
    log(cfg, f"converted {epub.name} -> {name} "
             f"(chapters={result['chapters']}, supporting={result['supporting']})")
    return True


def process_epub(epub: Path, cfg: dict) -> None:
    final_epub = cfg["epub_dest"] / epub.name
    if final_epub.exists():
        log(cfg, f"skip (epub already filed): {epub.name}")
        return
    try:
        split_epub_to_vault(epub, cfg)
    except Exception as e:
        log(cfg, f"convert FAILED for {epub.name}: {e}")
        log(cfg, traceback.format_exc())
        return
    cfg["epub_dest"].mkdir(parents=True, exist_ok=True)
    shutil.move(str(epub), str(final_epub))


def process_pdf(pdf: Path, cfg: dict) -> None:
    final_pdf = cfg["pdf_dest"] / pdf.name
    gen_epub_final = cfg["epub_dest"] / f"{pdf.stem}.epub"
    if final_pdf.exists():
        log(cfg, f"skip (pdf already filed): {pdf.name}")
        return
    with tempfile.TemporaryDirectory(prefix="pdf_epub_") as tmp:
        tmp_epub = Path(tmp) / f"{pdf.stem}.epub"
        if not pdf_to_epub(pdf, tmp_epub, cfg):
            return  # leave the pdf in the inbox so it can be retried
        try:
            split_epub_to_vault(tmp_epub, cfg)
        except Exception as e:
            log(cfg, f"convert FAILED for {pdf.name} (via epub): {e}")
            log(cfg, traceback.format_exc())
            return
        cfg["epub_dest"].mkdir(parents=True, exist_ok=True)
        if not gen_epub_final.exists():
            shutil.move(str(tmp_epub), str(gen_epub_final))
    cfg["pdf_dest"].mkdir(parents=True, exist_ok=True)
    shutil.move(str(pdf), str(final_pdf))
    log(cfg, f"filed pdf {pdf.name} -> pdf/ (+ epub/{gen_epub_final.name})")


def process_one(book: Path, cfg: dict) -> None:
    if not is_settled(book):
        log(cfg, f"skip (not settled yet): {book.name}")
        return
    if book.suffix.lower() == ".epub":
        if not is_valid_epub(book):
            log(cfg, f"skip (not a valid EPUB): {book.name}")
            return
        process_epub(book, cfg)
    elif book.suffix.lower() == ".pdf":
        process_pdf(book, cfg)


def run_once(cfg: dict) -> int:
    cfg["inbox"].mkdir(parents=True, exist_ok=True)
    books = sorted(
        p for p in cfg["inbox"].iterdir()
        if p.is_file() and p.suffix.lower() in (".epub", ".pdf")
    )
    if not books:
        return 0
    # Serialize with a lock file (best-effort, cross-platform).
    lock = Path(cfg["lock"])
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        log(cfg, "another run is already in progress, exiting")
        return 0
    try:
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
