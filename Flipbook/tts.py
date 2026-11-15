"""Natural AI voice for Flipbook's Read aloud (optional add-on).

The voice engine (onnxruntime + espeak-ng for pronunciation) ships inside
Flipbook. The voice itself - the Kokoro v1.0 model and its voice set, about
115 MB - is an add-on that is downloaded only when the user asks for it, into
"addons/voice" next to Flipbook.exe. Everything runs offline after that.
"""
import ctypes
import hashlib
import io
import os
import re
import sys
import threading
import time
import json
import shutil
import urllib.request
import wave

import kokoro_gpu

SAMPLE_RATE = 24000
MAX_PHONEMES = 500          # the model accepts up to 510 tokens per run
MODEL_MAX_SPEED = 1.3       # faster than this, consonants blur; the player speeds up the rest

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
ADDON_FILES = [
    # fp16 build: ~4x faster than the int8 one on ordinary CPUs (int8 ran slower than real time)
    {"name": "kokoro-v1.0.fp16.onnx", "size": 177464787,
     "sha256": "c1610a859f3bdea01107e73e50100685af38fff88f5cd8e5c56df109ec880204"},
    {"name": "voices-v1.0.bin", "size": 28214398,
     "sha256": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"},
]
ADDON_BYTES = sum(f["size"] for f in ADDON_FILES)

# A curated set of the best-sounding English voices (Kokoro v1.0 names).
VOICES = [
    ("af_heart",    "Heart · American woman (recommended)", "en-us"),
    ("af_bella",    "Bella · American woman", "en-us"),
    ("af_nicole",   "Nicole · American woman, soft", "en-us"),
    ("af_sarah",    "Sarah · American woman", "en-us"),
    ("am_michael",  "Michael · American man", "en-us"),
    ("am_fenrir",   "Fenrir · American man, deep", "en-us"),
    ("am_puck",     "Puck · American man, lively", "en-us"),
    ("bf_emma",     "Emma · British woman", "en-gb"),
    ("bf_isabella", "Isabella · British woman", "en-gb"),
    ("bm_george",   "George · British man", "en-gb"),
    ("bm_fable",    "Fable · British man, storyteller", "en-gb"),
    ("bm_lewis",    "Lewis · British man", "en-gb"),
]
VOICE_LANG = {v: lang for v, _, lang in VOICES}

VOCAB = {';': 1, ':': 2, ',': 3, '.': 4, '!': 5, '?': 6, '—': 9, '…': 10, '"': 11, '(': 12, ')': 13,
         '“': 14, '”': 15, ' ': 16, '̃': 17, 'ʣ': 18, 'ʥ': 19, 'ʦ': 20, 'ʨ': 21, 'ᵝ': 22,
         'ꭧ': 23, 'A': 24, 'I': 25, 'O': 31, 'Q': 33, 'S': 35, 'T': 36, 'W': 39, 'Y': 41,
         'ᵊ': 42, 'a': 43, 'b': 44, 'c': 45, 'd': 46, 'e': 47, 'f': 48, 'h': 50, 'i': 51, 'j': 52,
         'k': 53, 'l': 54, 'm': 55, 'n': 56, 'o': 57, 'p': 58, 'q': 59, 'r': 60, 's': 61, 't': 62,
         'u': 63, 'v': 64, 'w': 65, 'x': 66, 'y': 67, 'z': 68, 'ɑ': 69, 'ɐ': 70, 'ɒ': 71, 'æ': 72,
         'β': 75, 'ɔ': 76, 'ɕ': 77, 'ç': 78, 'ɖ': 80, 'ð': 81, 'ʤ': 82, 'ə': 83, 'ɚ': 85, 'ɛ': 86,
         'ɜ': 87, 'ɟ': 90, 'ɡ': 92, 'ɥ': 99, 'ɨ': 101, 'ɪ': 102, 'ʝ': 103, 'ɯ': 110, 'ɰ': 111,
         'ŋ': 112, 'ɳ': 113, 'ɲ': 114, 'ɴ': 115, 'ø': 116, 'ɸ': 118, 'θ': 119, 'œ': 120, 'ɹ': 123,
         'ɾ': 125, 'ɻ': 126, 'ʁ': 128, 'ɽ': 129, 'ʂ': 130, 'ʃ': 131, 'ʈ': 132, 'ʧ': 133, 'ʊ': 135,
         'ʋ': 136, 'ʌ': 138, 'ɣ': 139, 'ɤ': 140, 'χ': 142, 'ʎ': 143, 'ʒ': 147, 'ʔ': 148, 'ˈ': 156,
         'ˌ': 157, 'ː': 158, 'ʰ': 162, 'ʲ': 164, '↓': 169, '→': 171, '↗': 172, '↘': 173, 'ᵻ': 177}


def addon_dir(base):
    return os.path.join(base, "addons", "voice")


def engine_available():
    """True when onnxruntime/numpy/espeak-ng are present (always, in Flipbook.exe)."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        return os.path.isfile(_espeak_paths()[0])
    except Exception:
        return False


def installed(base):
    d = addon_dir(base)
    return all(os.path.isfile(os.path.join(d, f["name"])) and
               os.path.getsize(os.path.join(d, f["name"])) == f["size"] for f in ADDON_FILES)


# ---------------------------------------------------------------- text clean-up
ORD = ["", "First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth", "Tenth",
       "Eleventh", "Twelfth", "Thirteenth", "Fourteenth", "Fifteenth", "Sixteenth", "Seventeenth",
       "Eighteenth", "Nineteenth", "Twentieth"]
ROMAN_RE = re.compile(r"^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
NUMBERED = {"chapter", "chap", "volume", "vol", "book", "part", "section", "sect", "plate", "fig",
            "figure", "no", "canto", "psalm", "article", "art", "dynasty", "table", "lecture", "appendix",
            "degree", "act", "scene", "lesson", "tablet", "column", "col", "sura", "hymn", "verse", "page"}


def roman_value(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    for i, ch in enumerate(s):
        v = vals[ch]
        total += -v if i + 1 < len(s) and vals[s[i + 1]] > v else v
    return total


def _roman_sub(m):
    prev, word = m.group(1), m.group(2)
    if not word or not ROMAN_RE.match(word):
        return m.group(0)
    n = roman_value(word)
    key = prev.strip().rstrip(".").lower() if prev else ""
    if key in NUMBERED:
        return f"{prev}{n}"
    # "Rameses II" -> "Rameses the Second"
    if prev and prev.strip()[:1].isupper() and prev.strip().isalpha() and len(word) >= 2 and n < len(ORD):
        return f"{prev}the {ORD[n]}"
    if len(word) >= 2 and set(word) <= set("IVX"):
        return f"{prev or ''}{n}"
    return m.group(0)


def _year(m):
    a, b = m.group(1), m.group(2)
    if b == "00":
        return f"{a} hundred"
    if b[0] == "0":
        return f"{a} oh {b[1]}"
    return f"{a} {b}"


def _money(m):
    """$1,250.75 -> "1250 dollars and 75 cents"; $1 -> "1 dollar"; $0.50 -> "50 cents";
    $4.5 million -> "4 point 5 million dollars" (v1.8.14, the user's wish)."""
    whole, cents, scale = m.group(1).replace(",", ""), m.group(2), m.group(3)
    if scale:
        return f"{whole}{' point ' + cents if cents else ''} {scale.lower()} dollars"
    w = int(whole)
    c = int(cents.ljust(2, "0")) if cents else 0
    parts = []
    if w or not c:
        parts.append(f"{whole} dollar{'' if w == 1 else 's'}")
    if c:
        parts.append(f"{c} cent{'' if c == 1 else 's'}")
    return " and ".join(parts)


def normalize(text):
    t = text
    t = t.replace("­", "").replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬀ", "ff").replace("ﬃ", "ffi").replace("ﬄ", "ffl")
    t = t.replace("’", "'").replace("‘", "'").replace("–", "-").replace("&", " and ")
    t = re.sub(r"\[\d+\]|\*+|†|‡|§", " ", t)                        # footnote markers
    t = re.sub(r"(\b[A-Za-z][A-Za-z.]*\.?\s)?\b([MDCLXVI]{1,8})\b(?=[\s.,;:)!?]|$)", lambda m: _roman_sub(_Match(m)), t)
    t = re.sub(r"(?<!\$)\b(1[1-9])(\d\d)\b(?![,.]?\d)", _year, t)   # 1717 -> 17 17 (years; not $1850)
    # money (v1.8.14): $1,250.75 -> 1250 dollars and 75 cents, read after the year rule so 1250 stays a number
    t = re.sub(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?!\d)(?:\s+(thousand|million|billion|trillion)\b)?",
               _money, t, flags=re.I)
    # v1.8.13: phonemize() splits the text at every comma and full stop, so "4,000" was read
    # "four, zero zero zero" and "3.5" "three. five". Drop thousands separators (after the year rule,
    # so "1,500 men" is one thousand five hundred, not "fifteen hundred") and say decimal points.
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)                   # 4,000 -> 4000; 1,000,000 -> 1000000
    t = re.sub(r"(?<=\d)\.(?=\d)", " point ", t)                      # 3.5 -> 3 point 5
    t = re.sub(r"\be\.\s?g\.", "for example", t)
    t = re.sub(r"\bi\.\s?e\.", "that is", t)
    t = re.sub(r"\betc\.", "et cetera", t)
    t = re.sub(r"\bcf\.", "compare", t)
    t = re.sub(r"\bpp?\.\s?(?=\d)", "page ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


class _Match:
    """Adapter so _roman_sub can see the preceding word and the numeral separately."""
    def __init__(self, m):
        self._m = m

    def group(self, i):
        return self._m.group(0) if i == 0 else (self._m.group(i) or "")


# ---------------------------------------------------------------- espeak-ng (pronunciation)
def _espeak_paths():
    if getattr(sys, "frozen", False):
        root = os.path.join(sys._MEIPASS, "espeakng_loader")
    else:
        import espeakng_loader
        root = os.path.dirname(espeakng_loader.__file__)
    return os.path.join(root, "espeak-ng.dll"), os.path.join(root, "espeak-ng-data")


class _Espeak:
    def __init__(self):
        dll, data = _espeak_paths()
        lib = ctypes.cdll.LoadLibrary(dll)
        lib.espeak_Initialize.restype = ctypes.c_int
        lib.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        lib.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
        lib.espeak_TextToPhonemes.restype = ctypes.c_char_p
        lib.espeak_TextToPhonemes.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.c_int, ctypes.c_int]
        if lib.espeak_Initialize(0x02, 0, data.encode("utf-8"), 0) <= 0:
            raise RuntimeError("Could not start the pronunciation engine (espeak-ng).")
        self.lib, self.lang = lib, None
        self.lock = threading.Lock()

    def _raw(self, text):
        buf = ctypes.c_char_p(text.encode("utf-8"))
        ptr = ctypes.pointer(buf)
        out = []
        while ptr.contents.value:
            r = self.lib.espeak_TextToPhonemes(ptr, 1, 0x02)   # UTF-8 in, IPA out
            if r:
                out.append(r.decode("utf-8", "ignore"))
        return " ".join(out)

    def phonemize(self, text, lang):
        """IPA with stress, keeping punctuation so the voice phrases naturally."""
        parts = re.split(r'([;:,.!?—…"“”()]+)', text)
        with self.lock:
            if lang != self.lang:
                self.lib.espeak_SetVoiceByName(lang.encode())
                self.lang = lang
            out = ""
            for p in parts:
                if not p:
                    continue
                if re.fullmatch(r'[;:,.!?—…"“”()]+', p):
                    out = out.rstrip() + p + " "
                elif p.strip():
                    out += self._raw(p.strip()) + " "
        out = re.sub(r"\([a-z-]+\)", "", out)          # espeak language-switch flags
        out = "".join(ch for ch in out if ch in VOCAB)
        return re.sub(r"\s+", " ", out).strip()


