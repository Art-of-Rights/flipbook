"""Local web server for the Flipbook PDF reader.

- Lists every PDF in this folder (your "library") and any PDFs passed on the
  command line (e.g. dragged onto "Open Flipbook.bat").
- Supports HTTP Range requests, so even huge PDFs open quickly and pages load
  on demand.

Run:  python serve.py [file.pdf ...]
"""
import hashlib
import json
import os
import re
import socket
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

if getattr(sys, "frozen", False):
    # PyInstaller exe: bundled viewer files live in the temp extraction dir,
    # while the library and saved data live next to the exe.
    STATIC = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    STATIC = HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "flipbook-data")   # saved contents + bookmarks, one JSON per book
PORT = 8765
LAUNCHED = []   # absolute paths of PDFs given on the command line
VERSION = "1.8.20"

try:
    import tts                     # natural AI voice (engine bundled; voice files are an add-on)
except Exception:                  # pragma: no cover - running without the voice engine
    tts = None
_installer = None

try:
    import dictionary              # word look-up (optional add-on, built on the user's PC)
except Exception:                  # pragma: no cover
    dictionary = None
_dict_installer = None

try:
    import legal                   # legal dictionary (v1.8.17): Black's 2nd (download) + the user's own 4th/5th PDFs
except Exception:                  # pragma: no cover
    legal = None
_legal_installer = None
_legal_queue = []                  # editions still to build after the current one (one folder holding both, 1.8.18)

try:
    import mydict                  # the user's own dictionaries from PDF files (v1.8.18): legal or general
except Exception:                  # pragma: no cover
    mydict = None
_mydict_installer = None
_mydict_pending = None             # {"token", "files"}: the PDFs just chosen in the app's dialog, not yet built

try:
    import qwen                  # Qwen3-TTS voices on NVIDIA graphics cards (optional add-on)
except Exception:                  # pragma: no cover
    qwen = None
_qwen_installer = None


def qwen_installer():
    global _qwen_installer
    if _qwen_installer is None and qwen:
        _qwen_installer = qwen.Installer(HERE)
    return _qwen_installer


def qwen_status():
    g = qwen.gpu_check()
    inst = qwen.installed(HERE)
    return {"available": bool(g.get("ok")), "reason": g.get("reason", ""),
            "gpu": {k: g.get(k) for k in ("name", "vram_mb", "driver", "compute")},
            "installed": inst, "size": qwen.DOWNLOAD_BYTES, "disk": qwen.DISK_BYTES,
            "download": qwen_installer().status(),
            "engine": qwen.get_engine(HERE).state() if inst else {"state": "stopped", "error": ""},
            "voices": [{"id": s, "label": label} for s, label in qwen.SPEAKERS]}


def dict_installer():
    global _dict_installer
    if _dict_installer is None and dictionary:
        _dict_installer = dictionary.Installer(HERE)
    return _dict_installer


def dict_status():
    if not dictionary:
        return {"available": False, "installed": False, "size": 0, "download": None}
    return {"available": True, "installed": dictionary.installed(HERE), "size": dictionary.DOWNLOAD_BYTES,
            "download": dict_installer().status()}


def legal_installer():
    global _legal_installer
    if _legal_installer is None and legal:
        _legal_installer = legal.Installer(HERE)
    return _legal_installer


def legal_status():
    if not legal:
        return {"available": False, "installed": False, "sources": [], "size": 0, "download": None}
    i = legal.info(HERE)
    st = legal_installer().status()
    queue = list(_legal_queue)
    if queue and st.get("state") in ("done", "idle"):
        # between two editions of one folder: still busy, the next one is about to start
        st = dict(st, state="building", source=queue[0], pct=0, message="Starting the next edition", error=None)
    return {"available": True, "installed": i.get("installed", False), "sources": i.get("sources", []),
            "bytes": i.get("bytes", 0), "size": legal.DOWNLOAD_BYTES, "download": st, "queue": queue}


def _looks_blacks(folder, files):
    """Black's in the folder or file names, or on the first file's first pages (so another book whose
    title page says "Fifth Edition" isn't taken for Black's 5th)."""
    if "black" in (folder + " " + " ".join(os.path.basename(f) for f in files[:3])).lower():
        return True
    try:
        from pypdf import PdfReader
        r = PdfReader(files[0])
        t = " ".join((r.pages[i].extract_text() or "") for i in range(min(3, len(r.pages))))
        return "BLACK" in t.upper()
    except Exception:
        return False


