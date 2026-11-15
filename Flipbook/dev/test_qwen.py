"""End-to-end test of the Qwen3-TTS add-on (qwen.py + qwen_worker.py) against a scratch base folder.

    python Flipbook\\dev\\test_qwen.py <scratch base> [--mirror "dir1;dir2"] [--reuse] [--keep]

  <scratch base>  files land in <base>\\addons\\qwen  (NEVER the real Flipbook folder)
  --mirror        TEST ONLY: serve downloads from local copies (sets FLIPBOOK_QWEN_MIRROR);
                  without it the real pinned URLs are downloaded (~5.4 GB)
  --reuse         skip the installer if the add-on is already installed in <base>
  --keep          don't remove the add-on at the end

Checks: gpu_check; install with two cancel+resume cycles (in PyTorch and in the model); installed();
engine start; 3 sentences x each speaker (durations, RTF, sanity); same text -> same audio;
Stale while waiting; two threads at once (must equal the serial results); killing the worker
mid-sentence (that call fails, the next one recovers); float32 fallback; stop(); remove().
"""
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import numpy as np  # noqa: E402

args = [a for a in sys.argv[1:]]
if not args or args[0].startswith("--"):
    sys.exit(__doc__)
BASE = os.path.abspath(args[0])
if "--mirror" in args:
    os.environ["FLIPBOOK_QWEN_MIRROR"] = args[args.index("--mirror") + 1]
REUSE, KEEP = "--reuse" in args, "--keep" in args
if os.path.normcase(os.path.abspath(HERE)) in os.path.normcase(BASE) or "Flipbook 1.3" in BASE:
    sys.exit("refusing: use a scratch base folder, not the Flipbook folder")

import qwen  # noqa: E402

FAIL = []


