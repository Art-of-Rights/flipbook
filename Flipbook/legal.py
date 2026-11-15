"""Legal dictionary for Flipbook's word look-up (optional add-on).

Three editions of Black's Law Dictionary can be put into one local SQLite file,
"addons/legal/legal.sqlite" next to Flipbook.exe:

  * blacks2 - the 2nd edition (1910), public domain. Downloaded from archive.org
    (the OCR text of the scan), checked against a pinned size and SHA-256.
  * blacks4 - the Revised 4th edition (1968), read from the user's own PDFs.
  * blacks5 - the 5th edition (1979), read from the user's own PDFs.

Text from the user's PDFs never leaves the PC: it is only written to legal.sqlite.
Each source is built in a background thread and replaces only its own rows, so the
other sources stay usable while one is (re)built. Standard library + pypdf.
"""
import bisect
import collections
import hashlib
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.request
import zlib

import dictionary as _dict

DB_VERSION = "1"

SOURCES = {  # display order: newest edition first
    "blacks5": {"title": "Black's Law Dictionary, 5th ed. (1979)", "kind": "pdf"},
    "blacks4": {"title": "Black's Law Dictionary, Revised 4th ed. (1968)", "kind": "pdf"},
    "blacks2": {"title": "Black's Law Dictionary, 2nd ed. (1910)", "kind": "download",
                "url": "https://archive.org/download/blacks-law-dictionary-2nd-edition-1910/"
                       "Black%27s%20Law%20Dictionary%2C%202nd%20Edition%20%281910%29_djvu.txt",
                "name": "blacks2_djvu.txt",
                "size": 7190959,
                "sha256": "ac302a5763c474f2236ef347e8649285aa3e0bcabd6cb22841ba91a468406f6e"},
}
ORDER = list(SOURCES)
DOWNLOAD_BYTES = SOURCES["blacks2"]["size"]

MAX_ENTRY_CHARS = 6000
MAX_PER_SOURCE = 3
MAX_RELATED = 12


def addon_dir(base):
    return os.path.join(base, "addons", "legal")


def db_path(base):
    return os.path.join(addon_dir(base), "legal.sqlite")


def download_dir(base):
    return os.path.join(addon_dir(base), "download")


def _ro_connect(path):
    uri = "file:" + urllib.request.pathname2url(os.path.abspath(path)) + "?mode=ro"
    return sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30)


def _built_sources(path):
    """{id: entries} of the sources in the database file (empty if none / unreadable)."""
    if not os.path.isfile(path):
        return {}
    try:
        con = _ro_connect(path)
        try:
            return {r[0]: r[1] for r in con.execute("SELECT id, entries FROM sources")}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def installed(base):
    return bool(_built_sources(db_path(base)))


def info(base):
    p = db_path(base)
    built = _built_sources(p)
    return {"installed": bool(built),
            "sources": [{"id": s, "title": SOURCES[s]["title"], "entries": built[s]} for s in ORDER if s in built],
            "bytes": os.path.getsize(p) if os.path.isfile(p) else 0}


# ---------------------------------------------------------------- PDFs: files and edition
def _natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def pdf_files(path):
    """A folder -> its PDF files sorted A..Z naturally; a PDF file -> [file]."""
    if os.path.isdir(path):
        names = [n for n in os.listdir(path) if n.lower().endswith(".pdf")
                 and os.path.isfile(os.path.join(path, n))]
        return [os.path.join(path, n) for n in sorted(names, key=_natural)]
    if os.path.isfile(path) and path.lower().endswith(".pdf"):
        return [path]
    return []


def _expand(paths):
    if isinstance(paths, str):
        paths = [paths]
    out = []
    for p in paths or []:
        for f in pdf_files(p):
            if f not in out:
                out.append(f)
    return out


def detect_edition(paths):
    """'blacks5' / 'blacks4' / None, from the title page text (else the file and folder names)."""
    files = _expand(paths)
    if not files:
        return None
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None

    def edition_in(txt, footer=False):
        t = re.sub(r"[^A-Z0-9]", "", txt.upper())
        if "FIFTHEDITION" in t or (footer and "5THED" in t):
            return "blacks5"
        if "FOURTHEDITION" in t or (footer and "4THED" in t):
            return "blacks4"
        return None
    readers = []
    if PdfReader:
        for f in files[:2]:
            try:
                r = PdfReader(f)
                readers.append(r)
                for i in range(min(2, len(r.pages))):       # the title page names the edition
                    e = edition_in(r.pages[i].extract_text() or "")
                    if e:
                        return e
            except Exception:
                pass
    names = " ".join(files).lower()
    if re.search(r"5th|fifth", names):
        return "blacks5"
    if re.search(r"4th|fourth", names):
        return "blacks4"
    for r in readers:                    # a section without the title page: the "Black's ... 4th Ed." footers
        try:
            for i in range(min(48, len(r.pages))):
                e = edition_in(r.pages[i].extract_text() or "", footer=True)
                if e:
                    return e
        except Exception:
            pass
    return None


# ---------------------------------------------------------------- text helpers
_QUOTES = _dict._QUOTES


def _key(s):
    """Look-up key of a headword or a query: folded, no punctuation, hyphens as spaces."""
    s = _dict.fold((s or "").translate(_QUOTES))
    s = s.replace("'", "").replace("-", " ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*[A-Za-z]|[A-Za-z]")
_TERMINAL = tuple(".?!)]:;\"”’'|")


def _vocab_of(texts):
    c = collections.Counter()
    for t in texts:
        for w in _WORD.findall(t):
            c[w.lower()] += 1
    return c


def _resolve_hyphens(text, voc):
    """'ef-\\x02fect' -> 'effect'; 're-\\x02entry' stays 're-entry' when that is the usual spelling."""
    def rep(m):
        a, b = m.group(1), m.group(2)
        joined, hyph = (a + b).lower(), (a + "-" + b).lower()
        if voc.get(hyph, 0) > voc.get(joined, 0):
            return a + "-" + b
        return a + b
    text = re.sub(r"([A-Za-z]+)-\x02([a-z]+)", rep, text)
    return text.replace("\x02", "")


def _fix_splits(text, voc):
    """OCR spaces inside words: 'relinq uish' -> 'relinquish', 'entire ly' -> 'entirely'."""
    def rep(m):
        a, b = m.group(1), m.group(2)
        j = voc.get((a + b).lower(), 0)
        if j >= 2 and j > 2 * min(voc.get(a.lower(), 0), voc.get(b.lower(), 0)):
            return a + b
        return m.group(0)
    for _ in range(2):
        text = re.sub(r"(?<![\w'\-])([A-Za-z]+) ([a-z]+)(?![\w'\-])", rep, text)
    return text


