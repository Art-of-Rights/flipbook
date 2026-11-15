"""Offline English dictionary for Flipbook (optional add-on).

When the user asks for it, two free sources are downloaded from their official
sites, checked against pinned SHA-256 sums, and turned into one local SQLite
dictionary at "addons/dictionary/dictionary.sqlite" next to Flipbook.exe:

  * Webster's Revised Unabridged Dictionary (1913) - public domain, from
    Project Gutenberg (eBook #29765, the complete A-Z dictionary in one file).
  * Open English WordNet 2025 - CC BY 4.0, from its GitHub release.

The raw downloads are deleted once the dictionary is built. Everything runs
offline after that. Standard library only.
"""
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
import urllib.request
import zlib
import xml.etree.ElementTree as ET

DB_VERSION = "1"

ADDON_SOURCES = [
    {"name": "webster-1913.txt",
     "title": "Webster's Revised Unabridged Dictionary (1913)",
     "url": "https://www.gutenberg.org/files/29765/29765-0.txt",
     "size": 28936640,
     "sha256": "4d9c7a6f3b27add378aea8db3afc4d9190c2f4a6bd4341755b514155ab91873a",
     "licence": "Public domain in the USA. Project Gutenberg eBook #29765, "
                "\"Webster's Unabridged Dictionary\" (https://www.gutenberg.org/ebooks/29765)."},
    {"name": "english-wordnet-2025.xml.gz",
     "title": "Open English WordNet 2025",
     "url": "https://github.com/globalwordnet/english-wordnet/releases/download/2025-edition/english-wordnet-2025.xml.gz",
     "size": 11363503,
     "sha256": "9ca6d1dcb75f822fdd66617f7d9da48142ace38dd544d6ad5e2feca1674ad3fe",
     "licence": "CC BY 4.0. Open English WordNet (2025 edition) by John P. McCrae et al. and the "
                "Global WordNet Association, https://github.com/globalwordnet/english-wordnet, "
                "licensed under https://creativecommons.org/licenses/by/4.0/ ; based on Princeton "
                "WordNet 3.0 (c) Princeton University. Used here unchanged apart from reformatting."},
]
DOWNLOAD_BYTES = sum(s["size"] for s in ADDON_SOURCES)

ATTRIBUTION = ("Definitions from Webster's Revised Unabridged Dictionary (1913, public domain, via Project "
               "Gutenberg) and Open English WordNet 2025 (CC BY 4.0, globalwordnet.github.io).")

MAX_WEBSTER_ENTRIES = 8      # per headword
MAX_WEBSTER_DEFS = 12        # per entry
MAX_DEF_CHARS = 700
MAX_WN_SENSES = 10           # per headword
MAX_WN_EXAMPLES = 3
MAX_WN_SYNONYMS = 8


def addon_dir(base):
    return os.path.join(base, "addons", "dictionary")


def db_path(base):
    return os.path.join(addon_dir(base), "dictionary.sqlite")


def download_dir(base):
    return os.path.join(addon_dir(base), "download")


def installed(base):
    p = db_path(base)
    return os.path.isfile(p) and os.path.getsize(p) > 1 << 20


# ---------------------------------------------------------------- text helpers
_FOLD = {"æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss", "ø": "o", "Ø": "O", "ð": "d", "þ": "th",
         "ł": "l", "Ł": "L", "ı": "i"}
_FOLD_RE = re.compile("[" + "".join(_FOLD) + "]")


def fold(s):
    """Lower-case, ligatures expanded, accents removed: 'Preëminent' -> 'preeminent'."""
    s = unicodedata.normalize("NFKC", s)            # ﬁ ﬂ ﬀ ﬃ -> fi fl ff ffi
    s = _FOLD_RE.sub(lambda m: _FOLD[m.group(0)], s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower()


_QUOTES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'", "´": "'", "‐": "-", "‑": "-", "‒": "-",
                         "–": "-", "—": "-", "­": None, "​": None, "﻿": None})


def normalize_query(q):
    """'“Efiect’s,' -> 'efiect'; 'repre-\\nsentation' -> 'repre-sentation' (joined later)."""
    s = fold((q or "").translate(_QUOTES))
    s = re.sub(r"-\s+", "-", s)                     # 'repre- sentation' line-break leftovers
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^[^\w]+|[^\w]+$", "", s)          # surrounding punctuation, quotes, brackets
    s = re.sub(r"(?<=\w)'s$|(?<=s)'$", "", s)      # possessives: town's, towns'
    s = s.replace("_", " ")
    return s.strip(" -'")


# ---------------------------------------------------------------- Webster 1913 parser
_HEAD_RE = re.compile(r"^-?[A-Z0-9][A-Z0-9 ;,'\-\.&()ÆŒ]*$")
_POS_ABBR = ("n|a|adv|v|t|i|prep|conj|interj|pron|p|pr|pl|sing|imp|superl|compar|obs|vb|pa|"
             "dat|poss|obj|pret|nom|fem|masc|imper|inf|pers|sup")
_POS_RE = re.compile(r"^(.*?)(?:,\s*|\s+|(?<=[a-z\"`*])(?=(?:n|a|adv|v)\.))"
                     r"((?:(?:" + _POS_ABBR + r")\.(?:\s*(?:[;&,]|or|and)?\s*))+)(.*)$")
