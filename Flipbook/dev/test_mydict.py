"""Test of mydict.py (the user's own dictionaries).

  python dev\\test_mydict.py [--work DIR] [--keep] [--real DIR] [--only NAME ...]

Synthetic dictionaries (made-up words, written by our own little PDF writer with the standard
Type 1 fonts and real text operators) check the generic parser exactly; then the Installer, look-ups,
rename / set_kind / remove, cancel and MAX_DICTS in a temporary base folder.

--real DIR runs the parser on a representative set of the user's own dictionaries in DIR (their
copyrighted books: only counts and headwords looked up are printed, never their text, and nothing is
written outside the work folder).
"""
import argparse
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import logging  # noqa: E402

logging.disable(logging.CRITICAL)
import legal  # noqa: E402
import mydict  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
    return ok


# ---------------------------------------------------------------- a minimal PDF writer
FONTS = {"F1": "Times-Roman", "F2": "Times-Bold", "F3": "Courier", "F4": "Helvetica-Bold", "F5": "Times-Italic"}


def _esc(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class Page:
    def __init__(self):
        self.ops = []

    def text(self, x, y, parts, size=9):
        """One printed line: parts [(font, text)] drawn one after the other in one BT block."""
        s = "BT "
        for k, (f, t) in enumerate(parts):
            s += f"/{f} {size} Tf "
            if k == 0:
                s += f"1 0 0 1 {x:.2f} {y:.2f} Tm "
            s += f"({_esc(t)}) Tj "
        self.ops.append(s + "ET")

    def image(self):
        self.ops.append("q 400 0 0 600 100 100 cm /Im0 Do Q")


def write_pdf(path, pages, title=None, size=(612, 792), image=False):
    W, H = size
    objs = [None, None]
    fids = {}
    for k, name in FONTS.items():
        objs.append(f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} /Encoding /WinAnsiEncoding >>".encode())
        fids[k] = len(objs)
    img = None
    if image:
        data = bytes((i * 7) % 256 for i in range(64 * 64))
        objs.append(b"<< /Type /XObject /Subtype /Image /Width 64 /Height 64 /ColorSpace /DeviceGray "
                    b"/BitsPerComponent 8 /Length " + str(len(data)).encode() + b" >>\nstream\n" + data + b"\nendstream")
        img = len(objs)
    fontres = " ".join(f"/{k} {v} 0 R" for k, v in fids.items())
    kids = []
    for p in pages:
        c = "\n".join(p.ops).encode("latin-1")
        objs.append(b"<< /Length " + str(len(c)).encode() + b" >>\nstream\n" + c + b"\nendstream")
        cid = len(objs)
        xo = f" /XObject << /Im0 {img} 0 R >>" if img else ""
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] /Resources << /Font << {fontres} >>{xo} >> "
                    f"/Contents {cid} 0 R >>".encode())
        kids.append(len(objs))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    info = None
    if title is not None:
        objs.append(f"<< /Title ({_esc(title)}) >>".encode())
        info = len(objs)
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for o in offs:
        out += b"%010d 00000 n \n" % o
    tr = f"<< /Size {len(objs) + 1} /Root 1 0 R" + (f" /Info {info} 0 R" if info else "") + " >>"
    out += b"trailer\n" + tr.encode() + b"\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
    with open(path, "wb") as f:
        f.write(out)


# ---------------------------------------------------------------- made-up dictionaries
FURN = "Zorbish"            # in running heads and footers only: must never reach an entry


def _words(rng, n, cons, vows, syl=(2, 3)):
    out = set()
    while len(out) < n:
        k = rng.randint(*syl)
        out.add("".join(rng.choice(cons) + rng.choice(vows) for _ in range(k)) + rng.choice(["", "n", "l", "r", "t"]))
    return sorted(out)


FUNC = ["the", "of", "a", "to", "in", "or", "by", "for", "with", "which", "that", "is", "as", "an", "from", "when"]


