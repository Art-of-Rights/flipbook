"""Test the Kokoro graphics-card path (kokoro_gpu.py) end to end.

    python Flipbook\\dev\\test_gpu.py --voice-dir <dir with voices-v1.0.bin> --src <kokoro-v1.0.onnx> --work <scratch dir>

Copies voices-v1.0.bin into <work>\\addons\\voice, converts the fp32 model into <work>\\addons\\voice\\gpu,
then: GPU detection, helper start, synthesis of new sentence lengths (speed, finite audio), the
self-test probe (GPU vs CPU decoder), killing the helper mid-way (the next call must recover).
Never point --work at the real Flipbook folders.
"""
import argparse
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import numpy as np        # noqa: E402
import kokoro_gpu         # noqa: E402
import tts                # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--voice-dir", required=True)
ap.add_argument("--src", required=True)
ap.add_argument("--work", required=True)
a = ap.parse_args()
assert "Flipbook 1." not in os.path.abspath(a.work) and not os.path.abspath(a.work).endswith(os.path.join("Flipbook", "addons")), "use a scratch dir"
vdir = tts.addon_dir(a.work)
os.makedirs(vdir, exist_ok=True)
if not os.path.isfile(os.path.join(vdir, "voices-v1.0.bin")):
    shutil.copy(os.path.join(a.voice_dir, "voices-v1.0.bin"), vdir)
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg)
    if not ok:
        fails.append(msg)

print("GPU:", kokoro_gpu.detect_gpu())
if not kokoro_gpu.converted(vdir):
    t = time.time()
    info = kokoro_gpu.convert(a.src, vdir, progress=lambda s: print("  ", s))
    print(f"converted in {time.time() - t:.1f}s", info)
check(kokoro_gpu.converted(vdir), "converted() after conversion")

voices = np.load(os.path.join(vdir, "voices-v1.0.bin"))
es = tts._Espeak()
def case(text, v="af_heart"):
    ph = es.phonemize(tts.normalize(text), "en-gb" if v[0] == "b" else "en-us")
    ids = [tts.VOCAB[c] for c in ph if c in tts.VOCAB]
    return ids, voices[v][len(ids) - 1]

eng = kokoro_gpu.Engine(vdir)
t = time.time(); eng.start(); print(f"helper started in {time.time() - t:.1f}s (models {eng.load_s}s)")
words = "the old harbour was guarded by the keepers of the light who taught the young sailors in quiet rooms beside the water".split()
tot_a = tot_c = 0
for n in (4, 9, 15, 21, 12, 7):
    ids, st = case(" ".join(words[:n]).capitalize() + ".")
    t = time.time(); y = eng.synth(ids, st, 1.0); c = time.time() - t
    tot_a += y.size / 24000; tot_c += c
    check(np.isfinite(y).all() and y.size > 12000 and 0.01 < float(np.sqrt((y ** 2).mean())) < 0.5,
          f"synth {n:2d} words: {y.size / 24000:.2f}s audio in {c * 1000:.0f} ms")
print(f"overall {tot_a / tot_c:.1f}x real time")
res = eng.probe([{"tokens": ids, "style": st} for ids, st in (case("The effect of these old customs was profound."),
                                                              case("In the old harbour town, the ships were weighed against the tide of the morning.", "bm_george"))])
print("probe:", res)
check(all(r["finite"] and r["mel_db"] < 1.0 for r in res), "probe: GPU audio matches the CPU decoder (mel < 1 dB)")
check(sum(r["cpu_s"] for r in res) / max(1e-6, sum(r["gpu_s"] for r in res)) > 1.5, "probe: GPU decoder at least 1.5x faster")

# kill the helper: the call fails cleanly, the next one restarts it
eng.proc.kill(); time.sleep(0.5)
ids, st = case("After a crash the helper must come back.")
try:
    y = eng.synth(ids, st, 1.0)
    check(y.size > 0, "synth after the helper was killed (restarted transparently)")
except kokoro_gpu.GpuError as e:
    print("  first call after kill raised GpuError:", e)
    y = eng.synth(ids, st, 1.0)
    check(y.size > 0, "second call after kill recovers")
eng.stop()
check(not eng.running(), "stop() ends the helper")
print("FAILED:" if fails else "ALL PASSED", fails)
sys.exit(1 if fails else 0)