def check(cond, what):
    print(("  PASS " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        FAIL.append(what)


def smi_used():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        return int(r.stdout.split()[0])
    except Exception:
        return -1


def dir_size(d):
    return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(d) for f in fs)


def sane(a, text, label):
    dur = a.size / qwen.SAMPLE_RATE
    expect = len(text) / qwen.CHARS_PER_SEC
    rms = float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    ok = (a.dtype == np.float32 and a.ndim == 1 and np.isfinite(a).all() and 0.01 < rms < 0.5
          and peak <= 1.0 and 0.35 * expect <= dur <= 2.5 * max(expect, 1.2))
    check(ok, f"{label}: {dur:.2f}s audio for {len(text)} chars ({len(text) / max(dur, 1e-3):.1f} c/s), "
              f"rms {rms:.3f}, peak {peak:.2f}")
    return dur


SENTS = [
    "The old books of the library were not written for the idle reader.",
    "He closed the heavy volume, rose from his chair, and walked slowly toward the window overlooking the quiet courtyard.",
    "When the translator came to the final chapter, he discovered that several pages had been lost, and the meaning of the closing verses could only be guessed at from the commentaries of later scholars.",
]

# ------------------------------------------------------------------ 1. GPU
print("== gpu_check")
g = qwen.gpu_check()
print("  ", g)
check(g["ok"], "this GPU qualifies")
print(f"   DOWNLOAD_BYTES={qwen.DOWNLOAD_BYTES:,}  DISK_BYTES={qwen.DISK_BYTES:,}")

# ------------------------------------------------------------------ 2. install
d = qwen.addon_dir(BASE)
if REUSE and qwen.installed(BASE):
    print("== install skipped (--reuse)")
else:
    print("== install", "(mirror)" if os.environ.get("FLIPBOOK_QWEN_MIRROR") else "(real URLs)")
    inst = qwen.Installer(BASE)

    def run_until(stop_when, label):
        inst.start()
        t0 = last = time.time()
        while True:
            s = inst.status()
            if time.time() - last > 15:
                print(f"   {time.time() - t0:6.0f}s {s['state']:11} {s['done'] / 1e9:5.2f}/{s['total'] / 1e9:.2f} GB  {s['phase']}", flush=True)
                last = time.time()
            if stop_when and stop_when(s):
                t = time.time()
                inst.cancel()
                while inst.status()["state"] not in ("cancelled", "error", "done"):
                    time.sleep(0.1)
                print(f"   cancelled during '{s['phase']}' at {s['done'] / 1e9:.2f} GB, stopped in {time.time() - t:.2f}s")
                return inst.status()
            if s["state"] in ("done", "error", "cancelled"):
                print(f"   {label}: {s['state']} after {time.time() - t0:.0f}s {s['error']}")
                return s
            time.sleep(0.2)

    t_inst = time.time()
    s = run_until(lambda s: "PyTorch" in s["phase"] and s["done"] > 300e6, "first leg")
    check(s["state"] == "cancelled", "cancel during the PyTorch download")
    before = s["done"]
    s = run_until(lambda s: "model" in s["phase"] and s["done"] > 3.2e9, "second leg")
    check(s["state"] == "cancelled", "cancel during the model download")
    check(s["done"] > before, "download resumed instead of restarting")
    seen = {}

    def in_pip(s):                     # cancel 20 s into the pip run
        if "Installing PyTorch" in s["phase"]:
            seen.setdefault("t", time.time())
            seen["p"] = inst.proc
            return time.time() - seen["t"] > 20
        return False
    s = run_until(in_pip, "third leg")
    check(s["state"] == "cancelled", "cancel while pip is installing")
    p = seen.get("p")
    check(p is not None and p.poll() is not None, "the running pip process was killed")
    s = run_until(None, "final leg")
    check(s["state"] == "done", f"install finished ({time.time() - t_inst:.0f}s total)")
    if s["state"] != "done":
        print("   install.log tail:", inst._log_tail("install.log"))
        sys.exit(1)

check(qwen.installed(BASE), "installed() is True")
check(not os.path.exists(os.path.join(d, "w")) and not os.path.exists(os.path.join(d, "t")),
      "downloads and temp files were cleaned up")
print(f"   add-on size on disk: {dir_size(d):,} bytes")

# ------------------------------------------------------------------ 3. engine
print("== engine")
base_vram = smi_used()
eng = qwen.get_engine(BASE)
check(qwen.get_engine(BASE) is eng, "get_engine is a singleton")
t0 = time.time()
eng.start()
while eng.state()["state"] == "starting":
    time.sleep(0.2)
st = eng.state()
print(f"   {st}  (wall {time.time() - t0:.1f}s, worker says {eng.info.get('dtype')} on {eng.info.get('gpu')})")
check(st["state"] == "ready", "worker started")
if st["state"] != "ready":
    sys.exit(1)
print(f"   VRAM used by the worker (nvidia-smi delta): {smi_used() - base_vram} MiB")

print("== speech (3 sentences x speakers)")
serial = {}
tot_a = tot_w = 0.0
for spk, label in qwen.SPEAKERS:
    for i, text in enumerate(SENTS):
        t = time.time()
        a = eng.synth(text, spk)
        w = time.time() - t
        dur = sane(a, text, f"{label} #{i}")
        print(f"         wall {w:.2f}s  RTF {dur / w:.2f}")
        tot_a += dur
        tot_w += w
        serial[(spk, i)] = a
print(f"   total: {tot_a:.1f}s audio in {tot_w:.1f}s, RTF {tot_a / tot_w:.2f}")
try:
    import wave
    out = os.path.join(BASE, "qwen_test_out")
    os.makedirs(out, exist_ok=True)
    for (spk, i), a in serial.items():
        with wave.open(os.path.join(out, f"{spk}_{i}.wav"), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(qwen.SAMPLE_RATE)
            wf.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())
    print("   wrote", out)
except Exception as e:
    print("   (wav write failed)", e)

a2 = eng.synth(SENTS[0], "ryan")
check(np.array_equal(a2, serial[("ryan", 0)]), "same text + speaker -> identical audio (seeded by text)")
check(eng.synth("  ...  ", "aiden").size == 0, "punctuation-only text -> empty audio, no model call")
long_text = " ".join(SENTS * 4)          # > 800 chars: split into pieces
t = time.time()
a = eng.synth(long_text, "aiden")
sane(a, long_text, f"long text ({len(long_text)} chars, split in {len(qwen._split(long_text))})")
print(f"         wall {time.time() - t:.1f}s")

print("== Stale while waiting for the lock")
res = {}
busy = threading.Thread(target=lambda: res.setdefault("busy", eng.synth(SENTS[2], "aiden")))
busy.start()
time.sleep(0.3)
flag = {"v": False}
threading.Timer(0.5, lambda: flag.__setitem__("v", True)).start()
t = time.time()
try:
    eng.synth(SENTS[1], "ryan", cancelled=lambda: flag["v"])
    stale = False
except qwen.Stale:
    stale = True
check(stale and time.time() - t < 1.5, f"cancelled request raised Stale after {time.time() - t:.2f}s")
busy.join()
check(np.array_equal(res["busy"], serial[("aiden", 2)]), "the running request was unaffected")

print("== two threads at once")
res.clear()
ths = [threading.Thread(target=lambda k=k: res.__setitem__(k, eng.synth(SENTS[k], ("ryan", "aiden")[k % 2])))
       for k in (1, 2)]
t = time.time()
for th in ths:
    th.start()
for th in ths:
    th.join()
print(f"   both done in {time.time() - t:.1f}s")
check(np.array_equal(res[1], serial[("aiden", 1)]) and
      np.array_equal(res[2], serial[("ryan", 2)]), "concurrent results equal the serial ones (no corruption)")

print("== worker killed mid-sentence")
err = {}


def victim():
    try:
        eng.synth(SENTS[2], "ryan")
        err["e"] = None
    except Exception as e:
        err["e"] = e


th = threading.Thread(target=victim)
th.start()
time.sleep(1.0)
eng.proc.kill()
th.join(30)
check(isinstance(err.get("e"), RuntimeError), f"in-flight call raised RuntimeError: {err.get('e')}")
print("   state after kill:", eng.state())
t = time.time()
a = eng.synth(SENTS[0], "ryan")
check(np.array_equal(a, serial[("ryan", 0)]), f"next call restarted the worker and worked ({time.time() - t:.1f}s)")

print("== float32 fallback (what the worker uses below compute 8.0)")
eng.stop()
os.environ["QWEN_DTYPE"] = "float32"
try:
    t = time.time()
    a = eng.synth(SENTS[1], "aiden")
    check(eng.info.get("dtype") == "float32", f"worker ran in float32 (start+synth {time.time() - t:.1f}s)")
    sane(a, SENTS[1], "float32 sentence")
except Exception as e:
    check(False, f"float32 synth: {e}")
os.environ.pop("QWEN_DTYPE", None)

print("== stop")
pid = eng.proc.pid if eng.proc else None
eng.stop()
time.sleep(1.5)
gone = pid is None or subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True,
                                     text=True).stdout.find(str(pid)) < 0
check(eng.state()["state"] == "stopped" and gone, "stop() ended the worker")
print(f"   VRAM after stop vs before start: {smi_used() - base_vram:+} MiB")

if not KEEP:
    print("== remove")
    qwen.remove(BASE)
    check(not os.path.exists(d), "remove() deleted addons\\qwen")
    check(not qwen.installed(BASE), "installed() is False after remove")

print("\nRESULT:", "ALL PASSED" if not FAIL else f"{len(FAIL)} FAILED: {FAIL}")
sys.exit(1 if FAIL else 0)