def _tidy(text):
    text = text.replace("\ufffd", "").replace("\u00ad", "").replace("\x02", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\| ?", " ", text)                      # OCR column rules
    text = re.sub(r"(?<=\w)\s+([,;:])(?=\s|$)", r"\1", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _cap(text):
    if len(text) <= MAX_ENTRY_CHARS:
        return text
    cut = text.rfind(" ", 0, MAX_ENTRY_CHARS)
    return text[:cut if cut > MAX_ENTRY_CHARS - 300 else MAX_ENTRY_CHARS].rstrip(" ,;:") + " …"


def _finish(entries, voc):
    """Final clean-up of (head, paragraphs) -> [(head, text)]."""
    out = []
    for head, paras in entries:
        head = _fix_head(head, voc)
        if not head:
            continue
        ps = []
        for p in paras:
            p = _fix_splits(_resolve_hyphens(p, voc), voc)
            p = _tidy(p)
            if p:
                ps.append(p)
        text = _cap("\n\n".join(ps))
        if len(re.findall(r"[A-Za-z]", text)) < 2:
            continue                       # a headword with no definition: OCR debris
        out.append((head, text))
    return out


def _fix_head(head, voc):
    head = head.replace("\u00ad", "").replace("\ufffd", "").replace("\x02", "")
    head = re.sub(r"\s+", " ", head).strip(" .,;:-|*'\"‘’“”")
    head = re.sub(r"^\d+[A-Z]?\s+(?=[A-Z]{2})", "", head)          # '7 ARRECT': a stray page mark
    if not head:
        return ""
    # 'CHEV AGE' -> 'CHEVAGE' when the joined word is what the text uses
    words = head.split(" ")
    out = [words[0]]
    for w in words[1:]:
        a, b = out[-1], w
        if a.isalpha() and b.isalpha() and len(b) >= 1:
            j = voc.get((a + b).lower(), 0)
            if j >= 2 and j > 2 * min(voc.get(a.lower(), 0), voc.get(b.lower(), 0)):
                out[-1] = a + b
                continue
        out.append(w)
    return " ".join(_ocr_word(w, voc) for w in out)


# OCR slips in headwords, checked against how the dictionary's own text spells the word
# ('BaHment' -> 'Bailment', 'Uttoral' -> 'Littoral', 'OABLE' -> 'CABLE')
_HEAD_OCR = [("H", "il"), ("U", "Li"), ("U", "li"), ("ll", "il"), ("Il", "il"), ("rn", "m"), ("li", "h"),
             ("cl", "d"), ("1", "l"), ("0", "o"), ("5", "s"), ("O", "C"), ("G", "C"), ("n", "il"), ("I", "l"),
             ("l", "i"), ("tl", "d"), ("ii", "u"), ("c", "e"), ("e", "c")]


def _ocr_word(w, voc):
    core = w.strip(".,;:'\"()")
    if len(core) < 4 or not re.fullmatch(r"[A-Za-z015]+", core):
        return w
    base = voc.get(core.lower(), 0)
    if base > 2:
        return w
    best, bc = None, max(6, 10 * base)
    for a, b in _HEAD_OCR:
        i = core.find(a)
        while i >= 0:
            v = core[:i] + b + core[i + len(a):]
            c = voc.get(v.lower(), 0)
            if c >= bc:
                best, bc = v, c
            i = core.find(a, i + 1)
    if not best:
        return w
    if core.isupper():
        best = best.upper()
    elif core[:1].isupper():
        best = best[:1].upper() + best[1:].lower()
    return w.replace(core, best)


def _head_keys(head):
    """'SANG, or SANC' -> ['sang or sanc', 'sang', 'sanc']; 'A QUO; A QUA' -> [.., 'a quo', 'a qua']."""
    keys = [_key(head)]
    parts = [p for p in re.split(r"\s*;\s*|,?\s+or\s+", head) if p.strip()]
    if len(parts) > 1 and all(len(p.split()) <= 3 for p in parts):
        keys += [_key(p) for p in parts]
    # 'SCAVAGE, SCHEVAGE, SCHEWAGE' (variant spellings) - but not 'Estoppel, in pais' or 'Acquiescence, estoppel by'
    if "," in head and not re.search(r"[a-z]", head):
        parts = [p for p in re.split(r"\s*,\s*|,?\s+or\s+", head) if p.strip()]
        if len(parts) > 1 and all(len(p.split()) == 1 for p in parts):
            keys += [_key(p) for p in parts]
    return [k for k in dict.fromkeys(keys) if k]


# ---------------------------------------------------------------- capital headwords (2nd and 4th editions)
def _caps_token(t):
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return bool(re.fullmatch(r"[0-9&()\[\],;.'\-]+", t)) and t not in (".", ",")
    low = sum(1 for c in letters if c.islower())
    return low == 0 or (len(letters) >= 3 and low / len(letters) <= 0.34)


_INITIALS = re.compile(r"[A-Z]{1,2}(?:\. ?[A-Z]{1,2}){1,3}\.?")


def _caps_head(text):
    """'ABATEMENT OF A NUISANCE. - The' -> ('ABATEMENT OF A NUISANCE', ' - The'); else None.
    Returns (head, rest, complete): complete=False when the whole line is capitals with no stop
    (a headword that wraps onto the next line)."""
    text = re.sub(r"\s*[•·*]+\s*(?=[.,](?:\s|$))", "", text)      # 'MURDER •. The unlawful killing'
    toks = text.split(" ")
    taken = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "":
            taken.append(t)
            i += 1
            continue
        if _caps_token(t):
            taken.append(t)
            i += 1
            continue
        if t in ("or", "and", "&") and taken and i + 1 < len(toks) and _caps_token(toks[i + 1]) \
                and re.search(r"[A-Z]{2}", toks[i + 1]):
            taken.append(t)
            i += 1
            continue
        break
    while taken and taken[-1] == "":
        taken.pop()
    if not taken:
        return None
    whole = i >= len(toks) or all(t == "" for t in toks[i:])
    if whole and not taken[-1].endswith("."):
        h = " ".join(taken).strip()
        if re.search(r"[A-Z]{2}", h):
            return h, "", False          # all capitals and no stop: may go on on the next line
    # back off to the last token that ends with the headword's stop
    end = len(taken)
    while end > 0 and not re.search(r"[.,]$", taken[end - 1]):
        end -= 1
    if end == 0 and not whole and toks[i].startswith("(") and len(taken) <= 6:
        end = len(taken)                   # 'TORT (from Lat. torquere, to twist ...). A private wrong'
        taken[-1] += "."
    if end == 0:
        if whole:
            h = " ".join(taken).strip()
            if re.search(r"[A-Z]{2}", h):
                return h, "", False
        return None
    head_toks = taken[:end]
    # an internal ". " ends the headword ('A AVER ET TENER. UL. Fr.'), unless it is all initials ('A. C. C.')
    for k, t in enumerate(head_toks[:-1]):
        if t.endswith("."):
            if not all(re.fullmatch(r"[A-Z]\.", x) for x in head_toks[:k + 1]):
                head_toks = head_toks[:k + 1]
            break
    head = " ".join(head_toks).strip()
    rest = " ".join(toks[len(head_toks):]) if len(head_toks) < len(toks) else ""
    core = head.rstrip(".,").strip()
    letters = re.sub(r"[^A-Za-z]", "", core)
    if len(letters) < 2:
        return None
    if not re.search(r"[A-Z]{2}", core) and not re.fullmatch(r"(?:[A-Z]\.\s?){2,}", head):
        return None
    if re.fullmatch(r"[IVXLC]+", letters):                 # 'III.' 'XV.' (statute / amendment numbers)
        return None
    if re.match(r"\s*\d+\s*[,;]|\s*\d+\s+[A-Z(]", rest):  # 'N. BK. 521, 52 L. R. A.' - a citation
        return None
    if re.search(r"(?<![A-Za-z\\])\d", core) and not re.fullmatch(r"A ?1", core):
        return None
    if _INITIALS.fullmatch(head) and \
            (not re.match(r"\s*[A-Z][a-z]{2,}", rest) or re.search(r"\d", rest[:24])):
        return None                                        # 'N. Y. , 531' 'S.E. 2d' - inside a citation
    return core, rest, True


def _caps_head_lines(first, *more):
    """A headword in capitals that wraps onto the next lines ('... NON EST RECE-' + 'DENDUM. From ...').
    Returns (head, rest, True, lines used after the first) or None."""
    joined = first
    for used, nxt in enumerate(more, 1):
        if not nxt:
            return None
        a = joined.rstrip()
        joined = (a[:-1] if a.endswith(("-", "\u00ad")) else a + " ") + nxt.lstrip()
        h = _caps_head(joined)
        if not h:
            return None
        if h[2]:
            return h[0], h[1], True, used
    return None


# ---------------------------------------------------------------- 2nd edition (archive.org OCR text)
_LATIN_MARK = {"est", "non", "et", "ad", "de", "qui", "quod", "nemo", "nihil", "ubi", "ex", "cum", "sed", "quae",
               "sunt", "debet", "potest", "esse", "lex", "jus", "nec", "neque", "aut", "vel", "quam", "ubi",
               "nulla", "nullum", "nullus", "omnis", "omne", "omnia", "res", "pro", "sine", "semper", "contra",
               "per", "quia", "quis", "quid", "ita", "enim", "nisi", "si", "id", "ab", "eo", "eum", "ejus",
               "sui", "sua", "suum", "facit", "fit", "habet", "legis", "lege", "juris", "jure"}
_ENGLISH = {"the", "of", "to", "and", "is", "was", "which", "that", "by", "for", "from", "with", "as", "be", "or",
            "an", "are", "it", "this", "his", "their", "who", "when", "where", "shall", "may", "any", "such",
            "not", "law", "see", "used", "said", "term", "one", "has", "have", "under", "court", "english",
            "old", "civil", "scotch", "french", "spanish", "roman", "practice", "pleading", "feudal", "on"}


def _is_maxim(sentence):
    words = re.findall(r"[A-Za-z]+", sentence)
    if len(words) < 3 or len(words) > 40:
        return False
    low = [w.lower() for w in words]
    if any(w in _ENGLISH for w in low):
        return False
    latin = sum(1 for w in low if w in _LATIN_MARK or re.search(r"(?:um|us|is|ae|orum|arum|tur|unt|it|ur|ibus)$", w))
    return latin >= max(2, len(words) // 2)


def _junk_line_b2(s):
    if re.search(r"Law\s*Dict\w*\s*[.(]", s) and re.search(r"\(\s*2", s):
        return True                                        # 'BL. Law Dict.(2p Ep.)-—-1' signature marks
    letters = sum(1 for c in s if c.isalpha())
    if letters == 0 and not re.search(r"\d", s):
        return True
    if re.fullmatch(r"\d{1,4}", s):                        # page numbers
        return True
    if letters <= 2 and len(s) <= 3:
        return True
    return False


def _running_head_b2(s):
    """An all-capitals line without a stop: a page's guide words ('AOCESSORY OBLIGATION')."""
    return bool(re.fullmatch(r"[A-Z][A-Z \-'&,]{1,60}", s)) and len(s.split()) <= 6 and not s.endswith(",")


def parse_blacks2(path, progress=None, cancelled=None):
    """[(head, text)] from the archive.org OCR text of the 2nd edition."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read().replace("\r", "")
    lines = raw.split("\n")
    del raw
    start = 0
    for i, l in enumerate(lines[:5000]):
        if re.search(r"The English indefinite article", l):
            start = i
            break
    end = len(lines)
    for i in range(start, len(lines)):
        if re.match(r"^\W*TABLE OF ABBREVIATIONS", lines[i]):
            end = i
            break
    lines = [l.strip().lstrip("‘’“”'\"*_—|•·~ ").rstrip(" |") for l in lines[start:end]]
    # '7 ARRECT. To accuse' - a stray page mark before a headword
    lines = [re.sub(r"^\d{1,2}[A-Z]?\s+(?=[A-Z]{3,}[A-Z ,'\-]*\.\s)", "", l) for l in lines]
    voc = _vocab_of(lines)
    entries = []          # [head, [paragraphs]]
    cur = None
    para = None           # the open paragraph (str) of cur
    blank = True          # previous line was blank / junk
    prev = ""
    n = len(lines)
    i = 0
    first = True

    def close_para():
        nonlocal para
        if cur is not None and para is not None and para.strip():
            cur[1].append(para)
        para = None

    def add_line(s, new_para):
        nonlocal para
        if cur is None:
            return
        if new_para and para is not None:
            # a blank line in OCR text is often just column debris: keep a sentence together
            if para.rstrip().endswith(_TERMINAL) and not para.rstrip().endswith("-"):
                close_para()
        if para is None:
            para = s
        else:
            if re.search(r"[-=]$", para):
                para = para[:-1] + "-\x02" + s if s[:1].islower() else para[:-1] + "-" + s
            elif para.endswith("—"):
                para += s
            else:
                para += " " + s
    while i < n:
        if cancelled and i % 4000 == 0 and cancelled():
            raise _Cancelled()
        if progress and i % 4000 == 0:
            progress(i / n)
        s = lines[i]
        if not s:
            blank = True
            i += 1
            continue
        if _junk_line_b2(s):
            i += 1
            continue
        nxt = lines[i + 1] if i + 1 < n else ""
        if blank or first:
            can_start = not re.search(r"\b[Ss]ee(?: also)?$", prev)
            m = re.search(r"([A-Za-z]+)[-=]$", prev)
            if can_start and m:                        # 'BONDED WARE-' / 'HOUSE SYSTEM.' is one headword
                w2 = re.match(r"[A-Za-z]+", s)
                if w2 and voc.get((m.group(1) + w2.group(0)).lower(), 0) >= max(1, voc.get(w2.group(0).lower(), 0) // 4):
                    can_start = False
        else:
            can_start = prev.endswith(_TERMINAL) and \
                not re.search(r"(?:\b[Ss]ee(?: also)?|\bv\.|[-=,]|\b(?:of|and|or|the|in))$", prev)
        more = lines[i + 1:i + 6]
        if _running_head_b2(s) and not (can_start and nxt and _caps_head_lines(s, *more)):
            i += 1                                     # guide words of a page
            continue
        h = _caps_head(s) if can_start else None
        if h and not blank and _INITIALS.fullmatch(h[0]):
            h = None                                   # 'N. Y. 170' / 'U. S.' inside a citation
        if h and not h[2]:                             # headword wrapped onto the next line(s)
            h = _caps_head_lines(s, *more)
            if h:
                i += h[3]
        if h and h[2]:
            close_para()
            cur = [h[0], []]
            entries.append(cur)
            para = None
            rest = h[1].strip().lstrip("-—– ").strip()
            if rest:
                add_line(rest, False)
            blank = False
            first = False
            prev = s
            i += 1
            continue
        if blank and cur is not None and re.match(r"[A-Z][a-z]", s):
            # a Latin maxim: 'A communi observantia non est recedendum. From common observance ...'
            j, block = i, []
            while j < n and lines[j] and len(block) < 6:
                block.append(lines[j])
                j += 1
            joined = re.sub(r"[-=]\s+(?=[a-z])", "", " ".join(block))
            m = re.match(r"(.{8,240}?[a-z])\.\s+(?=[A-Z0-9])", joined)
            if m and _is_maxim(m.group(1)):
                close_para()
                cur = [m.group(1), []]
                entries.append(cur)
                para = None
                # the rest of the first lines belongs to the definition
                consumed = 0
                acc = ""
                while consumed < len(block):
                    acc = (acc[:-1] if acc.endswith(("-", "=")) else acc + " ") + block[consumed] if acc else block[consumed]
                    consumed += 1
                    if len(re.sub(r"\s+", "", acc)) >= len(re.sub(r"\s+", "", m.group(0))):
                        break
                tail = acc
                k = tail.find(". ", max(0, len(m.group(1)) - 40))
                tail = tail[k + 2:] if k >= 0 else ""
                if tail.strip():
                    add_line(tail.strip(), False)
                i += consumed
                blank = False
                prev = block[consumed - 1]
                continue
        add_line(s, blank)
        blank = False
        first = False
        prev = s
        i += 1
    close_para()
    if progress:
        progress(1.0)
    return _finish([(h, ps) for h, ps in entries], voc)


# ---------------------------------------------------------------- 4th and 5th editions (the user's PDFs)
def _column_split(lines, W):
    """x between the two columns. First choice: just left of the right column's own margin, the most
    common line start in the middle part of the page (Chief fix, 1.8.17: the old "widest gap between
    left edges" rule picked the gap between the right margin and run-in text after a headword, e.g.
    253 -> 305 on the 4th's HOMAGE page, and merged both columns line by line). The widest-gap rule is
    kept as the fallback for pages without a clear right margin."""
    good = [l for l in lines if sum(len(t.strip()) for f, t in l["items"]) > 3
            and not re.fullmatch(r"[\d\s]+", "".join(t for f, t in l["items"]))]
    starts = {}
    for l in good:
        if W * 0.3 <= l["x"] <= W * 0.75:
            k = round(l["x"] / 3)
            starts[k] = starts.get(k, 0) + 1
    if starts:
        k, n = max(starts.items(), key=lambda kv: (kv[1], -kv[0]))
        n += starts.get(k - 1, 0) + starts.get(k + 1, 0)
        if n >= 5:
            # the margin and the headwords hanging out to its left (the 5th sets headwords ~9 units
            # left of their continuation lines: 270 vs 279)
            near = [l["x"] for l in good if k * 3 - 20 <= l["x"] <= k * 3 + 4.5]
            return min(near) - 6
    xs = sorted({round(l["x"]) for l in good})
    if len(xs) < 4:
        return W * 0.45
    lo, hi = xs[0], xs[-1]
    best, at = 0, None
    for a, b in zip(xs, xs[1:]):
        if b - a > best and a < lo + 0.75 * (hi - lo):
            best, at = b - a, (a + b) / 2
    if at is None or best < 40:
        # no clear gap: one column, or the default middle
        return W * 0.45 if hi - lo > W * 0.3 else W
    return at


def _page_rows(page):
    """Rows of text on a page: [{'col','x','y','ch':[(font,text)],'text'}], reading order."""
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
        W = float(page.mediabox.width)
    except Exception:
        W = 520.0
    # 1. lines in the order the PDF draws them (a chunk's own position is only reliable at a line start)
    lines = []
    cur = None
    ended = True
    for x, y, f, text in chunks:
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
    # 2. reading order: column, top to bottom; pieces of one printed line are merged
    split = _column_split(lines, W)
    for l in lines:
        l["col"] = 1 if l["x"] > split else 0
    lines.sort(key=lambda l: (l["col"], -l["y"], l["x"]))
    rows = []
    for l in lines:
        r = rows[-1] if rows else None
        if r is not None and r["col"] == l["col"] and abs(r["y"] - l["y"]) <= 2:
            r["items"] += [("", " ")] + l["items"]
        else:
            rows.append(dict(l))
    out = []
    for r in rows:
        ch = [(f, t) for f, t in r["items"]]
        while ch and not ch[0][1].strip():
            ch.pop(0)
        text = "".join(t for f, t in ch)
        if not text.strip():
            continue
        text = text.strip()
        # OCR read some hyphens as middle dots: 'termi·' at a line end, 'twenty·second', 'ad· vance'
        if "·" in text:
            text = re.sub(r"(?<=[A-Za-z])·$", "\u00ad", text)
            text = re.sub(r"(?<=[a-z])· (?=[a-z])", "", text)
            text = re.sub(r"(?<=[A-Za-z])·(?=[A-Za-z])", "-", text)
            if ch[-1][1].rstrip().endswith("·"):
                ch[-1] = (ch[-1][0], ch[-1][1].rstrip()[:-1] + "\u00ad")
        out.append({"col": r["col"], "x": r["x"], "y": r["y"], "ch": ch, "text": text})
    return out


_FOOTER = re.compile(r"blackslawdictionary(?:revised)?\d?(?:th)?ed", re.I)


def _strip_page_furniture(rows):
    if not rows:
        return rows
    top = max(r["y"] for r in rows)
    bot = min(r["y"] for r in rows)
    keep = []
    for r in rows:
        t = r["text"]
        letters = re.sub(r"[^A-Za-z0-9]", "", t)
        if _FOOTER.search(letters):
            continue
        if r["y"] > top - 8 and not re.search(r"[a-z]", t) and ("." not in t or re.fullmatch(r"[\d\s.]+", t)):
            continue                                   # guide words / page number at the top
        if r["y"] < bot + 4 and re.fullmatch(r"[\d\s]{1,6}|[IVXLCivxlc\s]{1,8}", t):
            continue                                   # page number at the bottom
        if not re.search(r"[A-Za-z0-9]", t):
            continue
        keep.append(r)
    return keep


def _body_font(rows):
    c = collections.Counter()
    for r in rows:
        for f, t in r["ch"]:
            c[f] += len(t.strip())
    return c.most_common(1)[0][0] if c else ""


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def _lead_run(ch, body, allow_connectors=True):
    """Text of the leading chunks that are not in the body font ('Abiaticus, or aviaticus')."""
    out = []
    font = ch[0][0] if ch else None
    for k, (f, t) in enumerate(ch):
        if f != body:
            out.append(t)
            continue
        if allow_connectors and t.strip() in ("or", "and", ",", "&", ";") and k + 1 < len(ch) and ch[k + 1][0] != body:
            out.append(t)
            continue
        break
    return "".join(out), font, len(out)


_BACK_MATTER = re.compile(r"APPENDICES|TABLEOFABBREVIATIONS|REGNALYEARS|ATABLEOF$|CONSTITUTIONOFTHEUNITEDSTATES")


def _classify(rows, edition, state=None):
    """Events for one page: ('head', head, rest), ('sub', head, rest) (4th ed. run-in sub-entries),
    ('sub5', head, rest) (5th ed. bold run-ins, decided later) and ('text', text, new_para).
    state is a per-file dict (kept for future use)."""
    body = _body_font(rows)
    bx = {}
    lens = {}
    for col in (0, 1):
        xs = [r["x"] for r in rows if r["col"] == col and r["ch"] and r["ch"][0][0] == body]
        bx[col] = _median(xs) if len(xs) >= 3 else None
        lens[col] = _median([len(r["text"]) for r in rows if r["col"] == col]) or 40
    minx = {col: min([r["x"] for r in rows if r["col"] == col] or [0]) for col in (0, 1)}
    # scanned pages are often a little skewed, so the left edge of the text drifts down a column:
    # compare each line with the body-text lines near it
    body_idx = [i for i, r in enumerate(rows) if r["ch"] and r["ch"][0][0] == body]
    local = {}
    for i, r in enumerate(rows):
        near = sorted((abs(j - i), rows[j]["x"]) for j in body_idx if rows[j]["col"] == r["col"] and j != i)[:6]
        local[id(r)] = _median([x for d, x in near]) if len(near) >= 2 else bx[r["col"]]
        r["_bx"] = local[id(r)]

    def hanging(r, prev_ends=True):
        x0 = r.get("_bx", bx[r["col"]])
        if x0 is not None:
            return r["x"] <= x0 - 3
        return r["x"] <= minx[r["col"]] + 1.5 and prev_ends
    ev = []
    prev = None
    k = 0
    n = len(rows)
    while k < n:
        r = rows[k]
        t = r["text"]
        ch = r["ch"]
        x0 = r.get("_bx", bx[r["col"]])
        first_font = ch[0][0] if ch else ""
        prev_t = prev["text"] if prev is not None and prev["col"] == r["col"] else (prev["text"] if prev else "")
        prev_ends = (not prev_t) or prev_t.rstrip().endswith(_TERMINAL)
        head = None
        if edition == "blacks4":
            if first_font != body and (x0 is None or r["x"] <= x0 + 4):
                h = _caps_head(t)
                if h and not h[2]:
                    more = []
                    for q in rows[k + 1:k + 6]:
                        if q["col"] != r["col"] or not q["ch"] or q["ch"][0][0] == body:
                            break
                        more.append(q["text"])
                    h = _caps_head_lines(t, *more) if more else None
                    if h:
                        k += h[3]
                if h and h[2]:
                    head = ("head", h[0], h[1])
            if head is None and first_font != body and x0 is not None and r["x"] >= x0 + 4 and prev_ends:
                run, f, nrun = _lead_run(ch, body, False)
                rs = run.strip()
                if nrun < len(ch) and re.fullmatch(r"[A-Z][a-z][A-Za-z'\- ,]{1,60}\.", rs) and len(rs.split()) <= 6:
                    head = ("sub", rs[:-1], t[len(run.rstrip()) if t.startswith(run.rstrip()) else 0:].strip())
            if head is None and first_font != body and re.match(r"[-—–]\s?[A-Z]", t) and prev_ends:
                # '-Simple Battery. One not accompanied by ...' (a dash and bold type)
                run, f, nrun = _lead_run(ch, body, False)
                rs = run.strip()
                rest = t[len(rs):] if t.startswith(rs) else ""
                if nrun == len(ch) and not rs.endswith(".") and k + 1 < n and rows[k + 1]["ch"] \
                        and rows[k + 1]["ch"][0][0] != body and rows[k + 1]["col"] == r["col"]:
                    run2, _, n2 = _lead_run(rows[k + 1]["ch"], body, False)
                    run2 = run2.strip()
                    if run2.endswith(".") or (n2 < len(rows[k + 1]["ch"]) and ". " in run2 + " "):
                        nxt = rows[k + 1]["text"]
                        m2 = re.match(r"(.*?\.)(?=\s|$)", run2)
                        part = m2.group(1) if m2 else run2
                        rs = (rs[:-1] if rs.endswith(("-", "­")) else rs + " ") + part
                        rest = nxt[len(part):] if nxt.startswith(part) else ""
                        k += 1
                m = re.match(r"[-—–]\s?(.+?[^.\s]{2})\.(?=\s|$)", rs)
                if m and len(m.group(1).split()) <= 9:
                    tail = rs[m.end():]
                    head = ("sub", m.group(1), (tail + " " + rest).strip())
        else:  # blacks5: headwords hang out to the left of their definitions; sub-entries are bold run-ins
            hang = hanging(r, prev_ends)
            # (whether a run-in is a headword is decided by parse_pdfs: it must name the main headword)
            sub_ok = (not hang and re.search(r"[.)\]\"”]$", prev_t.rstrip() or ".") is not None
                      and x0 is not None and r["x"] <= x0 + 3)
            if (hang or sub_ok) and first_font != body and re.match(r"[\"'“‘(]?[A-Z0-9]", t):
                run, f, nrun = _lead_run(ch, body)
                run = run.strip()
                if nrun == len(ch) and not re.search(r"[.,:;]$", run) and k + 1 < n \
                        and rows[k + 1]["ch"] and rows[k + 1]["ch"][0][0] != body \
                        and rows[k + 1]["col"] == r["col"]:
                    run2, _, n2 = _lead_run(rows[k + 1]["ch"], body)
                    run2 = run2.strip()
                    if run2 and (n2 < len(rows[k + 1]["ch"]) or run2.endswith(".")) and len(run2) < 60:
                        nxt = rows[k + 1]["text"]
                        joined = (run[:-1] if run.endswith(("-", "\u00ad")) else run + " ") + run2
                        rest2 = nxt[len(run2):] if nxt.startswith(run2) else ""
                        run, k = joined, k + 1
                        t = joined + " " + rest2
                # the headword ends at its stop: 'Statute, n. An act of ...' (some bold runs go on)
                m = re.match(r"(.*?[^.\s]{2})\.(?=\s|$)", run)
                hrun = run[:m.end()] if m else run
                hs = hrun.strip().rstrip(".,:;").strip()
                hs = re.sub(r",?\s+(?:n|v|adj|adv|pl|n\.\s*pl)$", "", hs)       # 'Bail, n' -> 'Bail'
                rest = t[len(hrun):] if t.startswith(hrun) else t[len(hs):]
                if 1 <= len(hs) <= 140 and (hang or (m and len(hs.split()) <= 8)):
                    head = ("head" if hang else "sub5", hs, rest)
        if head:
            ev.append(head)
        else:
            indent = x0 is not None and r["x"] >= x0 + 4
            short_prev = prev is not None and len(prev_t) < 0.72 * lens[r["col"]]
            new_para = prev_ends and (indent or (short_prev and re.match(r"[A-Z\"“(]", t) is not None))
            if x0 is not None and r["x"] > x0 + 30 and len(t.split()) <= 4 and not t.endswith(_TERMINAL):
                ev.append(("text", t + ".", True))     # a centred section label ('Conditional', 'English Law')
                prev = dict(r, text=t + ".")
                k += 1
                continue
            ev.append(("text", t, new_para))
        prev = rows[k]
        k += 1
    return ev


def _join_lines(para, s):
    if para.endswith("\u00ad"):
        return para[:-1] + s
    if para.endswith("-") and s[:1].islower():
        return para[:-1] + "-\x02" + s
    if para.endswith(("-", "—", "/")):
        return para + s
    return para + " " + s


_PRON_END = re.compile(r"(?:/|(?<=\S)[Il|]|\s[Il|/])\s?\.?(?=\s+(?:[A-Z(\"“]|$)|$)")
_PRON_STOP = {"in", "the", "of", "a", "an", "to", "and", "or", "see", "lat", "for", "fr", "is", "by", "as", "on",
              "that", "which", "with", "from", "law", "old", "civil", "english"}


def _strip_pron(rest):
    """'labey.m(t)s/. Lapse in succession' -> 'Lapse in succession' (5th ed. pronunciations, as OCR read them:
    the slashes often came out as 'I' or 'l')."""
    s = rest.lstrip()
    m = _PRON_END.search(s, 0, 130)
    if not m or m.end() < 2:
        return rest
    seg = s[:m.end()]
    toks = seg.split()
    if len(toks) > 9 or any(t.lower().strip(".,;:()") in _PRON_STOP for t in toks):
        return rest
    if not (s[0] in "/lI|[" or re.search(r";|\(y\)|iy|ey|ow|uw|/", seg)
            or (len(toks) <= 3 and re.search(r"\s[/Il|]\.?$", seg))):
        return rest
    return s[m.end():].lstrip(" .")


def _names(sub, main):
    """Does a run-in heading name its main headword? ('Bail bond' / 'Bail'; 'Bailment for hire' / OCR'd
    'BaHment'; not 'Commercial law' / 'Bill')."""
    pk = _key(main).split()
    if not pk:
        return False
    p = pk[0]
    stem = p[:max(4, len(p) - 3)] if len(p) > 4 else p
    for w in _key(sub).split():
        if w.startswith(stem) or (len(p) >= 5 and abs(len(w) - len(p)) <= 3
                                  and _dict._lev(p, w[:len(p) + 1], 2) is not None):
            return True
    return False


def _sub_head(sub, parent):
    """A run-in sub-entry of the 4th ed. names its parent only by implication: 'Implied' under ACCEPTANCE
    is 'Implied acceptance', 'De bonis non' under ADMINISTRATION is 'Administration de bonis non'."""
    pk, sk = _key(parent).split(), _key(sub).split()
    if not pk or not sk:
        return sub
    stem = pk[0][:max(4, len(pk[0]) - 3)]
    if any(w.startswith(stem) for w in sk):
        return sub
    p = parent.capitalize() if parent.isupper() else parent
    if len(sk) == 1:
        return sub + " " + p.lower()
    rest = sub[0].lower() + sub[1:]
    if sk[0] in ("in", "under", "at", "by", "of", "for", "to", "on", "as", "with") or \
            sk[-1] in ("law", "practice", "pleading", "procedure"):
        return p + ", " + rest                 # 'Merger, contract law'  'Assurance, in conveyancing'
    return p + " " + rest


_CAPS_OCR = [(re.compile(r"l\\[Il1]|l\\'I|I\\I|l\\f"), "M"), (re.compile(r"lU(?=[A-Z])|(?<=[A-Z])lU"), "M"),
             (re.compile(r"l'i(?=[A-Z])"), "N"), (re.compile(r"\blE(?=[A-Z])|(?<=[A-Z])lE(?=[A-Z])"), "AE"),
             (re.compile(r"(?<=[A-Z])mA"), "MA"), (re.compile(r"!\\(?=[A-Z])"), "A")]


def _fix_caps_ocr(head):
    """Common OCR slips in the 4th ed.'s capital headwords: 'Al\\IBITUS' -> 'AMBITUS', 'lEQUITAS' -> 'AEQUITAS'."""
    if sum(c.isupper() for c in head) < 2 * sum(c.islower() for c in head):
        return head
    for rx, rep in _CAPS_OCR:
        head = rx.sub(rep, head)
    return head


def parse_pdfs(paths, edition, progress=None, cancelled=None):
    """[(head, text)] from the user's Black's PDFs (4th or 5th edition)."""
    from pypdf import PdfReader
    files = _expand(paths)
    readers = []
    total = 0
    for f in files:
        r = PdfReader(f)
        readers.append((f, r))
        total += len(r.pages)
    if not total:
        raise RuntimeError("No pages found in the PDF files.")
    done = 0
    evs = []              # [kind, a, b, file index] for every line of every page
    texts_for_vocab = []
    seen_z = False
    for fi, (fname, reader) in enumerate(readers):
        last_file = fi == len(readers) - 1
        pages = reader.pages
        n = len(pages)
        # front matter: in a file that starts with the title page, the dictionary starts at the
        # page headed "DICTIONARY OF LAW" (after the preface, pronunciation guide, codes ...)
        start = 0
        stop = n
        state = {}
        if fi == 0:
            try:
                t0 = re.sub(r"[^A-Z]", "", (pages[0].extract_text() or "").upper())
            except Exception:
                t0 = ""
            if "LAWDICTIONARY" in t0 or "DICTIONARYOFLAW" in t0:
                start = None
        pi = 0
        while pi < n:
            if cancelled and cancelled():
                raise _Cancelled()
            if pi >= stop:
                done += n - pi
                break
            done += 1
            if progress:
                progress(min(1.0, done / total), f"Reading page {min(done, total)} of {total}")
            try:
                rows = _page_rows(pages[pi])
            except Exception:
                pi += 1
                continue
            top = re.sub(r"[^A-Z]", "", " ".join(r["text"] for r in sorted(rows, key=lambda r: -r["y"])[:3]).upper())
            if start is None:
                if "DICTIONARYOFLAW" in top and pi > 0:
                    start = pi
                elif pi > 160:          # no such page after all: read the file from the start
                    start = 0
                    total += pi
                    pi = 1
                    continue
                else:
                    pi += 1
                    continue
            pi += 1
            if _BACK_MATTER.search(top) and pi > 1:
                stop = pi
                continue
            letters = sum(len(re.findall(r"[A-Za-z]", r["text"])) for r in rows)
            if letters < 150:
                if seen_z and last_file:
                    stop = pi
                continue
            rows = _strip_page_furniture(rows)
            ev = _classify(rows, edition, state)
            if seen_z and last_file and not any(e[0] == "head" for e in ev):
                stop = pi                      # past Z: the tables at the back of the book
                continue
            for e in ev:
                kind, a, b = e[0], e[1], e[2]
                if kind in ("head", "sub5"):
                    b = b.strip()
                    if edition == "blacks5":
                        b = _strip_pron(b).strip()
                        a = re.sub(r"\s+[/|Il]$", "", a)     # an OCR'd slash of the pronunciation
                    else:
                        a = _fix_caps_ocr(a)
                    if kind == "head" and _key(a)[:1] == "z":
                        seen_z = True
                if kind == "text":
                    texts_for_vocab.append(a)
                else:
                    texts_for_vocab.append(a + " " + b.strip())
                evs.append([kind, a, b, fi])
    voc = _vocab_of(texts_for_vocab)
    del texts_for_vocab
    if progress:
        progress(1.0, "Tidying the entries")
    for e in evs:
        if e[0] in ("head", "sub5", "sub"):
            e[1] = _fix_head(e[1], voc) or e[1]
    _drop_out_of_order(evs)
    # assemble the entries
    entries = []          # [head, [paragraphs]]
    cur = sub = None
    main = ""
    for kind, a, b, fi in evs:
        if kind == "sub5":
            # a bold run-in is its own headword when it names the main one ('Bail bond' under 'Bail');
            # otherwise it is a section of the entry ('Commercial law.' under 'Bill')
            if _key(a)[:3] == _key(main)[:3] or _names(a, main):
                kind = "head"
            else:
                kind, a, b = "text", a + ". " + b, True
        elif kind == "head":
            main = a
        if kind == "head":
            cur = [a, [b] if b else []]
            entries.append(cur)
            sub = None
        elif kind == "sub":
            if cur is None:
                continue
            cur[1].append(a + ". " + b.strip())
            sub = [_sub_head(a, cur[0]), [b.strip()] if b.strip() else []]
            entries.append(sub)
        else:
            if cur is None:
                continue
            if b or not cur[1]:
                cur[1].append(a)
                sub = None
            else:
                cur[1][-1] = _join_lines(cur[1][-1], a)
                if sub is not None:
                    if sub[1]:
                        sub[1][-1] = _join_lines(sub[1][-1], a)
                    else:
                        sub[1].append(a)
    del evs
    return _finish(entries, voc)


def _lnds(keys):
    """Indices of a longest non-decreasing subsequence of keys (patience sorting with back-pointers)."""
    tails, tails_i, back = [], [], [-1] * len(keys)
    for j, k in enumerate(keys):
        p = bisect.bisect_right(tails, k)
        if p == len(tails):
            tails.append(k)
            tails_i.append(j)
        else:
            tails[p] = k
            tails_i[p] = j
        back[j] = tails_i[p - 1] if p > 0 else -1
    keep = set()
    j = tails_i[-1] if tails_i else -1
    while j >= 0:
        keep.add(j)
        j = back[j]
    return keep


def _drop_out_of_order(evs):
    """Main headwords run in alphabetical order. A 'headword' out of that order is a section heading
    inside an entry ('Contracts.' and 'Criminal law.' inside BILL) or OCR debris: it becomes text.
    Found per PDF file as the longest non-decreasing run of the headwords' first two letters."""
    by_file = collections.defaultdict(list)
    for i, e in enumerate(evs):
        if e[0] == "head":
            by_file[e[3]].append(i)
    for fi, idx in by_file.items():
        # The 5th sorts letter by letter ('A fortiori' between 'Afforest' and 'After'); the 4th puts the
        # 'A ...' phrases first. A headword is kept if it is in order under either convention.
        keep = set()
        for nospace in (True, False):
            keys = [(_key(evs[i][1]).replace(" ", "") if nospace else _key(evs[i][1]) or "~")[:2] or "~"
                    for i in idx]
            keep |= _lnds(keys)
        for j, i in enumerate(idx):
            if j not in keep:
                e = evs[i]
                evs[i] = ["text", (e[1] + ". " + e[2]).strip(), True, e[3]]


# ---------------------------------------------------------------- database
def _schema(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, title TEXT, entries INTEGER, built TEXT, origin TEXT);
        CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY, src TEXT, head TEXT, data BLOB);
        CREATE INDEX IF NOT EXISTS entries_src ON entries(src);
        CREATE TABLE IF NOT EXISTS heads(key TEXT, src TEXT, eid INTEGER, PRIMARY KEY(key, src, eid)) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS heads_src ON heads(src);
    """)


def _write_source(base, src, entries, origin=""):
    """Replace one source's rows in a single transaction (the other sources are untouched)."""
    os.makedirs(addon_dir(base), exist_ok=True)
    con = sqlite3.connect(db_path(base), timeout=60)
    try:
        _schema(con)
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM heads WHERE src=?", (src,))
        con.execute("DELETE FROM entries WHERE src=?", (src,))
        nid = (con.execute("SELECT max(id) FROM entries").fetchone()[0] or 0) + 1
        erows, hrows = [], []
        for head, text in entries:
            erows.append((nid, src, head, zlib.compress(text.encode("utf-8"), 6)))
            for k in _head_keys(head):
                hrows.append((k, src, nid))
            nid += 1
        con.executemany("INSERT INTO entries VALUES(?,?,?,?)", erows)
        con.executemany("INSERT OR IGNORE INTO heads VALUES(?,?,?)", hrows)
        con.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)",
                    (src, SOURCES[src]["title"], len(erows), time.strftime("%Y-%m-%d %H:%M:%S"), origin))
        con.execute("INSERT OR REPLACE INTO meta VALUES('version', ?)", (DB_VERSION,))
        con.commit()
    except BaseException:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        con.close()
    _forget_vocab()
    return len(entries)


# ---------------------------------------------------------------- installer
class _Cancelled(Exception):
    pass


_running = {}            # base -> Installer whose thread is busy
_running_lock = threading.Lock()


class Installer:
    def __init__(self, base):
        self.base = base
        self.lock = threading.Lock()
        self.thread = None
        self.cancel_flag = False
        self._st = {"state": "idle", "source": None, "pct": 0, "message": "", "error": None}

    def status(self):
        return dict(self._st)

    def _set(self, **kw):
        if "pct" in kw:
            kw["pct"] = max(0, min(100, int(kw["pct"])))
        self._st.update(kw)

    def busy(self):
        return bool(self.thread and self.thread.is_alive())

    def _launch(self, target, *args):
        with self.lock:
            if self.busy():
                return False
            self.cancel_flag = False
            self.thread = threading.Thread(target=self._wrap, args=(target,) + args, daemon=True)
            with _running_lock:
                _running[os.path.abspath(self.base)] = self
            self.thread.start()
            return True

    def _wrap(self, target, *args):
        try:
            target(*args)
        except _Cancelled:
            self._set(state="cancelled", message="Cancelled", error=None)
        except Exception as e:  # network errors, checksum mismatch, unreadable PDFs, disk full ...
            self._set(state="error", message="Error", error=str(e) or e.__class__.__name__)
        finally:
            with _running_lock:
                if _running.get(os.path.abspath(self.base)) is self:
                    _running.pop(os.path.abspath(self.base), None)

    def cancel(self):
        self.cancel_flag = True

    def _check(self):
        if self.cancel_flag:
            raise _Cancelled()

    # -- 2nd edition: download + build
    def start_download(self):
        if self.busy():
            return self.status()
        self._set(state="downloading", source="blacks2", pct=0, message="Starting the download", error=None)
        self._launch(self._run_download)
        return self.status()

    def _run_download(self):
        s = SOURCES["blacks2"]
        d = download_dir(self.base)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"Can't write to the Flipbook folder ({e.strerror}). Move Flipbook to a folder "
                               "you can write to, such as Documents.")
        dest = os.path.join(d, s["name"])
        ok = os.path.isfile(dest) and os.path.getsize(dest) == s["size"] and _sha256(dest) == s["sha256"]
        p0 = 0                     # a finished download left from an earlier try: the build is the whole bar
        if not ok:
            if os.path.isfile(dest):
                os.remove(dest)
            self._fetch(s, dest)
            p0 = 70
        self._check()
        self._set(state="building", pct=p0, message="Reading the 2nd edition")
        t0 = time.time()
        entries = parse_blacks2(dest, lambda x: self._set(pct=p0 + (96 - p0) * x,
                                                          message=f"Reading the 2nd edition ({int(x * 100)}%)"),
                                lambda: self.cancel_flag)
        self._check()
        self._set(pct=97, message=f"Saving {len(entries):,} entries")
        _write_source(self.base, "blacks2", entries, origin=s["url"])
        shutil.rmtree(d, ignore_errors=True)
        self.build_seconds = time.time() - t0
        self._set(state="done", pct=100, message=f"Installed: {len(entries):,} entries")

    def _fetch(self, s, dest):
        part = dest + ".part"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if have > s["size"]:
            os.remove(part)
            have = 0
        h = hashlib.sha256()
        if have:
            with open(part, "rb") as fp:
                for chunk in iter(lambda: fp.read(1 << 20), b""):
                    h.update(chunk)
        mb = s["size"] / 1e6
        tries = 0
        while have < s["size"]:
            self._check()
            self._set(state="downloading", pct=70 * have / s["size"],
                      message=f"Downloading the 2nd edition ({have / 1e6:.1f} of {mb:.1f} MB)")
            req = urllib.request.Request(s["url"], headers={"User-Agent": "Flipbook", "Accept-Encoding": "identity"})
            if have:
                req.add_header("Range", f"bytes={have}-")
            try:
                with urllib.request.urlopen(req, timeout=30) as r, open(part, "ab") as out:
                    if have and r.status != 206:      # the server ignored the resume request: start over
                        out.truncate(0)
                        have, h = 0, hashlib.sha256()
                    while True:
                        self._check()
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        if have + len(chunk) > s["size"]:
                            chunk = chunk[:s["size"] - have]
                        out.write(chunk)
                        h.update(chunk)
                        have += len(chunk)
                        self._set(pct=70 * have / s["size"],
                                  message=f"Downloading the 2nd edition ({have / 1e6:.1f} of {mb:.1f} MB)")
                        if have >= s["size"]:
                            break
                tries = 0
            except _Cancelled:
                raise
            except Exception as e:
                if getattr(e, "code", None) == 416:
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                    have, h = 0, hashlib.sha256()
                tries += 1
                if tries > 5:
                    raise RuntimeError(f"The download stopped ({e}). Check the internet connection and try again; "
                                       "it will continue where it left off.")
                time.sleep(2 * tries)
        self._set(message="Checking the download")
        if have != s["size"] or h.hexdigest() != s["sha256"]:
            try:
                os.remove(part)
            except OSError:
                pass
            raise RuntimeError("The download didn't match its checksum, so it was deleted. Please try again "
                               "(if it keeps happening, the file on archive.org has changed and Flipbook needs an update).")
        os.replace(part, dest)

    # -- 4th / 5th edition: the user's PDFs
    def start_import(self, paths, edition=None):
        if self.busy():
            return self.status()
        if edition is not None and edition not in ("blacks4", "blacks5"):
            self._set(state="error", source=None, pct=0, message="Error", error=f"Unknown edition: {edition}")
            return self.status()
        files = _expand(paths)
        if not files:
            self._set(state="error", source=edition, pct=0, message="Error", error="No PDF files were found there.")
            return self.status()
        self._set(state="building", source=edition, pct=0, message="Checking the PDFs", error=None)
        self._launch(self._run_import, files, edition)
        return self.status()

    def _run_import(self, files, edition):
        if edition is None:
            edition = detect_edition(files)
            if edition is None:
                raise RuntimeError("Couldn't tell which edition of Black's Law Dictionary these PDFs are "
                                   "(4th or 5th). Choose the edition and try again.")
        self._set(source=edition, pct=1, message="Reading the PDFs")
        t0 = time.time()
        entries = parse_pdfs(files, edition, lambda x, m: self._set(pct=1 + 94 * x, message=m),
                             lambda: self.cancel_flag)
        self._check()
        if len(entries) < 50:
            raise RuntimeError(f"Only {len(entries)} entries were found in these PDFs: they don't look like "
                               f"{SOURCES[edition]['title']}.")
        self._set(pct=96, message=f"Saving {len(entries):,} entries")
        _write_source(self.base, edition, entries, origin=f"{len(files)} PDF files")
        self.build_seconds = time.time() - t0
        self._set(state="done", pct=100, message=f"Installed: {len(entries):,} entries")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- removing