_POS_NAMES = [
    (r"v\.\s*t\.\s*&\s*i\.|v\.\s*i\.\s*&\s*t\.", "verb (transitive & intransitive)"),
    (r"v\.\s*t\.", "verb (transitive)"), (r"v\.\s*i\.", "verb (intransitive)"),
    (r"p\.\s*p\.", "past participle"), (r"p\.\s*pr\.", "present participle"), (r"p\.\s*a\.", "participial adjective"),
    (r"imp\.", "past tense"), (r"vb\.\s*n\.", "verbal noun"), (r"n\.\s*pl\.", "plural noun"),
    (r"n\.\s*sing\.", "singular noun"), (r"pl\.", "plural"), (r"superl\.", "superlative"),
    (r"compar\.", "comparative"), (r"adv\.", "adverb"), (r"prep\.", "preposition"), (r"conj\.", "conjunction"),
    (r"interj\.", "interjection"), (r"pron\.", "pronoun"), (r"n\.", "noun"), (r"a\.", "adjective"), (r"v\.", "verb"),
]
_POS_NAMES = [(re.compile(r"^\s*(?:&|and|or|,|;)?\s*" + p), name) for p, name in _POS_NAMES]
_INFL_WORD = re.compile(r"(?<![\w.])([A-Za-z][A-Za-z'\-]*[a-z])(?![\w.])")
_INFL_SKIP = {"or", "and", "obs", "also", "see", "written", "formerly", "sometimes", "colloq", "rarely", "the",
              "of", "in", "a", "an", "r", "pl", "pr", "vb", "imp", "p", "n", "it", "is", "as", "etc"}
_SYLL = re.compile(r'(?<=[A-Za-z])[*"`\'](?=[A-Za-z])|(?<=[A-Za-z])["`]|[*]')
_ABBR_END = re.compile(r"(?:\b(?:Cf|cf|e\.g|i\.e|fr|viz|esp|q\.v|Mr|Dr|St|Gr|L|F|OF|AS|OE|NL|LL|G|It|Sp|Pg|Ar|Heb|Skr|"
                       r"Lat|Ger|Dan|Sw|Icel|D|W|Ir|Gael|Prov|Pr|Mex|Peruv|Chin|Jap|Hind|Per|Turk|imp|pp|p|pl|"
                       r"compar|superl|pref|suff|Fr|Eng|Scot|Am|Brit|Obs|Bot|Zool|Chem|Mus|Law|Arch|Anat|Med|"
                       r"Geom|Math|Astron|Gram|Rhet|Logic|Mil|Naut|Her|Theol|Eccl|Myth|Physics|Zoöl)\.|\b[A-Z]\.|See)$")
_AUTHOR_TAIL = re.compile(r"(?<=[.!?\]])\s+((?:[A-Z][A-Za-z'&]*\.?|[A-Z]\.|St\.)"
                          r"(?:\s+(?:[A-Z][A-Za-z'&]*\.?|[A-Z]\.|&|de|of|von|van|\([^)]{0,20}\))){0,3})$")
_NOT_AUTHOR = {"See", "Cf.", "Cf", "Also", "Hence", "Syn.", "Obs.", "R.", "Colloq.", "Eng.", "U.", "Scot.", "Law."}


def _pos_name(s):
    s = s.strip(" ;,")
    out = []
    while s:
        for rx, name in _POS_NAMES:
            m = rx.match(s)
            if m:
                if name not in out:
                    out.append(name)
                s = s[m.end():].strip(" ;,")
                break
        else:
            out.append(s)
            break
    return " & ".join(out)


def _clean(s):
    s = s.replace("�", "")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,;:.])", r"\1", s)
    s = re.sub(r"\(\s*\)|\[\s*\]", "", s)
    return s.strip()


def _clean_def(s):
    s = _SYLL.sub("", s.replace("�", "")) if re.search(r'[A-Za-z][*`][A-Za-z]', s) else s.replace("�", "")
    s = re.sub(r"\s*\(?[^\s()\[\]]{0,6}\s*Etym:\s*", " ", s)      # 'rotulorum (r Etym: [LL., ...]'
    s = _clean(s).lstrip(",;:. ")
    for _ in range(2):
        m = _AUTHOR_TAIL.search(s)
        if not m or len(m.group(1)) > 28 or m.group(1).split()[0] in _NOT_AUTHOR:
            break
        before = s[:m.start()].rstrip()
        if re.search(r"(?:\b(?:Cf|See|and|or|of|pl|fr|v|n|a)\.?|&)$", before) or len(before) < 3:
            break
        s = before
    if len(s) > MAX_DEF_CHARS:
        cut = s.rfind(". ", 0, MAX_DEF_CHARS)
        s = s[:cut + 1] if cut > 200 else s[:MAX_DEF_CHARS].rstrip() + " …"
    return s


def _take_bracket(text, i):
    """text[i] == '['; return (inside, end index) with nesting; unbalanced -> to the end."""
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
    return text[i + 1:], len(text)


def _def_lines(lines):
    """Definition text from a paragraph: stop where the illustrative quotations start."""
    out = []
    for k, ln in enumerate(lines):
        if ln.startswith(" --") or ln.startswith("Syn."):
            break
        s = ln.strip()
        if out:
            prev = out[-1]
            if prev.endswith((".", "!", "?", "]", ".)", ".]")) and re.match(r"[A-Z0-9\"“'‘\[]", s) \
                    and not _ABBR_END.search(prev):
                break
        out.append(s)
    return " ".join(out)