def make_entries(seed, n, case="title", multi=0.12, shared=()):
    rng = random.Random(seed)
    heads = _words(rng, int(n * 1.2), "bdgkprstv", "aeiou", (2, 3))
    vocab = _words(rng, 700, "fhjlmnwz", "aeiouy", (2, 2))
    chosen = sorted(rng.sample(heads, n))
    out = []
    for h in chosen + list(shared):
        if h in shared:
            pass
        elif rng.random() < multi:
            h = h + " " + rng.choice(vocab)
        if case == "title":
            hd = h[:1].upper() + h[1:]
        elif case == "upper":
            hd = h.upper()
        else:
            hd = h
        sents = []
        for _ in range(rng.randint(1, 3)):
            ws = [rng.choice(vocab if rng.random() < 0.6 else FUNC) for _ in range(rng.randint(6, 20))]
            ws[0] = ws[0][:1].upper() + ws[0][1:]
            sents.append(" ".join(ws) + ".")
        out.append((hd, " ".join(sents)))
    out.sort(key=lambda e: legal._key(e[0]))
    return out


def wrap(s, n):
    lines, cur = [], ""
    for w in s.split():
        if cur and len(cur) + 1 + len(w) > n:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w) if cur else w
    if cur:
        lines.append(cur)
    return lines


def prose_pages(rng, n, vocab, heading=None):
    """Front/back matter or a non-dictionary: paragraphs with a first-line indent."""
    pages = []
    for p in range(n):
        pg = Page()
        y = 730
        if heading and p == 0:
            pg.text(220, 750, [("F4", heading)], 14)
            y = 710
        while y > 90:
            ws = [rng.choice(vocab if rng.random() < 0.6 else FUNC) for _ in range(rng.randint(40, 90))]
            ws[0] = ws[0][:1].upper() + ws[0][1:]
            para = " ".join(ws) + "."
            for k, l in enumerate(wrap(para, 95)):
                if y < 90:
                    break
                pg.text(72 + (18 if k == 0 else 0), y, [("F1", l)], 10)
                y -= 12
            y -= 4
        pg.text(300, 50, [("F1", str(p + 1))], 9)
        pages.append(pg)
    return pages


def layout_two_col(entries, first_page_no=1, front=0, back=0, seed=1, plain_every=0):
    """Two columns; a bold headword run in at the margin, the definition's other lines hang 9 pt in.
    Running head (guide words + page number) and a repeated footer. plain_every=n: every n-th headword
    is in the body font (as when a scan's OCR loses the bold)."""
    rng = random.Random(seed)
    vocab = _words(rng, 300, "fhjlmnwz", "aeiouy", (2, 2))
    pages = []
    if front:
        t = Page()
        t.text(180, 600, [("F4", "THE " + FURN.upper() + " LEXICON")], 20)
        t.text(230, 560, [("F1", "of made-up words")], 12)
        pages.append(t)
        pages += prose_pages(rng, front - 1, vocab, "Preface")
    cols = (50, 320)
    width = 52
    pg, col, y, on_page = None, 0, 0, []
    pno = first_page_no

    def new_page():
        nonlocal pg, col, y, on_page, pno
        if pg is not None:
            finish()
        pg, col, y, on_page = Page(), 0, 735, []

    def finish():
        nonlocal pno
        if on_page:
            pg.text(50, 765, [("F2", on_page[0].upper())], 9)
            pg.text(300, 765, [("F1", str(pno))], 9)
            pg.text(480, 765, [("F2", on_page[-1].upper())], 9)
        pg.text(240, 40, [("F5", FURN + " Lexicon of Made-up Words")], 8)
        pages.append(pg)
        pno += 1
    new_page()
    for n, (head, text) in enumerate(entries):
        hf = "F1" if plain_every and n % plain_every == plain_every - 1 else "F2"
        first = head + ","
        lines = wrap(first + " n. " + text, width)
        # the headword is bold, the rest of the first line is in the body font
        for k, l in enumerate(lines):
            if y < 70:
                if col == 0:
                    col, y = 1, 735
                else:
                    new_page()
            x = cols[col]
            if k == 0:
                on_page.append(head)
                pg.text(x, y, [(hf, first), ("F1", l[len(first):])])
            else:
                pg.text(x + 9, y, [("F1", l)])
            y -= 10.5
    finish()
    if back:
        pages += prose_pages(rng, back, vocab, "Appendix")
    return pages, [(h, "n. " + t) for h, t in entries]


