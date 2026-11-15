"""Development server: run the Flipbook viewer in a normal browser / the Claude browser pane.

Serves flipbook.html from the Flipbook folder on http://127.0.0.1:8765 (no window is opened),
like the app does, with optional redirection of the add-on folders so tests never touch the
real Flipbook\\addons folder.

Usage (from the project root; Python 3.13 in .venv since v1.8.7):
    .venv\\Scripts\\python.exe Flipbook\\dev\\devserver.py [--addons DIR] [--log] [PDF ...]

    --addons DIR   put/read the AI voice and Dictionary add-ons under DIR\\voice and
                   DIR\\dictionary instead of Flipbook\\addons (recommended for tests)
    --log          append one JSON line per AI-voice synthesis to dev\\synth_log.jsonl
                   (text, speed, duration, done/stale) - handy for queue/latency tests
    PDF ...        extra PDFs to list in the Library (like dragging them onto the exe)

Then open http://127.0.0.1:8765/flipbook.html  (or ?open=<id> printed below).
Stop it with Ctrl+C. NOTE: the port is claimed exclusively; if 8765 is busy the start fails -
stop the old dev server first, by PID (its process is named python).
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
F = os.path.dirname(HERE)                      # the Flipbook folder
sys.path.insert(0, F)

import serve      # noqa: E402
import tts        # noqa: E402
import dictionary  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--addons")
ap.add_argument("--log", action="store_true")
ap.add_argument("--port", type=int, default=8765)
ap.add_argument("pdfs", nargs="*")
a = ap.parse_args()

if a.addons:
    root = os.path.abspath(a.addons)
    tts.addon_dir = lambda base: os.path.join(root, "voice")
    tts.settings_path = lambda base: os.path.join(root, "voice-device.json")
    serve.DATA = os.path.join(root, "flipbook-data")     # book list, bookmarks, contents: keep tests apart
    print("data (book list, bookmarks) redirected to", serve.DATA)
    dictionary.addon_dir = lambda base: os.path.join(root, "dictionary")
    try:
        import qwen
        qwen.addon_dir = lambda base: os.path.join(root, "qwen")
    except ImportError:
        pass
    print("add-ons redirected to", root)

if a.log:
    LOG = os.path.join(HERE, "synth_log.jsonl")
    _orig = tts.Voice.synth

    def _logged(self, text, voice="af_heart", speed=1.0, cancelled=None):
        t = time.time()
        res = "done"
        try:
            return _orig(self, text, voice, speed, cancelled)
        except tts.Stale:
            res = "stale"
            raise
        finally:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t0": round(t, 2), "dt": round(time.time() - t, 2), "res": res,
                                    "text": text[:60], "speed": speed}) + "\n")
    tts.Voice.synth = _logged
    print("logging syntheses to", LOG)

serve.collect_launched(a.pdfs)
for p in serve.LAUNCHED:
    print(f"  {serve.pdf_id(p)}  {p}")
print(f"voice installed: {tts.installed(serve.HERE)}   dictionary installed: {dictionary.installed(serve.HERE)}")
print(f"http://127.0.0.1:{a.port}/flipbook.html")
serve.make_server(a.port).serve_forever()