def _parse_headline(text):
    """'Trav"el, v. i. [imp. & p. p. Traveled or Travelled; ...] Etym: [...]' -> parts."""
    etym = ""
    forms = []
    notes = []
    i = text.find("Etym:")
    rest_after = ""
    if i >= 0:
        j = i + 5
        while j < len(text) and text[j] == " ":
            j += 1
        if j < len(text) and text[j] == "[":
            etym, end = _take_bracket(text, j)
            rest_after = text[end:]
        elif j < len(text) and text[j] == "(":
            k = text.find(")", j)
            k = len(text) if k < 0 else k + 1
            etym, rest_after = text[j + 1:k - 1], text[k:]
        else:
            etym, rest_after = text[j:], ""
        text = text[:i]
    # inflection brackets: [imp. & p. p. Traveled; p. pr. & vb. n. Traveling.]  [pl. Libraries]
    def infl(m):
        body = m.group(1)
        if re.match(r"\s*(imp\.|p\. ?p\.|p\. ?pr\.|pl\.|compar\.|superl\.|pres\.|3d|sing\.|obs\. imp)", body):
            forms.extend(w for w in _INFL_WORD.findall(body) if w.lower() not in _INFL_SKIP)
            return " "
        if re.match(r"\s*(Written|Also|Sometimes|Formerly|Often|Usually|Obs|R\.|Colloq|Archaic|Prov|Local|U\. ?S|"
                    r"Eng|Scot|Cant|Slang|Vulgar|Poetic|Low|Rare|Spelt|Spelled|Contr|Abbrev|Pl\.)", body):
            notes.append(body.strip())
            return " "
        if not etym and len(body) > 2:   # bracket without "Etym:" is still the etymology
            return "\x00" + body + "\x01"
        return " "
    text = re.sub(r"\[([^\[\]]*)\]?", infl, text)
    m = re.search(r"\x00(.*?)\x01", text)
    if m and not etym:
        etym = m.group(1)
    text = re.sub(r"\x00.*?\x01", " ", text)

    def plural(m):
        for w in re.split(r"\s+or\s+", m.group(1)):
            if not w.startswith("-"):
                forms.append(w)
        return " "
    pl_word = r"-?[A-Za-z][A-Za-z\-\*\"`']*[A-Za-z]"
    text = re.sub(r"(?<=[;,.])\s*pl\.\s+(" + pl_word + r"(?:\s+or\s+" + pl_word + r")?)(?=[\s,.;(]|$)(?!\s*Etym)",
                  plural, text)
    text = text.replace("{", " ").replace("}", " ")
    pos = ""
    rest = ""
    m = _POS_RE.match(text.strip())
    if m:
        pron, pos, rest = m.group(1), m.group(2), m.group(3)
    else:
        pron = text
    pron = re.sub(r"\([^)]*\)?", "", pron).replace("�", "")
    pron = re.sub(r"\s+", " ", pron).strip(" ,.;")
    labels = re.findall(r"\(([A-Z][^()]{1,30}\.)\)", rest + " " + rest_after)
    residual = re.sub(r"\([^()]*\)", " ", rest + " " + rest_after)
    residual = re.sub(r"\s+", " ", residual).strip(" ,.;")
    forms = [_SYLL.sub("", f).strip("-'") for f in forms]
    if re.match(r"\s*(Written also|Also written|Sometimes written|Formerly written|Spelt|Spelled)", etym):
        notes.insert(0, etym)
        etym = ""
    pos = _pos_name(pos) if pos else ""
    pos = {"noun & plural": "noun", "obs.": "obsolete", "plural noun & sing.": "plural noun",
           "singular noun & plural": "noun"}.get(pos, pos)
    return pron, pos, _clean(etym), labels, residual, forms, [_clean(x) for x in notes]