def layout_caps(entries, seed=2):
    """One column, Courier only: 'BLORVAN, v. Definition ...'; continuation lines at the margin; a section
    letter between letters; running head in capitals with the page number."""
    pages = []
    pg, y, on_page = None, 0, []
    pno = 1
    width = 80

    def finish():
        if on_page:
            pg.text(72, 760, [("F3", f"{on_page[0]}   {pno}   {on_page[-1]}")], 9)
        pages.append(pg)
    pg, y = Page(), 730
    last_letter = None
    for head, text in entries:
        letter = head[0]
        lines = wrap(head + ", v. " + text, width)
        need = len(lines) + (3 if letter != last_letter else 0)
        if y - need * 11 < 60:
            finish()
            pno += 1
            pg, y, on_page = Page(), 730, []
        if letter != last_letter:
            pg.text(300, y, [("F3", letter)], 12)
            y -= 22
            last_letter = letter
        on_page.append(head)
        for k, l in enumerate(lines):
            pg.text(72, y, [("F3", l)])
            y -= 11
    finish()
    return pages, [(h, "v. " + t) for h, t in entries]


def layout_own_line(entries, seed=3):
    """A bold headword on a line of its own, the definition below it, indented."""
    pages = []
    pg, y = Page(), 740
    pno = 1
    for head, text in entries:
        lines = wrap(text, 85)
        if y - (len(lines) + 1) * 12 - 8 < 60:
            pg.text(290, 40, [("F1", f"- {pno} -")], 9)
            pages.append(pg)
            pno += 1
            pg, y = Page(), 740
        pg.text(72, y, [("F4", head)], 10)
        y -= 12
        for l in lines:
            pg.text(90, y, [("F1", l)], 10)
            y -= 12
        y -= 8
    pg.text(290, 40, [("F1", f"- {pno} -")], 9)
    pages.append(pg)
    return pages, entries


def layout_indent(entries, seed=4):
    """Two columns, one font (like an OCR text layer): a CAPITAL headword at a first-line indent, the
    definition's other lines at the margin; some entries have run-in sub-entries naming the headword
    ('Torvel blorvan.') and plain paragraphs ('In ...'), both indented."""
    rng = random.Random(seed)
    vocab = _words(rng, 200, "fhjlmnwz", "aeiouy", (2, 2))
    pages = []
    cols = (40, 315)
    pg, col, y = Page(), 0, 740
    expected = []

    def put(x_first, text):
        nonlocal pg, col, y
        for k, l in enumerate(wrap(text, 58)):
            if y < 60:
                if col == 0:
                    col, y = 1, 740
                else:
                    pages.append(pg)
                    pg, col, y = Page(), 0, 740
            pg.text(cols[col] + (x_first if k == 0 else 0), y, [("F1", l)])
            y -= 10
    for n, (head, text) in enumerate(entries):
        put(10, head + ". " + text)
        full = [text]
        if n % 7 == 3:
            word = head.lower()
            sub = rng.choice(vocab).capitalize() + " " + word
            subdef = " ".join(rng.choice(vocab) for _ in range(rng.randint(8, 25))) + "."
            put(10, sub + ". " + subdef)
            expected.append((sub, subdef))
            full.append(sub + ". " + subdef)
        if n % 5 == 1:
            para = "In " + " ".join(rng.choice(vocab + FUNC) for _ in range(rng.randint(10, 30))) + "."
            put(10, para)
            full.append(para)
        expected.append((head, " ".join(full)))
    pages.append(pg)
    return pages, expected


# ---------------------------------------------------------------- checking a parse
def compare(name, got, expected, min_frac=0.95):
    norm = lambda s: re.sub(r"\s+", " ", s).strip()
    by_head = {}
    for h, t in got:
        by_head.setdefault(h, []).append(norm(t))
    heads_ok = sum(1 for h, t in expected if h in by_head)
    text_ok = sum(1 for h, t in expected if norm(t) in by_head.get(h, []))
    n = len(expected)
    check(f"{name}: headwords right", heads_ok >= min_frac * n, f"{heads_ok} of {n}")
    check(f"{name}: entry texts exact", text_ok >= min_frac * n, f"{text_ok} of {n}")
    extra = len(got) - heads_ok
    check(f"{name}: no extra entries", extra <= 0.03 * n + 1, f"{len(got)} entries, {extra} not expected")
    junk = [h for h, t in got if FURN.lower() in (h + " " + t).lower() or re.search(r"\bPreface\b|\bAppendix\b", t)]
    check(f"{name}: no running heads, footers or front/back matter in entries", not junk, f"{len(junk)} with furniture")


