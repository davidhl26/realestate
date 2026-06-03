"""Flip Board — Desktop launcher.

Starts the FastAPI server on a free localhost port in a background thread,
then opens a native PyWebView window pointing at the local server.

Run: python3 app.py
"""

import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

# Make backend importable
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import webview  # pywebview
import uvicorn

from backend.server import app

log = logging.getLogger("flip-board.app")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def _free_port(default: int = 8765) -> int:
    """Pick a free local TCP port; prefer the default."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", default))
            return default
    except OSError:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def _start_server(port: int):
    """Run uvicorn server (blocking)."""
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.run()


def _wait_for_server(port: int, timeout: float = 10.0) -> bool:
    """Poll until the server accepts connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


class JSApi:
    """Python ↔ JS bridge exposed to the web app via `window.pywebview.api.*`.

    WKWebView's HTML `<input type="file">` is unreliable on macOS — the change
    event sometimes doesn't fire. So we expose native file dialogs through this
    bridge as a bulletproof alternative.
    """

    def __init__(self, backend_port: int):
        self.backend_port = backend_port

    def pick_pdf(self):
        """Open a native macOS file picker and return the chosen path."""
        try:
            win = webview.windows[0] if webview.windows else None
            if not win:
                return None
            paths = win.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("PDF Files (*.pdf)",),
            )
            if not paths:
                return None
            # paths is a tuple/list of strings
            p = paths[0] if isinstance(paths, (list, tuple)) else paths
            log.info("User picked PDF: %s", p)
            return p
        except Exception as e:
            log.exception("pick_pdf failed: %s", e)
            return None

    def ping(self):
        """Sanity check that the JS bridge is wired up."""
        return {"ok": True, "port": self.backend_port}


def main():
    port = _free_port()
    log.info("Starting backend on port %d", port)
    t = threading.Thread(target=_start_server, args=(port,), daemon=True)
    t.start()

    if not _wait_for_server(port):
        log.error("Backend failed to start within timeout")
        sys.exit(1)

    url = f"http://127.0.0.1:{port}/"
    log.info("Opening window at %s", url)

    webview.create_window(
        title="Flip Board — Fix & Flip Evaluator",
        url=url,
        width=1280,
        height=820,
        min_size=(1024, 700),
        resizable=True,
        confirm_close=False,
        js_api=JSApi(backend_port=port),
    )
    # debug=True enables right-click "Inspect Element" so we can see JS errors
    webview.start(debug=True)


if __name__ == "__main__":
    main()
