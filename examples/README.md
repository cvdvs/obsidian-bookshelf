# Example walkthrough

This repo ships **no books** — it processes your own library. To try it with a
legal, public-domain title, grab one from [Project Gutenberg](https://www.gutenberg.org/)
(everything there is free to download and redistribute).

## 1. Configure

```bash
cp config.example.ini config.ini
```

Edit `config.ini` so one library points at a real (or test) Obsidian vault. For
a throwaway test you can point it at a scratch folder:

```ini
[paths]
library_root = ~/bookshelf-demo
vault_root = ~/bookshelf-demo/vault
ebook_convert = /Applications/calibre.app/Contents/MacOS/ebook-convert

[library:demo]
vault_books_dir = books
unsorted = false
```

## 2. Create the folders

```bash
python3 install.py --no-watch
```

This makes `~/bookshelf-demo/demo-library/{_inbox,epub,pdf}`.

## 3. Drop a book and convert

Download a Gutenberg EPUB (e.g. *The Time Machine* by H. G. Wells) into the
inbox, then run once:

```bash
cp ~/Downloads/pg35.epub ~/bookshelf-demo/demo-library/_inbox/
python3 scripts/process_inbox.py demo
```

## 4. What you get

```
~/bookshelf-demo/vault/books/
└── pg35_md/
    ├── README.md                  ← table of contents / conversion log
    ├── chapters/
    │   ├── 01_The_Time_Traveller.md
    │   ├── 02_The_Machine.md
    │   └── ...
    ├── supporting_sections/       ← front matter, notes, etc.
    └── images/
```

The original EPUB is filed in `demo-library/epub/`, and the inbox is empty
again. Open the `pg35_md` folder in Obsidian and you have the book as linkable,
per-chapter notes.

For hands-off use on macOS, run `python3 install.py` instead and the watcher
converts anything you drop into `_inbox/` automatically.
