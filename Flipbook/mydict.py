"""The user's own dictionaries (legal or general) for Flipbook's word look-up.

The user adds any dictionary they have as a PDF (one file, a folder with one PDF per letter, or several
volumes chosen together) and says whether it is a "legal" or a "general" dictionary. There is no
per-book reader: parse_generic() works out how THIS book marks a new entry (a bold run at the line
start, an ALL-CAPS headword, a hanging indent, a first-line indent ...) by trying each signal on a
sample of pages and keeping the one whose headwords come out in alphabetical order.

Everything goes into one SQLite file, addons/mydicts/mydicts.sqlite next to Flipbook.exe; each
dictionary is a source "u1", "u2", ... Text from the user's PDFs never leaves the PC.
Standard library + pypdf; the page-reading helpers come from legal.py.
"""
import collections
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import zlib

import dictionary as _dict
import legal

KINDS = ("legal", "general")
MAX_DICTS = 20
MAX_PER_DICT = 3
MAX_RELATED = 12
MIN_ENTRIES = 40
DB_VERSION = "1"

_key = legal._key
_TERMINAL = legal._TERMINAL


def addon_dir(base):
    return os.path.join(base, "addons", "mydicts")


def db_path(base):
    return os.path.join(addon_dir(base), "mydicts.sqlite")


