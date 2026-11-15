"""Security regression test for serve.py (v1.8.6). Run: python Flipbook\\dev\\test_security.py

Starts serve.py's real request handler on a free 127.0.0.1 port with its data folder in a temporary
directory (the user's flipbook-data and add-ons are never touched), then sends forged requests:
  - DNS rebinding: requests carrying another site's Host or Origin must get 403 (fix 2);
  - normal requests from Flipbook's own page must still work;
  - /api/books/add must ignore network (UNC) paths without touching them (fix 3).
No request leaves this computer: the UNC paths point at an unused TEST-NET address (192.0.2.1), and
the test also checks that the check happens before any file-system call.
"""
import http.client
import json
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import serve  # noqa: E402

tmp = tempfile.mkdtemp(prefix="flipbook-sec-")
serve.DATA = os.path.join(tmp, "flipbook-data")
serve.HERE = tmp                     # library folder: empty
serve._started = True                # skip the startup graphics-card check
pdf = os.path.join(tmp, "local test.pdf")
with open(pdf, "wb") as f:
    f.write(b"%PDF-1.4\n%%EOF\n")

srv = serve.make_server(0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []


def req(method, path, host=None, origin=None, body=None, flip=True):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}"}
    if origin:
        h["Origin"] = origin
    if flip and method == "POST":
        h["X-Flipbook"] = "1"
    if body is not None:
        body = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        fails.append(name)


own = f"http://127.0.0.1:{port}"
evil_host = f"rebind.example:{port}"

# --- fix 2: Host / Origin ---
check("own page: GET /api/info", req("GET", "/api/info")[0] == 200)
check("own page via localhost name", req("GET", "/api/info", host=f"localhost:{port}")[0] == 200)
check("own page: viewer file", req("GET", "/flipbook.html")[0] in (200, 404))   # 404 only if run outside the source folder
# v1.8.7: PDF.js modules and wasm must be served with the right types, whatever the Windows registry says
_h = serve.Handler.__new__(serve.Handler)
for ext, want in ((".mjs", "text/javascript"), (".wasm", "application/wasm"), (".js", "text/javascript")):
    got = _h.guess_type("x" + ext)
    check(f"content type for {ext}", got == want, got)
for p in ("/api/info", "/api/books", "/flipbook.html", "/api/store/" + "a" * 16):
    s, _ = req("GET", p, host=evil_host)
    check(f"rebinding GET {p} refused", s == 403, s)
s, _ = req("POST", "/api/store/" + "b" * 16, host=evil_host, origin=f"http://{evil_host}", body={"x": 1})
check("rebinding POST refused", s == 403, s)
s, _ = req("POST", "/api/store/" + "b" * 16, origin="http://rebind.example", body={"x": 1})
check("POST with a foreign Origin refused", s == 403, s)
s, _ = req("POST", "/api/store/" + "c" * 16, origin=own, body={"ok": True})
check("own page POST still works", s == 204, s)
s, d = req("GET", "/api/store/" + "c" * 16)
check("own page reads it back", s == 200 and json.loads(d) == {"ok": True}, d[:40])
s, _ = req("POST", "/api/store/" + "d" * 16, body={"x": 1}, flip=False)
check("POST without X-Flipbook still refused", s == 403, s)

# --- fix 3: no network paths through the page ---
touched = []
real_isfile = os.path.isfile
os.path.isfile = lambda p: (touched.append(p), real_isfile(p))[1] if str(p).startswith("\\\\") else real_isfile(p)
try:
    unc = ["\\\\192.0.2.1\\share\\x.pdf", "//192.0.2.1/share/x.pdf", "\\\\?\\UNC\\192.0.2.1\\s\\x.pdf"]
    s, d = req("POST", "/api/books/add", origin=own, body={"paths": unc + [pdf]})
    st = json.loads(d)
    check("books/add answers", s == 200, s)
    check("network paths never touched", not touched, touched)
    check("network paths not added", not any(b["path"].startswith("\\\\") for b in st["books"]))
    check("local PDF still added", len(st.get("added", [])) == 1, st.get("added"))
finally:
    os.path.isfile = real_isfile
check("is_network_path: local drive path is local", not serve.is_network_path("C:\\Books\\a.pdf"))

srv.shutdown()
print(f"\n{len(fails)} failed" if fails else "\nall passed")
sys.exit(1 if fails else 0)