def legal_jobs(paths):
    """[(edition, files)] for the chosen folders: a folder of one edition's PDFs, or (1.8.18) a folder holding
    one folder per edition, such as "Black's Law" with "Black's Law 4th" and "Black's Law 5th Edition A-Z"."""
    jobs, seen = [], set()
    for p in paths:
        if is_network_path(p):
            continue
        cands = []
        files = legal.pdf_files(p)
        if files:
            cands.append((p, files))
        if os.path.isdir(p):
            try:
                subs = sorted(os.listdir(p), key=legal._natural)
            except OSError:
                subs = []
            for n in subs:
                sub = os.path.join(p, n)
                if os.path.isdir(sub) and not is_network_path(sub):
                    f = legal.pdf_files(sub)
                    if f:
                        cands.append((sub, f))
        for folder, files in cands:
            ed = legal.detect_edition(files)
            if ed and ed not in seen and _looks_blacks(folder, files):
                seen.add(ed)
                jobs.append((ed, files))
    return jobs


def _legal_run_rest(jobs):
    """Build the other editions of one choice, one after the other, after the first one."""
    inst = legal_installer()
    try:
        for k, (ed, files) in enumerate(jobs):
            if k:
                if inst.status().get("state") != "done" or not _legal_queue:
                    break                                   # cancelled, failed or removed: stop here
                inst.start_import(files, ed)
                _legal_queue[:] = [e for e, f in jobs[k + 1:]]
            t = inst.thread
            if t:
                t.join()
    finally:
        _legal_queue[:] = []


def legal_import(paths):
    """Start building the legal dictionary from the user's own Black's PDFs (chosen in the app's folder
    dialog, Python side: the page never sends a path)."""
    if not legal:
        return {"error": "The legal dictionary is not available in this copy of Flipbook."}
    if legal_installer().busy() or _legal_queue:
        return {"error": "A legal dictionary is being built already. Wait for it to finish, or cancel it."}
    if not any(legal.pdf_files(p) or os.path.isdir(p) for p in paths if not is_network_path(p)):
        return {"error": "No PDF files were found there."}
    jobs = legal_jobs(paths)
    if not jobs:
        return {"error": "These PDFs don't look like Black's Law Dictionary, 4th or 5th edition."}
    legal_installer().start_import(jobs[0][1], jobs[0][0])
    _legal_queue[:] = [e for e, f in jobs[1:]]
    if len(jobs) > 1:
        threading.Thread(target=_legal_run_rest, args=(jobs,), daemon=True).start()
    st = legal_status()
    st["edition"] = jobs[0][0]
    st["editions"] = [e for e, f in jobs]
    return st


# ---------------------------------------------------------------- the user's own dictionaries (1.8.18)
def mydict_installer():
    global _mydict_installer
    if _mydict_installer is None and mydict:
        _mydict_installer = mydict.Installer(HERE)
    return _mydict_installer


def mydict_status():
    if not mydict:
        return {"available": False, "installed": False, "dicts": [], "bytes": 0, "max": 0, "build": None}
    i = mydict.info(HERE)
    return {"available": True, "installed": i.get("installed", False), "dicts": i.get("dicts", []),
            "bytes": i.get("bytes", 0), "max": mydict.MAX_DICTS, "build": mydict_installer().status()}


