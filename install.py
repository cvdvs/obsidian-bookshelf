#!/usr/bin/env python3
"""obsidian-bookshelf installer.

Reads config.ini and:
  1. Creates each library's folders (<name>-library/_inbox, epub, pdf,
     plus _unsorted/ subfolders when that library is marked unsorted).
  2. On macOS, generates a LaunchAgent per library from
     templates/launchagent.plist.template and loads it, so dropping a book
     into an _inbox auto-converts it.

On Linux/Windows the folders are created and you run the watcher yourself:
    python3 scripts/process_inbox.py <library> --watch

Usage:
  python3 install.py            # create folders (+ install watchers on macOS)
  python3 install.py --no-watch # create folders only
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "scripts"))
import process_inbox as pi  # noqa: E402

PYTHON = sys.executable
SCRIPT = str(HERE / "scripts" / "process_inbox.py")
TEMPLATE = HERE / "templates" / "launchagent.plist.template"


def make_folders(conf: dict) -> None:
    for name in conf["libraries"]:
        cfg = pi.cfg_for(name, conf)
        base = conf["library_root"] / f"{name}-library"
        for sub in ("_inbox", "epub", "pdf"):
            (base / sub).mkdir(parents=True, exist_ok=True)
        if conf["libraries"][name]["unsorted"]:
            (base / "epub" / "_unsorted").mkdir(parents=True, exist_ok=True)
            (base / "pdf" / "_unsorted").mkdir(parents=True, exist_ok=True)
        (conf["library_root"] / ".logs").mkdir(parents=True, exist_ok=True)
        print(f"  folders ready: {base}")


def install_watchers_macos(conf: dict) -> None:
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    tpl = TEMPLATE.read_text(encoding="utf-8")
    for name in conf["libraries"]:
        cfg = pi.cfg_for(name, conf)
        plist = (tpl
                 .replace("{{LIBRARY}}", name)
                 .replace("{{PYTHON}}", PYTHON)
                 .replace("{{SCRIPT}}", SCRIPT)
                 .replace("{{INBOX}}", str(cfg["inbox"]))
                 .replace("{{LOG}}", str(cfg["log"]).removesuffix(".log")))
        dest = agents / f"com.obsidian-bookshelf.{name}.watcher.plist"
        dest.write_text(plist, encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(dest)],
                       capture_output=True)
        r = subprocess.run(["launchctl", "load", "-w", str(dest)],
                           capture_output=True, text=True)
        ok = "loaded" if r.returncode == 0 else f"FAILED ({r.stderr.strip()})"
        print(f"  watcher {name}: {ok}")


def main() -> None:
    no_watch = "--no-watch" in sys.argv
    conf = pi.load_config(None)
    print(f"Using config: {conf['config_path']}")
    print("Creating library folders...")
    make_folders(conf)
    if no_watch:
        print("Skipping watcher install (--no-watch).")
    elif sys.platform == "darwin":
        print("Installing macOS watchers...")
        install_watchers_macos(conf)
        print("\nDone. Drop a book into any <library>-library/_inbox/ and it converts.")
    else:
        print("\nFolders ready. This OS has no auto-watcher set up — run, per library:")
        for name in conf["libraries"]:
            print(f"  python3 scripts/process_inbox.py {name} --watch")


if __name__ == "__main__":
    main()
