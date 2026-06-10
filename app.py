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
            p = paths[0] if isinstance(paths, (list, tuple)) else paths
            log.info("User picked PDF: %s", p)
            return p
        except Exception as e:
            log.exception("pick_pdf failed: %s", e)
            return None

    def save_pdf(self, url: str, default_filename: str = "report.pdf"):
        """Open native Save dialog → fetch PDF from backend URL → write to disk.

        WKWebView's `<a download>` attribute is unreliable. This method uses
        pywebview's create_file_dialog(SAVE_DIALOG) for the picker + httpx to
        download the bytes. Returns {ok, path} or {ok: false, error, cancelled}.
        """
        try:
            import httpx
            win = webview.windows[0] if webview.windows else None
            if not win:
                return {"ok": False, "error": "No window"}
            # Open save dialog
            path = win.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=default_filename,
                file_types=("PDF Files (*.pdf)",),
            )
            if not path:
                return {"ok": False, "cancelled": True}
            if isinstance(path, (list, tuple)):
                path = path[0]
            # If the URL is relative, prepend the backend host
            if url.startswith("/"):
                url = f"http://127.0.0.1:{self.backend_port}{url}"
            log.info("Downloading %s → %s", url, path)
            with httpx.Client(timeout=120, follow_redirects=True) as c:
                r = c.get(url)
                r.raise_for_status()
                from pathlib import Path
                Path(path).write_bytes(r.content)
            return {"ok": True, "path": str(path), "size": len(r.content)}
        except Exception as e:
            log.exception("save_pdf failed: %s", e)
            return {"ok": False, "error": str(e)}

    def save_pdf_blob(self, base64_data: str, default_filename: str = "report.pdf"):
        """Save a base64-encoded PDF (e.g. from /pdf-with-options POST) to disk."""
        try:
            import base64 as _b64
            win = webview.windows[0] if webview.windows else None
            if not win:
                return {"ok": False, "error": "No window"}
            path = win.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=default_filename,
                file_types=("PDF Files (*.pdf)",),
            )
            if not path:
                return {"ok": False, "cancelled": True}
            if isinstance(path, (list, tuple)):
                path = path[0]
            # Strip data URL prefix if present
            if "," in base64_data and base64_data.startswith("data:"):
                base64_data = base64_data.split(",", 1)[1]
            data = _b64.b64decode(base64_data)
            from pathlib import Path
            Path(path).write_bytes(data)
            return {"ok": True, "path": str(path), "size": len(data)}
        except Exception as e:
            log.exception("save_pdf_blob failed: %s", e)
            return {"ok": False, "error": str(e)}

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
