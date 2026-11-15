"""Test of the legal dictionary add-on (legal.py).

  .venv\\Scripts\\python.exe Flipbook\\dev\\test_legal.py --work <scratch dir> --blacks2 <djvu.txt>
        [--pdf4 <folder of the 4th ed. PDFs>] [--pdf5 <folder of the 5th ed. PDFs>]

Builds into <scratch dir>/addons/legal (never into Flipbook/addons): the 2nd edition from a local copy
of the archive.org text (placed where the installer looks for a finished download, so nothing is
downloaded), and the 4th / 5th from the user's PDFs. Then checks look-ups, inflections, phrases,
suggestions, remove(source), cancel and remove(None), and prints sample entries and timings.
"""
import argparse
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import legal  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def wait(inst, label, cancel_at=None):
    t0 = time.time()
    pcts = []
    last_msg = ""
    while True:
        st = inst.status()
        pcts.append(st["pct"])
        if cancel_at is not None and st["state"] == "building" and st["pct"] >= cancel_at:
            inst.cancel()
            cancel_at = None
        if st["message"] != last_msg and (len(pcts) % 40 == 0 or st["state"] in ("done", "error", "cancelled")):
            last_msg = st["message"]
        if not inst.busy() and st["state"] in ("done", "error", "cancelled", "idle"):
            break
        time.sleep(0.05)
    st = inst.status()
    secs = time.time() - t0
    print(f"  {label}: {st['state']} after {secs:.1f} s - {st['message']}" + (f" - ERROR {st['error']}" if st["error"] else ""))
    return st, secs, pcts


def smooth(pcts):
    """Progress never goes backwards, and has no jump bigger than 30 points."""
    seq = [p for i, p in enumerate(pcts) if i == 0 or p != pcts[i - 1]]
    back = any(b < a for a, b in zip(seq, seq[1:]))
    jump = max((b - a for a, b in zip(seq, seq[1:])), default=0)
    return (not back) and jump <= 30, f"{len(seq)} distinct values, biggest step {jump}"


def build_blacks2(base, djvu):
    d = legal.download_dir(base)
    os.makedirs(d, exist_ok=True)
    shutil.copyfile(djvu, os.path.join(d, legal.SOURCES["blacks2"]["name"]))
    inst = legal.Installer(base)
    st0 = inst.start_download()
    st1 = inst.start_download()                  # a second start while busy only returns the status
    check("blacks2: second start while busy returns the status", st1["state"] in ("downloading", "building")
          and st1["source"] == "blacks2", st1["state"])
    st, secs, pcts = wait(inst, "blacks2 build")
    check("blacks2: built", st["state"] == "done", st["error"] or st["message"])
    ok, det = smooth(pcts)
    check("blacks2: progress advances smoothly", ok, det)
    check("blacks2: the download folder was cleaned up", not os.path.isdir(d))
    return secs