def parse(files):
    return mydict.parse_generic(files)


# ---------------------------------------------------------------- the synthetic tests
def synthetic(work):
    d = os.path.join(work, "synth")
    os.makedirs(d, exist_ok=True)
    out = {}
    # 1. two columns, bold run-in + hanging indent, with front and back matter
    e1 = make_entries(11, 700)
    pages, exp1 = layout_two_col(e1, front=3, back=2, plain_every=25)
    p1 = os.path.join(d, "Zorbish two columns.pdf")
    write_pdf(p1, pages, title="The Zorbish Lexicon of Made-up Words")
    t0 = time.time()
    got, style = parse(p1)
    print(f"     two columns: {len(got)} entries, {style.get('signal')}, {style.get('columns')} col, "
          f"front {style.get('skipped_front')}, back {style.get('skipped_back')}, {time.time() - t0:.1f}s")
    compare("two columns bold+hang", got, exp1)
    check("two columns: signal uses bold or hang", re.search(r"bold|hang|font", style["signal"]), style["signal"])
    check("two columns: 2 columns seen", style["columns"] == 2, style["columns"])
    check("two columns: front matter skipped", style["skipped_front"] >= 3, style["skipped_front"])
    check("two columns: back matter skipped", style["skipped_back"] >= 2, style["skipped_back"])
    out["p1"] = p1
    out["e1"] = exp1
    # 2. one column, ALL-CAPS headwords, one font
    e2 = make_entries(22, 500, case="upper")
    pages, exp2 = layout_caps(e2)
    p2 = os.path.join(d, "untitled caps.pdf")
    write_pdf(p2, pages, title="Untitled")
    got, style = parse(p2)
    print(f"     caps: {len(got)} entries, {style.get('signal')}")
    compare("one column ALL-CAPS", got, exp2)
    check("caps: signal is caps", "caps" in style["signal"], style["signal"])
    # 3. bold headword on its own line + indented definition
    e3 = make_entries(33, 400, case="lower", shared=("quorvel",))
    pages, exp3 = layout_own_line(e3)
    p3 = os.path.join(d, "own line.pdf")
    write_pdf(p3, pages)
    got, style = parse(p3)
    print(f"     own line: {len(got)} entries, {style.get('signal')}")
    compare("head on its own line", got, exp3)
    out["p3"] = p3
    out["e3"] = exp3
    # 3b. one font, capital headwords at a first-line indent, run-in sub-entries
    e5 = make_entries(55, 450, case="upper", multi=0)
    pages, exp5 = layout_indent(e5)
    p5 = os.path.join(d, "indent one font.pdf")
    write_pdf(p5, pages)
    got, style = parse(p5)
    print(f"     first-line indent: {len(got)} entries, {style.get('signal')}")
    compare("first-line indent + sub-entries", got, exp5)
    check("first-line indent: signal uses caps or indent", re.search(r"caps|indent", style["signal"]), style["signal"])
    # 4. a multi-file book: one PDF per letter range, each section with a title page
    e4 = make_entries(44, 900)
    parts = [[e for e in e4 if e[0][0] < "G"], [e for e in e4 if "G" <= e[0][0] < "R"], [e for e in e4 if e[0][0] >= "R"]]
    # the last entry of the first file is long enough to fill a whole page with no headword on it
    long_head = parts[0][-1][0]
    long_text = parts[0][-1][1]
    while len(long_text) < 9000:
        long_text += " " + parts[0][-1][1]
    parts[0][-1] = (long_head, long_text)
    md = os.path.join(d, "Zorbish Lexicon parts")
    os.makedirs(md, exist_ok=True)
    exp4 = []
    for k, (lab, part) in enumerate(zip("ABC", parts)):
        pages, ex = layout_two_col(part, first_page_no=1 + 100 * k, front=1, seed=k)
        write_pdf(os.path.join(md, f"Zorbish Lexicon - Sec. {lab}.pdf"), pages)
        exp4 += ex
    cap = legal.MAX_ENTRY_CHARS
    legal.MAX_ENTRY_CHARS = 10 ** 6          # (entries are normally cut at 6,000 characters)
    try:
        got, style = parse(md)
    finally:
        legal.MAX_ENTRY_CHARS = cap
    print(f"     multi-file: {len(got)} entries, {style.get('signal')}, files {style.get('files')}")
    compare("multi-file book", got, exp4)
    want = dict(exp4)[long_head]
    have = [re.sub(r"\s+", " ", t) for h, t in got if h == long_head]
    check("multi-file: a full page with no headword at a file's end is kept", have and have[0] == want,
          f"{len(have[0]) if have else 0} of {len(want)} chars")
    check("multi-file: 3 files", style["files"] == 3, style["files"])
    out["md"] = md
    # 5. image-only and non-dictionary PDFs
    pg = []
    for _ in range(6):
        p = Page()
        p.image()
        pg.append(p)
    pimg = os.path.join(d, "scan without ocr.pdf")
    write_pdf(pimg, pg, image=True)
    try:
        parse(pimg)
        check("image-only PDF refused", False, "no error")
    except Exception as e:
        check("image-only PDF refused with the no-text message", "no text" in str(e) and "OCR" in str(e), str(e))
    rng = random.Random(5)
    vocab = _words(rng, 500, "bdgkprstvfhjlmnwz", "aeiou", (2, 3))
    pnd = os.path.join(d, "an essay.pdf")
    pages = []
    for k in range(4):
        pages += prose_pages(rng, 3, vocab, heading=f"Chapter {k + 1}")
    write_pdf(pnd, pages)
    try:
        got, style = parse(pnd)
        check("non-dictionary text PDF refused", False, f"{len(got)} entries, {style.get('signal')}")
    except mydict.NotADictionary as e:
        check("non-dictionary text PDF refused", "dictionary" in str(e), str(e))
    out["img"], out["nd"] = pimg, pnd
    # 6. helpers for the dialog
    check("check_pdfs: text book", mydict.check_pdfs([p1]) == {"pages": len(layout_two_col(e1, front=3, back=2)[0]),
                                                              "text": True, "files": 1}, mydict.check_pdfs([p1]))
    ci = mydict.check_pdfs(pimg)
    check("check_pdfs: image-only book has no text", ci["text"] is False and ci["pages"] == 6, ci)
    cm = mydict.check_pdfs([md])
    check("check_pdfs: folder counts its files", cm["files"] == 3 and cm["text"], cm)
    check("suggest_title: real /Title", mydict.suggest_title([p1]) == "The Zorbish Lexicon of Made-up Words",
          mydict.suggest_title([p1]))
    check("suggest_title: 'Untitled' -> file name", mydict.suggest_title(p2) == "Untitled Caps", mydict.suggest_title(p2))
    check("suggest_title: per-letter folder -> common prefix", mydict.suggest_title([md]) == "Zorbish Lexicon",
          mydict.suggest_title([md]))
    py = os.path.join(d, "1927_zorbish_a_concise_law_dictionary.pdf")
    write_pdf(py, [Page()], title="Microsoft Word - zorbish.doc")
    check("suggest_title: year moved to the end", mydict.suggest_title(py) == "Zorbish a Concise Law Dictionary (1927)",
          mydict.suggest_title(py))
    check("guess_kind: law title -> legal", mydict.guess_kind([py], "A Concise Law Dictionary") == "legal")
    check("guess_kind: made-up words -> general", mydict.guess_kind([p1], "The Zorbish Lexicon") == "general")
    return out