def remove(base, source=None):
    """Remove one source's rows, or the whole add-on (source None, or the last source)."""
    with _running_lock:
        inst = _running.get(os.path.abspath(base))
    if inst is not None and inst.busy():
        raise RuntimeError("A legal dictionary is being built; cancel it first.")
    unload()
    d = addon_dir(base)
    p = db_path(base)
    built = _built_sources(p)
    if source is not None and source not in SOURCES:
        raise ValueError(f"Unknown source: {source}")
    if source is not None and (set(built) - {source}):
        con = sqlite3.connect(p, timeout=60)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM heads WHERE src=?", (source,))
            con.execute("DELETE FROM entries WHERE src=?", (source,))
            con.execute("DELETE FROM sources WHERE id=?", (source,))
            con.commit()
            con.execute("VACUUM")
        finally:
            con.close()
        _forget_vocab()
        return
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


# ---------------------------------------------------------------- look-ups
_con = None
_con_path = None
_con_lock = threading.RLock()
_vocab = None            # key -> number of entries, for suggestions
_buckets = None          # (first letter, length) -> [(key, letter mask)]


def _forget_vocab():
    global _vocab, _buckets
    with _con_lock:
        _vocab = None
        _buckets = None


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
        _forget_vocab()


def _db(base):
    global _con, _con_path
    p = db_path(base)
    if _con is not None and _con_path != p:
        unload()
    if _con is None:
        if not os.path.isfile(p):
            return None
        try:
            _con = _ro_connect(p)
            _con.execute("PRAGMA query_only=1")
            _con.execute("SELECT 1 FROM heads LIMIT 1")
        except sqlite3.Error:
            unload()
            return None
        _con_path = p
    return _con