# ---------------------------------------------------------------- the voice
class Voice:
    def __init__(self, base):
        import numpy as np
        self.base = base
        d = addon_dir(base)
        with open(os.path.join(d, ADDON_FILES[1]["name"]), "rb") as f:
            npz = np.load(io.BytesIO(f.read()))
            self.voices = {k: npz[k] for k in npz.files}
        self.np = np
        self.espeak = _Espeak()
        self.lock = threading.Lock()
        self.sess = None                    # the CPU model, loaded when first needed

    def _cpu(self):
        with self.lock:
            if self.sess is None:
                import onnxruntime as ort
                np = self.np
                so = ort.SessionOptions()
                so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                # one thread per physical core is fastest (hyper-threads slow it down)
                so.intra_op_num_threads = max(2, min(8, (os.cpu_count() or 4) // 2))
                # read the model into memory so the add-on folder is never locked
                with open(os.path.join(addon_dir(self.base), ADDON_FILES[0]["name"]), "rb") as f:
                    model = f.read()
                sess = ort.InferenceSession(model, so, providers=["CPUExecutionProvider"])
                kinds = {"tensor(float16)": np.float16, "tensor(float)": np.float32,
                         "tensor(int64)": np.int64, "tensor(int32)": np.int32}
                self.dtypes = {i.name: kinds.get(i.type, np.float32) for i in sess.get_inputs()}
                self.tok_name = "input_ids" if "input_ids" in self.dtypes else "tokens"
                self.sess = sess
            return self.sess

    def tokens_for(self, text, voice="af_heart"):
        lang = VOICE_LANG.get(voice, "en-gb" if voice.startswith("b") else "en-us")
        ph = self.espeak.phonemize(normalize(text), lang)
        ids = [VOCAB[c] for c in ph if c in VOCAB][:MAX_PHONEMES]
        return ids, self.voices[voice][min(len(ids), len(self.voices[voice])) - 1]

    def _run(self, tokens, style, speed):
        """One model run: on the graphics card when that is the active device, else on the CPU."""
        dev = device(self.base)
        if dev.active == "gpu":
            try:
                return dev.engine().synth(tokens, style, speed)
            except kokoro_gpu.GpuError as e:
                dev.gpu_failed(str(e))          # switch to the CPU for good (until the user tests again)
        return self._cpu_run(tokens, style, speed)

    def _cpu_run(self, tokens, style, speed):
        np = self.np
        sess = self._cpu()
        return sess.run(None, {
            self.tok_name: np.array([[0, *tokens, 0]], dtype=self.dtypes[self.tok_name]),
            "style": np.asarray(style, dtype=self.dtypes["style"]).reshape(1, -1),
            "speed": np.array([speed], dtype=self.dtypes["speed"]),
        })[0].ravel().astype(np.float32)

    def _chunks(self, ph):
        """Split long phoneme strings at punctuation, then at spaces."""
        out = []
        while len(ph) > MAX_PHONEMES:
            cut = max(ph.rfind(m, 0, MAX_PHONEMES) for m in ".!?;:,—")
            if cut < MAX_PHONEMES // 3:
                cut = ph.rfind(" ", 0, MAX_PHONEMES)
            if cut <= 0:
                cut = MAX_PHONEMES
            out.append(ph[:cut + 1].strip())
            ph = ph[cut + 1:].strip()
        if ph:
            out.append(ph)
        return out

    def _trim(self, a):
        """Cut the model's leading/trailing silence without touching speech.

        v1.1 cut at the last sample louder than 0.012 plus 30 ms, which could clip
        quiet word endings (the release of t/k/s/f, e.g. 'effect' -> 'effe(t)').
        Now: a 5 ms loudness envelope with a much lower threshold, a 120 ms safety
        margin after the last sound, and a short fade instead of a hard cut.
        """
        np = self.np
        n = int(0.005 * SAMPLE_RATE)
        frames = len(a) // n
        if not frames:
            return a
        env = np.sqrt((a[:frames * n].reshape(frames, n) ** 2).mean(axis=1))
        active = np.where(env > max(0.0025, env.max() * 0.01))[0]
        if not len(active):
            return a[:0]
        start = max(0, active[0] * n - int(0.04 * SAMPLE_RATE))
        end = min(len(a), (active[-1] + 1) * n + int(0.12 * SAMPLE_RATE))
        out = a[start:end].copy()
        fi, fo = min(len(out), int(0.005 * SAMPLE_RATE)), min(len(out), int(0.03 * SAMPLE_RATE))
        out[:fi] *= np.linspace(0, 1, fi, dtype=out.dtype)
        out[len(out) - fo:] *= np.linspace(1, 0, fo, dtype=out.dtype)
        return out

    def synth(self, text, voice="af_heart", speed=1.0, cancelled=None):
        """Return (wav bytes, playback rate). Above MODEL_MAX_SPEED the model starts
        to swallow consonant clusters ('effect' -> 'effet'), so the voice is made at
        that pace and the player speeds it up the rest of the way, keeping pitch."""
        np = self.np
        if voice not in self.voices:
            voice = "af_heart"
        want = float(min(2.0, max(0.5, speed)))
        speed = min(want, MODEL_MAX_SPEED)
        rate = round(want / speed, 3)
        lang = VOICE_LANG.get(voice, "en-gb" if voice.startswith("b") else "en-us")
        ph = self.espeak.phonemize(normalize(text), lang)
        parts = []
        for chunk in self._chunks(ph):
            if cancelled and cancelled():
                raise Stale()
            tokens = [VOCAB[c] for c in chunk if c in VOCAB]
            if not tokens:
                continue
            style = self.voices[voice][min(len(tokens), len(self.voices[voice])) - 1]
            # CPU: ONNX Runtime sessions are safe to run from two threads (the gate allows two);
            # GPU: the helper process takes one request at a time.
            audio = self._run(tokens, style, speed)
            parts.append(self._trim(audio))
            parts.append(np.zeros(int(0.12 / speed * SAMPLE_RATE), dtype=np.float32))
        tail = np.zeros(int(0.18 / speed * SAMPLE_RATE), dtype=np.float32)
        audio = np.concatenate(parts + [tail]) if parts else tail
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)
        return buf.getvalue(), rate


def wav_bytes(audio, tail_s=0.18):
    """float32 mono 24 kHz -> 16-bit WAV, with a short silence at the end (as the Kokoro voice has)."""
    import numpy as np
    a = np.concatenate([np.asarray(audio, dtype=np.float32).ravel(), np.zeros(int(tail_s * SAMPLE_RATE), dtype=np.float32)])
    pcm = (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class Stale(Exception):
    """The reader moved on; this speech is no longer wanted."""


class Gate:
    """One synthesis at a time, newest reading position first.

    Requests carry the reader's generation number; anything older than the newest
    generation seen is refused (the reader jumped, skipped or changed voice), so a
    burst of clicks never leaves a queue of unwanted sentences in front of the one
    that is needed. "now" requests (the sentence about to be spoken) go before
    "ahead" requests (sentences prepared in advance); each kind is first-come
    first-served.
    """

    # Two lanes: one "now" request may run while an "ahead" request is still finishing, so the
    # sentence about to be spoken never waits behind one being prepared in advance (a model run
    # can't be interrupted). "ahead" requests wait while anything "now" runs or waits.
    def __init__(self):
        self.cv = threading.Condition()
        self.running = {0: False, 1: False}    # lane 0 = now, lane 1 = ahead
        self.latest = 0
        self.waiting = []          # [(lane, ticket)]
        self.ticket = 0

    def stale(self, gen):
        return gen < self.latest

    def acquire(self, gen, now):
        with self.cv:
            if gen > self.latest:
                self.latest = gen
                self.cv.notify_all()          # let older waiters notice they are stale
            if gen < self.latest:
                raise Stale()
            self.ticket += 1
            lane = 0 if now else 1
            me = (lane, self.ticket)
            self.waiting.append(me)
            try:
                while True:
                    first = min((w for w in self.waiting if w[0] == lane), default=None)
                    if lane == 0:
                        ok = not self.running[0] and first == me
                    else:
                        ok = (not self.running[0] and not self.running[1] and first == me
                              and not any(w[0] == 0 for w in self.waiting))
                    if ok:
                        break
                    self.cv.wait(0.5)
                    if gen < self.latest:
                        raise Stale()
                self.running[lane] = True
                return lane
            finally:
                self.waiting.remove(me)

    def release(self, lane):
        with self.cv:
            self.running[lane] = False
            self.cv.notify_all()


gate = Gate()

_voice = None
_voice_lock = threading.Lock()


def get_voice(base):
    global _voice
    with _voice_lock:
        if _voice is None:
            _voice = Voice(base)
        return _voice


def unload():
    global _voice
    with _voice_lock:
        _voice = None


# ---------------------------------------------------------------- add-on download
class Installer:
    files = ADDON_FILES

    def __init__(self, base):
        self.base = base
        self.lock = threading.Lock()
        self.state = "idle"        # idle | downloading | verifying | converting | done | error | cancelled
        self.done = 0
        self.error = ""
        self.phase = ""
        self.cancel_flag = False
        self.thread = None
        self.total = sum(f["size"] for f in self.files)

    def dest(self):
        return addon_dir(self.base)

    def after(self):
        """Run once every file is downloaded and checked (subclasses convert/clean up)."""

    def status(self):
        return {"state": self.state, "done": self.done, "total": self.total, "error": self.error, "phase": self.phase}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.state, self.error, self.cancel_flag = "downloading", "", False
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def cancel(self):
        self.cancel_flag = True

    def _run(self):
        d = self.dest()
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            self.state, self.error = "error", f"Can't write to the Flipbook folder ({e.strerror}). Move Flipbook to a folder you can write to, such as Documents."
            return
        try:
            finished = 0
            for f in self.files:
                dest = os.path.join(d, f["name"])
                if os.path.isfile(dest) and os.path.getsize(dest) == f["size"]:
                    finished += f["size"]
                    self.done = finished
                    continue
                self._fetch(f, dest, finished)
                finished += f["size"]
            self.done = self.total
            self.after()
            self.state = "done"
        except _Cancelled:
            self.state = "cancelled"
        except Exception as e:  # network errors, checksum mismatch, disk full ...
            self.state, self.error = "error", str(e) or e.__class__.__name__

    def _fetch(self, f, dest, before):
        part = dest + ".part"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if have > f["size"]:
            os.remove(part)
            have = 0
        h = hashlib.sha256()
        if have:
            with open(part, "rb") as fp:
                for chunk in iter(lambda: fp.read(1 << 20), b""):
                    h.update(chunk)
        tries = 0
        while have < f["size"]:
            if self.cancel_flag:
                raise _Cancelled()
            req = urllib.request.Request(RELEASE + f["name"], headers={"User-Agent": "Flipbook/1.1"})
            if have:
                req.add_header("Range", f"bytes={have}-")
            try:
                with urllib.request.urlopen(req, timeout=30) as r, open(part, "ab") as out:
                    if have and r.status != 206:      # server ignored the resume request: start over
                        out.truncate(0)
                        have, h = 0, hashlib.sha256()
                    while True:
                        if self.cancel_flag:
                            raise _Cancelled()
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        out.write(chunk)
                        h.update(chunk)
                        have += len(chunk)
                        self.done = before + have
            except _Cancelled:
                raise
            except Exception as e:
                tries += 1
                if tries > 5:
                    raise RuntimeError(f"The download stopped ({e}). Check the internet connection and try again; it will continue where it left off.")
                time.sleep(2 * tries)
        self.state = "verifying"
        if h.hexdigest() != f["sha256"]:
            os.remove(part)
            raise RuntimeError(f"{f['name']} didn't match its checksum, so it was deleted. Please try again.")
        os.replace(part, dest)
        self.state = "downloading"


class _Cancelled(Exception):
    pass


def remove(base):
    unload()
    remove_gpu(base)
    d = addon_dir(base)
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        p = os.path.join(d, name)
        for _ in range(10):
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
                break
            except PermissionError:
                time.sleep(0.3)
    try:
        os.rmdir(d)
        os.rmdir(os.path.dirname(d))
    except OSError:
        pass


# ---------------------------------------------------------------- the graphics card ("GPU speed-up")
GPU_FILES = [kokoro_gpu.SRC]
GPU_BYTES = kokoro_gpu.SRC["size"]


def settings_path(base):
    """Where the CPU/GPU choice and the last graphics-card test are kept."""
    return os.path.join(base, "flipbook-data", "voice-device.json")


def gpu_installed(base):
    return kokoro_gpu.converted(addon_dir(base))


class GpuInstaller(Installer):
    """Downloads the fp32 Kokoro model and converts it for the graphics card (then deletes it)."""
    files = GPU_FILES

    def dest(self):
        return kokoro_gpu.gpu_dir(addon_dir(self.base))

    def after(self):
        self.state = "converting"
        src = os.path.join(self.dest(), kokoro_gpu.SRC["name"])
        try:
            kokoro_gpu.convert(src, addon_dir(self.base), progress=lambda s: setattr(self, "phase", s))
        except MemoryError:
            raise RuntimeError("Not enough free memory to prepare the graphics-card model. Close other programs and try again.")
        except Exception as e:
            raise RuntimeError(f"Preparing the graphics-card model failed ({e.__class__.__name__}: {e}).")
        try:
            os.remove(src)                     # the converted copies are all that is needed
        except OSError:
            pass
        self.phase = ""
        device(self.base).test_soon(force=True)


def remove_gpu(base):
    dev = _devices.get(base)
    if dev:
        dev.stop_engine()
    d = kokoro_gpu.gpu_dir(addon_dir(base))
    for _ in range(10):
        if not os.path.isdir(d):
            break
        try:
            shutil.rmtree(d)
        except OSError:
            time.sleep(0.3)
    if dev:
        dev.decide()


class Device:
    """Chooses where Kokoro runs: the graphics card (GPU) or the processor (CPU).

    mode "auto": the GPU when it is capable, its model is installed, and the self-test passed
    (same audio as the CPU, clearly faster) and it has not failed since; otherwise the CPU.
    mode "gpu": the GPU whenever it can be used at all; mode "cpu": always the CPU.
    A GPU error while reading switches to the CPU at once and is remembered (until the user
    presses "Test again", or the graphics card, driver or Flipbook changes).
    """

    def __init__(self, base):
        self.base = base
        self.path = settings_path(base)
        self.lock = threading.RLock()
        self.cfg = {"mode": "auto"}
        try:
            with open(self.path, encoding="utf-8") as f:
                self.cfg.update(json.load(f))
        except Exception:
            pass
        if self.cfg.get("mode") not in ("auto", "gpu", "cpu"):
            self.cfg["mode"] = "auto"
        self.active = "cpu"
        self.testing = False
        self.note = ""
        self._engine = None
        self.decide()

    # -- settings
    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=1)
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            pass

    def signature(self):
        g = kokoro_gpu.detect_gpu()
        try:
            import onnxruntime as ort
            ov = ort.__version__
        except Exception:
            ov = "?"
        return f"{g['name']}|{g['vram_mb']}|{g['driver']}|ort {ov}|conv {kokoro_gpu.CONVERT_VERSION}"

    def set_mode(self, mode):
        if mode not in ("auto", "gpu", "cpu"):
            return
        with self.lock:
            self.cfg["mode"] = mode
            if mode == "gpu":
                self.cfg.pop("failure", None)   # the user asked for the GPU: try it again
            self._save()
        self.decide()
        if mode != "cpu" and self.usable() and not self._probe_current():
            self.test_soon()

    # -- decision
    def usable(self):
        return installed(self.base) and gpu_installed(self.base) and kokoro_gpu.detect_gpu()["ok"]

    def _probe_current(self):
        p = self.cfg.get("probe")
        return p if p and p.get("sig") == self.signature() else None

    def _failure_current(self):
        f = self.cfg.get("failure")
        return f if f and f.get("sig") == self.signature() else None

    def decide(self):
        with self.lock:
            mode, want = self.cfg["mode"], "cpu"
            g = kokoro_gpu.detect_gpu()
            if mode == "cpu":
                self.note = "You chose the processor."
            elif not g["ok"]:
                self.note = g["reason"]
            elif not installed(self.base):
                self.note = "The AI voice isn't installed."
            elif not gpu_installed(self.base):
                self.note = "The graphics-card speed-up isn't installed."
            elif self._failure_current():
                self.note = "The graphics card failed while reading (" + self._failure_current()["error"][:160] + "), so the processor is used."
            elif mode == "gpu":
                want, self.note = "gpu", "You chose the graphics card."
            else:
                p = self._probe_current()
                if not p:
                    self.note = "Testing the graphics card…" if self.testing else "The graphics card hasn't been tested yet."
                elif p.get("ok"):
                    want, self.note = "gpu", f"The graphics card passed its test (about {p.get('speedup', 0):.0f}× faster than the processor)."
                else:
                    self.note = p.get("why") or "The graphics card didn't pass its test."
            self.active = want
            if want == "cpu":
                self.stop_engine()
            return want

    def engine(self):
        with self.lock:
            if self._engine is None:
                self._engine = kokoro_gpu.Engine(addon_dir(self.base))
            return self._engine

    def stop_engine(self):
        e, self._engine = self._engine, None
        if e:
            threading.Thread(target=e.stop, daemon=True).start()

    def gpu_failed(self, error):
        with self.lock:
            self.cfg["failure"] = {"sig": self.signature(), "error": error, "when": time.strftime("%Y-%m-%d %H:%M")}
            self._save()
        self.decide()

    # -- the self-test
    def test_soon(self, force=False):
        if self.testing:
            return
        if not force and self._probe_current():
            return
        self.testing = True
        self.decide()
        threading.Thread(target=self._test, daemon=True).start()

    def _test(self):
        result = {"sig": self.signature(), "when": time.strftime("%Y-%m-%d %H:%M"), "ok": False}
        try:
            if not self.usable():
                result["why"] = "The graphics card can't be used."
                return
            voice = get_voice(self.base)
            cases = []
            for text, v in (("The effect of these old customs upon the mind of the traveller was profound.", "af_heart"),
                            ("In the old harbour town, the ships were weighed against the tide of the morning.", "bm_george")):
                ids, style = voice.tokens_for(text, v)
                cases.append({"tokens": ids, "style": style})
            res = self.engine().probe(cases)
            gpu_s = sum(r["gpu_s"] for r in res)          # whole GPU path: encoder (CPU) + decoder (GPU)
            mel = max(r["mel_db"] for r in res)            # GPU decoder vs the same decoder on the CPU
            # compare with the voice as it runs on the processor today (the add-on's CPU model)
            had_cpu = voice.sess is not None
            sess = voice._cpu()
            np_ = voice.np
            for c in cases[:1]:                            # warm up
                voice._cpu_run(c["tokens"], c["style"], 1.0)
            t = time.time()
            for c in cases:
                voice._cpu_run(c["tokens"], c["style"], 1.0)
            cpu_s = time.time() - t
            if not had_cpu and self.cfg["mode"] != "cpu":
                voice.sess = None                         # free it again; loaded on demand if ever needed
            del sess, np_
            result.update({"speedup": round(cpu_s / max(gpu_s, 1e-6), 1), "mel_db": round(mel, 2),
                           "gpu_s": round(gpu_s, 3), "cpu_s": round(cpu_s, 3)})
            if not all(r["finite"] for r in res):
                result["why"] = "The graphics card produced invalid audio in its test, so the processor is used."
            elif mel > 1.5:
                result["why"] = f"The graphics card's audio differed from the processor's in its test ({mel:.1f} dB), so the processor is used."
            elif result["speedup"] < 1.5:
                result["why"] = f"The graphics card wasn't faster than the processor in its test ({result['speedup']}×), so the processor is used."
            else:
                result["ok"] = True
        except Exception as e:
            result["why"] = f"The graphics card couldn't run the voice ({str(e)[:200]}), so the processor is used."
            self.stop_engine()
        finally:
            with self.lock:
                self.cfg["probe"] = result
                if result["ok"]:
                    self.cfg.pop("failure", None)
                self._save()
                self.testing = False
            self.decide()

    def status(self):
        g = kokoro_gpu.detect_gpu()
        return {"mode": self.cfg["mode"], "active": self.active, "note": self.note, "testing": self.testing,
                "gpu": {k: g[k] for k in ("ok", "reason", "name", "vram_mb", "driver", "vendor")},
                "gpu_model": {"installed": gpu_installed(self.base), "size": GPU_BYTES},
                "probe": self._probe_current(), "failure": self._failure_current()}


_devices = {}
_devices_lock = threading.Lock()


def device(base):
    with _devices_lock:
        if base not in _devices:
            _devices[base] = Device(base)
        return _devices[base]


def startup_check(base):
    """At Flipbook start (background): look at the graphics card and, if the GPU voice is set up
    and its last test is out of date (new GPU, driver or Flipbook), test it again."""
    def run():
        try:
            dev = device(base)
            dev.decide()
            if dev.cfg["mode"] != "cpu" and dev.usable():
                dev.test_soon()
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()
