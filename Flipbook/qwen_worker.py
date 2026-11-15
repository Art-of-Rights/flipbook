"""Qwen3-TTS worker process. Runs in the add-on's private Python 3.12 (with torch), NOT in Flipbook.exe.

    <addon>\\py\\python.exe -u <addon>\\runtime\\qwen_worker.py <model dir> [<parent pid>]

Protocol (stdin/stdout, binary): every frame is a 4-byte little-endian length + payload.
  worker -> {"ready": true, "load_seconds": x, ...}  or  {"error": "..."} (then exits)
  client -> {"op": "synth", "text", "speaker", "language", "seed", "max_new_tokens"}
  worker -> {"ok": true, "samples": n, "sr": 24000, "seconds": t} + one frame of float32 LE samples
            or {"ok": false, "error": "..."}
  client -> {"op": "quit"}   (closing stdin also ends the worker; so does the parent process dying)
Everything a library prints goes to stderr (the log file), never into the frame stream.
"""
import json
import os
import struct
import sys
import threading
import time
import traceback

MAX_NEW_TOKENS = 1700      # the static KV cache holds 2048 positions (prompt + generated frames)


def _protect_stdout():
    """Keep the real stdout for frames only; send fd 1 (prints, C libraries) to stderr."""
    import msvcrt
    fd = os.dup(1)
    msvcrt.setmode(fd, os.O_BINARY)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(fd, "wb", buffering=0)


OUT = _protect_stdout()
IN = sys.stdin.buffer
_out_lock = threading.Lock()


def send(payload):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode("utf-8")
    with _out_lock:
        OUT.write(struct.pack("<I", len(payload)) + payload)


def recv():
    head = IN.read(4)
    if len(head) < 4:
        return None
    (n,) = struct.unpack("<I", head)
    data = IN.read(n)
    if len(data) < n:
        return None
    return json.loads(data.decode("utf-8"))


def _watch_parent(pid):
    """Exit as soon as Flipbook exits, even while the model is still loading."""
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, pid)        # SYNCHRONIZE
    if not h:
        return
    k32.WaitForSingleObject(h, 0xFFFFFFFF)
    os._exit(0)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def main():
    model_dir = sys.argv[1]
    if len(sys.argv) > 2 and sys.argv[2].isdigit():
        threading.Thread(target=_watch_parent, args=(int(sys.argv[2]),), daemon=True).start()
    t0 = time.time()
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import qwen_shims
        log("stubbed:", qwen_shims.install())
        import numpy as np
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available to PyTorch (no usable NVIDIA GPU or driver).")
        cap = torch.cuda.get_device_capability(0)
        want = os.environ.get("QWEN_DTYPE", "")
        # bfloat16 needs compute 8.0+. float16 is NOT an option: it produces NaN/inf and the
        # sampler hits a device-side assert. float32 works (slower, ~5 GiB). QWEN_DTYPE is for tests.
        dtype = getattr(torch, want) if want else (torch.bfloat16 if cap[0] >= 8 else torch.float32)
        from faster_qwen3_tts import FasterQwen3TTS
        model = FasterQwen3TTS.from_pretrained(model_dir, device="cuda", dtype=dtype,
                                               attn_implementation="sdpa", max_seq_len=2048)
        model.warmup(prefill_len=100)                       # CUDA graph capture
        torch.manual_seed(0)
        model.generate_custom_voice(text="Ready.", speaker="ryan", language="English", max_new_tokens=36)
        speakers = model.model.get_supported_speakers() or []
    except Exception as e:
        traceback.print_exc()
        msg = str(e) or e.__class__.__name__
        if "out of memory" in msg.lower():
            msg = "The graphics card ran out of memory while loading the Qwen voice. Close other programs that use the GPU (games, video editors) and try again."
        send({"error": msg})
        return 1
    load = time.time() - t0
    log(f"ready in {load:.1f}s, dtype={dtype}, gpu={torch.cuda.get_device_name(0)}, "
        f"allocated={torch.cuda.memory_allocated() >> 20} MiB")
    send({"ready": True, "load_seconds": round(load, 2), "dtype": str(dtype).replace("torch.", ""),
          "gpu": torch.cuda.get_device_name(0), "speakers": speakers, "sr": int(model.sample_rate)})

    while True:
        try:
            req = recv()
        except (OSError, ValueError):
            req = None
        if req is None or req.get("op") == "quit":
            return 0
        if req.get("op") != "synth":
            send({"ok": False, "error": f"unknown op {req.get('op')!r}"})
            continue
        try:
            t = time.time()
            torch.manual_seed(int(req.get("seed", 0)))
            wavs, sr = model.generate_custom_voice(
                text=req["text"], speaker=req["speaker"], language=req.get("language", "English"),
                max_new_tokens=max(12, min(int(req.get("max_new_tokens", MAX_NEW_TOKENS)), MAX_NEW_TOKENS)))
            a = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
            dt = time.time() - t
            log(f"synth {len(req['text'])} chars -> {len(a) / sr:.2f}s audio in {dt:.2f}s")
            send({"ok": True, "samples": int(a.size), "sr": int(sr), "seconds": round(dt, 3)})
            send(a.astype("<f4").tobytes())
        except Exception as e:
            traceback.print_exc()
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
            send({"ok": False, "error": f"{e.__class__.__name__}: {e}"})


if __name__ == "__main__":
    code = main()
    sys.stderr.flush()
    os._exit(code or 0)