def parse_webster(path, progress=None, cancelled=None):
    """Yield (headwords, entry, extra_forms) for every entry in the Gutenberg text."""
    size = os.path.getsize(path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    a = raw.find("*** START OF")
    a = raw.find("\n", a) + 1 if a >= 0 else 0
    b = raw.find("*** END OF")
    body = raw[a:b if b > 0 else len(raw)].replace("\r", "")
    del raw
    lines = body.split("\n")
    n = len(lines)
    # entry starts: an all-caps line after a blank line, followed by text
    starts = [i for i in range(1, n - 1)
              if lines[i] and lines[i - 1] == "" and lines[i + 1].strip() and _HEAD_RE.match(lines[i])
              and not lines[i].isdigit()]
    if lines and lines[0] and _HEAD_RE.match(lines[0]):
        starts.insert(0, 0)
    starts.append(n)
    total = len(starts) - 1
    for si in range(total):
        if si % 2000 == 0:
            if cancelled and cancelled():
                raise _Cancelled()
            if progress:
                progress(si / total)
        s, e = starts[si], starts[si + 1]
        heads = [h.strip() for h in lines[s].split(";") if h.strip()]
        # paragraphs
        paras, cur = [], []
        for ln in lines[s + 1:e]:
            if ln.strip():
                cur.append(ln)
            elif cur:
                paras.append(cur)
                cur = []
        if cur:
            paras.append(cur)
        if not paras:
            continue
        pron, pos, etym, labels, residual, forms, notes = _parse_headline(" ".join(x.strip() for x in paras[0]))
        defs, syn, extra = [], "", []
        if residual and len(residual.split()) >= 3 and not residual.startswith(("See ", "Cf.", "-", "or ")):
            defs.append(_clean_def(residual))
        elif residual.startswith("See ") and len(residual) < 80:
            defs.append(_clean_def(residual + "."))
        pending = ""
        for para in paras[1:]:
            first = para[0]
            # run-on derivatives: " -- O*sir"i*an, a."
            for ln in para:
                m = re.match(r"^ -- ([A-Z][A-Za-z]*[*\"`][A-Za-z*\"`'\-]*), (?:n|a|adv|v)\.", ln)
                if m:
                    extra.append(_SYLL.sub("", m.group(1)).replace('"', "").replace("`", ""))
            if first.startswith(("Note:", "Syn.", " --")):
                if first.startswith("Syn.") and not syn:
                    txt = " ".join(x.strip() for x in para[1:])
                    txt = re.sub(r"^-+\s*", "", txt)
                    if 0 < len(txt) < 220:
                        syn = _clean(txt).rstrip(".")
                continue
            m = re.match(r"^(\d{1,2})\.\s+(.*)$", first)
            if first.startswith("Defn:"):
                text = _def_lines([first[5:]] + para[1:])
            elif m:
                rest = m.group(2)
                if rest.startswith("Etym:"):
                    p2, _, e2, l2, r2, _, _ = _parse_headline(" ".join(x.strip() for x in para))
                    if e2 and not etym:
                        etym = e2
                    elif e2 and e2 not in etym and len(etym) < 600:
                        etym = etym + " | " + e2
                    pending = (" ".join("(" + x + ")" for x in l2) + " ").lstrip()
                    if r2 and len(r2.split()) >= 3:
                        text = r2
                    else:
                        continue
                elif len(para) == 1 and len(rest) < 30 and (re.fullmatch(r"[\w\s.,&()\[\]]*\.\)?\]?", rest)
                                                            or re.fullmatch(r"\([^()]*\)", rest)):
                    pending = rest + " "    # '9. pl.' / '2. (Mus.)' label for the next Defn:
                    continue
                else:
                    text = _def_lines([rest] + para[1:])
            elif re.match(r"^\([a-z]\)\s", first):
                text = _def_lines(para)
            else:
                continue                   # quotations, citations, stray lines
            text = _clean_def(pending + text)
            pending = ""
            if text and len(text) > 2:
                defs.append(text)
        defs = [d for d in dict.fromkeys(defs) if d][:MAX_WEBSTER_DEFS]
        if not defs and not etym:
            continue
        entry = {"pos": pos, "pron": pron, "etym": etym, "defs": defs}
        if labels:
            entry["label"] = " ".join(labels)
        if notes:
            entry["note"] = " ".join(notes)
        if syn:
            entry["synonyms"] = syn
        display = [_SYLL.sub("", p).replace('"', "").replace("`", "").strip() for p in pron.split(",")]
        yield heads, display, entry, forms + extra
    if progress:
        progress(1.0)


# ---------------------------------------------------------------- Open English WordNet parser
_WN_POS = {"n": "noun", "v": "verb", "a": "adjective", "s": "adjective", "r": "adverb",
           "c": "conjunction", "p": "preposition", "x": "other", "u": "other"}


def parse_wordnet(path, progress=None, cancelled=None):
    """Return (entries, synsets, forms). entries: key -> [(lemma, pos, [synset ids], ipa)]."""
    entries = {}
    synsets = {}
    forms = []
    lemma_of_entry = {}
    size = os.path.getsize(path)
    raw = open(path, "rb")
    gz = gzip.GzipFile(fileobj=raw)
    count = 0
    try:
        for event, el in ET.iterparse(gz, events=("end",)):
            tag = el.tag
            if tag == "LexicalEntry":
                lem = el.find("Lemma")
                if lem is not None:
                    word = lem.get("writtenForm") or ""
                    pos = lem.get("partOfSpeech") or ""
                    ipa = ""
                    pr = lem.find("Pronunciation")
                    if pr is not None and pr.text:
                        ipa = pr.text.strip()
                    sids = [s.get("synset") for s in el.findall("Sense")]
                    lemma_of_entry[el.get("id")] = word
                    entries.setdefault(fold(word), []).append((word, pos, sids, ipa))
                    for fm in el.findall("Form"):
                        w = fm.get("writtenForm")
                        if w:
                            forms.append((fold(w), fold(word)))
                el.clear()
                count += 1
            elif tag == "Synset":
                d = el.find("Definition")
                exs = [x.text.strip() for x in el.findall("Example") if x.text][:MAX_WN_EXAMPLES]
                synsets[el.get("id")] = (d.text.strip() if d is not None and d.text else "", exs,
                                         (el.get("members") or "").split(), el.get("partOfSpeech") or "")
                el.clear()
                count += 1
            elif tag in ("Lexicon",):
                el.clear()
            if count % 20000 == 0 and count:
                if cancelled and cancelled():
                    raise _Cancelled()
                if progress:
                    progress(min(0.99, raw.tell() / size))
    finally:
        gz.close()
        raw.close()
    for sid, (d, exs, members, pos) in synsets.items():
        synsets[sid] = (d, exs, [lemma_of_entry.get(m, "") for m in members], pos)
    if progress:
        progress(1.0)
    return entries, synsets, forms


# ---------------------------------------------------------------- building the database
_WORD_CASE = re.compile(r"[A-Za-z]+")
_OF_FORM = re.compile(r"^(?:(?:obs\.|archaic|old)\s+)?(?:imp\.|p\. ?p\.|p\. ?pr\.|pret\.|compar\.|superl\.|pl\.|"
                      r"3d pers\. sing\. pres\.)(?:\s*(?:&|or|and)\s*(?:imp\.|p\. ?p\.|p\. ?pr\.|vb\. n\.|"
                      r"p\. a\.))*,?\s+of\s+([A-Z]?[a-z][a-z\-]+)\.?(?:\s*\[[^\]]*\])?\.?$", re.I)


def build(src_webster, src_wordnet, out_path, progress=None, cancelled=None):
    """Build the SQLite dictionary. progress(fraction, phase_text)."""
    def prog(a, b, text):
        return (lambda x: progress(a + (b - a) * x, text)) if progress else None

    data = {}            # key -> {"word": display, "w": [...], "n": [...], "ipa": ""}
    forms = set()
    freq = {}
    caps = {}            # how often a word is written with a capital inside a sentence (proper names)

    def count(text, own=()):
        for m in _WORD_CASE.finditer(text):
            t = m.group(0)
            lo = t.lower()
            if lo in own:          # a headword used in its own entry says nothing about how common it is
                continue
            freq[lo] = freq.get(lo, 0) + 1
            if t[0].isupper() and m.start() > 0 and text[m.start() - 2:m.start()] not in (". ", "? ", "! ", ": ", ", ", "& ") \
                    and not text[max(0, m.start() - 8):m.start()].endswith(("See ", "Cf. ", "cf. ", "as ", "Same as ", "or ")):
                caps[lo] = caps.get(lo, 0) + 1   # cross-references ('See Staurolite') are always capitalised

    # 1. Webster
    for heads, display, entry, infl in parse_webster(src_webster, prog(0.0, 0.45, "Reading Webster's dictionary"),
                                                     cancelled):
        keys = []
        for i, h in enumerate(heads):
            k = fold(h).strip(" .")
            if not k or k.startswith("-") and len(k) < 2:
                continue
            keys.append(k)
            d = data.get(k)
            if d is None:
                disp = next((x for x in display if fold(x) == k), h.capitalize() if len(h) > 1 else h)
                d = data[k] = {"word": disp, "w": [], "n": []}
            if len(d["w"]) < MAX_WEBSTER_ENTRIES and not any(e["defs"] == entry["defs"] for e in d["w"]):
                d["w"].append(entry)
        if keys:
            for f in infl:
                fk = fold(f)
                if fk and fk not in keys and " " not in fk:
                    # attach to the headword variant it shares most letters with (historically -> historical)
                    best = max(keys, key=lambda k: (len(os.path.commonprefix([k, fk])), -len(k)))
                    forms.add((fk, best))
            # 'Bigger: compar. of Big.'  'Stopped: imp. & p. p. of Stop.'
            for d in entry["defs"][:2]:
                m = _OF_FORM.match(d)
                if m:
                    for k in keys:
                        forms.add((k, fold(m.group(1))))
        own = set(keys)
        for d in entry["defs"]:
            count(d, own)

    # 2. WordNet
    entries, synsets, wn_forms = parse_wordnet(src_wordnet, prog(0.45, 0.85, "Reading Open English WordNet"), cancelled)
    forms.update(wn_forms)
    order = {"n": 0, "v": 1, "a": 2, "s": 2, "r": 3}
    for k, lst in entries.items():
        lst = sorted(lst, key=lambda x: order.get(x[1], 4))
        senses = []
        npos = len({order.get(x[1], 4) for x in lst})
        per_pos_cap = MAX_WN_SENSES if npos == 1 else max(3, MAX_WN_SENSES - 2 * (npos - 1))
        ipa = ""
        display = None
        for word, pos, sids, pr in lst:
            ipa = ipa or pr
            if display is None or (display[:1].isupper() and word[:1].islower()):
                display = word
            n = 0
            for sid in sids:
                if len(senses) >= MAX_WN_SENSES or n >= per_pos_cap:
                    break
                s = synsets.get(sid)
                if not s:
                    continue
                d, exs, members, spos = s
                syn = [m for m in dict.fromkeys(members) if m and fold(m) != k][:MAX_WN_SYNONYMS]
                senses.append({"pos": _WN_POS.get(pos, pos), "def": d, "examples": exs, "synonyms": syn})
                n += 1
                count(d, (k,))
                for x in exs:
                    count(x, (k,))
        d = data.get(k)
        if d is None:
            d = data[k] = {"word": display or k, "w": [], "n": [], "wn": True}
        elif display:
            d["word"], d["wn"] = display, True
        d["n"] = senses
        if ipa:
            d["ipa"] = ipa
    del entries, synsets

    # 3. write
    if progress:
        progress(0.86, "Writing the dictionary")
    if os.path.exists(out_path):
        os.remove(out_path)
    con = sqlite3.connect(out_path)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA page_size=4096")
        con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        con.execute("CREATE TABLE words(key TEXT PRIMARY KEY, word TEXT, freq INTEGER, data BLOB) WITHOUT ROWID")
        con.execute("CREATE TABLE forms(form TEXT, lemma TEXT, PRIMARY KEY(form, lemma)) WITHOUT ROWID")
        rows = []
        keys = sorted(data)
        total = len(keys)
        for i, k in enumerate(keys):
            d = data[k]
            payload = {"w": d["w"], "n": d["n"]}
            if d.get("ipa"):
                payload["ipa"] = d["ipa"]
            if not d.get("wn"):
                # Webster capitalises every headword; keep the capital only for names (Minerva, Saturn)
                w = d["word"]
                if w[:1].isupper() and not w.isupper() and caps.get(k, 0) <= (freq.get(k, 0) - caps.get(k, 0)):
                    d["word"] = w[:1].lower() + w[1:]
            blob = zlib.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
            rows.append((k, d["word"], freq.get(k, 0), blob))
            if len(rows) >= 5000:
                con.executemany("INSERT INTO words VALUES(?,?,?,?)", rows)
                rows = []
                if cancelled and cancelled():
                    raise _Cancelled()
                if progress:
                    progress(0.86 + 0.12 * i / total, "Writing the dictionary")
        con.executemany("INSERT INTO words VALUES(?,?,?,?)", rows)
        con.executemany("INSERT OR IGNORE INTO forms VALUES(?,?)",
                        [(f, l) for f, l in forms if f != l and l in data])
        con.executemany("INSERT INTO meta VALUES(?,?)", [
            ("version", DB_VERSION), ("built", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("sources", json.dumps(ADDON_SOURCES)), ("attribution", ATTRIBUTION)])
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    if progress:
        progress(1.0, "Finished")
    return len(data)


# ---------------------------------------------------------------- add-on download + build
class Installer:
    def __init__(self, base):
        self.base = base
        self.lock = threading.Lock()
        self.state = "idle"        # idle | downloading | verifying | building | done | error | cancelled
        self.done = 0
        self.error = ""
        self.phase = ""
        self.build_progress = 0.0
        self.cancel_flag = False
        self.thread = None
        if installed(base):
            self.state, self.done, self.phase = "done", DOWNLOAD_BYTES, "Installed"

    def status(self):
        return {"state": self.state, "done": self.done, "total": DOWNLOAD_BYTES, "error": self.error,
                "phase": self.phase, "build": round(self.build_progress, 3)}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.state, self.error, self.cancel_flag = "downloading", "", False
            self.phase = "Starting download"
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def cancel(self):
        self.cancel_flag = True

    def _run(self):
        if installed(self.base):
            self.state, self.done, self.phase = "done", DOWNLOAD_BYTES, "Installed"
            return
        d = download_dir(self.base)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            self.state, self.error = "error", f"Can't write to the Flipbook folder ({e.strerror}). Move Flipbook to a folder you can write to, such as Documents."
            self.phase = "Error"
            return
        try:
            finished = 0
            for f in ADDON_SOURCES:
                dest = os.path.join(d, f["name"])
                if os.path.isfile(dest) and os.path.getsize(dest) == f["size"]:
                    # left over from an earlier, interrupted build: re-check it (takes well under a second)
                    self.state, self.phase = "verifying", f"Checking {f['title']}"
                    if _sha256(dest) == f["sha256"]:
                        finished += f["size"]
                        self.done = finished
                        continue
                    os.remove(dest)
                self._fetch(f, dest, finished)
                finished += f["size"]
            self.done = DOWNLOAD_BYTES
            self._build(d)
            self.state, self.phase = "done", "Installed"
        except _Cancelled:
            self.state, self.phase = "cancelled", "Cancelled"
        except Exception as e:  # network errors, checksum mismatch, disk full ...
            self.state, self.error = "error", str(e) or e.__class__.__name__
            self.phase = "Error"

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
        self.done = before + have
        tries = 0
        mb = f["size"] / 1e6
        while have < f["size"]:
            if self.cancel_flag:
                raise _Cancelled()
            self.state = "downloading"
            self.phase = f"Downloading {f['title']} ({have / 1e6:.1f} of {mb:.1f} MB)"
            req = urllib.request.Request(f["url"], headers={"User-Agent": "Flipbook/1.2",
                                                            "Accept-Encoding": "identity"})
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
                        if have + len(chunk) > f["size"]:
                            chunk = chunk[:f["size"] - have]
                        out.write(chunk)
                        h.update(chunk)
                        have += len(chunk)
                        self.done = before + have
                        self.phase = f"Downloading {f['title']} ({have / 1e6:.1f} of {mb:.1f} MB)"
                        if have >= f["size"]:
                            break
                tries = 0
            except _Cancelled:
                raise
            except Exception as e:
                if getattr(e, "code", None) == 416:   # range not satisfiable: our partial file is bad
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                    have, h = 0, hashlib.sha256()
                tries += 1
                if tries > 5:
                    raise RuntimeError(f"The download stopped ({e}). Check the internet connection and try again; it will continue where it left off.")
                time.sleep(2 * tries)
        self.state, self.phase = "verifying", f"Checking {f['title']}"
        if h.hexdigest() != f["sha256"]:
            os.remove(part)
            raise RuntimeError(f"{f['name']} didn't match its checksum, so it was deleted. Please try again "
                               "(if it keeps happening, the source file has changed and Flipbook needs an update).")
        os.replace(part, dest)
        self.state = "downloading"

    def _build(self, d):
        self.state, self.phase, self.build_progress = "building", "Building the dictionary", 0.0
        out = db_path(self.base)
        tmp = out + ".building"

        def progress(x, text):
            self.build_progress = x
            self.phase = f"Building the dictionary: {text} ({int(x * 100)}%)"
        try:
            build(os.path.join(d, ADDON_SOURCES[0]["name"]), os.path.join(d, ADDON_SOURCES[1]["name"]), tmp,
                  progress, lambda: self.cancel_flag)
        except BaseException:
            for _ in range(5):
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                    break
                except OSError:
                    time.sleep(0.2)
            raise
        _close()
        os.replace(tmp, out)
        self.build_progress = 1.0
        shutil.rmtree(d, ignore_errors=True)


class _Cancelled(Exception):
    pass


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- lookups
_con = None
_con_lock = threading.RLock()
_vocab = None            # key -> freq, single plain words only
_buckets = None          # (first letter, length) -> [word] / [(word, letter mask)]
_warming = None          # threading.Event while the background index build runs
_con_path = None


def _close():
    global _con, _vocab, _buckets, _warming
    with _con_lock:
        ev = _warming
        _warming = None
        if ev is not None:
            ev.wait(5)                    # let the warm-up thread close its own connection
        if _con is not None:
            try:
                _con.close()
            except Exception:
                pass
        _con = None
        _vocab = None
        _buckets = None


def unload():
    _close()


def _db(base):
    global _con, _warming, _con_path
    if _con is not None and _con_path != db_path(base):
        _close()
    if _con is None:
        _con_path = db_path(base)
        p = db_path(base)
        if not installed(base):
            return None
        uri = "file:" + urllib.request.pathname2url(os.path.abspath(p)) + "?mode=ro"
        _con = sqlite3.connect(uri, uri=True, check_same_thread=False)
        _con.execute("PRAGMA query_only=1")
        _con.execute("PRAGMA cache_size=-8000")
        # build the spelling-suggestion index in the background so the first typo lookup is quick
        _warming = threading.Event()
        threading.Thread(target=_warm, args=(_con, uri, _warming), daemon=True).start()
    return _con


def _warm(con, uri, ev):
    """Runs without the lookup lock (lookups hold it while waiting for this)."""
    global _vocab, _buckets
    try:
        c2 = sqlite3.connect(uri, uri=True)
        try:
            voc, buckets = _read_vocab(c2)
        finally:
            c2.close()
        if _con is con and _vocab is None:
            _buckets = buckets
            _vocab = voc
    except Exception:
        pass
    finally:
        ev.set()


def preload(base):
    """Optional: open the dictionary and start building the suggestion index now (e.g. at app start)."""
    with _con_lock:
        _db(base)


def _mask(w):
    m = 0
    for ch in w:
        m |= 1 << (ord(ch) - 97)
    return m


def _read_vocab(con):
    voc = {}
    buckets = {}
    for k, fr in con.execute("SELECT key, freq FROM words"):
        if k.isascii() and k.isalpha():
            voc[k] = fr
            b = buckets.get((k[0], len(k)))
            if b is None:
                b = buckets[(k[0], len(k))] = []
            b.append(k)
    return voc, buckets


def _bucket(key):
    """Words of one (first letter, length) bucket with their letter masks (computed on first use)."""
    b = _buckets.get(key)
    if not b:
        return ()
    if isinstance(b[0], str):
        b = _buckets[key] = [(w, _mask(w)) for w in b]
    return b


def _load_vocab(con):
    global _vocab, _buckets
    if _vocab is None:
        ev = _warming
        if ev is not None and ev.wait(5) and _vocab is not None:   # the background warm-up finished it
            return _vocab
        _vocab, _buckets = _read_vocab(con)
    return _vocab


def _get(con, key):
    row = con.execute("SELECT word, freq, data FROM words WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    d = json.loads(zlib.decompress(row[2]).decode("utf-8"))
    d["word"], d["freq"] = row[0], row[1]
    return d


def _exists(con, key):
    return con.execute("SELECT 1 FROM words WHERE key=?", (key,)).fetchone() is not None


_RULES = [  # (suffix, replacement) - Morphy-style detachment rules, all parts of speech
    ("ies", "y"), ("ses", "s"), ("xes", "x"), ("zes", "z"), ("ches", "ch"), ("shes", "sh"), ("men", "man"),
    ("es", "e"), ("es", ""), ("s", ""),
    ("ied", "y"), ("ed", "e"), ("ed", ""), ("ing", "e"), ("ing", ""), ("ying", "ie"),
    ("iest", "y"), ("ier", "y"), ("est", "e"), ("est", ""), ("er", "e"), ("er", ""),
    ("ily", "y"), ("ically", "ic"), ("ally", "al"), ("ly", ""), ("ly", "le"),
    ("ness", ""), ("iness", "y"),
]


def _lemma_candidates(w):
    out = []
    for suf, rep in _RULES:
        if w.endswith(suf) and len(w) - len(suf) >= 2:
            stem = w[:-len(suf)]
            out.append(stem + rep)
            # doubled final consonant: stopped -> stop, bigger -> big, admitted -> admit
            if rep == "" and suf in ("ed", "ing", "er", "est") and len(stem) >= 3 and stem[-1] == stem[-2] \
                    and stem[-1] not in "aeiouslz":
                out.append(stem[:-1])
            if rep == "" and suf in ("ed", "ing", "er", "est") and len(stem) >= 3 and stem[-1] == stem[-2] \
                    and stem[-1] in "lsz":
                out.append(stem[:-1])
    return list(dict.fromkeys(out))


def _lemmatise(con, w):
    rows = con.execute("SELECT lemma FROM forms WHERE form=?", (w,)).fetchall()
    if rows:
        lem = sorted((r[0] for r in rows), key=lambda x: (abs(len(x) - len(w)), x))
        return lem[0]
    for c in _lemma_candidates(w):
        if c != w and _exists(con, c):
            return c
    # two steps for things like 'curiously' -> 'curious', 'effects' handled above
    # two steps: 'carelessnesses' -> 'carelessness' -> 'careless'
    for c in _lemma_candidates(w):
        for c2 in _lemma_candidates(c):
            if c2 != w and len(c2) >= 3 and _exists(con, c2):
                return c2
    return None


# OCR confusions (multi-character): what the scanner printed -> what was meant
_OCR = [("rn", "m"), ("m", "rn"), ("li", "h"), ("h", "li"), ("cl", "d"), ("d", "cl"), ("ii", "u"), ("u", "ii"),
        ("vv", "w"), ("fi", "ff"), ("ff", "fi"), ("fl", "ff"), ("fi", "f"), ("ri", "n"), ("in", "m"), ("nn", "m"),
        ("tl", "d"), ("1", "l"), ("0", "o"), ("5", "s")]
_OCR_SUB = {("b", "h"), ("h", "b"), ("c", "e"), ("e", "c"), ("i", "l"), ("l", "i"), ("t", "f"), ("f", "t"),
            ("n", "u"), ("u", "n"), ("o", "a"), ("a", "o"), ("o", "c"), ("c", "o"), ("s", "e"), ("e", "s"),
            ("i", "f"), ("f", "i"), ("j", "i"), ("i", "j"), ("y", "v"), ("v", "y"), ("h", "k"), ("k", "h")}
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _edits1(w):
    splits = [(w[:i], w[i:]) for i in range(len(w) + 1)]
    out = set()
    for a, b in splits:
        if b:
            out.add(a + b[1:])
            for c in _LETTERS:
                out.add(a + c + b[1:])
        if len(b) > 1:
            out.add(a + b[1] + b[0] + b[2:])
        for c in _LETTERS:
            out.add(a + c + b)
    return out


def _lev(a, b, k):
    """Damerau-Levenshtein (optimal string alignment) with OCR-weighted substitutions; None if > k."""
    la, lb = len(a), len(b)
    if abs(la - lb) > k:
        return None
    prev2 = None
    prev = [j for j in range(lb + 1)]
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ai = a[i - 1]
        best = cur[0]
        for j in range(1, lb + 1):
            bj = b[j - 1]
            if ai == bj:
                cost = 0
            elif (ai, bj) in _OCR_SUB:
                cost = 0.6
            else:
                cost = 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and ai == b[j - 2] and a[i - 2] == bj:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            if v < best:
                best = v
        if best > k:
            return None
        prev2, prev = prev, cur
    return prev[lb] if prev[lb] <= k else None


def _suggest(con, w, exclude=(), limit=6):
    voc = _load_vocab(con)
    w = re.sub(r"[^a-z0-9]", "", w)
    if len(w) < 2:
        return []
    import math
    scores = {}

    def add(c, dist):
        if c in voc and c not in exclude and c != w:
            s = dist - 0.12 * math.log1p(voc[c])
            if c not in scores or s < scores[c]:
                scores[c] = s
    # OCR multi-letter confusions, alone and combined with one ordinary edit
    for a, b in _OCR:
        i = w.find(a)
        while i >= 0:
            v = w[:i] + b + w[i + len(a):]
            add(v, 0.4)
            i = w.find(a, i + 1)
    for c in _edits1(w):
        if c in voc:
            dist = _lev(w, c, 1)
            add(c, dist if dist is not None else 1)
    # distance 2: words with the same first letter and a similar length
    if len(w) >= 4 and len([s for s in scores.values() if s < 0.8]) < limit:
        qm = _mask(w) if w.isalpha() else 0
        for L in range(len(w) - 2, len(w) + 3):
            for c, m in _bucket((w[0], L)):
                if bin(qm ^ m).count("1") > 4:
                    continue
                dist = _lev(w, c, 2)
                if dist is not None:
                    add(c, dist)
    ranked = sorted(scores.items(), key=lambda x: (x[1], x[0]))
    return [c for c, s in ranked[:limit]]


def _thin(d):
    """True when an exact hit says little more than 'inflection of X'."""
    w, n = d.get("w", []), d.get("n", [])
    if not w:
        return True
    defs = [x for e in w for x in e.get("defs", [])]
    if all(_OF_FORM.match(x) or x.startswith("See ") for x in defs):
        return True
    return len(defs) + len(n) <= 2


def _poor(d):
    """A Webster-only hit that is just a variant/obsolete spelling ('Tbe', 'Tho: The. [Obs.]') - often an
    OCR slip for a common word, so suggestions are offered alongside it."""
    defs = [x for e in d.get("w", []) for x in e.get("defs", [])]
    if not defs:
        return True
    for x in defs:
        core = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", x).strip(" .;")
        if not (x.startswith(("See ", "Same as ", "An obsolete", "Obs.")) or _OF_FORM.match(x)
                or len(core) <= 20):
            return False
    return True


def lookup(base, word):
    """Look a word up. Thread-safe; the database is opened on first use."""
    res = {"query": word, "word": None, "lemma_of": None, "webster": [], "wordnet": [], "suggestions": []}
    with _con_lock:
        con = _db(base)
        if con is None:
            res["error"] = "The dictionary add-on is not installed."
            return res
        q = normalize_query(word)
        if not q:
            return res
        tries = [q]
        if "-" in q:
            tries += [q.replace("-", ""), q.replace("-", " ")]
        if " " in q:
            tries.append(q.replace(" ", "-"))
            tries.append(q.replace(" ", ""))
        found = None
        lemma = None
        for t in tries:
            found = _get(con, t)
            if found:
                q = t
                break
        if not found:
            for t in tries:
                if " " in t:
                    continue
                lem = _lemmatise(con, t)
                if lem:
                    found = _get(con, lem)
                    if found:
                        lemma = lem
                        q = t
                        break
        if found and not lemma and q.isalpha() and _thin(found):
            # 'effects' (WordNet: belongings), 'bigger' ('compar. of Big') - add the base word's entry too
            lem = _lemmatise(con, q)
            base_entry = _get(con, lem) if lem and lem != q else None
            if base_entry:
                res["word"] = found["word"]
                res["lemma_of"] = base_entry["word"]
                res["webster"] = (found.get("w", []) + base_entry.get("w", []))[:MAX_WEBSTER_ENTRIES]
                res["wordnet"] = (found.get("n", []) + base_entry.get("n", []))[:MAX_WN_SENSES]
                if found.get("ipa") or base_entry.get("ipa"):
                    res["ipa"] = found.get("ipa") or base_entry.get("ipa")
                return res
        if found:
            res["word"] = found["word"]
            res["lemma_of"] = found["word"] if lemma else None
            res["webster"] = found.get("w", [])
            res["wordnet"] = found.get("n", [])
            if found.get("ipa"):
                res["ipa"] = found["ipa"]
            # a "poor match": an obscure Webster-only word (often an OCR slip) - offer common neighbours too
            if not res["wordnet"] and not lemma and q.isalpha() and _poor(found):
                voc = _load_vocab(con)
                own = found.get("freq", 0)
                cands = _suggest(con, q, exclude={q})
                res["suggestions"] = [c for c in cands if voc.get(c, 0) >= max(20, 20 * own)][:6]
        else:
            key = re.sub(r"[\s\-']", "", tries[0])
            res["suggestions"] = _suggest(con, key)
    return res


def info(base):
    """Source/attribution details for an About box."""
    return {"installed": installed(base), "attribution": ATTRIBUTION,
            "sources": [{k: s[k] for k in ("title", "url", "licence")} for s in ADDON_SOURCES]}


def remove(base):
    _close()
    d = addon_dir(base)
    if not os.path.isdir(d):
        return
    for root, dirs, files in os.walk(d, topdown=False):
        for name in files:
            p = os.path.join(root, name)
            for _ in range(10):
                try:
                    os.remove(p)
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
        os.rmdir(os.path.dirname(d))      # 'addons' only if now empty
    except OSError:
        pass