def wait(inst, timeout=300):
    t0 = time.time()
    while inst.busy() and time.time() - t0 < timeout:
        time.sleep(0.1)
    return inst.status()


def installer_tests(work, s):
    base = os.path.join(work, "base")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base)
    check("nothing installed: installed() is False", not mydict.installed(base))
    r = mydict.lookup(base, "blorvan")
    check("nothing installed: lookup gives an error", "error" in r, r.get("error"))
    inst = mydict.Installer(base)
    st = inst.start_import([s["p1"]], "Zorbish Law Lexicon", "legal")
    check("start_import: building", st["state"] == "building" and st["kind"] == "legal", st)
    busy = inst.start_import([s["p3"]], "x", "general")
    check("start_import while busy: error status", busy["state"] == "error" and "already" in busy["error"], busy)
    st = wait(inst)
    check("first dictionary built", st["state"] == "done" and st["source"] == "u1", st)
    st = inst.start_import([s["p3"]], "", "general")
    st = wait(inst)
    check("second dictionary built", st["state"] == "done" and st["source"] == "u2", st)
    i = mydict.info(base)
    check("info: two dictionaries in the order added",
          [(d["id"], d["kind"]) for d in i["dicts"]] == [("u1", "legal"), ("u2", "general")], i["dicts"])
    check("info: titles (an empty title -> suggested)", [d["title"] for d in i["dicts"]] == ["Zorbish Law Lexicon", "Own Line"],
          [d["title"] for d in i["dicts"]])
    check("info: entries, files, pages, bytes",
          all(d["entries"] > 300 and d["files"] == 1 and d["pages"] > 5 for d in i["dicts"]) and i["bytes"] > 0, i)
    check("installed(kind)", mydict.installed(base) and mydict.installed(base, "legal") and mydict.installed(base, "general"))
    # look-ups
    h1, t1 = s["e1"][100]
    r = mydict.lookup(base, h1.lower())
    check("lookup: exact headword", r["found"] and r["entries"][0]["head"] == h1 and
          re.sub(r"\s+", " ", r["entries"][0]["text"]) == t1 and r["entries"][0]["src"] == "u1"
          and r["entries"][0]["kind"] == "legal" and r["entries"][0]["title"] == "Zorbish Law Lexicon", r["entries"][:1])
    single = [h for h, t in s["e1"] if " " not in h and not h.lower().endswith(("s", "e", "y"))]
    w = single[7].lower()
    for form in (w + "s", w + "ed", w + "ing"):
        r = mydict.lookup(base, form)
        check(f"lookup: inflection '{form}'", r["found"] and r["entries"][0]["head"].lower() == w,
              [e["head"] for e in r["entries"]])
    r = mydict.lookup(base, "“" + w.capitalize() + "’s,")
    check("lookup: quotes, capitals, possessive", r["found"] and r["entries"][0]["head"].lower() == w)
    miss = w[:-1] + ("x" if w[-1] != "x" else "q")
    r = mydict.lookup(base, miss)
    check("lookup: misspelling -> suggestion", not r["found"] and w in r["suggestions"], r["suggestions"])
    h3 = [h for h, t in s["e3"] if h != "quorvel"][50]
    r = mydict.lookup(base, h3, kind="legal")
    check("lookup kind=legal: not in the general one", not r["found"])
    r = mydict.lookup(base, h3, kind="general")
    check("lookup kind=general", r["found"] and r["entries"][0]["src"] == "u2")
    r = mydict.lookup(base, "quorvel")
    check("lookup: a word in one dictionary", r["found"] and {e["src"] for e in r["entries"]} == {"u2"})
    multi = [h for h, t in s["e1"] if " " in h]
    if multi:
        first = multi[0].split()[0].lower()
        r = mydict.lookup(base, first)
        check("lookup: related phrases", any(x["head"] == multi[0] for x in r["related"]) or r["found"], r["related"][:3])
    # rename / set_kind
    mydict.rename(base, "u2", "My Own Lines")
    mydict.set_kind(base, "u2", "legal")
    i = mydict.info(base)
    check("rename + set_kind", i["dicts"][1]["title"] == "My Own Lines" and i["dicts"][1]["kind"] == "legal", i["dicts"][1])
    r = mydict.lookup(base, h3, kind="legal")
    check("lookup after set_kind", r["found"] and r["entries"][0]["title"] == "My Own Lines")
    r = mydict.lookup(base, h3, kind="general")
    check("lookup kind with none of that kind: error", "error" in r, r.get("error"))
    try:
        mydict.rename(base, "u9", "x")
        check("rename unknown id: ValueError", False)
    except ValueError:
        check("rename unknown id: ValueError", True)
    # bad starts
    st = mydict.Installer(base).start_import([s["p3"]], "x", "medical")
    check("bad kind: error status", st["state"] == "error", st["error"])
    st = mydict.Installer(base).start_import([os.path.join(work, "nothing-here")], "x", "general")
    check("no files: error status", st["state"] == "error", st["error"])
    old = mydict.MAX_DICTS
    mydict.MAX_DICTS = 2
    st = mydict.Installer(base).start_import([s["p3"]], "x", "general")
    check("MAX_DICTS reached: error status", st["state"] == "error" and "Remove one" in st["error"], st["error"])
    mydict.MAX_DICTS = old
    # failed builds write nothing
    before = mydict.info(base)
    inst = mydict.Installer(base)
    inst.start_import([s["img"]], "scan", "general")
    st = wait(inst)
    check("image-only via the Installer: error, nothing written",
          st["state"] == "error" and "no text" in st["error"] and mydict.info(base)["dicts"] == before["dicts"], st["error"])
    inst.start_import([s["nd"]], "essay", "general")
    st = wait(inst)
    check("non-dictionary via the Installer: refused, nothing written",
          st["state"] == "error" and "dictionary" in st["error"] and mydict.info(base)["dicts"] == before["dicts"], st["error"])
    # cancel mid-build
    big = os.path.join(work, "synth", "big.pdf")
    if not os.path.isfile(big):
        pages, _ = layout_two_col(make_entries(77, 9000), seed=7)
        write_pdf(big, pages)
    inst.start_import([big], "big", "general")
    t0 = time.time()
    while inst.busy() and inst.status()["pct"] < 10 and time.time() - t0 < 120:
        time.sleep(0.05)
    try:
        mydict.remove(base, "u1")
        check("remove while building: RuntimeError", False)
    except RuntimeError:
        check("remove while building: RuntimeError", True)
    inst.cancel()
    st = wait(inst)
    check("cancel mid-build: cancelled, nothing written",
          st["state"] == "cancelled" and mydict.info(base)["dicts"] == before["dicts"], st)
    # remove
    try:
        mydict.remove(base, "u7")
        check("remove unknown id: ValueError", False)
    except ValueError:
        check("remove unknown id: ValueError", True)
    mydict.remove(base, "u1")
    i = mydict.info(base)
    check("remove one", [d["id"] for d in i["dicts"]] == ["u2"] and not mydict.lookup(base, h1)["found"], i["dicts"])
    inst = mydict.Installer(base)
    inst.start_import([s["p1"]], "again", "general")
    st = wait(inst)
    check("next id after a removal is max ever used + 1", st["source"] == "u3", st["source"])
    mydict.remove(base, "u3")
    mydict.remove(base, "u2")
    check("remove the last one: add-on folder gone", not os.path.exists(mydict.addon_dir(base)))
    check("after removing all: lookup gives an error", "error" in mydict.lookup(base, h1))
    inst = mydict.Installer(base)
    inst.start_import([s["p3"]], "x", "general")
    st = wait(inst)
    inst.start_import([s["p1"]], "y", "legal")
    st = wait(inst)
    check("ids start again at u1 once everything was removed", [d["id"] for d in mydict.info(base)["dicts"]] == ["u1", "u2"])
    mydict.remove(base)
    check("remove(None): everything gone", not os.path.exists(mydict.addon_dir(base)))
    mydict.unload()


