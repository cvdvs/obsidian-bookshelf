# 📚 obsidian-bookshelf

**Drop an EPUB or PDF into a folder. Get clean, per-chapter Markdown in your Obsidian vault — automatically.**

A small, local, no-cloud pipeline for turning a shelf of ebooks into a linkable
knowledge base. You drop a book into an inbox folder; a background watcher splits
it into one Markdown file per chapter and files it into the right Obsidian vault.
The original book stays in your library — only the Markdown goes to the vault.

Built for people who use Obsidian as a research or work knowledge base and want
their books to live there as searchable, linkable, citable notes.

---

## How it works

A book passes through three places:

```
  your library                 the engine                 your Obsidian vault
 ┌──────────────┐   drop    ┌──────────────────┐  files  ┌──────────────────────┐
 │  _inbox/      │ ───────►  │ EPUB → split      │ ──────► │ raw/sources/books/    │
 │  epub/        │           │ PDF  → EPUB→split │         │   <book>_md/          │
 │  pdf/         │ ◄──────── │ (Calibre+pandoc)  │         │     chapters/         │
 └──────────────┘  source    └──────────────────┘         │     images/  README   │
                   filed back                              └──────────────────────┘
```

1. **EPUB** → split into per-chapter Markdown (using its table of contents).
2. **PDF** → converted to EPUB first (via Calibre), then split the same way.
3. The result — `<book>_md/` with `chapters/`, `supporting_sections/`, `images/`,
   and a `README.md` index — is moved into your vault's books folder.
4. The source file is filed back into `epub/` or `pdf/`, and the inbox empties.

Re-dropping a book that's already converted is safely ignored. Concurrent drops
are serialized by a lock file.

---

## Requirements

- **Python 3.8+** — no pip packages needed (standard library only).
- **[pandoc](https://pandoc.org/)** — converts the book's HTML into Markdown.
- **[Calibre](https://calibre-ebook.com/)** — *only* needed for PDF support
  (provides `ebook-convert`). EPUB-only setups can skip it.

```bash
# macOS
brew install pandoc
brew install --cask calibre
```

---

## Quick start

```bash
git clone https://github.com/cvdvs/obsidian-bookshelf.git
cd obsidian-bookshelf

cp config.example.ini config.ini   # then edit it (see below)
python3 install.py                  # creates folders; on macOS installs watchers
```

Now drop a book into any `<library>-library/_inbox/` and it converts on its own.
Prefer to run it by hand? `python3 scripts/process_inbox.py <library>`.

See [`examples/`](examples/) for a full walkthrough with a free Project Gutenberg book.

---

## Configuration

Everything lives in `config.ini` (copied from `config.example.ini`):

```ini
[paths]
library_root  = ~/Documents/Library      # holds your per-vault libraries
vault_root    = ~/Documents/Obsidian      # holds your Obsidian vaults
ebook_convert = /Applications/calibre.app/Contents/MacOS/ebook-convert  # blank = no PDF

[library:work]
vault_books_dir = Work Wiki/raw/sources/books   # where Markdown lands (under vault_root)
unsorted        = false

[library:personal]
vault_books_dir = Personal Wiki/raw/sources/books
unsorted        = true      # processed sources go to epub/_unsorted, for vaults
                            # that organize books into category subfolders
```

Each `[library:NAME]` creates a drop folder at `<library_root>/NAME-library/_inbox/`
and sends finished Markdown to `<vault_root>/<vault_books_dir>/`.

**Adding a library** is just another section in `config.ini` plus a re-run of
`python3 install.py`.

---

## Usage

```bash
# Process an inbox once
python3 scripts/process_inbox.py work

# Watch an inbox continuously (any OS — no LaunchAgent needed)
python3 scripts/process_inbox.py work --watch

# Use a specific config
python3 scripts/process_inbox.py work --config /path/to/config.ini
```

On **macOS**, `install.py` sets up a LaunchAgent per library so drops convert
automatically in the background. On **Linux/Windows**, create the folders with
`install.py` and run `--watch` (e.g. under systemd, Task Scheduler, or just a
terminal). Auto-watcher contributions for other platforms are welcome.

---

## What the conversion cleans up

EPUBs (and Calibre's PDF→EPUB output) carry a lot of styling cruft. The converter:

- strips leftover `<span class="...">` / `<div>` / `class=` wrappers — **but skips
  fenced ` ``` ` code blocks and inline `` `code` ``**, so HTML/CSS examples in
  technical books keep their attributes;
- merges bold/italic runs that those wrappers split apart;
- rejects file-path-as-title chapters (a Calibre PDF→EPUB quirk) and falls back to
  a real heading.

---

## Honest caveats

- **Scanned / image-only PDFs won't convert to text** — there's no OCR. They'd
  produce empty files, so check first: `pdftotext -f 1 -l 5 book.pdf - | wc -c`
  (near-zero = scanned). Keep those out of the inbox.
- **PDFs split into coarser chapters than EPUBs**, because a PDF has no real table
  of contents. The text and images come through; the chaptering is rougher. Prefer
  a native EPUB when one exists.
- **Only `.epub` and `.pdf` are handled.** Convert other formats with Calibre first.
- This processes **your own** books. It hosts nothing and ships no content.

---

## Contributing

Issues and PRs welcome — especially Linux/Windows auto-watchers, OCR for scanned
PDFs, and a category auto-sorter. Keep it dependency-light (standard library +
the two external tools).

## License

[MIT](LICENSE) © 2026 Claudia Vaduvescu