def _mask(w):
    m = 0
    for ch in w:
        o = ord(ch) - 97
        if 0 <= o < 26:
            m |= 1 << o
    return m


def _load_vocab(con):
    global _vocab, _buckets
    if _vocab is None:
        voc, buckets = {}, {}
        for k, n in con.execute("SELECT key, count(*) FROM heads GROUP BY key"):
            voc[k] = n
            buckets.setdefault((k[:1], len(k)), []).append((k, _mask(k)))
        _vocab, _buckets = voc, buckets
    return _vocab


def _suggest(con, w, limit=6):
    voc = _load_vocab(con)
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
        qm = _mask(w)
        for L in range(len(w) - 2, len(w) + 3):
            for c, m in _buckets.get((w[0], L), ()):
                if bin(qm ^ m).count("1") > 4:
                    continue
                d = _dict._lev(w, c, 2)
                if d is not None:
                    add(c, d)
    ranked = sorted(scores.items(), key=lambda x: (x[1], x[0]))
    return [c for c, s in ranked[:limit]]


def _has(con, key):
    return con.execute("SELECT 1 FROM heads WHERE key=? LIMIT 1", (key,)).fetchone() is not None


def _lemma(con, q):
    """The headword a query is an inflection of: 'abatements' -> 'abatement', 'abetted' -> 'abet'."""
    words = q.split(" ")
    for pos in ([len(words) - 1] + list(range(len(words) - 1))):
        w = words[pos]
        if len(w) < 4 or not w.isalpha():
            continue
        cands = _dict._lemma_candidates(w)
        for c in cands:
            k = " ".join(words[:pos] + [c] + words[pos + 1:])
            if c != w and _has(con, k):
                return k
        for c in cands:
            for c2 in _dict._lemma_candidates(c):
                k = " ".join(words[:pos] + [c2] + words[pos + 1:])
                if c2 != w and len(c2) >= 3 and _has(con, k):
                    return k
    return None


