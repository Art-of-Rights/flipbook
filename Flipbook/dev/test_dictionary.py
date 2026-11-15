"""End-to-end test of dictionary.py: real downloads, cancel + resume, build, lookups, remove()."""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the Flipbook folder
import dictionary as D

base = os.path.join(os.environ.get("TEMP", "."), "flipbook-dict-test")   # scratch install location
os.makedirs(base, exist_ok=True)
D.remove(base)
assert not D.installed(base)
print("DOWNLOAD_BYTES", D.DOWNLOAD_BYTES)


def wait(inst, until, timeout=900):
    t = time.time()
    while time.time() - t < timeout:
        s = inst.status()
        if until(s):
            return s
        time.sleep(0.05)
    raise SystemExit("timeout " + repr(inst.status()))


# ---- 1. start, cancel mid-way through the first file
inst = D.Installer(base)
print("initial", inst.status())
inst.start()
s = wait(inst, lambda s: s["done"] > 8_000_000 or s["state"] == "error")
print("cancelling at", s)
inst.cancel()
inst.thread.join(30)
s = inst.status()
print("after cancel", s)
assert s["state"] == "cancelled", s
part = os.path.join(D.download_dir(base), D.ADDON_SOURCES[0]["name"] + ".part")
have = os.path.getsize(part)
print("partial file kept:", have, "bytes")

# ---- 2. resume: a new Installer (as after an app restart) must continue, not restart
inst = D.Installer(base)
t0 = time.time()
inst.start()
s = wait(inst, lambda s: s["done"] > 0)
print("resumed, first progress report:", s["done"], "(partial was", have, ")")
assert s["done"] >= have, "download restarted from zero"
min_done = s["done"]
phases = {}
last_state = None
while True:
    s = inst.status()
    if s["state"] != last_state:
        phases[s["state"]] = time.time()
        print(f"  {time.time() - t0:6.1f}s state -> {s['state']}  ({s['phase']})")
        last_state = s["state"]
    if s["state"] in ("done", "error", "cancelled"):
        break
    min_done = min(min_done, s["done"])
    time.sleep(0.05)
t_end = time.time()
print("final", s)
assert s["state"] == "done", s
assert min_done >= have
dl_time = phases.get("building", t_end) - t0
build_time = t_end - phases["building"]
print(f"download+verify (resumed part) {dl_time:.1f}s, build {build_time:.1f}s")
assert D.installed(base)
assert not os.path.exists(D.download_dir(base)), "download folder should be deleted"
size = os.path.getsize(D.db_path(base))
print(f"db size {size / 1e6:.1f} MB ({size} bytes)")
print("files in addon dir:", os.listdir(D.addon_dir(base)))
# a second start on an installed add-on is a no-op
i2 = D.Installer(base)
print("installed Installer status:", i2.status())

# ---- 3. lookups
WORDS = ["candidate", "candidates", "effect", "effects", "custodian", "library", "libraries", "travelled",
         "parliament", "Minerva", "efiect", "tbe", "studies", "Town", "fiat"]
D.preload(base)
time.sleep(1.0)
times = []
for w in WORDS:
    t = time.perf_counter()
    r = D.lookup(base, w)
    ms = (time.perf_counter() - t) * 1000
    times.append(ms)
    print(f"\n=== {w!r}  {ms:.1f} ms  word={r['word']!r} lemma_of={r['lemma_of']!r} ipa={r.get('ipa')!r}")
    for e in r["webster"][:2]:
        print(f"  W [{e['pos']}] {e['pron']}" + (f"  ({e['label']})" if e.get("label") else ""))
        if e["etym"]:
            print(f"     etym: {e['etym']}")
        for d in e["defs"][:3]:
            print(f"     - {d[:200]}")
    for n in r["wordnet"][:3]:
        print(f"  N [{n['pos']}] {n['def']}" + (f"  e.g. {n['examples'][0]!r}" if n["examples"] else "")
              + (f"  syn: {', '.join(n['synonyms'][:5])}" if n["synonyms"] else ""))
    if r["suggestions"]:
        print("  suggestions:", r["suggestions"])
times.sort()
print(f"\nlookup ms: median {times[len(times) // 2]:.2f}, max {times[-1]:.1f}")
r = D.lookup(base, "candidate")
print("ETYM candidate:", r["webster"][0]["etym"])
r = D.lookup(base, "effect")
print("ETYM effect:", r["webster"][0]["etym"])
assert D.lookup(base, "travelled")["lemma_of"] == "travel"
assert D.lookup(base, "efiect")["suggestions"][0] == "effect"
assert D.lookup(base, "tbe")["suggestions"][0] == "the"
assert D.lookup(base, "libraries")["word"] == "library"

# typo timing (worst case: long words, distance-2 scan)
for w in ["representatian", "irnpression", "philosophicai", "tlie", "rnodern", "efiectual", "xqzt"]:
    t = time.perf_counter()
    r = D.lookup(base, w)
    print(f"typo {w!r}: {(time.perf_counter() - t) * 1000:.1f} ms -> {r['word'] or r['suggestions']}")

# normalisation
for w in ["“Town’s,”", "ﬁat", "efﬁcient", "repre-sentation", "Towns'", "PREËMINENT", "(candidates)"]:
    r = D.lookup(base, w)
    print(f"norm {w!r} -> word={r['word']!r} lemma_of={r['lemma_of']!r} sugg={r['suggestions']}")

# ---- 4. thread safety
errors = []


def worker():
    try:
        for w in WORDS * 20:
            D.lookup(base, w)
    except Exception as e:
        errors.append(e)


ths = [threading.Thread(target=worker) for _ in range(8)]
t = time.time()
[x.start() for x in ths]
[x.join() for x in ths]
print(f"8 threads x {len(WORDS) * 20} lookups: {time.time() - t:.2f}s, errors={errors}")
assert not errors

# ---- 5. remove while the connection is open
D.lookup(base, "town")
D.remove(base)
print("after remove: installed =", D.installed(base), " addon dir exists =", os.path.exists(D.addon_dir(base)))
assert not os.path.exists(D.addon_dir(base))
r = D.lookup(base, "town")
print("lookup after remove:", r)
print("\nALL TESTS PASSED")

