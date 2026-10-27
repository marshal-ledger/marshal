"""Capture the web view screenshots in the README, in light and dark.

Serves a store (by default the one ``marshal demo`` builds) on a free local port and
saves each page with headless Chrome:

    python scripts/capture_screenshots.py [--db demo.sqlite] [--chrome PATH]
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import uvicorn

from marshal_ledger.web import WebSettings, create_web_app

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
PAGES = {
    "web-inventory": "/",
    "web-drift": "/agent?" + urlencode({"id": "eval_testAgent"}),
}
CHROME = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "google-chrome", "chromium", "chromium-browser"]


def find_chrome(given: str | None) -> str:
    for candidate in ([given] if given else CHROME):
        found = shutil.which(candidate) or (candidate if os.path.exists(candidate) else None)
        if found:
            return found
    sys.exit("Chrome not found; pass --chrome PATH")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="demo.sqlite")
    parser.add_argument("--chrome")
    args = parser.parse_args()
    chrome = find_chrome(args.chrome)

    port = free_port()
    app = create_web_app(args.db, WebSettings(enabled=True, allow_unauthenticated=True))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    try:
        for name, path in PAGES.items():
            for scheme in ("light", "dark"):
                out = ASSETS / f"{name}-{scheme}.png"
                with tempfile.TemporaryDirectory() as profile:
                    subprocess.run([
                        chrome, "--headless=new", f"--user-data-dir={profile}", "--hide-scrollbars",
                        "--window-size=1440,900", "--force-device-scale-factor=2",
                        *(["--force-dark-mode", "--blink-settings=preferredColorScheme=0"] if scheme == "dark"
                          else ["--blink-settings=preferredColorScheme=1"]),
                        f"--screenshot={out}", f"http://127.0.0.1:{port}{path}",
                    ], check=True, capture_output=True)
                print(out.relative_to(ROOT))
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()
