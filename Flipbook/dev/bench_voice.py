"""Benchmark / sanity-check the AI voice outside the app.

    python Flipbook\\dev\\bench_voice.py <base-folder-that-contains-addons\\voice> [text]

Prints load time and, for several speeds, seconds of audio vs. seconds to make it
(RTF = time to make / audio length; must stay well below 1.0 for gap-free reading),
plus the playback rate the app would apply, and writes WAVs to dev\\bench_*.wav.

History (i9-11900H, 8 cores): int8 model RTF ~1.0-1.3 (too slow -> dropped),
fp32 RTF ~0.34, fp16 RTF ~0.27 with 8 threads (chosen). Threads above the number of
physical cores made it slower.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import tts  # noqa: E402

base = sys.argv[1]
text = sys.argv[2] if len(sys.argv) > 2 else (
    "The old library to-day is the keeper of what were then known as the "
    "lesser histories. This had a great effect on the town.")
if not tts.installed(base):
    sys.exit(f"AI voice not installed under {tts.addon_dir(base)}")
t = time.time()
v = tts.get_voice(base)
print(f"load {time.time() - t:.2f}s; spoken text: {tts.normalize(text)!r}")
print("phonemes:", v.espeak.phonemize(tts.normalize(text), "en-us"))
for voice, sp in [("af_heart", 1.0), ("bm_george", 1.0), ("af_heart", 1.5), ("af_heart", 2.0), ("af_heart", 0.7)]:
    t = time.time()
    wav, rate = v.synth(text, voice, sp)
    dt = time.time() - t
    secs = (len(wav) - 44) / 2 / tts.SAMPLE_RATE
    with open(os.path.join(HERE, f"bench_{voice}_{sp}.wav"), "wb") as f:
        f.write(wav)
    print(f"{voice:10s} {sp}x: {secs:5.1f}s audio made in {dt:4.2f}s (RTF {dt / secs:.2f}); app plays it at x{rate}")