def mydict_prepare(paths):
    """The PDFs chosen in the app's dialog (Python side): checked, then kept here under a one-time token, so
    the page only ever sends the token back with the name and kind the user chose, never a path."""
    global _mydict_pending
    if not mydict:
        return {"error": "Adding your own dictionaries is not available in this copy of Flipbook."}
    files = []
    for p in paths:
        if is_network_path(p):
            continue
        if os.path.isdir(p):
            found = [os.path.join(p, n) for n in sorted(os.listdir(p), key=_natural_key)
                     if n.lower().endswith(".pdf") and os.path.isfile(os.path.join(p, n))]
        else:
            found = [p] if p.lower().endswith(".pdf") and os.path.isfile(p) else []
        files += [f for f in found if f not in files and not is_network_path(f)]
    if not files:
        return {"error": "No PDF files were found there."}
    try:
        chk = mydict.check_pdfs(files)
    except Exception as e:
        return {"error": f"Flipbook couldn't open that PDF ({e})."}
    if not chk.get("text"):
        return {"error": "This PDF has no text in it (a scan without OCR), so Flipbook can't read its words. "
                         "A PDF that has been through text recognition (OCR) works."}
    title = mydict.suggest_title(files)
    token = hashlib.sha256(os.urandom(32)).hexdigest()[:24]
    _mydict_pending = {"token": token, "files": files}
    return {"token": token, "count": len(files), "names": [os.path.basename(f) for f in files[:8]],
            "pages": chk.get("pages", 0), "title": title, "kind": mydict.guess_kind(files, title)}


def _natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def installer():
    global _installer
    if _installer is None and tts:
        _installer = tts.Installer(HERE)
    return _installer


_gpu_installer = None


def gpu_installer():
    global _gpu_installer
    if _gpu_installer is None and tts:
        _gpu_installer = tts.GpuInstaller(HERE)
    return _gpu_installer


def voice_status():
    ok = bool(tts) and tts.engine_available()
    st = {"engine": ok, "installed": ok and tts.installed(HERE),
          "size": tts.ADDON_BYTES if tts else 0, "download": None, "voices": []}
    if ok:
        st["download"] = installer().status()
        st["voices"] = [{"id": v, "label": label} for v, label, _ in tts.VOICES]
        try:
            st["device"] = tts.device(HERE).status()
            st["device"]["gpu_model"]["download"] = gpu_installer().status()
        except Exception as e:      # never let the GPU check break the voice
            st["device"] = {"mode": "cpu", "active": "cpu", "note": f"Graphics-card check failed: {e}",
                            "gpu": {"ok": False, "reason": str(e)}, "gpu_model": {"installed": False, "size": 0}}
    return st


_started = False


def startup():
    """Background checks once per run (the graphics card for the AI voice)."""
    global _started
    if _started:
        return
    _started = True
    if tts and tts.engine_available():
        tts.startup_check(HERE)


def pdf_id(path):
    return hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:16]


def folder_pdfs():
    """PDFs lying next to Flipbook.exe: id -> path."""
    files = {}
    for name in sorted(os.listdir(HERE), key=str.lower):
        p = os.path.join(HERE, name)
        if name.lower().endswith(".pdf") and os.path.isfile(p):
            files[pdf_id(p)] = p
    return files


def library():
    """All PDFs we are willing to serve: id -> path (command line, the folder, the saved book list)."""
    files = {}
    for b in books_load()["books"]:
        files[b["id"]] = b["path"]
    for p in LAUNCHED:
        files[pdf_id(p)] = p
    files.update(folder_pdfs())
    return files


# ---------------------------------------------------------------- the book list (v1.5)
# flipbook-data\books.json: every book opened (with its last page) or added "for later".
# Removing a book only takes it off the list; the PDF, its bookmarks and contents are kept.
BOOKS = lambda: os.path.join(DATA, "books.json")
_books_lock = threading.RLock()


