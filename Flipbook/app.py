"""Flipbook as a native desktop window (pywebview + Edge WebView2).

Run:  python app.py [file.pdf | folder ...]
Frozen with PyInstaller (--onefile --windowed) this becomes Flipbook.exe.
"""
import ctypes
import json
import os
import sys
import threading
import traceback
from urllib.parse import quote

import serve

# localStorage is scoped to the origin, and the origin includes the port.
# So we use a FIXED preferred port (so last-read page, theme and sidebar state
# persist between launches) and only fall back to the next free ones if taken.
PREFERRED_PORT = 47863
PORT_TRIES = 50

WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
APPDIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Flipbook")


def message_box(text, title="Flipbook"):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def log_error(text):
    try:
        os.makedirs(APPDIR, exist_ok=True)
        with open(os.path.join(APPDIR, "error.log"), "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def start_server():
    """Bind 127.0.0.1 on the preferred port, else the next free one."""
    last = None
    for port in range(PREFERRED_PORT, PREFERRED_PORT + PORT_TRIES):
        try:
            server = serve.make_server(port)
            break
        except OSError as e:
            last = e
    else:
        raise last
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Api:
    """Exposed to the page as window.pywebview.api (window-level fullscreen, the Open dialog)."""

    def toggle_fullscreen(self):
        import webview
        webview.windows[0].toggle_fullscreen()

    def choose_pdfs(self, multiple=False):
        """Windows' Open dialog. The chosen PDFs go on the book list (with their full path, so they can
        be opened again later). Returns [{id, name, size}] or [] if cancelled."""
        import webview
        try:
            paths = webview.windows[0].create_file_dialog(
                webview.FileDialog.OPEN, allow_multiple=bool(multiple), file_types=("PDF files (*.pdf)", "All files (*.*)"))
        except Exception:
            log_error("open dialog: " + traceback.format_exc())
            return []
        if not paths:
            return []
        return [{"id": b["id"], "name": b["name"], "size": b.get("size", 0)} for b in serve.books_add(list(paths))]

    def mydict_pick(self, kind="files"):
        """Add-ons > "Add a dictionary (PDF)…" (v1.8.18): Windows' file dialog (one or more PDFs, e.g. the
        volumes of one dictionary) or folder dialog (one PDF per letter). The chosen files are checked and
        kept on the Python side under a one-time token; the page gets the token, a suggested name and kind,
        and sends back only the token with the name and kind the user chose."""
        import webview
        try:
            w = webview.windows[0]
            if kind == "folder":
                paths = w.create_file_dialog(webview.FileDialog.FOLDER)
            else:
                paths = w.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                             file_types=("PDF files (*.pdf)", "All files (*.*)"))
        except Exception:
            log_error("mydict dialog: " + traceback.format_exc())
            return {"error": "The file dialog could not be opened."}
        if not paths:
            return {"cancelled": True}
        try:
            return serve.mydict_prepare(list(paths) if not isinstance(paths, str) else [paths])
        except Exception as e:
            log_error("mydict prepare: " + traceback.format_exc())
            return {"error": str(e)}

    def legal_import(self, kind="folder"):
        """Add-ons > Legal dictionary > "Add your Black's PDFs…" (v1.8.17): Windows' folder dialog (or a
        file dialog for PDFs), then build that edition into addons\\legal on this PC. The path never
        comes from the page. Returns the legal status, {"cancelled": True}, or {"error": ...}."""
        import webview
        try:
            w = webview.windows[0]
            if kind == "files":
                paths = w.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                             file_types=("PDF files (*.pdf)", "All files (*.*)"))
            else:
                paths = w.create_file_dialog(webview.FileDialog.FOLDER)
        except Exception:
            log_error("legal dialog: " + traceback.format_exc())
            return {"error": "The folder dialog could not be opened."}
        if not paths:
            return {"cancelled": True}
        try:
            return serve.legal_import(list(paths) if not isinstance(paths, str) else [paths])
        except Exception as e:
            log_error("legal import: " + traceback.format_exc())
            return {"error": str(e)}


def on_drop(window, event):
    """A file was dropped on the window: WebView2 gives Python its full path (the page only gets the
    contents), so it can be put on the book list and opened from there."""
    try:
        files = (event.get("dataTransfer") or {}).get("files") or []
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        books = serve.books_add(paths)
        if books:
            b = books[0]
            window.evaluate_js("window.flipbookOpenBook && window.flipbookOpenBook(%s)" %
                               json.dumps({"id": b["id"], "name": b["name"], "size": b.get("size", 0)}))
    except Exception:
        log_error("drop: " + traceback.format_exc())


def on_loaded(window):
    try:
        window.dom.document.events.drop += lambda e: on_drop(window, e)
        window.evaluate_js("window.__fbNativeDrop = true")
    except Exception:
        log_error("drop setup: " + traceback.format_exc())


def main():
    import webview

    serve.collect_launched(sys.argv[1:])
    server = start_server()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/flipbook.html"
    if len(serve.LAUNCHED) == 1:
        url += "?open=" + quote(serve.pdf_id(serve.LAUNCHED[0]))

    window = webview.create_window(
        "Flipbook", url, width=1400, height=900, min_size=(800, 560),
        background_color="#1c1e21", maximized=True, js_api=Api(),
    )
    window.events.loaded += lambda: on_loaded(window)
    storage = os.path.join(APPDIR, "WebView")
    os.makedirs(storage, exist_ok=True)
    # The window icon comes from the full multi-size flipbook.ico (built into the exe), so Windows
    # shows the hand-drawn 16/20/24 px book in the title bar. Without it pywebview takes only the
    # exe's 32 px image and shrinks it, which blurs (v1.8.4).
    ico = os.path.join(serve.STATIC, "flipbook.ico")
    try:
        webview.start(gui="edgechromium", private_mode=False, storage_path=storage,
                      icon=ico if os.path.isfile(ico) else None)
    except Exception:
        log_error(traceback.format_exc())
        message_box("Flipbook could not start its window.\n\n"
                    "Microsoft Edge WebView2 Runtime is required.\n"
                    "Download it from:\n" + WEBVIEW2_URL + "\n\n"
                    "Details were written to " + os.path.join(APPDIR, "error.log"))
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--voice-worker":
        # Flipbook.exe started by itself as the graphics-card voice helper (kokoro_gpu.Engine)
        import kokoro_gpu
        try:
            code = kokoro_gpu.worker_main(sys.argv[2:]) or 0
        except Exception:
            log_error("voice helper: " + traceback.format_exc())
            code = 1
        os._exit(code)
    code = 1
    try:
        code = main()
    except Exception:
        log_error(traceback.format_exc())
        message_box("Flipbook hit an unexpected error.\nDetails: " + os.path.join(APPDIR, "error.log"))
    finally:
        for s in (sys.stdout, sys.stderr):
            try:
                if s:
                    s.flush()
            except Exception:
                pass
        os._exit(code)