def _entries_for(con, key):
    rows = con.execute("SELECT e.id, e.src, e.head, e.data FROM heads h JOIN entries e ON e.id = h.eid "
                       "WHERE h.key=? ORDER BY e.id", (key,)).fetchall()
    return [(eid, src, head, data) for eid, src, head, data in rows]


def _related(con, keys, exclude):
    seen = {}
    for k in keys:
        if not k:
            continue
        rows = con.execute("SELECT h.key, h.src, e.head FROM heads h JOIN entries e ON e.id = h.eid "
                           "WHERE h.key > ? AND h.key < ? LIMIT 600", (k + " ", k + "!")).fetchall()
        for key, src, head in rows:
            if key in exclude:
                continue
            old = seen.get(key)
            if old is None or ORDER.index(src) < ORDER.index(old[0]):
                seen[key] = (src, head)
    ranked = sorted(seen.items(), key=lambda kv: (len(kv[0].split()), kv[0]))
    return [{"src": s, "head": h} for k, (s, h) in ranked[:MAX_RELATED]]


def lookup(base, word):
    res = {"word": word, "found": False, "entries": [], "related": [], "suggestions": []}
    with _con_lock:
        con = _db(base)
        if con is None:
            res["error"] = "The legal dictionary add-on is not installed."
            return res
        q = _key(_dict.normalize_query(word))
        if not q:
            return res
        tries = [q]
        if " " in q:
            tries.append(q.replace(" ", ""))
        exact = None
        for t in tries:
            if _has(con, t):
                exact = t
                break
        lem = _lemma(con, q)
        if lem == exact:
            lem = None
        keys = [k for k in (exact, lem) if k]
        if not keys:
            res["related"] = _related(con, [q], set())
            res["suggestions"] = _suggest(con, q)
            return res
        per = {}
        used = set()
        for k in keys:
            for eid, src, head, data in _entries_for(con, k):
                if eid in used:
                    continue
                lst = per.setdefault(src, [])
                if len(lst) < MAX_PER_SOURCE:
                    used.add(eid)
                    lst.append({"src": src, "title": SOURCES.get(src, {}).get("title", src), "head": head,
                                "text": zlib.decompress(data).decode("utf-8")})
        for s in ORDER:
            res["entries"] += per.get(s, [])
        res["found"] = bool(res["entries"])
        res["related"] = _related(con, keys, set(keys))
        if not res["found"]:
            res["suggestions"] = _suggest(con, q)
    return res