# ---------------------------------------------------------------- the catalogue
def _origin(v):
    try:
        d = json.loads(v or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _read_dicts(con):
    """[{id, title, kind, entries, built, files, pages, ...}] in the order added."""
    out = []
    for sid, title, entries, built, origin in con.execute("SELECT id, title, entries, built, origin FROM sources"):
        o = _origin(origin)
        out.append({"id": sid, "title": title, "kind": o.get("kind", "general"), "entries": entries,
                    "built": built, "files": len(o.get("files", [])), "names": list(o.get("files", [])),
                    "pages": o.get("pages", 0), "seq": o.get("seq", 0), "style": o.get("style", {})})
    out.sort(key=lambda d: (d["seq"], d["id"]))
    return out


def _dicts_at(path):
    if not os.path.isfile(path):
        return []
    try:
        con = legal._ro_connect(path)
        try:
            return _read_dicts(con)
        finally:
            con.close()
    except sqlite3.Error:
        return []


def installed(base, kind=None):
    return any(kind is None or d["kind"] == kind for d in _dicts_at(db_path(base)))


def info(base):
    p = db_path(base)
    ds = _dicts_at(p)
    keep = ("id", "title", "kind", "entries", "built", "files", "names", "pages")
    return {"installed": bool(ds), "dicts": [{k: d[k] for k in keep} for d in ds],
            "bytes": os.path.getsize(p) if os.path.isfile(p) else 0}


# ---------------------------------------------------------------- choosing files: title, kind, quick check
def _open(f):
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        r = PdfReader(f)
        if r.is_encrypted:
            try:
                r.decrypt("")
            except Exception:
                raise RuntimeError(f"\"{os.path.basename(f)}\" is locked with a password, so Flipbook can't read it.")
        len(r.pages)
        return r
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError(f"Flipbook couldn't open \"{os.path.basename(f)}\": it may be damaged or not a real PDF.")


def _stem(path):
    return os.path.splitext(os.path.basename(path.rstrip("\\/")))[0]


def _tidy_name(s):
    s = s.replace("_", " ").strip()
    s = re.sub(r"\s+", " ", s)
    if " " not in s and len(re.findall(r"[A-Z]", s)) >= 2 and re.search(r"[a-z]", s):
        # a name written as one word (Chief, 1.8.18): 'GlossaryofLegalLatin' -> 'Glossary of Legal Latin'
        parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+(?:st|nd|rd|th)?|\S", s)
        out = []
        for k, p in enumerate(parts):
            m = re.fullmatch(r"([A-Za-z]{4,})(of|and|the|for)", p)
            if m and k + 1 < len(parts) and parts[k + 1][:1].isupper():
                out += [m.group(1), m.group(2)]
            else:
                out.append(p)
        s = re.sub(r"\s+", " ", " ".join(out)).strip()
    m = re.match(r"^(1[4-9]\d\d|20\d\d)\s+(.+)$", s)          # '1927 osborn a concise ...' -> '... (1927)'
    if m:
        s = f"{m.group(2)} ({m.group(1)})"
    if s == s.lower():
        small = {"a", "an", "and", "of", "the", "to", "in", "on", "for", "or", "by", "with", "de"}
        ws = s.split(" ")
        s = " ".join(w if (k and w in small) else w[:1].upper() + w[1:] for k, w in enumerate(ws))
    return _cut(s, 80)


def _cut(s, n):
    s = s.strip()
    if len(s) <= n:
        return s
    c = s.rfind(" ", 0, n)
    return s[:c if c > n // 2 else n].rstrip(" ,;:-")


def _real_title(t):
    t = re.sub(r"\s+", " ", (t or "").replace("\x00", "")).strip()
    if not t or len(t) < 4:
        return None
    low = t.lower()
    if re.search(r"untitled|^microsoft (word|powerpoint)|\.(pdf|docx?|rtf|txt|indd|qxd|tiff?|jpe?g)\b|^document\d*$"
                 r"|^scan|^img[_ ]?\d|^\d+$|^title$|^none$", low):
        return None
    letters = sum(c.isalpha() for c in t)
    if letters < 4 or letters < 0.6 * len(t.replace(" ", "")):
        return None
    if " " not in t and (re.search(r"\d", t) or "_" in t or len(t) > 24):
        return None                                            # 'N1', 'AdminDict', 'blacks_law_v2'
    return _cut(t, 80)


def suggest_title(files):
    """A title for the dialog: the PDF's own /Title when it looks real, else the file or folder name."""
    paths = [files] if isinstance(files, str) else list(files or [])
    fl = legal._expand(paths)
    if not fl:
        return ""
    meta = None
    try:
        r = _open(fl[0])
        meta = _real_title(str((r.metadata or {}).get("/Title", "") or ""))
    except Exception:
        meta = None
    if len(fl) == 1:
        return meta or _tidy_name(_stem(fl[0]))
    if meta:
        return re.sub(r"[\s,.;:-]*\b(?:vol(?:ume)?\.?\s*[\dIVX]+|part\s+\d+)\s*$", "", meta, flags=re.I) or meta
    stems = [_stem(f) for f in fl]
    pre = os.path.commonprefix(stems)
    # 'Ballentine's Law 3rd Edition - Sec. ' -> 'Ballentine's Law 3rd Edition'
    for _ in range(3):
        pre = re.sub(r"[\s\-_–—,.(#]+$", "", pre)
        pre = re.sub(r"(?i)\b(?:sec(?:tion)?|vol(?:ume)?|part|letters?|book|no|v)\.?$", "", pre)
    pre = pre.strip(" -_–—,.(")
    if sum(c.isalpha() for c in pre) >= 4 and not re.fullmatch(r"(?i)(?:sec|section|vol|volume|part)\.?", pre):
        return _tidy_name(pre)
    folders = {os.path.dirname(f) for f in fl}
    if len(folders) == 1:
        return _tidy_name(os.path.basename(folders.pop()))
    return _tidy_name(stems[0])


_LEGAL_RX = re.compile(r"\b(?:law|laws|legal|jur(?:is|id|y)\w*|judicial|black'?s|bouvier|ballentine|court|statut\w*|"
                       r"lex|juris|maxims?)\b", re.I)


def guess_kind(files, title=""):
    """'legal' or 'general', only to preselect the choice in the dialog."""
    paths = [files] if isinstance(files, str) else list(files or [])
    fl = legal._expand(paths)
    names = " ".join([title or ""] + [_stem(f) for f in fl] + [os.path.basename(os.path.dirname(f)) for f in fl[:1]])
    if _LEGAL_RX.search(names.replace("_", " ")):
        return "legal"
    hits = words = 0
    try:
        r = _open(fl[0])
        n = len(r.pages)
        for i in sorted({int(n * q) for q in (0.3, 0.5, 0.7)}):
            t = r.pages[min(i, n - 1)].extract_text() or ""
            words += len(t.split())
            hits += len(re.findall(r"\b(?:law|legal|court|statute|plaintiff|defendant|contract|tort|equity|lien)s?\b",
                                   t, re.I))
    except Exception:
        pass
    return "legal" if words and hits >= max(6, words / 120) else "general"


def _sample_points(sizes, k, lo=0.05, hi=0.95):
    """Up to k (file index, page index) spread over lo..hi of all the pages."""
    total = sum(sizes)
    if not total:
        return []
    if total <= k + 5:
        gs = list(range(total))
    else:
        gs = sorted({int(round((lo + (hi - lo) * j / (k - 1)) * (total - 1))) for j in range(k)})
    out = []
    for g in gs:
        for fi, n in enumerate(sizes):
            if g < n:
                out.append((fi, g))
                break
            g -= n
    return out


def check_pdfs(files):
    """{"pages", "text", "files"}: fast (looks at up to 8 pages spread through the books)."""
    paths = [files] if isinstance(files, str) else list(files or [])
    fl = legal._expand(paths)
    readers, sizes = [], []
    for f in fl:
        try:
            r = _open(f)
            readers.append(r)
            sizes.append(len(r.pages))
        except Exception:
            readers.append(None)
            sizes.append(0)
    good = tried = 0
    for fi, pi in _sample_points(sizes, 8, 0.1, 0.9):
        tried += 1
        try:
            t = readers[fi].pages[pi].extract_text() or ""
        except Exception:
            t = ""
        if len(re.findall(r"[A-Za-z]", t)) >= 150:
            good += 1
    return {"pages": sum(sizes), "text": bool(tried) and good >= max(1, 0.4 * tried), "files": len(fl)}


# ---------------------------------------------------------------- reading a page
def _split_columns(lines, W):
    """x positions between columns ([] for one column). Two columns: legal._column_split (tested on
    Black's); three or more: the left margins that many lines start at."""
    good = [l for l in lines if sum(len(t.strip()) for f, t in l["items"]) > 3
            and not re.fullmatch(r"[\d\s]+", "".join(t for f, t in l["items"]))]
    if len(good) < 6:
        return []
    starts = collections.Counter(round(l["x"] / 3) for l in good)
    need = max(5, 0.08 * len(good))
    peaks = []
    for k, n in sorted(starts.items(), key=lambda kv: -kv[1]):
        n += starts.get(k - 1, 0) + starts.get(k + 1, 0)
        if n < need:
            break
        if all(abs(k * 3 - p) >= 0.2 * W for p in peaks):
            peaks.append(k * 3)
    peaks.sort()
    if len(peaks) >= 3:
        bounds = []
        for p in peaks[1:]:
            near = [l["x"] for l in good if p - 20 <= l["x"] <= p + 4.5]
            bounds.append(min(near) - 6)
        return bounds
    s = legal._column_split(lines, W)
    if s >= W * 0.97:
        return []
    right = sum(1 for l in good if l["x"] > s)
    if right < 4 or right < 0.12 * len(good):
        return []
    return [s]


def _rows(page):
    """Rows of text on a page, in reading order: [{'col','x','y','ch':[(font,text)],'text'}].
    Like legal._page_rows, plus: coordinates that are not in page units (some OCR layers draw in image
    pixels) are scaled to the page; words of one printed line whose heights jitter are put back into one
    row; three or more columns."""
    chunks = []

    def vis(text, cm, tm, fd, fs):
        if not text:
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        f = ""
        try:
            f = str(fd.get("/BaseFont", "")) if fd else ""
        except Exception:
            pass
        chunks.append((x, y, f, text))
    page.extract_text(visitor_text=vis)
    try:
        box = page.mediabox
        X0, Y0, W, H = float(box.left), float(box.bottom), float(box.width), float(box.height)
    except Exception:
        X0, Y0, W, H = 0.0, 0.0, 520.0, 760.0
    pts = [(c[0], c[1]) for c in chunks if c[3].strip()]
    if not pts:
        return []
    mx = max(p[0] for p in pts)
    my = max(p[1] for p in pts)
    s = 1.0
    if mx > X0 + W * 1.25 or my > Y0 + H * 1.25:
        s = min(W * 0.97 / max(mx, 1.0), H * 0.97 / max(my, 1.0))
        X0 = Y0 = 0.0
    lines = []
    cur = None
    ended = True
    for x, y, f, text in chunks:
        x, y = (x - X0) * s, (y - Y0) * s
        parts = text.split("\n")
        for k, part in enumerate(parts):
            if k > 0:
                ended = True
            if not part:
                continue
            if ended or cur is None or (k == 0 and abs(y - cur["y"]) > 3):
                if not part.strip() and (ended or cur is None):
                    continue
                cur = {"x": x, "y": y, "items": []}
                lines.append(cur)
                ended = False
            cur["items"].append((f, part))
    bounds = _split_columns(lines, W)
    for l in lines:
        l["col"] = sum(1 for b in bounds if l["x"] > b)
    lines.sort(key=lambda l: (l["col"], -l["y"], l["x"]))
    groups = []
    for l in lines:
        g = groups[-1] if groups else None
        if g is not None and g[0]["col"] == l["col"] and g[0]["y"] - l["y"] <= 2.2:
            g.append(l)
        else:
            groups.append([l])
    out = []
    for g in groups:
        g.sort(key=lambda l: l["x"])
        ch = []
        for l in g:
            if ch:
                ch.append(("", " "))
            ch += l["items"]
        while ch and not ch[0][1].strip():
            ch.pop(0)
        text = re.sub(r"\s+", " ", "".join(t for f, t in ch)).strip()
        if not text:
            continue
        if "·" in text:
            text = re.sub(r"(?<=[A-Za-z])·$", "­", text)
            text = re.sub(r"(?<=[a-z])· (?=[a-z])", "", text)
            text = re.sub(r"(?<=[A-Za-z])·(?=[A-Za-z])", "-", text)
        y = max(l["y"] for l in g)
        out.append({"col": g[0]["col"], "x": g[0]["x"], "y": y, "ch": ch, "text": text})
    return out


def _norm_furn(t):
    return re.sub(r"[^a-z]+", "", _dict.fold(t))


def _pitch(rows):
    gaps = []
    for a, b in zip(rows, rows[1:]):
        if a["col"] == b["col"]:
            d = a["y"] - b["y"]
            if 0.5 < d < 40:
                gaps.append(d)
    return legal._median(gaps) or 11.0


def _furniture_strings(row_pages):
    """Normalised text of the top and bottom rows that repeat on many sampled pages (a running title,
    a footer), with the digits taken out."""
    c = collections.Counter()
    for rows in row_pages:
        if not rows:
            continue
        top = max(rows, key=lambda r: r["y"])
        bot = min(rows, key=lambda r: r["y"])
        for r in {id(top): top, id(bot): bot}.values():
            k = _norm_furn(r["text"])
            if 3 <= len(k) <= 120:
                c[k] += 1
    n = len([r for r in row_pages if r])
    need = max(2, 0.15 * n)
    return {k for k, v in c.items() if v >= need}


_NUMTOK = re.compile(r"(?:^|\s)(?:\d{1,4}[a-d*]?|[ivxlc]{1,6})(?:\s|$)", re.I)


def _strip_furniture(rows, rep=()):
    """legal._strip_page_furniture, plus: a running head or footer set apart from the text (a gap) that
    holds a page number or repeats on many pages; lone section letters ('L', '— B —')."""
    rows = legal._strip_page_furniture(rows)
    if not rows:
        return rows
    pitch = _pitch(rows)
    ys = sorted({round(r["y"], 1) for r in rows}, reverse=True)
    top, bot = ys[0], ys[-1]
    below_top = [y for y in ys if y < top - 2.5]
    above_bot = [y for y in ys if y > bot + 2.5]
    top_gap = (top - below_top[0]) if below_top else 0
    bot_gap = (above_bot[-1] - bot) if above_bot else 0
    keep = []
    for r in rows:
        t = r["text"]
        k = _norm_furn(t)
        at_top = r["y"] >= top - 2.5
        at_bot = r["y"] <= bot + 2.5
        if (at_top or at_bot) and k and k in rep:
            continue
        if at_top and top_gap >= 1.35 * pitch and len(t.split()) <= 12 and _NUMTOK.search(t):
            continue
        if at_bot and bot_gap >= 1.35 * pitch and len(t.split()) <= 8 and _NUMTOK.search(t):
            continue
        if re.fullmatch(r"[\W_]*[A-Za-z](?:[\W_]+[A-Za-z])?[\W_]*", t) and len(t) <= 9:
            continue                                   # 'L', '— A —', 'A a'
        keep.append(r)
    return keep


# ---------------------------------------------------------------- headwords
_BOLD = re.compile(r"bold|black|heavy|semibold|demi|medium|[,\-]b$|bd$|-sb$", re.I)


def _is_bold(f):
    return bool(f) and bool(_BOLD.search(f.split("+")[-1]))


_POS = r"(?:n|v|vb|vt|vi|adj|adv|a|prep|pl|pron|conj|interj|int|abbr|sing|pp|ppr|imp)"
_STOP = re.compile(r"[,.;:(\[=—]|\s[–-]\s|\s" + _POS + r"\.(?=\s|$)|\s\d+[.)]?\s")
_LEADJUNK = re.compile(r"^(?:[*•·«»“”\"'‘’\[\]†‡§¶|~_]+\s*|\d{1,4}[.)]\s+)+")
_NOT_FIRST = {"see", "cf", "also", "and", "or", "of", "the", "in", "to", "by", "for", "with", "from", "as", "is",
              "that", "which", "but", "if", "it", "on", "at", "this", "an"}


def _lead_run(ch, body):
    """The leading chunks in the first chunk's font (like legal._lead_run, but a change to any other
    font ends the run: 'claim of privilege' + italic 'See')."""
    if not ch or ch[0][0] == body:
        return "", ch[0][0] if ch else None, 0
    f0 = ch[0][0]
    out = []
    for k, (f, t) in enumerate(ch):
        if f == f0 or not t.strip():
            out.append(t)
            continue
        if t.strip() in ("or", "and", ",", "&", ";") and k + 1 < len(ch) and ch[k + 1][0] == f0:
            out.append(t)
            continue
        break
    while out and not out[-1].strip():
        out.pop()
    return "".join(out), f0, len(out)


def _clean_head(h):
    h = h.replace("­", "").replace("�", "")
    h = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰*†‡]+", "", h)
    h = re.sub(r"(?<=[A-Za-z])\d{1,2}$", "", h.strip())             # homograph numbers: 'bank1'
    h = re.sub(r"\s+/[^/]{1,40}/?$", "", h)                          # a trailing /pronunciation/
    h = re.sub(r",?\s+" + _POS + r"\.?$", "", h)                      # 'Bail, n' -> 'Bail'
    h = re.sub(r"\s+", " ", h).strip(" .,;:'\"‘’“”|*_")
    return h


def _valid_head(h, words=6):
    if not h or len(h) > 60:
        return False
    if not re.match(r"-?[^\W\d_]", h):
        return False
    ws = h.split()
    if len(ws) > words or any(len(w) > 30 for w in ws):
        return False
    if re.search(r"\d", h):
        return False
    letters = sum(c.isalpha() for c in h)
    if letters < 1 or letters < 0.7 * len(h.replace(" ", "")):
        return False
    if len(ws) > 1 and ws[0].lower().strip(".,") in _NOT_FIRST:
        return False
    if len(ws) > 1 and ws[-1].lower() in ("of", "the", "and", "or", "to", "in", "a", "by", "for", "with"):
        return False
    return bool(_key(h))


def _cut_head(s, whole_ok=True, words=6):
    """'Abandon, v. To give up ...' -> ('Abandon', 'v. To give up ...'); None if s doesn't start with one."""
    s0 = s
    s = _LEADJUNK.sub("", s.strip())
    lead = len(s0.strip()) - len(s)
    m = _STOP.search(s)
    if m and m.start() > 0:
        head, rest = s[:m.start()], s[m.start():]
    elif not m and whole_ok and len(s.split()) <= words:
        head, rest = s, ""
    else:
        return None
    if len(head.split()) > words:
        return None
    h = _clean_head(head)
    if not _valid_head(h, words):
        # 'Mortgage Im6rg;j I . A mortgage ...': a headword followed by an OCR'd pronunciation
        ws = h.split()
        k = next((i for i, w in enumerate(ws) if re.search(r"[\d/;|\\]", w)), None)
        if not k or not _valid_head(" ".join(ws[:k]), words):
            return None
        h = " ".join(ws[:k])
        cut = s.find(ws[k], len(h))
        rest = legal._strip_pron(s[cut:]) if cut >= 0 else rest
        rest = re.sub(r"^[^A-Za-z(\"“]*", "", rest) if rest is not None else ""
        return h, rest, lead, ". "
    stop = rest[:2]
    rest = re.sub(r"^[\s.,;:—–=-]+", "", rest)
    return h, rest, lead, stop


def _from_run(run, text, words=6):
    """The headword in a bold/other-font run at the line start (the run's end is a stop too)."""
    r = run.strip()
    if not r or not re.search(r"[^\W\d_]", r):
        return None
    c = _cut_head(r, True, words)
    if not c:
        return None
    h = c[0]
    # the rest of the line after the headword as printed
    t = text.strip()
    idx = t.find(h[:max(1, min(len(h), 12))])
    after = t[idx + len(h):] if idx >= 0 else t[len(r):]
    after = re.sub(r"^[¹²³⁴⁵⁶⁷⁸⁹⁰*]*\d{0,2}(?!\d)[\s.,;:—–=-]*", "", after)
    return h, after


def _from_caps(text, nxt=()):
    t = _LEADJUNK.sub("", text.strip())
    t = re.sub(r"^([^a-z]{2,60}?[A-Z][.,])[‘’'\"`*°]+(?=\s)", r"\1", t)   # 'ABORTION.‘ The act': a footnote mark
    h = legal._caps_head(t)
    used = 0
    if h and not h[2]:
        h2 = legal._caps_head_lines(t, *nxt) if nxt else None
        if h2:
            h, used = (h2[0], h2[1], True), h2[3]
        else:
            h = (h[0], "", True) if len(h[0].split()) <= 4 else None
    if not h:
        return None
    hd = _clean_head(legal._fix_caps_ocr(h[0]))        # OCR slips in capitals: 'Al\IBITUS' -> 'AMBITUS'
    if not _valid_head(hd, 8) and not _valid_head(re.sub(r",.*$", "", hd), 6):
        return None
    rest = re.sub(r"^[\s.,;:—–=-]+", "", h[1])
    return hd, rest, used


# ---------------------------------------------------------------- per-page features
DETECTORS = [  # (name, flags that must all hold, weight[, or these flags: a second way to mark a headword])
    ("bold", ("bold",), 1.0), ("font", ("font",), 0.97), ("caps", ("caps",), 1.0),
    ("hang", ("hang",), 0.97), ("indent", ("indent",), 0.9), ("para", ("para",), 0.8),
    ("bold+hang", ("bold", "hang"), 1.0), ("bold+indent", ("bold", "indent"), 1.0),
    ("font+hang", ("font", "hang"), 0.98), ("font+indent", ("font", "indent"), 0.98),
    ("caps+hang", ("caps", "hang"), 1.0), ("caps+indent", ("caps", "indent"), 1.0),
    ("caps+bold", ("caps", "bold"), 1.0), ("caps+font", ("caps", "font"), 0.99),
    # a headword set out to the left of the text even where the scan lost its bold type
    ("bold|out", ("bold",), 1.0, ("out",)), ("font|out", ("font",), 0.97, ("out",)),
    ("bold+hang|out", ("bold", "hang"), 1.0, ("out",)), ("font+hang|out", ("font", "hang"), 0.98, ("out",)),
    ("caps|out", ("caps",), 1.0, ("out",)),
    ("bold|hang", ("bold",), 1.0, ("hang",)), ("font|hang", ("font",), 0.97, ("hang",)),
]
_DET = {d[0]: (d[1], d[2], d[3] if len(d) > 3 else None) for d in DETECTORS}
TOL = 3.0


def _levels(rows):
    """dx of each row from the left edge of the text near it (robust to skew and to one stray row)."""
    by_col = collections.defaultdict(list)
    for i, r in enumerate(rows):
        by_col[r["col"]].append(i)
    for col, idx in by_col.items():
        xs = [rows[i]["x"] for i in idx]
        for j, i in enumerate(idx):
            win = sorted(xs[max(0, j - 8):j + 9])
            need = max(2, 0.15 * len(win))
            base = None
            k = 0
            while k < len(win):              # the lowest cluster of line starts with enough lines in it
                e = k
                while e + 1 < len(win) and win[e + 1] - win[k] <= 1.5:
                    e += 1
                if e - k + 1 >= need:
                    base = win[k]
                    break
                k = e + 1
            if base is None:
                base = legal._median(win)
            dx = rows[i]["x"] - base
            rows[i]["dx"] = dx
            rows[i]["_lvl"] = -1 if dx <= -TOL else 0 if dx < TOL else 1 if dx <= 40 else 2
    ind = sum(1 for r in rows if r["_lvl"] == 1) / max(1, len(rows))
    for r in rows:
        r["_indfrac"] = ind


def _page_feats(rows, carry=None):
    """For each row: can a paragraph start here, and which head signals fire (with the headword each
    signal would give). carry = the last row of the previous page (or None)."""
    body = legal._body_font(rows)
    body_bold = _is_bold(body)
    _levels(rows)
    medlen = {}
    for col in {r["col"] for r in rows}:
        medlen[col] = legal._median([len(r["text"]) for r in rows if r["col"] == col]) or 40
    feats = []
    prev = carry
    for i, r in enumerate(rows):
        t = r["text"]
        ch = r["ch"]
        lvl = r["_lvl"]
        pt = prev["text"] if prev else ""
        prev_ends = (not pt) or pt.rstrip().endswith(_TERMINAL)
        prev_short = prev is not None and len(pt) < 0.7 * prev.get("_med", medlen.get(r["col"], 40))
        same = prev is not None and prev.get("col") == r["col"] and prev is not carry
        prev_lvl = prev.get("_lvl", 0) if same else 0
        # out to the left of the text near it, or at the margin of a column with indented lines
        hangs = lvl == -1 or (lvl == 0 and r["_indfrac"] >= 0.05)
        # an indented line where most lines are at the margin: a first-line indent (a paragraph starts,
        # even when the line before has lost its full stop to OCR)
        indents = lvl == 1 and r["_indfrac"] < 0.4
        can = prev is None or prev_ends or prev_short or hangs or indents or (same and lvl < prev_lvl)
        f = {"can": can, "lvl": lvl, "flags": set(), "run": None, "caps": None, "line": None}
        if can:
            if ch and ch[0][0] != body:
                run, font, nrun = _lead_run(ch, body)
                if re.search(r"[^\W\d_]", run):
                    f["flags"].add("font")
                    if _is_bold(font) and not body_bold:
                        f["flags"].add("bold")
                    h = None
                    if nrun == len(ch) and not re.search(r"[.,:;]$", run.strip()) and i + 1 < len(rows):
                        q = rows[i + 1]       # a headword that wraps onto the next line
                        if q["col"] == r["col"] and q["ch"] and q["ch"][0][0] == font:
                            run2, _, n2 = _lead_run(q["ch"], body)
                            run2 = run2.strip()
                            if run2 and (n2 < len(q["ch"]) or run2.endswith(".")) and len(run2) < 60:
                                j = run.rstrip()
                                j = (j[:-1] if j.endswith(("-", "­")) else j + " ") + run2
                                h = _from_run(j, j + " " + q["text"][len(run2):], 8)
                                if h:
                                    f["wrap"] = 1
                    f["run"] = h or _from_run(run, t)
            if re.match(r"[\W_]*[A-Z]{2}|[\W_]*[A-Z]\.?\s?[A-Z]", t):
                nxt = [q["text"] for q in rows[i + 1:i + 4] if q["col"] == r["col"]]
                c = _from_caps(t, nxt)
                if c:
                    f["flags"].add("caps")
                    f["caps"] = c
            if hangs:
                f["flags"].add("hang")
            if lvl == -1:
                f["flags"].add("out")
            if lvl == 1 and (prev_ends or prev_short or indents):
                f["flags"].add("indent")
            if prev_short and prev_ends:
                f["flags"].add("para")
            if f["flags"] - {"font", "bold", "caps"} or f["run"] is None:
                c = _cut_head(t)
                f["line"] = (c[0], c[1], c[3]) if c else None
        r["_med"] = medlen.get(r["col"], 40)
        feats.append(f)
        prev = r
    return feats


def _head_for(f, det):
    """(head, rest, rows used after this one) if detector det fires on this row's features."""
    flags, _, alt = _DET[det]
    if not f["can"]:
        return None
    if not all(x in f["flags"] for x in flags):
        if not alt or not all(x in f["flags"] for x in alt):
            return None
        flags = alt
    if "caps" in flags:
        h = f["caps"]
        return (h[0], h[1], h[2]) if h else None
    if "bold" in flags or "font" in flags:
        h = f["run"]
        return (h[0], h[1], f.get("wrap", 0)) if h else None
    h = f["line"]
    return (h[0], h[1], 0) if h else None


def _sub_entry(f, main):
    """A run-in sub-entry that names the headword it is under ('Heir presumptive.', 'Heiress.' under
    HEIR; 'Express covenant.' under COVENANT): (head, rest) or None. It must look like a headword (in
    another font, or a short phrase ended by a full stop at a paragraph start). Dictionaries put these
    in the main entry's place, so they need not be in alphabetical order."""
    if not main or not f["can"]:
        return None
    if f["run"] and "font" in f["flags"]:
        cand = f["run"]
        if len(cand[0].split()) > 5:
            return None
    elif f["line"] and f["line"][2][:1] == "." and ("indent" in f["flags"] or "para" in f["flags"]):
        cand = f["line"]
        if len(cand[0].split()) > 4:
            return None
    else:
        return None
    h = cand[0]
    if main[:1].isupper() and not h[:1].isupper():
        return None
    km, kh = _key(main), _key(h)
    if not km or kh == km or not legal._names(h, main):
        return None
    return cand[0], cand[1]


def _order_keep(heads):
    """Indices of headwords that are in alphabetical order (first two letters), letter by letter or
    word by word (like legal._drop_out_of_order)."""
    keep = set()
    for nospace in (True, False):
        keys = []
        for h in heads:
            k = _key(h)
            k = k.replace(" ", "") if nospace else k
            keys.append(k[:2] or "~")
        keep |= legal._lnds(keys)
    return keep


def _score(heads_by_file):
    n = sum(len(h) for h in heads_by_file)
    if not n:
        return 0.0, 0, 0, 0.0
    ok = sum(len(_order_keep(h)) for h in heads_by_file)
    frac = ok / n
    return ok * frac * frac, ok, n, frac


# ---------------------------------------------------------------- the parser
class NotADictionary(RuntimeError):
    pass


def _them(nfiles, one, many):
    return one if nfiles == 1 else many


def parse_generic(files, progress=None, cancelled=None):
    """[(head, text)] from any dictionary PDF(s), and a short description of what was detected."""
    fl = legal._expand(files)
    if not fl:
        raise RuntimeError("No PDF files were found there.")
    readers = [_open(f) for f in fl]
    sizes = [len(r.pages) for r in readers]
    total = sum(sizes)
    if not total:
        raise RuntimeError("No pages were found in the PDF files.")
    nf = len(fl)

    def check():
        if cancelled and cancelled():
            raise legal._Cancelled()

    def prog(x, m):
        if progress:
            progress(max(0.0, min(1.0, x)), m)
    # 1. sample pages: is there text, what furniture repeats, which head signal does this book use
    prog(0, "Looking at the layout")
    pts = _sample_points(sizes, 40)
    cache = {}
    sample = []
    letters = []
    for k, (fi, pi) in enumerate(pts):
        check()
        prog(0.04 * k / max(1, len(pts)), "Looking at the layout")
        try:
            rows = _rows(readers[fi].pages[pi])
        except Exception:
            rows = []
        cache[(fi, pi)] = rows
        letters.append(sum(len(re.findall(r"[A-Za-z]", r["text"])) for r in rows))
        sample.append((fi, pi, rows))
    texty = [n for n in letters if n >= 150]
    if not letters or len(texty) < max(1, 0.3 * len(letters)):
        raise RuntimeError(_them(nf, "This PDF has no text in it (a scan without OCR), so Flipbook can't read its words.",
                                 "These PDFs have no text in them (scans without OCR), so Flipbook can't read their words."))
    rep = _furniture_strings([rows for _, _, rows in sample])
    per = collections.defaultdict(lambda: collections.defaultdict(list))   # det -> file -> heads
    ncols = collections.Counter()
    head_lvls = collections.defaultdict(collections.Counter)
    for fi, pi, rows in sample:
        rows = _strip_furniture([dict(r) for r in rows], rep)
        if not rows:
            continue
        ncols[1 + max(r["col"] for r in rows)] += 1
        feats = _page_feats(rows)
        for name in _DET:
            for r, f in zip(rows, feats):
                h = _head_for(f, name)
                if h:
                    per[name][fi].append(h[0])
                    head_lvls[name][r["_lvl"]] += 1
    best = None
    scores = {}
    for name, (flags, w, alt) in _DET.items():
        sc, ok, n, frac = _score(list(per[name].values()))
        keys = collections.Counter(_key(h) for hs in per[name].values() for h in hs)
        if n and (len(keys) < 0.5 * n or keys.most_common(1)[0][1] > max(3, 0.2 * n)):
            sc = 0.0                       # the same few 'headwords' again and again: list labels, not entries
        scores[name] = (round(sc * w, 1), ok, n, round(frac, 2))
        if best is None or sc * w > scores[best][0]:
            best = name
    sc, ok, n, frac = scores[best]
    npages = max(1, len([1 for _, _, rows in sample if rows]))
    need = 8 if total <= 15 else 15
    if sc <= 0 or ok < need or frac < 0.6 or ok < 3 * math.sqrt(n) or ok / npages < 1.0:
        raise NotADictionary(_them(nf, "This PDF doesn't look like a dictionary: Flipbook couldn't find headwords "
                                       "in alphabetical order in it.",
                                   "These PDFs don't look like a dictionary: Flipbook couldn't find headwords "
                                   "in alphabetical order in them."))
    det = best
    flags = _DET[det][0]
    # the level of the definitions' own lines: indented under headwords at the margin, else the margin
    hl = head_lvls[det].most_common(1)
    text_lvl = 1 if "hang" in flags and hl and hl[0][0] == 0 else 0
    # 2. read every page with that signal
    evs = []                  # [kind, a, b, fi, gpage]
    letters_on = {}
    done = 0
    carry = None
    main = None
    gp = 0
    for fi, r in enumerate(readers):
        carry = None
        for pi in range(sizes[fi]):
            check()
            done += 1
            if done == 1 or done % 5 == 0 or done == total:
                prog(0.04 + 0.9 * done / total, f"Reading page {done:,} of {total:,}")
            rows = cache.pop((fi, pi), None)
            if rows is None:
                try:
                    rows = _rows(r.pages[pi])
                except Exception:
                    rows = []
            rows = _strip_furniture(rows, rep)
            g = gp + pi
            if not rows:
                continue
            letters_on[g] = sum(sum(c.isalpha() for c in row["text"]) for row in rows)
            feats = _page_feats(rows, carry)
            k = 0
            while k < len(rows):
                row, f = rows[k], feats[k]
                h = _head_for(f, det)
                if h:
                    evs.append(["head", h[0], h[1], fi, g])
                    main = h[0]
                    k += 1 + h[2]
                    continue
                s = _sub_entry(f, main)
                if s:
                    evs.append(["sub", s[0], s[1], fi, g])
                    k += 1
                    continue
                t = row["text"]
                prev = rows[k - 1] if k else carry
                pt = prev["text"] if prev else ""
                prev_ends = (not pt) or pt.rstrip().endswith(_TERMINAL)
                short = prev is not None and len(pt) < 0.7 * prev.get("_med", 40)
                new_para = prev_ends and ((row["_lvl"] - text_lvl >= 1 and row["_lvl"] < 2) or
                                          (short and re.match(r"[A-Z0-9\"“(]", t) is not None))
                evs.append(["text", t, new_para, fi, g])
                k += 1
            carry = rows[-1]
        gp += sizes[fi]
    prog(0.95, "Tidying the entries")
    # 3. headwords out of alphabetical order are text (a section heading, a stray line ...)
    # Files in the right order (A..Z, volume 1..n) are checked as one sequence, so an alphabetical list
    # at the back of the last file (abbreviations, tables) can't pass for the dictionary; files chosen
    # in another order are checked one by one.
    by_file = collections.defaultdict(list)
    for i, e in enumerate(evs):
        if e[0] == "head":
            by_file[e[3]].append(i)
    per_file = set()
    for fi, idx in by_file.items():
        per_file |= {idx[j] for j in _order_keep([evs[i][1] for i in idx])}
    allidx = [i for i, e in enumerate(evs) if e[0] == "head"]
    whole = {allidx[j] for j in _order_keep([evs[i][1] for i in allidx])}
    keep = whole if len(whole) >= 0.9 * len(per_file) else per_file
    for i in allidx:
        if i not in keep:
            e = evs[i]
            evs[i] = ["text", (e[1] + ". " + e[2]).strip(), True, e[3], e[4]]
    # 4. front and back matter: the pages before the first / after the last page with headwords
    heads_on = collections.Counter(e[4] for e in evs if e[0] == "head")
    dens = legal._median([v for v in heads_on.values()]) or 0
    thr = max(1, 0.25 * dens)
    dict_pages = {g for g, v in heads_on.items() if v >= thr}
    skip = set()
    front = back = 0
    gp = 0
    for fi, n in enumerate(sizes):
        pages = [g for g in range(gp, gp + n) if g in dict_pages]
        if pages:
            first, last = pages[0], pages[-1]
            # a page or two with fewer headwords just before the first full page still counts
            while first - 1 >= gp and heads_on.get(first - 1, 0) >= 1:
                first -= 1
            if last + 1 < gp + n and heads_on.get(last + 1, 0) >= 1:
                last += 1
            lead = list(range(gp, first))
            tail = list(range(last + 1, gp + n))
            # Between two files of one book, a single full page without headwords may be an entry going
            # on from one file to the next: only a nearly empty one (a section's title page) is dropped.
            if fi > 0 and len(lead) == 1 and letters_on.get(lead[0], 0) >= 400:
                lead = []
            if fi < len(sizes) - 1 and len(tail) == 1 and letters_on.get(tail[0], 0) >= 400:
                tail = []
            skip.update(lead)
            skip.update(tail)
            front += len(lead)
            back += len(tail)
        else:
            skip.update(range(gp, gp + n))
        gp += n
    evs = [e for e in evs if e[4] not in skip]
    # 5. assemble the entries
    voc = legal._vocab_of(e[1] + (" " + e[2] if e[0] != "text" else "") for e in evs)
    entries = []
    cur = sub = None
    for kind, a, b, fi, g in evs:
        if kind == "head":
            cur = [a, [b] if b else []]
            entries.append(cur)
            sub = None
        elif kind == "sub":
            # its own entry, and still a paragraph of the main entry (like legal.parse_pdfs' 'sub')
            sub = [a, [b] if b else []]
            entries.append(sub)
            if cur is not None:
                cur[1].append((a + ". " + b).strip())
        elif cur is not None:
            if b or not cur[1]:
                cur[1].append(a)
                sub = None
            else:
                cur[1][-1] = legal._join_lines(cur[1][-1], a)
                if sub is not None:
                    if sub[1]:
                        sub[1][-1] = legal._join_lines(sub[1][-1], a)
                    else:
                        sub[1].append(a)
    del evs
    out = legal._finish(entries, voc)
    style = {"signal": det, "columns": ncols.most_common(1)[0][0] if ncols else 1, "pages": total,
             "skipped_front": front, "skipped_back": back, "order": round(frac, 2), "files": nf}
    if len(out) < MIN_ENTRIES:
        raise NotADictionary(f"Only {len(out)} entries were found: "
                             + _them(nf, "this PDF doesn't look like a dictionary.",
                                     "these PDFs don't look like a dictionary."))
    style["scores"] = {k: v for k, v in sorted(scores.items(), key=lambda kv: -kv[1][0])[:4]}
    prog(1.0, "Tidying the entries")
    return out, style


# ---------------------------------------------------------------- database
def _write_new(base, title, kind, entries, origin):
    """Add one dictionary in a single transaction; returns its id. Nothing is written on failure."""
    os.makedirs(addon_dir(base), exist_ok=True)
    con = sqlite3.connect(db_path(base), timeout=60)
    try:
        legal._schema(con)
        con.execute("BEGIN IMMEDIATE")
        have = con.execute("SELECT count(*) FROM sources").fetchone()[0]
        if have >= MAX_DICTS:
            raise RuntimeError(f"You already have {have} dictionaries, the most Flipbook can hold. Remove one first.")
        row = con.execute("SELECT v FROM meta WHERE k='next_id'").fetchone()
        used = [int(m.group(1)) for (s,) in con.execute("SELECT id FROM sources") for m in [re.match(r"u(\d+)$", s)] if m]
        n = max([int(row[0]) if row and str(row[0]).isdigit() else 1] + [u + 1 for u in used])
        did = f"u{n}"
        seq = (con.execute("SELECT count(*) FROM sources").fetchone()[0] or 0)
        seqs = [_origin(o).get("seq", 0) for (o,) in con.execute("SELECT origin FROM sources")]
        seq = max(seqs + [0]) + 1
        nid = (con.execute("SELECT max(id) FROM entries").fetchone()[0] or 0) + 1
        erows, hrows = [], []
        for head, text in entries:
            erows.append((nid, did, head, zlib.compress(text.encode("utf-8"), 6)))
            for k in legal._head_keys(head):
                hrows.append((k, did, nid))
            nid += 1
        con.executemany("INSERT INTO entries VALUES(?,?,?,?)", erows)
        con.executemany("INSERT OR IGNORE INTO heads VALUES(?,?,?)", hrows)
        origin = dict(origin, kind=kind, seq=seq)
        con.execute("INSERT INTO sources VALUES(?,?,?,?,?)",
                    (did, title, len(erows), time.strftime("%Y-%m-%d %H:%M:%S"), json.dumps(origin)))
        con.execute("INSERT OR REPLACE INTO meta VALUES('next_id', ?)", (str(n + 1),))
        con.execute("INSERT OR REPLACE INTO meta VALUES('version', ?)", (DB_VERSION,))
        con.commit()
    except BaseException:
        try:
            con.rollback()
        except Exception:
            pass
        con.close()
        _drop_if_empty(base)
        raise
    con.close()
    _forget()
    return did


def _drop_if_empty(base):
    if not _dicts_at(db_path(base)):
        _rmtree(addon_dir(base))


def _rmtree(d):
    if not os.path.isdir(d):
        return
    for root, dirs, files in os.walk(d, topdown=False):
        for name in files:
            fp = os.path.join(root, name)
            for _ in range(10):
                try:
                    os.remove(fp)
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    time.sleep(0.3)
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(d)
    except OSError:
        pass


def _update(base, did, fn):
    p = db_path(base)
    if did not in {d["id"] for d in _dicts_at(p)}:
        raise ValueError(f"Unknown dictionary: {did}")
    con = sqlite3.connect(p, timeout=60)
    try:
        con.execute("BEGIN IMMEDIATE")
        fn(con)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    _forget()


def rename(base, did, title):
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    if not title:
        raise ValueError("The title can't be empty.")
    title = title[:120]
    _update(base, did, lambda con: con.execute("UPDATE sources SET title=? WHERE id=?", (title, did)))


def set_kind(base, did, kind):
    if kind not in KINDS:
        raise ValueError(f"Unknown kind: {kind}")

    def fn(con):
        o = _origin(con.execute("SELECT origin FROM sources WHERE id=?", (did,)).fetchone()[0])
        o["kind"] = kind
        con.execute("UPDATE sources SET origin=? WHERE id=?", (json.dumps(o), did))
    _update(base, did, fn)


def remove(base, did=None):
    """Remove one dictionary, or all of them (did None, or the last one: the add-on folder goes)."""
    with _running_lock:
        inst = _running.get(os.path.abspath(base))
    if inst is not None and inst.busy():
        raise RuntimeError("A dictionary is being added; cancel it first.")
    with _con_lock:                 # no look-up may reopen the file while it is being changed or deleted
        _remove(base, did)


def _remove(base, did):
    p = db_path(base)
    ds = _dicts_at(p)
    if did is not None and did not in {d["id"] for d in ds}:
        raise ValueError(f"Unknown dictionary: {did}")
    unload()
    if did is not None and len(ds) > 1:
        con = sqlite3.connect(p, timeout=60)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM heads WHERE src=?", (did,))
            con.execute("DELETE FROM entries WHERE src=?", (did,))
            con.execute("DELETE FROM sources WHERE id=?", (did,))
            con.commit()
            con.execute("VACUUM")
        finally:
            con.close()
        _forget()
        return
    _rmtree(addon_dir(base))


# ---------------------------------------------------------------- installer
_running = {}
_running_lock = threading.Lock()


class Installer:
    def __init__(self, base):
        self.base = base
        self.lock = threading.Lock()
        self.thread = None
        self.cancel_flag = False
        self.build_seconds = None
        self.style = None
        self._st = {"state": "idle", "source": None, "title": "", "kind": None, "pct": 0, "message": "",
                    "error": None}

    def status(self):
        return dict(self._st)

    def _set(self, **kw):
        if "pct" in kw:
            kw["pct"] = max(0, min(100, int(kw["pct"])))
        self._st.update(kw)

    def busy(self):
        return bool(self.thread and self.thread.is_alive())

    def cancel(self):
        self.cancel_flag = True

    def _fail(self, msg, title="", kind=None):
        self._set(state="error", source=None, title=title, kind=kind, pct=0, message="Error", error=msg)
        return self.status()

    def start_import(self, files, title=None, kind=None):
        with self.lock:
            if self.busy():
                st = self.status()
                st.update(state="error", error="A dictionary is already being added. Wait for it, or cancel it.")
                return st
            if kind not in KINDS:
                return self._fail("Choose whether this is a legal or a general dictionary.", title or "", None)
            fl = legal._expand(files)
            if not fl:
                return self._fail("No PDF files were found there.", title or "", kind)
            n = len(_dicts_at(db_path(self.base)))
            if n >= MAX_DICTS:
                return self._fail(f"You already have {n} dictionaries, the most Flipbook can hold. Remove one first.",
                                  title or "", kind)
            title = re.sub(r"\s+", " ", str(title or "")).strip()[:120]
            if not title:
                try:
                    title = suggest_title(fl) or "My dictionary"
                except Exception:
                    title = "My dictionary"
            self.cancel_flag = False
            self._set(state="building", source=None, title=title, kind=kind, pct=0, message="Opening the PDFs",
                      error=None)
            self.thread = threading.Thread(target=self._wrap, args=(fl, title, kind), daemon=True)
            with _running_lock:
                _running[os.path.abspath(self.base)] = self
            self.thread.start()
            return self.status()

    def _wrap(self, fl, title, kind):
        try:
            self._run(fl, title, kind)
        except legal._Cancelled:
            self._set(state="cancelled", message="Cancelled", error=None)
        except Exception as e:
            self._set(state="error", message="Error", error=str(e) or e.__class__.__name__)
        finally:
            with _running_lock:
                if _running.get(os.path.abspath(self.base)) is self:
                    _running.pop(os.path.abspath(self.base), None)

    def _run(self, fl, title, kind):
        t0 = time.time()
        entries, style = parse_generic(fl, lambda x, m: self._set(pct=94 * x, message=m), lambda: self.cancel_flag)
        if self.cancel_flag:
            raise legal._Cancelled()
        self.style = style
        self._set(pct=95, message=f"Saving {len(entries):,} entries")
        origin = {"files": [os.path.basename(f) for f in fl], "pages": style.get("pages", 0),
                  "style": {k: v for k, v in style.items() if k != "scores"}}
        did = _write_new(self.base, title, kind, entries, origin)
        self.build_seconds = time.time() - t0
        self._set(state="done", source=did, pct=100, message=f"Added: {len(entries):,} entries")


# ---------------------------------------------------------------- look-ups
_con = None
_con_path = None
_con_lock = threading.RLock()
_vocabs = {}             # tuple of source ids -> (vocab, buckets)


def _forget():
    with _con_lock:
        _vocabs.clear()


def unload():
    global _con, _con_path
    with _con_lock:
        if _con is not None:
            try:
                _con.close()
            except Exception:
                pass
        _con = None
        _con_path = None
        _vocabs.clear()


def _db(base):
    global _con, _con_path
    p = db_path(base)
    if _con is not None and _con_path != p:
        unload()
    if _con is not None and not os.path.isfile(p):
        unload()
    if _con is None:
        if not os.path.isfile(p):
            return None
        try:
            _con = legal._ro_connect(p)
            _con.execute("PRAGMA query_only=1")
            _con.execute("SELECT 1 FROM heads LIMIT 1")
        except sqlite3.Error:
            unload()
            return None
        _con_path = p
    return _con


def _in(srcs):
    return "(" + ",".join("?" * len(srcs)) + ")"


def _has(con, key, srcs):
    return con.execute(f"SELECT 1 FROM heads WHERE key=? AND src IN {_in(srcs)} LIMIT 1",
                       (key, *srcs)).fetchone() is not None


def _lemma(con, q, srcs):
    words = q.split(" ")
    for pos in ([len(words) - 1] + list(range(len(words) - 1))):
        w = words[pos]
        if len(w) < 4 or not w.isalpha():
            continue
        cands = _dict._lemma_candidates(w)
        for c in cands:
            k = " ".join(words[:pos] + [c] + words[pos + 1:])
            if c != w and _has(con, k, srcs):
                return k
        for c in cands:
            for c2 in _dict._lemma_candidates(c):
                k = " ".join(words[:pos] + [c2] + words[pos + 1:])
                if c2 != w and len(c2) >= 3 and _has(con, k, srcs):
                    return k
    return None


def _vocab(con, srcs):
    t = tuple(srcs)
    if t not in _vocabs:
        voc, buckets = {}, {}
        for k, n in con.execute(f"SELECT key, count(*) FROM heads WHERE src IN {_in(srcs)} GROUP BY key", srcs):
            voc[k] = n
            buckets.setdefault((k[:1], len(k)), []).append((k, legal._mask(k)))
        _vocabs[t] = (voc, buckets)
    return _vocabs[t]


def _suggest(con, w, srcs, limit=6):
    voc, buckets = _vocab(con, srcs)
    w = re.sub(r"[^a-z0-9 ]", "", w).strip()
    if len(w) < 3:
        return []
    scores = {}

    def add(c, dist):
        if c in voc and c != w:
            s = dist - 0.12 * math.log1p(voc[c])
            if c not in scores or s < scores[c]:
                scores[c] = s
    for a, b in _dict._OCR:
        i = w.find(a)
        while i >= 0:
            add(w[:i] + b + w[i + len(a):], 0.4)
            i = w.find(a, i + 1)
    for c in _dict._edits1(w):
        if c in voc:
            d = _dict._lev(w, c, 1)
            add(c, d if d is not None else 1)
    if len(w) >= 4 and len([s for s in scores.values() if s < 0.8]) < limit:
        qm = legal._mask(w)
        for L in range(len(w) - 2, len(w) + 3):
            for c, m in buckets.get((w[0], L), ()):
                if bin(qm ^ m).count("1") > 4:
                    continue
                d = _dict._lev(w, c, 2)
                if d is not None:
                    add(c, d)
    ranked = sorted(scores.items(), key=lambda x: (x[1], x[0]))
    return [c for c, s in ranked[:limit]]


def _related(con, keys, exclude, srcs, order):
    seen = {}
    for k in keys:
        if not k:
            continue
        rows = con.execute(f"SELECT h.key, h.src, e.head FROM heads h JOIN entries e ON e.id = h.eid "
                           f"WHERE h.key > ? AND h.key < ? AND h.src IN {_in(srcs)} LIMIT 600",
                           (k + " ", k + "!", *srcs)).fetchall()
        for key, src, head in rows:
            if key in exclude:
                continue
            old = seen.get(key)
            if old is None or order[src] < order[old[0]]:
                seen[key] = (src, head)
    ranked = sorted(seen.items(), key=lambda kv: (len(kv[0].split()), kv[0]))
    return [{"src": s, "head": h} for k, (s, h) in ranked[:MAX_RELATED]]


def lookup(base, word, kind=None):
    res = {"word": word, "found": False, "entries": [], "related": [], "suggestions": []}
    with _con_lock:
        con = _db(base)
        ds = _read_dicts(con) if con is not None else []
        ds = [d for d in ds if kind is None or d["kind"] == kind]
        if not ds:
            res["error"] = ("No dictionary of your own is installed." if kind is None else
                            f"No {kind} dictionary of your own is installed.")
            return res
        srcs = [d["id"] for d in ds]
        order = {d["id"]: i for i, d in enumerate(ds)}
        meta = {d["id"]: d for d in ds}
        q = _key(_dict.normalize_query(word))
        if not q:
            return res
        tries = [q] + ([q.replace(" ", "")] if " " in q else [])
        exact = next((t for t in tries if _has(con, t, srcs)), None)
        lem = _lemma(con, q, srcs)
        if lem == exact:
            lem = None
        keys = [k for k in (exact, lem) if k]
        if not keys:
            res["related"] = _related(con, [q], set(), srcs, order)
            res["suggestions"] = _suggest(con, q, srcs)
            return res
        per = {}
        used = set()
        for k in keys:
            rows = con.execute(f"SELECT e.id, e.src, e.head, e.data FROM heads h JOIN entries e ON e.id = h.eid "
                               f"WHERE h.key=? AND h.src IN {_in(srcs)} ORDER BY e.id", (k, *srcs)).fetchall()
            for eid, src, head, data in rows:
                if eid in used:
                    continue
                lst = per.setdefault(src, [])
                if len(lst) < MAX_PER_DICT:
                    used.add(eid)
                    lst.append({"src": src, "title": meta[src]["title"], "kind": meta[src]["kind"], "head": head,
                                "text": zlib.decompress(data).decode("utf-8")})
        for s in srcs:
            res["entries"] += per.get(s, [])
        res["found"] = bool(res["entries"])
        res["related"] = _related(con, keys, set(keys), srcs, order)
        if not res["found"]:
            res["suggestions"] = _suggest(con, q, srcs)
    return res