# ---------------------------------------------------------------- the user's real books
# (name or [volumes], kind expected: "legal" / "general" / "not" (must be refused) / "maybe"[, check law words])
# The list of your own books is read from dev/real_books.json (or --books FILE), so it stays on your PC.
REAL = []
COMMON = ["abandon", "bail", "contract", "deed", "easement", "fee", "homage", "lien", "mortgage", "seisin", "tort", "will"]


def load_books(path):
    """The list of your own test books: dev/real_books.json by default (it stays on your PC)."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "real_books.json")
    if not os.path.isfile(path):
        print(f"No list of test books ({path}). Make one like this:\n"
              '  {"books": [["My Law Dictionary.pdf", "legal"], ["A folder of per-letter PDFs", "legal"], '
              '["Word Origins.pdf", "general"], ["An essay.pdf", "not"]]}')
        return []
    return [list(b) for b in json.load(open(path, encoding="utf-8")).get("books", [])]


def real(root, only):
    rows = []
    for spec in REAL:
        name, kind = spec[0], spec[1]
        words = spec[2] if len(spec) > 2 else True
        names = name if isinstance(name, list) else [name]
        label = names[0] + (f" (+{len(names) - 1} vols)" if len(names) > 1 else "")
        if only and not any(o.lower() in label.lower() for o in only):
            continue
        paths = [os.path.join(root, *n.split("/")) for n in names]
        if not all(os.path.exists(p) for p in paths):
            print(f"SKIP {label}: not found")
            continue
        t0 = time.time()
        cp = mydict.check_pdfs(paths)
        title = mydict.suggest_title(paths)
        gk = mydict.guess_kind(paths, title)
        print(f"     {label}: check_pdfs {cp} in {time.time() - t0:.1f}s; title {title!r}; guessed kind {gk}", flush=True)
        if kind in ("legal", "general"):
            check(f"real: {label} guess_kind", gk == kind, gk)
        t0 = time.time()
        err = None
        try:
            ents, style = mydict.parse_generic(paths)
        except Exception as e:
            ents, style, err = [], {}, str(e)
        secs = time.time() - t0
        used = cp["pages"] - style.get("skipped_front", 0) - style.get("skipped_back", 0)
        keys = set()
        for h, t in ents:
            keys.update(legal._head_keys(h))
        found = [w for w in COMMON if w in keys]
        row = {"book": label[:46], "text": "yes" if cp["text"] else "no", "pages": cp["pages"],
               "signal": style.get("signal", "-"), "entries": len(ents),
               "per_page": round(len(ents) / max(1, used), 1), "secs": round(secs), "found": found, "err": err,
               "kind": kind, "cols": style.get("columns", "-")}
        rows.append(row)
        if err:
            print(f"     {label}: refused: {err} ({secs:.0f}s)", flush=True)
        else:
            print(f"     {label}: {len(ents):,} entries, {style.get('signal')}, {style.get('columns')} col, "
                  f"front {style.get('skipped_front')} back {style.get('skipped_back')}, {secs:.0f}s"
                  + (f", common law words {len(found)}/12" if kind == "legal" else ""), flush=True)
        if kind == "not":
            check(f"real: {label} refused", err is not None, err or f"{len(ents)} entries")
        elif kind in ("legal", "general"):
            if cp["text"]:
                check(f"real: {label} read", err is None and len(ents) >= 50, err or f"{len(ents)} entries")
                if kind == "legal" and err is None and words:
                    check(f"real: {label} has common law words", len(found) >= 6, f"{len(found)}/12: missing "
                          + ", ".join(w for w in COMMON if w not in found))
            else:
                check(f"real: {label} (no text layer) refused with the no-text message",
                      err is not None and "no text" in err, err)
        mydict.unload()
    print()
    print(f"{'book':46} {'text':4} {'pages':>6} {'signal':12} {'cols':>4} {'entries':>8} {'/page':>6} {'secs':>5}  law words")
    for r in rows:
        lw = f"{len(r['found'])}/12" if r["kind"] == "legal" and not r["err"] else ""
        sig = r["signal"] if not r["err"] else ("no text" if "no text" in r["err"] else "refused")
        print(f"{r['book']:46} {r['text']:4} {r['pages']:>6} {sig:12} {str(r['cols']):>4} {r['entries']:>8} "
              f"{r['per_page']:>6} {r['secs']:>5}  {lw}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--real", default=None)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--no-synthetic", action="store_true")
    ap.add_argument("--books", default=None, help="list of test books for --real (default: dev/real_books.json)")
    a = ap.parse_args()
    if a.real:
        REAL[:] = load_books(a.books)
    work = a.work or tempfile.mkdtemp(prefix="mydict_test_")
    os.makedirs(work, exist_ok=True)
    t0 = time.time()
    try:
        if not a.no_synthetic:
            s = synthetic(work)
            installer_tests(work, s)
        if a.real:
            real(a.real, a.only)
    finally:
        mydict.unload()
        if not a.keep and not a.work:
            shutil.rmtree(work, ignore_errors=True)
        elif not a.keep:
            for sub in ("base",):
                shutil.rmtree(os.path.join(work, sub), ignore_errors=True)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed in {time.time() - t0:.0f}s"
          + ("" if not failed else "; FAILED: " + "; ".join(failed)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
