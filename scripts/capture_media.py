"""Record the web view walkthrough and screenshots used in the README and on the site.

Serves a store (by default one built by ``marshal demo``) on a free local port and drives
it with Playwright in the installed Chrome. Needs ffmpeg for the GIF:

    pip install playwright
    python scripts/capture_media.py [--db demo.sqlite] [--only screenshots|gif]

Writes docs/assets/web-tour.gif and docs/assets/web-{inventory,drift}-{light,dark}.png.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from playwright.sync_api import Page, sync_playwright

from marshal_ledger.web import WebSettings, create_web_app

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
DRIFTED = "eval_testAgent"
PROPOSED = "agent_openai_agents_sdk/agent_openai_agents_sdk"
SCREENSHOTS = {"web-inventory": "/", "web-drift": "/agent?" + urlencode({"id": DRIFTED})}
SIZE = {"width": 1280, "height": 760}

# Video has no pointer, so the page draws one that follows the mouse.
POINTER = """
addEventListener('DOMContentLoaded', () => {
  const dot = document.createElement('div');
  dot.style.cssText = 'position:fixed;z-index:9999;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;'
    + 'background:rgba(29,28,27,.35);border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4);pointer-events:none;'
    + 'left:' + (sessionStorage.x || 640) + 'px;top:' + (sessionStorage.y || 380) + 'px;transition:transform .12s';
  document.body.appendChild(dot);
  addEventListener('mousemove', e => { dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px';
    sessionStorage.x = e.clientX; sessionStorage.y = e.clientY; });
  addEventListener('mousedown', () => dot.style.transform = 'scale(.7)');
  addEventListener('mouseup', () => dot.style.transform = '');
});
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(db: str) -> tuple[str, uvicorn.Server]:
    port = free_port()
    app = create_web_app(db, WebSettings(enabled=True, allow_unauthenticated=True))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server


def glide(page: Page, selector: str, click: bool = True) -> None:
    """Scroll an element into view, move the pointer to it in small steps, then click it."""
    box = page.locator(selector).first.bounding_box()
    if box["y"] + box["height"] > SIZE["height"] - 40 or box["y"] < 0:
        scroll(page, int(box["y"] - SIZE["height"] / 2))
        page.wait_for_timeout(300)
        box = page.locator(selector).first.bounding_box()
    page.mouse.move(box["x"] + min(box["width"] / 2, 60), box["y"] + box["height"] / 2, steps=25)
    page.wait_for_timeout(350)
    if click:
        page.mouse.down()
        page.mouse.up()
        page.wait_for_load_state()
        page.wait_for_timeout(300)


def scroll(page: Page, pixels: int) -> None:
    page.evaluate("y => scrollBy(0, y)", pixels)
    page.wait_for_timeout(400)


def tour(page: Page, base: str) -> None:
    page.goto(base + "/")
    page.wait_for_timeout(1800)
    # The inventory: one environment in drift.
    glide(page, f'a.name[href="agent?{urlencode({"id": DRIFTED})}"]')
    page.wait_for_timeout(2600)  # three HIGH changes: the tool now runs on the maker's credentials
    scroll(page, 680)
    page.wait_for_timeout(1800)  # the tools, and whose credentials each uses
    glide(page, 'aside a[href="./"]')
    page.wait_for_timeout(600)
    glide(page, f'a.name[href="agent?{urlencode({"id": PROPOSED})}"]')
    page.wait_for_timeout(2200)  # a proposed change: a refund function and the grant to run it
    glide(page, "aside a[href='deps']")
    glide(page, 'input[name="q"]')
    page.keyboard.type("*CheckWeather", delay=70)
    page.wait_for_timeout(300)
    glide(page, "form button")
    page.wait_for_timeout(2600)  # the Agentforce agents that share one Apex action


def record_gif(browser, base: str) -> None:
    with tempfile.TemporaryDirectory() as work:
        context = browser.new_context(viewport=SIZE, record_video_dir=work, record_video_size=SIZE, color_scheme="light")
        context.add_init_script(POINTER)
        page = context.new_page()
        tour(page, base)
        video = Path(page.video.path())
        context.close()
        out = ASSETS / "web-tour.gif"
        # The first half second is a blank page while the browser starts.
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-y", "-ss", "0.6", "-i", str(video), "-vf",
            "fps=12,scale=1000:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
            str(out),
        ], check=True)
        print(out.relative_to(ROOT), f"{out.stat().st_size // 1024} KB")


def screenshots(browser, base: str) -> None:
    for scheme in ("light", "dark"):
        context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, color_scheme=scheme)
        page = context.new_page()
        for name, path in SCREENSHOTS.items():
            page.goto(base + path)
            out = ASSETS / f"{name}-{scheme}.png"
            page.screenshot(path=str(out))
            print(out.relative_to(ROOT))
        context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="demo.sqlite")
    parser.add_argument("--only", choices=["screenshots", "gif"])
    args = parser.parse_args()
    base, server = serve(args.db)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            if args.only != "screenshots":
                record_gif(browser, base)
            if args.only != "gif":
                screenshots(browser, base)
            browser.close()
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()