def books_load():
    with _books_lock:
        try:
            with open(BOOKS(), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        d.setdefault("books", [])
        d.setdefault("hidden", [])           # PDFs in Flipbook's folder the user removed from the list
        return d


def books_save(d):
    with _books_lock:
        os.makedirs(DATA, exist_ok=True)
        with open(BOOKS() + ".tmp", "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, ensure_ascii=False)
        os.replace(BOOKS() + ".tmp", BOOKS())


def _now():
    import time
    return int(time.time())


def books_add(paths, opened=False):
    """Add PDFs (by full path) to the list. Returns their entries. Ignores non-PDFs / missing files."""
    out = []
    with _books_lock:
        d = books_load()
        by_id = {b["id"]: b for b in d["books"]}
        for p in paths:
            if not isinstance(p, str):
                continue
            p = os.path.abspath(p)
            if not p.lower().endswith(".pdf") or not os.path.isfile(p):
                continue
            i = pdf_id(p)
            b = by_id.get(i)
            if b is None:
                b = {"id": i, "path": p, "name": os.path.basename(p), "title": "", "added": _now(),
                     "opened": None, "page": 0, "pages": 0}
                d["books"].append(b)
                by_id[i] = b
            b["size"] = os.path.getsize(p)
            if opened:
                b["opened"] = _now()
            if i in d["hidden"]:
                d["hidden"].remove(i)
            out.append(b)
        books_save(d)
    return out


def is_network_path(p):
    """UNC and device paths (\\\\server\\share\\..., //server/..., \\\\?\\UNC\\..., \\\\.\\...): anything that is not a
    plain local drive path. Checked on the raw text, before any file-system call touches it."""
    return p.strip().replace("/", "\\").startswith("\\\\")


def books_progress(i, page, pages, title):
    """The reader opened a book or turned a page: remember where."""
    with _books_lock:
        d = books_load()
        b = next((x for x in d["books"] if x["id"] == i), None)
        if b is None:
            path = library().get(i)
            if not path:
                return False
            b = books_add([path])[0]
            d = books_load()
            b = next(x for x in d["books"] if x["id"] == i)
        b["opened"] = _now()
        if isinstance(page, int) and page > 0:
            b["page"] = page
        if isinstance(pages, int) and pages > 0:
            b["pages"] = pages
        if isinstance(title, str) and title.strip():
            b["title"] = title.strip()[:300]
        if i in d["hidden"]:
            d["hidden"].remove(i)
        books_save(d)
        return True


def books_remove(i):
    with _books_lock:
        d = books_load()
        d["books"] = [b for b in d["books"] if b["id"] != i]
        if i in folder_pdfs() and i not in d["hidden"]:
            d["hidden"].append(i)
        books_save(d)


def books_status():
    """{last, books}: the list for the start screen / Library, newest first.
    Includes PDFs in Flipbook's folder (unless removed) and command-line PDFs."""
    d = books_load()
    items, seen = [], set()
    for b in d["books"]:
        e = dict(b)
        e["exists"] = os.path.isfile(b["path"])
        e["in_folder"] = os.path.normcase(os.path.dirname(b["path"])) == os.path.normcase(HERE)
        items.append(e)
        seen.add(b["id"])
    extra = [(pdf_id(p), p) for p in LAUNCHED] + list(folder_pdfs().items())
    for i, p in extra:
        if i in seen or i in d["hidden"] or not os.path.isfile(p):
            continue
        seen.add(i)
        items.append({"id": i, "path": p, "name": os.path.basename(p), "title": "", "added": None,
                      "opened": None, "page": 0, "pages": 0, "size": os.path.getsize(p), "exists": True,
                      "in_folder": os.path.normcase(os.path.dirname(p)) == os.path.normcase(HERE)})
    items.sort(key=lambda e: (-(e["opened"] or 0), -(e["added"] or 0), (e["title"] or e["name"]).lower()))
    last = next((e for e in items if e["opened"] and e["exists"]), None)
    return {"last": last, "books": items}


class Handler(SimpleHTTPRequestHandler):
    # v1.8.7: PDF.js 6 is ES modules (.mjs) + WebAssembly. Browsers refuse a module script unless it is served
    # as JavaScript, and Python's guess falls back on the Windows registry, which other software can change.
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".mjs": "text/javascript",
                      ".js": "text/javascript", ".wasm": "application/wasm", ".json": "application/json"}

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def addressed_to_us(self):
        """v1.8.6 security: answer only requests addressed to this server itself.
        A website that points its own name at 127.0.0.1 ("DNS rebinding") still sends its own name in
        the Host header (and its own Origin), so it is refused here and can't read the book list or
        PDFs, or make changes."""
        port = self.server.server_address[1]
        ours = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if (self.headers.get("Host") or "").strip().lower() not in ours:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.strip().lower() in {"http://" + h for h in ours}

    def send_bytes(self, body, ctype="application/json", code=200):
        """Send a complete in-memory response (used by GET and POST handlers)."""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (getattr(self, "_extra_headers", None) or {}).items():
            self.send_header(k, v)
        self._extra_headers = None
        self.end_headers()
        self.wfile.write(body)

    def send_head(self):
        if not self.addressed_to_us():
            self.send_error(403, "Not addressed to Flipbook")
            return None
        url = urlparse(self.path)
        if url.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/flipbook.html")
            self.end_headers()
            return None
        if url.path == "/api/qwen" and not qwen:
            self.send_error(404)
            return None
        if url.path in ("/api/info", "/api/voice", "/api/dict", "/api/dict/lookup", "/api/qwen", "/api/books",
                        "/api/legal", "/api/legal/lookup", "/api/mydict", "/api/mydict/lookup"):
            if url.path == "/api/info":
                data = {"version": VERSION}
            elif url.path == "/api/mydict":
                data = mydict_status()
            elif url.path == "/api/mydict/lookup":
                q = parse_qs(url.query)
                word = (q.get("w") or [""])[0][:80]
                kind = (q.get("kind") or [""])[0] or None
                if not mydict or (kind is not None and kind not in mydict.KINDS) or not mydict.installed(HERE, kind):
                    data = {"error": "not installed"}
                else:
                    try:
                        data = mydict.lookup(HERE, word, kind)
                    except Exception as e:
                        data = {"error": str(e)}
            elif url.path == "/api/legal":
                data = legal_status()
            elif url.path == "/api/legal/lookup":
                word = (parse_qs(url.query).get("w") or [""])[0][:80]
                if not legal or not legal.installed(HERE):
                    data = {"error": "not installed"}
                else:
                    try:
                        data = legal.lookup(HERE, word)
                    except Exception as e:
                        data = {"error": str(e)}
            elif url.path == "/api/books":
                data = books_status()
            elif url.path == "/api/qwen":
                try:
                    data = qwen_status()
                except Exception as e:
                    data = {"available": False, "reason": f"Couldn't check the graphics card ({e})", "installed": False,
                            "size": 0, "disk": 0, "download": None, "engine": None, "voices": []}
            elif url.path == "/api/voice":
                data = voice_status()
            elif url.path == "/api/dict":
                data = dict_status()
            else:
                word = (parse_qs(url.query).get("w") or [""])[0][:80]
                if not dictionary or not dictionary.installed(HERE):
                    data = {"error": "not installed"}
                else:
                    try:
                        data = dictionary.lookup(HERE, word)
                    except Exception as e:
                        data = {"error": str(e)}
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._remaining = None
            from io import BytesIO
            return BytesIO(body)
        if url.path == "/api/library":
            items = [{"id": i, "name": os.path.basename(p), "size": os.path.getsize(p)}
                     for i, p in library().items() if os.path.isfile(p)]
            body = json.dumps(items).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._remaining = None
            from io import BytesIO
            return BytesIO(body)
        m = re.fullmatch(r"/api/store/([0-9a-f]{16,64})", url.path)
        if m:
            p = os.path.join(DATA, m.group(1) + ".json")
            body = open(p, "rb").read() if os.path.isfile(p) else b"null"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._remaining = None
            from io import BytesIO
            return BytesIO(body)
        m = re.fullmatch(r"/pdf/([0-9a-f]{16})", url.path)
        if m:
            path = library().get(m.group(1))
            if not path or not os.path.isfile(path):
                self.send_error(404, "PDF not found")
                return None
            return self.send_file(path, "application/pdf")
        # static files (the viewer itself); never serve PDFs by raw path
        path = self.translate_path(self.path)
        if os.path.isfile(path) and not path.lower().endswith(".pdf"):
            return self.send_file(path, self.guess_type(path))
        self.send_error(404)
        return None

    def do_POST(self):
        """Save per-book data (contents cache, bookmarks); run the AI voice."""
        # Only the Flipbook page sends this header. A web page elsewhere can't add it
        # without a CORS preflight, which this server never approves.
        if self.headers.get("X-Flipbook") != "1" or not self.addressed_to_us():
            self.send_error(403)
            return
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if path.startswith("/api/voice/") or path == "/api/tts":
            return self.voice_post(path, length)
        if path.startswith("/api/books/"):
            try:
                req = json.loads(self.rfile.read(min(length, 200_000)) or b"{}") if length else {}
            except ValueError:
                return self.send_bytes(b'{"error":"bad request"}', code=400)
            if path == "/api/books/progress":
                ok = books_progress(str(req.get("id", "")), req.get("page"), req.get("pages"), req.get("title"))
                return self.send_bytes(json.dumps({"ok": ok}).encode("utf-8"))
            if path == "/api/books/remove":
                books_remove(str(req.get("id", "")))
            elif path == "/api/books/add":
                paths = req.get("paths") or []
                # v1.8.6 security: from the page, only local paths. Even checking whether "\\server\share\x.pdf"
                # exists makes Windows contact that server and send the user's login hash (NTLM). Network
                # shares still work through the app's own Open dialog and drag-and-drop (Python side).
                paths = [p for p in (paths if isinstance(paths, list) else [])
                         if isinstance(p, str) and not is_network_path(p)]
                added = books_add(paths, opened=False)
                st = books_status()
                st["added"] = [b["id"] for b in added]
                return self.send_bytes(json.dumps(st).encode("utf-8"))
            else:
                return self.send_bytes(b'{"error":"unknown"}', code=404)
            return self.send_bytes(json.dumps(books_status()).encode("utf-8"))
        if path.startswith("/api/qwen/"):
            if length:
                self.rfile.read(min(length, 10000))
            if not qwen:
                return self.send_bytes(b'{"error":"Qwen3-TTS is not available in this copy of Flipbook."}', code=501)
            inst = qwen_installer()
            if path == "/api/qwen/install":
                if not qwen.gpu_check().get("ok"):
                    return self.send_bytes(json.dumps({"error": qwen.gpu_check().get("reason")}).encode("utf-8"), code=409)
                inst.start()
            elif path == "/api/qwen/cancel":
                inst.cancel()
            elif path == "/api/qwen/remove":
                if inst.thread and inst.thread.is_alive():
                    inst.cancel()
                    inst.thread.join(15)
                qwen.remove(HERE)
                globals()["_qwen_installer"] = None
            elif path == "/api/qwen/start":
                if qwen.installed(HERE):
                    qwen.get_engine(HERE).start()
            else:
                return self.send_bytes(b'{"error":"unknown"}', code=404)
            return self.send_bytes(json.dumps(qwen_status()).encode("utf-8"))
        if path.startswith("/api/legal/"):
            try:
                req = json.loads(self.rfile.read(min(length, 10000)) or b"{}") if length else {}
            except ValueError:
                req = {}
            if not legal:
                return self.send_bytes(b'{"error":"The legal dictionary is not available in this copy of Flipbook."}', code=501)
            inst = legal_installer()
            if path == "/api/legal/install":          # the free Black's 2nd edition (1910)
                inst.start_download()
            elif path == "/api/legal/cancel":
                _legal_queue[:] = []
                inst.cancel()
            elif path == "/api/legal/remove":
                src = req.get("source") if isinstance(req, dict) else None
                if src is not None and src not in legal.SOURCES:
                    return self.send_bytes(b'{"error":"unknown source"}', code=400)
                _legal_queue[:] = []
                t = getattr(inst, "thread", None)
                if t and t.is_alive():
                    inst.cancel()
                    t.join(15)
                try:
                    legal.remove(HERE, src)
                except (RuntimeError, ValueError, OSError) as e:
                    return self.send_bytes(json.dumps({"error": str(e)}).encode("utf-8"), code=409)
                globals()["_legal_installer"] = None
            else:
                return self.send_bytes(b'{"error":"unknown"}', code=404)
            return self.send_bytes(json.dumps(legal_status()).encode("utf-8"))
        if path.startswith("/api/mydict/"):
            try:
                req = json.loads(self.rfile.read(min(length, 10000)) or b"{}") if length else {}
            except ValueError:
                req = {}
            if not isinstance(req, dict):
                req = {}
            if not mydict:
                return self.send_bytes(b'{"error":"Adding your own dictionaries is not available in this copy of Flipbook."}', code=501)
            inst = mydict_installer()
            known = {d["id"] for d in mydict.info(HERE).get("dicts", [])}
            did = req.get("id")
            title = req.get("title")
            title = re.sub(r"\s+", " ", title).strip()[:80] if isinstance(title, str) else ""
            kind = req.get("kind")
            try:
                if path == "/api/mydict/install":         # the PDFs chosen in the app's dialog (mydict_prepare)
                    pend = _mydict_pending
                    if not pend or not isinstance(req.get("token"), str) or req["token"] != pend["token"]:
                        return self.send_bytes(b'{"error":"Choose the PDF files again."}', code=400)
                    if kind not in mydict.KINDS or not title:
                        return self.send_bytes(b'{"error":"bad name or kind"}', code=400)
                    globals()["_mydict_pending"] = None
                    inst.start_import(pend["files"], title, kind)
                elif path == "/api/mydict/cancel":
                    inst.cancel()
                elif path in ("/api/mydict/remove", "/api/mydict/rename", "/api/mydict/kind"):
                    if did not in known:
                        return self.send_bytes(b'{"error":"unknown dictionary"}', code=400)
                    if path == "/api/mydict/remove":
                        mydict.remove(HERE, did)
                    elif path == "/api/mydict/rename":
                        if not title:
                            return self.send_bytes(b'{"error":"empty name"}', code=400)
                        mydict.rename(HERE, did, title)
                    else:
                        if kind not in mydict.KINDS:
                            return self.send_bytes(b'{"error":"bad kind"}', code=400)
                        mydict.set_kind(HERE, did, kind)
                else:
                    return self.send_bytes(b'{"error":"unknown"}', code=404)
            except (RuntimeError, ValueError, OSError) as e:
                return self.send_bytes(json.dumps({"error": str(e)}).encode("utf-8"), code=409)
            return self.send_bytes(json.dumps(mydict_status()).encode("utf-8"))
        if path.startswith("/api/dict/"):
            if length:
                self.rfile.read(min(length, 10000))
            if not dictionary:
                return self.send_bytes(b'{"error":"The dictionary is not available in this copy of Flipbook."}', code=501)
            inst = dict_installer()
            if path == "/api/dict/install":
                inst.start()
            elif path == "/api/dict/cancel":
                inst.cancel()
            elif path == "/api/dict/remove":
                if inst.thread and inst.thread.is_alive():
                    inst.cancel()
                    inst.thread.join(10)
                dictionary.remove(HERE)
                globals()["_dict_installer"] = None
            else:
                return self.send_bytes(b'{"error":"unknown"}', code=404)
            return self.send_bytes(json.dumps(dict_status()).encode("utf-8"))
        m = re.fullmatch(r"/api/store/([0-9a-f]{16,64})", path)
        if not m or length > 20_000_000:
            self.send_error(400)
            return
        body = self.rfile.read(length)
        try:
            json.loads(body)
        except ValueError:
            self.send_error(400, "Invalid JSON")
            return
        os.makedirs(DATA, exist_ok=True)
        p = os.path.join(DATA, m.group(1) + ".json")
        with open(p + ".tmp", "wb") as f:
            f.write(body)
        os.replace(p + ".tmp", p)
        self.send_response(204)
        self.end_headers()

    def voice_post(self, path, length):
        body = self.rfile.read(min(length, 200_000)) if length else b""
        if not tts or not tts.engine_available():
            return self.send_bytes(b'{"error":"The voice engine is not available in this copy of Flipbook."}', code=501)
        if path == "/api/voice/install":
            installer().start()
        elif path == "/api/voice/cancel":
            installer().cancel()
        elif path == "/api/voice/remove":
            inst = installer()
            if inst.thread and inst.thread.is_alive():
                inst.cancel()
                inst.thread.join(10)
            g = gpu_installer()
            if g.thread and g.thread.is_alive():
                g.cancel()
                g.thread.join(10)
            tts.remove(HERE)
            inst.state, inst.done = "idle", 0
            g.state, g.done = "idle", 0
        elif path == "/api/voice/device":
            try:
                mode = json.loads(body or b"{}").get("mode")
            except ValueError:
                mode = None
            tts.device(HERE).set_mode(mode)
        elif path == "/api/voice/test":
            tts.device(HERE).test_soon(force=True)
        elif path == "/api/voice/gpu/install":
            if not tts.kokoro_gpu.detect_gpu()["ok"]:
                return self.send_bytes(b'{"error":"This graphics card cannot be used."}', code=409)
            gpu_installer().start()
        elif path == "/api/voice/gpu/cancel":
            gpu_installer().cancel()
        elif path == "/api/voice/gpu/remove":
            g = gpu_installer()
            if g.thread and g.thread.is_alive():
                g.cancel()
                g.thread.join(10)
            tts.remove_gpu(HERE)
            g.state, g.done = "idle", 0
        elif path == "/api/tts":
            try:
                req = json.loads(body or b"{}")
            except ValueError:
                return self.send_bytes(b'{"error":"bad request"}', code=400)
            voice = str(req.get("voice", "af_heart"))
            is_qwen = voice.startswith("qwen:")
            if is_qwen and not (qwen and qwen.installed(HERE)):
                return self.send_bytes(b'{"error":"The Qwen3-TTS voices are not installed."}', code=409)
            if not is_qwen and not tts.installed(HERE):
                return self.send_bytes(b'{"error":"The AI voice is not installed."}', code=409)
            try:
                text = str(req.get("text", ""))[:4000]
                gen = int(req.get("gen", 0))
                speed = float(min(2.0, max(0.5, float(req.get("speed", 1.0)))))
                lane = tts.gate.acquire(gen, req.get("prio") != "ahead")
                try:
                    if is_qwen:
                        # Qwen has no speed setting: made at its natural pace, played back faster/slower
                        audio = qwen.get_engine(HERE).synth(tts.normalize(text), voice[5:],
                                                            cancelled=lambda: tts.gate.stale(gen))
                        wav, rate, dev = tts.wav_bytes(audio), round(speed, 3), "qwen"
                    else:
                        wav, rate = tts.get_voice(HERE).synth(text, voice, speed, cancelled=lambda: tts.gate.stale(gen))
                        dev = tts.device(HERE).active
                finally:
                    tts.gate.release(lane)
            except tts.Stale:
                return self.send_bytes(b'{"error":"stale","stale":true}', code=409)
            except Exception as e:
                if qwen and isinstance(e, qwen.Stale):
                    return self.send_bytes(b'{"error":"stale","stale":true}', code=409)
                return self.send_bytes(json.dumps({"error": str(e)}).encode("utf-8"), code=500)
            self._extra_headers = {"X-Playback-Rate": str(rate), "X-Voice-Device": dev}
            return self.send_bytes(wav, "audio/wav")
        else:
            return self.send_bytes(b'{"error":"unknown"}', code=404)
        self.send_bytes(json.dumps(voice_status()).encode("utf-8"))

    def send_file(self, path, ctype):
        size = os.path.getsize(path)
        f = open(path, "rb")
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start, end = max(0, size - int(m.group(2))), size - 1
            end = min(end, size - 1)
            if start > end:
                f.close()
                self.send_error(416)
                return None
            f.seek(start)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            length = end - start + 1
        else:
            self.send_response(200)
            length = size
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self._remaining = length
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        try:
            if remaining is None:
                return super().copyfile(source, outputfile)
            while remaining > 0:
                chunk = source.read(min(1 << 16, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass


def collect_launched(args):
    """Add PDFs (or every PDF inside given folders) from args to LAUNCHED."""
    for arg in args:
        p = os.path.abspath(arg)
        if os.path.isfile(p) and p.lower().endswith(".pdf"):
            LAUNCHED.append(p)
        elif os.path.isdir(p):  # a dropped folder: add every PDF inside it
            for name in sorted(os.listdir(p), key=str.lower):
                if name.lower().endswith(".pdf"):
                    LAUNCHED.append(os.path.join(p, name))


class ExclusiveServer(ThreadingHTTPServer):
    # Python's HTTPServer sets SO_REUSEADDR, which on Windows lets a second
    # program bind a port that is already in use (requests then go to either
    # one). Claim the port exclusively so a second Flipbook moves to the next one.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(port=0):
    """Create (not start) the server on 127.0.0.1. port=0 lets the OS choose."""
    server = ExclusiveServer(("127.0.0.1", port), Handler)
    startup()
    return server


def port_free(p):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", p)) != 0


if __name__ == "__main__":
    collect_launched(sys.argv[1:])
    port = PORT
    while not port_free(port):
        port += 1
    server = make_server(port)
    url = f"http://localhost:{port}/flipbook.html"
    if len(LAUNCHED) == 1:
        url += "?open=" + quote(pdf_id(LAUNCHED[0]))
    print(f"Flipbook running at {url}")
    print("Leave this window open while reading. Close it (or press Ctrl+C) to stop.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