def build_pdf(base, folder, expect):
    files = legal.pdf_files(folder)
    ed = legal.detect_edition(folder)
    check(f"{expect}: detect_edition(folder) = {expect}", ed == expect, f"got {ed}, {len(files)} PDFs")
    inst = legal.Installer(base)
    inst.start_import(folder)
    st, secs, pcts = wait(inst, f"{expect} build")
    check(f"{expect}: built (edition auto-detected)", st["state"] == "done" and st["source"] == expect,
          st["error"] or st["message"])
    ok, det = smooth(pcts)
    check(f"{expect}: progress advances smoothly", ok, det)
    return secs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--blacks2", required=True)
    ap.add_argument("--pdf4")
    ap.add_argument("--pdf5")
    ap.add_argument("--keep", action="store_true", help="reuse sources already built in --work")
    a = ap.parse_args()
    base = os.path.abspath(a.work)
    os.makedirs(base, exist_ok=True)
    print("work folder:", base)
    if not a.keep and os.path.isdir(legal.addon_dir(base)):
        legal.remove(base)
    times = {}
    built0 = {s["id"] for s in legal.info(base)["sources"]}

    print("\n== building")
    if "blacks2" not in built0:
        times["blacks2"] = build_blacks2(base, a.blacks2)
    if a.pdf5 and "blacks5" not in built0:
        times["blacks5"] = build_pdf(base, a.pdf5, "blacks5")
    if a.pdf4 and "blacks4" not in built0:
        times["blacks4"] = build_pdf(base, a.pdf4, "blacks4")
    inf = legal.info(base)
    built = [s["id"] for s in inf["sources"]]
    print("\n== sources")
    for s in inf["sources"]:
        print(f"  {s['id']}: {s['entries']:,} entries  ({s['title']})" +
              (f", built in {times[s['id']]:.1f} s" if s["id"] in times else ""))
    print(f"  database: {inf['bytes'] / 1e6:.1f} MB")
    check("installed()", legal.installed(base))
    check("info lists the built sources newest first", built == [s for s in legal.ORDER if s in built], built)
    for s in inf["sources"]:
        check(f"{s['id']}: at least 10,000 entries", s["entries"] >= 10000, f"{s['entries']:,}")

    print("\n== look-ups that every built source must have")
    words = ["abatement", "abet", "abandon", "fee simple", "a fortiori", "habeas corpus", "estoppel", "tort",
             "mortmain", "seisin"]
    for w in words:
        r = legal.lookup(base, w)
        srcs = {e["src"] for e in r["entries"]}
        missing = [s for s in built if s not in srcs]
        heads = "; ".join(f"{e['src']}:{e['head']}" for e in r["entries"])
        check(f"'{w}' found in every source", r["found"] and not missing,
              ("missing " + ",".join(missing) + " - " if missing else "") + heads)

    print("\n== inflections, phrases, case and accents")
    for w, lemma in (("abatements", "abatement"), ("abetted", "abet"), ("Estoppels", "estoppel"),
                     ("ABATEMENT", "abatement"), ("“Seisin,”", "seisin")):
        r = legal.lookup(base, w)
        srcs = {e["src"] for e in r["entries"]}
        ok = r["found"] and all(s in srcs for s in built) and \
            any(legal._key(e["head"]) == lemma or lemma in legal._head_keys(e["head"]) for e in r["entries"])
        check(f"'{w}' -> {lemma}", ok, "; ".join(f"{e['src']}:{e['head']}" for e in r["entries"]))
    r = legal.lookup(base, "mens rea")
    if any(s in built for s in ("blacks4", "blacks5")):
        check("'mens rea' (phrase) found in the 4th / 5th", r["found"] and
              any(e["src"] in ("blacks4", "blacks5") for e in r["entries"]),
              "; ".join(f"{e['src']}:{e['head']}" for e in r["entries"]))
    else:
        print("  'mens rea' in the 2nd edition:", r["found"], "(not a headword there)")
    r = legal.lookup(base, "res judicata")
    check("'res judicata' (phrase) found", r["found"], "; ".join(f"{e['src']}:{e['head']}" for e in r["entries"]))
    r = legal.lookup(base, "fee simple")
    check("'fee simple' word stays as given", r["word"] == "fee simple")
    r = legal.lookup(base, "abatement")
    rel = [x["head"] for x in r["related"]]
    check("'abatement' has related phrases", any(legal._key(h).startswith("abatement ") for h in rel), "; ".join(rel))
    check("related list is de-duplicated and at most 12",
          len(rel) <= 12 and len({legal._key(h) for h in rel}) == len(rel))
    r = legal.lookup(base, "fee")
    print("  related for 'fee':", "; ".join(f"{x['src']}:{x['head']}" for x in r["related"]))
    print("  entries per source for 'fee':", {s: sum(1 for e in r["entries"] if e["src"] == s) for s in built})
    check("at most 3 entries per source", all(sum(1 for e in r["entries"] if e["src"] == s) <= 3 for s in built))
    check("entries newest edition first",
          [e["src"] for e in r["entries"]] == sorted((e["src"] for e in r["entries"]), key=legal.ORDER.index))

    print("\n== not found / suggestions")
    for w in ("banana", "tomato"):
        r = legal.lookup(base, w)
        check(f"'{w}' not found, no crash", not r["found"] and not r["entries"],
              "suggestions: " + ", ".join(r["suggestions"]))
    for typo, want in (("estoppcl", "estoppel"), ("rnortmain", "mortmain"), ("habeas corpns", "habeas corpus")):
        r = legal.lookup(base, typo)
        check(f"'{typo}' suggests '{want}'", not r["found"] and want in r["suggestions"], ", ".join(r["suggestions"]))
    for w in ("", "   ", "--", "x", "'s", "a" * 300, "fee\nsimple", "re-\nentry"):
        try:
            r = legal.lookup(base, w)
            check(f"odd query {w[:20]!r} handled", isinstance(r["found"], bool) and (w.strip(" -") or not r["found"]),
                  f"found={r['found']}")
        except Exception as e:
            check(f"odd query {w!r} handled", False, repr(e))

    print("\n== sample entries")
    samples = {"blacks2": ["abatement", "seisin", "mortmain"], "blacks4": ["estoppel", "fee simple", "tort"],
               "blacks5": ["habeas corpus", "abet", "a fortiori"]}
    for s in built:
        for w in samples[s]:
            r = legal.lookup(base, w)
            for e in r["entries"]:
                if e["src"] == s:
                    print(f"  --- {s} / {e['head']} ({len(e['text'])} chars)")
                    t = e["text"][:700].replace("\n\n", "\n      ")
                    print("      " + t + (" ..." if len(e["text"]) > 700 else ""))
                    break
    longest = max((len(e["text"]) for w in ("abandonment", "court", "estate") for e in legal.lookup(base, w)["entries"]),
                  default=0)
    check("entries are capped at about 6,000 characters", longest <= legal.MAX_ENTRY_CHARS + 2, f"longest {longest}")

    if len(built) >= 2 and "blacks2" in built:
        print("\n== remove(one source), cancel, rebuild")
        n2 = {s["id"]: s["entries"] for s in legal.info(base)["sources"]}
        legal.remove(base, "blacks2")
        after = {s["id"]: s["entries"] for s in legal.info(base)["sources"]}
        check("remove('blacks2') removes only blacks2", "blacks2" not in after and
              all(after.get(s) == n2[s] for s in n2 if s != "blacks2"), str(after))
        r = legal.lookup(base, "abatement")
        check("look-ups still work from the other sources", r["found"] and all(e["src"] != "blacks2" for e in r["entries"]))
        # cancel mid-build
        d = legal.download_dir(base)
        os.makedirs(d, exist_ok=True)
        shutil.copyfile(a.blacks2, os.path.join(d, legal.SOURCES["blacks2"]["name"]))
        inst = legal.Installer(base)
        inst.start_download()
        st, secs, pcts = wait(inst, "blacks2 build, cancelled at 80%", cancel_at=80)
        check("cancel mid-build -> 'cancelled'", st["state"] == "cancelled", st["state"])
        check("a cancelled build leaves the database unchanged",
              {s["id"]: s["entries"] for s in legal.info(base)["sources"]} == after)
        times["blacks2 (rebuild)"] = build_blacks2(base, a.blacks2)
        check("blacks2 rebuilt after the cancel", "blacks2" in {s["id"] for s in legal.info(base)["sources"]})

    print("\n== import errors")
    inst = legal.Installer(base)
    st = inst.start_import(os.path.join(base, "no such folder"))
    check("start_import of a folder without PDFs -> error", st["state"] == "error", st["error"])
    st = inst.start_import(a.pdf4 or a.pdf5 or base, edition="blacks9")
    check("start_import with an unknown edition -> error", st["state"] == "error", st["error"])

    print("\n== remove(None) on a separate scratch base")
    base2 = os.path.join(base, "remove_test")
    os.makedirs(os.path.join(base2, "addons"), exist_ok=True)
    sentinel = os.path.join(base2, "addons", "keep-me.txt")
    with open(sentinel, "w") as f:
        f.write("not part of the legal add-on")
    d = legal.download_dir(base2)
    os.makedirs(d, exist_ok=True)
    shutil.copyfile(a.blacks2, os.path.join(d, legal.SOURCES["blacks2"]["name"]))
    inst = legal.Installer(base2)
    inst.start_download()
    wait(inst, "blacks2 build (remove test)")
    legal.lookup(base2, "abatement")                 # opens (caches) a connection, as the app would
    legal.remove(base2)
    check("remove(None) deletes addons/legal", not os.path.exists(legal.addon_dir(base2)))
    check("remove(None) leaves everything else", os.path.isfile(sentinel))
    shutil.rmtree(base2, ignore_errors=True)

    print("\n== timings")
    for k, v in times.items():
        print(f"  {k}: {v:.1f} s")
    print(f"  database: {legal.info(base)['bytes'] / 1e6:.1f} MB")
    legal.unload()
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)} of {len(results)} checks passed")
    for n in bad:
        print("  FAILED:", n)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
