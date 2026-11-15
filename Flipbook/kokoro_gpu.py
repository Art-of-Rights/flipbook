"""Kokoro on the graphics card (DirectML) for Flipbook's Read aloud.

How it works
- The AI voice add-on's own model (kokoro-v1.0.fp16.onnx) is tuned for the CPU and cannot run on
  DirectML. The GPU path uses the plain fp32 model from the same pinned release
  (kokoro-v1.0.onnx, the "GPU speed-up" download), converted ONCE on the user's PC into two files:
    enc.onnx  text -> durations, pitch, energy      (runs on the CPU: tiny, and its LSTMs are slow
                                                      and imprecise on DirectML)
    dec.onnx  the audio generator                    (runs on the GPU: the heavy part)
- The conversion keeps the maths identical; it only works around DirectML problems:
    1. 1-D ConvTranspose -> Unsqueeze + 2-D ConvTranspose + Squeeze (DirectML rejects 1-D).
    2. The sine source's phase in exact integer arithmetic (GPU float sums, low-precision Resize
       weights and sin() of 1e5-radian angles changed the voice).
    3. DC/Nyquist imaginary parts set deterministically, the way the CPU computes them (DirectML's
       STFT leaves noise there, which flipped atan2 between +pi and -pi).
    4. A safe atan2 division (0/0 is NaN on the CPU but 0 on DirectML).
  Measured on an RTX 3070 Laptop GPU: mel distance to the original fp32 voice 0.3-0.9 dB (an
  inaudible 0.14 % pitch change alone gives 1.2-2 dB), about 13x faster than real time vs 3.8x.
- The GPU runs in a separate helper process (Flipbook.exe --voice-worker ...). A GPU driver reset
  or crash then only ends the helper; Flipbook falls back to the CPU voice. Requests are
  serialised: two DirectML runs at once made Windows suspend the GPU device in testing.
"""
import ctypes
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid

SAMPLE_RATE = 24000
SRC = {"name": "kokoro-v1.0.onnx", "size": 325532387,
       "sha256": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"}
ENC, DEC = "enc.onnx", "dec.onnx"
CONVERT_VERSION = 1          # bump when the conversion changes (forces a re-convert)
MIN_VRAM_MB = 2048           # dedicated video memory a "capable" GPU must have


def gpu_dir(voice_dir):
    return os.path.join(voice_dir, "gpu")


def converted(voice_dir):
    d = gpu_dir(voice_dir)
    try:
        with open(os.path.join(d, "converted.json"), encoding="utf-8") as f:
            info = json.load(f)
    except Exception:
        return False
    return (info.get("version") == CONVERT_VERSION and
            all(os.path.isfile(os.path.join(d, n)) and os.path.getsize(os.path.join(d, n)) == info.get(n, -1)
                for n in (ENC, DEC)))


# ---------------------------------------------------------------- which GPU (DXGI)
_gpu_cache = None


def detect_gpu():
    """The adapter DirectML's 'high performance' choice uses. Never raises.
    {"ok", "reason", "name", "vram_mb", "driver", "vendor", "provider"}"""
    global _gpu_cache
    if _gpu_cache is not None:
        return dict(_gpu_cache)
    info = {"ok": False, "reason": "", "name": "", "vram_mb": 0, "driver": "", "vendor": "", "provider": False}
    try:
        import onnxruntime as ort
        info["provider"] = "DmlExecutionProvider" in ort.get_available_providers()
    except Exception:
        pass
    try:
        info.update(_dxgi_high_performance())
    except Exception as e:
        info["reason"] = f"Couldn't read the graphics card details ({e.__class__.__name__})."
    if not info["provider"]:
        info["reason"] = "The graphics-card engine (DirectML) isn't available in this copy of Flipbook."
    elif not info["name"]:
        info["reason"] = info["reason"] or "No graphics card was found."
    elif info.get("software"):
        info["reason"] = "Only Windows' software display driver was found (no graphics card)."
    elif info["vram_mb"] < MIN_VRAM_MB:
        info["reason"] = f"{info['name']} has {info['vram_mb']} MB of its own video memory; at least {MIN_VRAM_MB} MB is needed."
    else:
        info["ok"] = True
    info.pop("software", None)
    _gpu_cache = info
    return dict(info)


class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, s):
        g = cls()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(s).bytes_le, 16)
        return g


class _DESC1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint), ("DeviceId", ctypes.c_uint),
                ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t), ("SharedSystemMemory", ctypes.c_size_t),
                ("LuidLow", ctypes.c_uint32), ("LuidHigh", ctypes.c_int32), ("Flags", ctypes.c_uint)]


def _vcall(obj, index, *args):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    types = [ctypes.c_void_p if type(a).__name__ == "CArgObject" else type(a) for a in args]
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(vtbl[index])(obj, *args)


def _dxgi_high_performance():
    dxgi = ctypes.WinDLL("dxgi")
    f1, f6, ad = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    if dxgi.CreateDXGIFactory1(ctypes.byref(_GUID.of("770aae78-f26f-4dba-a829-253c83d1b387")), ctypes.byref(f1)) != 0:
        raise OSError("CreateDXGIFactory1")
    try:
        if _vcall(f1, 0, ctypes.byref(_GUID.of("c1b6694f-ff09-44a9-b03c-77900a0a1d17")), ctypes.byref(f6)) == 0:
            # IDXGIFactory6::EnumAdapterByGpuPreference(0, HIGH_PERFORMANCE)
            hr = _vcall(f6, 29, ctypes.c_uint(0), ctypes.c_int(1),
                        ctypes.byref(_GUID.of("29038f61-3839-4626-91fd-086879011a05")), ctypes.byref(ad))
        else:   # older Windows: the first adapter
            hr = _vcall(f1, 12, ctypes.c_uint(0), ctypes.byref(ad))
        if hr != 0 or not ad:
            return {}
        d = _DESC1()
        _vcall(ad, 10, ctypes.byref(d))
        ver = ctypes.c_int64()
        drv = ""
        if _vcall(ad, 9, ctypes.byref(_GUID.of("54ec77fa-1377-44e6-8c32-88fd5f44c84c")), ctypes.byref(ver)) == 0:
            v = ver.value
            drv = f"{(v >> 48) & 0xffff}.{(v >> 32) & 0xffff}.{(v >> 16) & 0xffff}.{v & 0xffff}"
        vendor = {0x10de: "NVIDIA", 0x1002: "AMD", 0x8086: "Intel", 0x1414: "Microsoft"}.get(d.VendorId, hex(d.VendorId))
        return {"name": d.Description.strip(), "vram_mb": int(d.DedicatedVideoMemory >> 20), "driver": drv,
                "vendor": vendor, "software": bool(d.Flags & 2)}
    finally:
        for o in (ad, f6, f1):
            if o:
                ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
                    ctypes.cast(o, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[2])(o)


# ---------------------------------------------------------------- one-time model conversion
def convert(src_path, voice_dir, progress=None):
    """fp32 model -> gpu/enc.onnx + gpu/dec.onnx. Needs the `onnx` package (bundled in Flipbook.exe)."""
    import numpy as np
    import onnx
    from onnx import helper, numpy_helper, TensorProto, utils

    say = progress or (lambda s: None)
    out = gpu_dir(voice_dir)
    os.makedirs(out, exist_ok=True)
    say("Reading the model…")
    m = onnx.load(src_path)
    g = m.graph
    init = {i.name: i for i in g.initializer}
    say("Adapting it for the graphics card…")
    _ct2d(g, init, np, helper, numpy_helper)
    _exact_phase(g, np, helper, numpy_helper, TensorProto)
    _dc_nyquist(g, np, helper, numpy_helper)
    _safe_atan(g, np, helper, numpy_helper)
    onnx.checker.check_model(m)
    full = os.path.join(out, "full.tmp.onnx")
    onnx.save(m, full)
    del m, g, init
    say("Splitting it into its CPU and GPU parts…")
    cut = ["/encoder/MatMul_1_output_0", "/encoder/Reshape_output_0", "/encoder/Reshape_1_output_0"]
    tmp_e, tmp_d = os.path.join(out, ENC + ".tmp"), os.path.join(out, DEC + ".tmp")
    utils.extract_model(full, tmp_e, ["tokens", "style", "speed"], cut, check_model=False)
    utils.extract_model(full, tmp_d, cut + ["style"], ["audio"], check_model=False)
    os.replace(tmp_e, os.path.join(out, ENC))
    os.replace(tmp_d, os.path.join(out, DEC))
    os.remove(full)
    info = {"version": CONVERT_VERSION, ENC: os.path.getsize(os.path.join(out, ENC)),
            DEC: os.path.getsize(os.path.join(out, DEC))}
    with open(os.path.join(out, "converted.json"), "w", encoding="utf-8") as f:
        json.dump(info, f)
    return info


def _ct2d(g, init, np, helper, numpy_helper):
    new = []
    g.initializer.append(numpy_helper.from_array(np.array([2], dtype=np.int64), "ct2d_axes"))
    for n in g.node:
        w = numpy_helper.to_array(init[n.input[1]]) if n.op_type == "ConvTranspose" and n.input[1] in init else None
        if w is None or w.ndim != 3:
            new.append(n)
            continue
        init[n.input[1]].CopyFrom(numpy_helper.from_array(w[:, :, None, :].copy(), n.input[1]))
        at = {a.name: helper.get_attribute_value(a) for a in n.attribute}
        pads = at.get("pads", [0, 0])
        attrs = dict(kernel_shape=[1, at.get("kernel_shape", [w.shape[2]])[0]], strides=[1, at.get("strides", [1])[0]],
                     dilations=[1, at.get("dilations", [1])[0]], pads=[0, pads[0], 0, pads[1]], group=at.get("group", 1))
        if at.get("output_padding", [0])[0]:
            attrs["output_padding"] = [0, at["output_padding"][0]]
        x4, y4 = n.input[0] + "__4d_" + n.name, n.output[0] + "__4d"
        new.append(helper.make_node("Unsqueeze", [n.input[0], "ct2d_axes"], [x4], name=n.name + "_unsq"))
        new.append(helper.make_node("ConvTranspose", [x4] + list(n.input[1:]), [y4], name=n.name, **attrs))
        new.append(helper.make_node("Squeeze", [y4, "ct2d_axes"], list(n.output), name=n.name + "_sq"))
    del g.node[:]
    g.node.extend(new)


def _exact_phase(g, np, helper, numpy_helper, TensorProto, q_bits=20, scale=300):
    nodes = list(g.node)
    cons = {}
    for n in nodes:
        for i in n.input:
            cons.setdefault(i, []).append(n)
    cs = [n for n in nodes if n.op_type == "CumSum" and "l_sin_gen" in n.name]
    assert len(cs) == 1, "sine generator not found"
    cs = cs[0]
    chain, cur = [cs], cs
    while cur.op_type != "Sin":
        nxt = cons[cur.output[0]]
        assert len(nxt) == 1
        cur = nxt[0]
        chain.append(cur)
    kinds = [n.op_type for n in chain]
    assert kinds == ["CumSum", "Mul", "Mul", "Transpose", "Mul", "Resize", "Transpose", "Sin"], kinds
    sin = chain[-1]
    rad, axis = cs.input[0], cs.input[1]
    p = "xph"
    half = scale // 2
    w1 = ((np.arange(half) + half + 0.5) / scale).astype(np.float32)
    w2 = ((np.arange(half) + 0.5) / scale).astype(np.float32)
    C = lambda name, v, dt: numpy_helper.from_array(np.array(v, dtype=dt), p + name)
    g.initializer.extend([
        C("_q", float(2 ** q_bits), np.float32), C("_one", 1 << q_bits, np.int64), C("_scale", scale, np.int64),
        C("_inv", 1.0 / 2 ** q_bits, np.float32), C("_2pi", 2 * np.pi, np.float32),
        C("_w1", w1, np.float32), C("_w2", w2, np.float32), C("_pads", [0, 0, 1, 0, 0, 1], np.int64),
        C("_s0", [0], np.int64), C("_s1", [1], np.int64), C("_s2", [2], np.int64), C("_em2", [-2], np.int64),
        C("_em1", [-1], np.int64), C("_emax", [2 ** 62], np.int64), C("_ax2", [2], np.int64), C("_ax3", [3], np.int64),
        C("_shape", [0, 0, -1], np.int64)])
    mk, N = helper.make_node, (lambda s: p + s)
    F = TensorProto.FLOAT
    new = [
        mk("Mul", [rad, N("_q")], [N("_rq")], name=N("_rq")),
        mk("Round", [N("_rq")], [N("_rr")], name=N("_rr")),
        mk("Cast", [N("_rr")], [N("_ri")], to=TensorProto.INT64, name=N("_ri")),
        mk("CumSum", [N("_ri"), axis], [N("_ci")], name=N("_ci")),
        mk("Mul", [N("_ci"), N("_scale")], [N("_ui")], name=N("_ui")),
        mk("Transpose", [N("_ui")], [N("_ut")], perm=[0, 2, 1], name=N("_ut")),
        mk("Pad", [N("_ut"), N("_pads")], [N("_Q")], mode="edge", name=N("_pad")),
        mk("Slice", [N("_Q"), N("_s0"), N("_em2"), N("_ax2")], [N("_L")], name=N("_sl")),
        mk("Slice", [N("_Q"), N("_s1"), N("_em1"), N("_ax2")], [N("_M")], name=N("_sm")),
        mk("Slice", [N("_Q"), N("_s2"), N("_emax"), N("_ax2")], [N("_R")], name=N("_sr")),
        mk("Div", [N("_M"), N("_one")], [N("_wh")], name=N("_wh")),
        mk("Mul", [N("_wh"), N("_one")], [N("_base")], name=N("_base")),
        mk("Sub", [N("_L"), N("_base")], [N("_Li")], name=N("_Li")),
        mk("Sub", [N("_M"), N("_base")], [N("_Mi")], name=N("_Mi")),
        mk("Sub", [N("_R"), N("_base")], [N("_Ri")], name=N("_Ri")),
        mk("Cast", [N("_Li")], [N("_Lc")], to=F, name=N("_Lc")),
        mk("Cast", [N("_Mi")], [N("_Mc")], to=F, name=N("_Mc")),
        mk("Cast", [N("_Ri")], [N("_Rc")], to=F, name=N("_Rc")),
        mk("Mul", [N("_Lc"), N("_inv")], [N("_Lf")], name=N("_Lf")),
        mk("Mul", [N("_Mc"), N("_inv")], [N("_Mf")], name=N("_Mf")),
        mk("Mul", [N("_Rc"), N("_inv")], [N("_Rf")], name=N("_Rf")),
        mk("Unsqueeze", [N("_Lf"), N("_ax3")], [N("_L4")], name=N("_L4")),
        mk("Unsqueeze", [N("_Mf"), N("_ax3")], [N("_M4")], name=N("_M4")),
        mk("Unsqueeze", [N("_Rf"), N("_ax3")], [N("_R4")], name=N("_R4")),
        mk("Sub", [N("_M4"), N("_L4")], [N("_d1")], name=N("_d1")),
        mk("Mul", [N("_d1"), N("_w1")], [N("_e1")], name=N("_e1")),
        mk("Add", [N("_L4"), N("_e1")], [N("_o1")], name=N("_o1")),
        mk("Sub", [N("_R4"), N("_M4")], [N("_d2")], name=N("_d2")),
        mk("Mul", [N("_d2"), N("_w2")], [N("_e2")], name=N("_e2")),
        mk("Add", [N("_M4"), N("_e2")], [N("_o2")], name=N("_o2")),
        mk("Concat", [N("_o1"), N("_o2")], [N("_cat")], axis=3, name=N("_cat")),
        mk("Reshape", [N("_cat"), N("_shape")], [N("_cyc")], name=N("_rs")),
        mk("Transpose", [N("_cyc")], [N("_cyt")], perm=[0, 2, 1], name=N("_cyt")),
        mk("Mul", [N("_cyt"), N("_2pi")], [N("_phase")], name=N("_phase")),
    ]
    sin.input[0] = N("_phase")
    dead = {id(n) for n in chain[:-1]}
    rest = [n for n in g.node if id(n) not in dead]
    k = rest.index(sin)
    del g.node[:]
    g.node.extend(rest[:k] + new + rest[k:])
    dead_outs = {o for n in chain[:-1] for o in n.output}
    keep = [v for v in g.value_info if v.name not in dead_outs]
    del g.value_info[:]
    g.value_info.extend(keep)


def _dc_nyquist(g, np, helper, numpy_helper):
    atan = [n for n in g.node if n.op_type == "Atan" and n.name.endswith("generator/Atan")]
    assert len(atan) == 1, "atan2 not found"
    div = [n for n in g.node if atan[0].input[0] in n.output][0]
    assert div.op_type == "Div"
    imag, real = div.input[0], div.input[1]
    stft = [n for n in g.node if n.op_type == "STFT"][0]
    flen = None
    for i in g.initializer:
        if i.name == stft.input[3]:
            flen = int(numpy_helper.to_array(i))
    for n in g.node:
        if flen is None and n.op_type == "Constant" and n.output[0] == stft.input[3]:
            flen = int(numpy_helper.to_array(n.attribute[0].t))
    bins = flen // 2 + 1
    mask = np.ones((bins, 1), dtype=np.float32)
    mask[0] = mask[-1] = 0
    kvec = np.zeros((bins, 1), dtype=np.float32)
    kvec[0] = -1e-7          # DC: imag has the opposite sign of real -> phase +pi when real < 0 (CPU / PyTorch)
    bvec = np.zeros((bins, 1), dtype=np.float32)
    bvec[-1] = -1e-12        # Nyquist: imag slightly negative -> phase -pi when real < 0 (as on the CPU)
    g.initializer.extend([numpy_helper.from_array(mask, "dcny_mask"), numpy_helper.from_array(kvec, "dcny_k"),
                          numpy_helper.from_array(bvec, "dcny_b")])
    out = imag + "_dcny"
    mk = helper.make_node
    new = [mk("Mul", [imag, "dcny_mask"], [out + "_m"], name="dcny_mul"),
           mk("Mul", [real, "dcny_k"], [out + "_k"], name="dcny_kr"),
           mk("Add", [out + "_m", out + "_k"], [out + "_s"], name="dcny_add1"),
           mk("Add", [out + "_s", "dcny_b"], [out], name="dcny_add2")]
    nodes = list(g.node)
    producer = max(k for k, n in enumerate(nodes) if imag in n.output or real in n.output)
    for n in nodes:
        for k, i in enumerate(n.input):
            if i == imag:
                n.input[k] = out
    del g.node[:]
    g.node.extend(nodes[:producer + 1] + new + nodes[producer + 1:])


def _safe_atan(g, np, helper, numpy_helper):
    atan = [n for n in g.node if n.op_type == "Atan" and n.name.endswith("generator/Atan")][0]
    div = [n for n in g.node if atan.input[0] in n.output][0]
    g.initializer.append(numpy_helper.from_array(np.array(1e-12, dtype=np.float32), "safediv_eps"))
    den = div.input[1]
    add = helper.make_node("Add", [den, "safediv_eps"], [den + "_safe"], name="safediv_add")
    div.input[1] = den + "_safe"
    nodes = list(g.node)
    k = nodes.index(div)
    del g.node[:]
    g.node.extend(nodes[:k] + [add] + nodes[k:])


# ---------------------------------------------------------------- framing (parent <-> helper)
def _send(sock, header, payload=b""):
    h = json.dumps(header).encode("utf-8")
    sock.sendall(struct.pack("<II", len(h), len(payload)) + h + payload)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            raise ConnectionError("the graphics-card helper stopped")
        buf += chunk
    return bytes(buf)


def _recv(sock):
    hl, pl = struct.unpack("<II", _recv_exact(sock, 8))
    header = json.loads(_recv_exact(sock, hl).decode("utf-8"))
    return header, (_recv_exact(sock, pl) if pl else b"")


# ---------------------------------------------------------------- the helper process
def worker_main(argv):
    """Flipbook.exe --voice-worker <port> <token> <gpu dir>  (or: python kokoro_gpu.py --worker ...)"""
    port, token, d = int(argv[0]), argv[1], argv[2]
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    sock.settimeout(None)
    try:
        _send(sock, {"hello": token})
        import numpy as np
        import onnxruntime as ort
        t0 = time.time()
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(2, min(8, (os.cpu_count() or 4) // 2))
        enc = ort.InferenceSession(os.path.join(d, ENC), so, providers=["CPUExecutionProvider"])
        sg = ort.SessionOptions()
        sg.enable_mem_pattern = False                     # required by DirectML
        sg.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        dec = ort.InferenceSession(os.path.join(d, DEC), sg,
                                   providers=[("DmlExecutionProvider", {"performance_preference": "high_performance"}),
                                              "CPUExecutionProvider"])
        if "DmlExecutionProvider" not in dec.get_providers():
            raise RuntimeError("DirectML could not use the graphics card.")
        cut = [o.name for o in enc.get_outputs()]
        state = {"cpu_dec": None}
        _send(sock, {"ready": True, "load_s": round(time.time() - t0, 2)})
    except Exception as e:
        try:
            _send(sock, {"ready": False, "error": f"{e.__class__.__name__}: {e}"})
        finally:
            sock.close()
        return 1

    def encode(tokens, style, speed):
        return enc.run(None, {"tokens": np.array([[0, *tokens, 0]], dtype=np.int64),
                              "style": style.reshape(1, -1).astype(np.float32),
                              "speed": np.array([speed], dtype=np.float32)})

    def decode(sess, e, style):
        return sess.run(None, dict(zip(cut, e), style=style.reshape(1, -1).astype(np.float32)))[0].ravel().astype(np.float32)

    while True:
        try:
            h, payload = _recv(sock)
        except Exception:
            return 0                                    # parent closed: quit
        op = h.get("op")
        if op == "quit":
            return 0
        try:
            if op == "synth":
                style = np.frombuffer(payload, dtype=np.float32)
                audio = decode(dec, encode(h["tokens"], style, float(h["speed"])), style)
                if not np.isfinite(audio).all():
                    raise RuntimeError("the graphics card returned invalid audio")
                _send(sock, {"ok": True, "samples": int(audio.size)}, audio.tobytes())
            elif op == "probe":
                styles = np.frombuffer(payload, dtype=np.float32).reshape(len(h["cases"]), -1)
                if state["cpu_dec"] is None:
                    state["cpu_dec"] = ort.InferenceSession(os.path.join(d, DEC), so, providers=["CPUExecutionProvider"])
                res = []
                for case, style in zip(h["cases"], styles):
                    t0 = time.time(); e = encode(case["tokens"], style, 1.0); te = time.time() - t0
                    decode(dec, e, style)                    # first run of a new length compiles; not timed
                    t1 = time.time(); ag = decode(dec, e, style); tg = time.time() - t1 + te
                    t2 = time.time(); ac = decode(state["cpu_dec"], e, style); tc = time.time() - t2
                    res.append({"audio_s": round(ac.size / SAMPLE_RATE, 3), "gpu_s": round(tg, 4), "cpu_s": round(tc, 4),
                                "finite": bool(np.isfinite(ag).all()), "mel_db": round(_mel_l1(ac, ag, np), 3)})
                state["cpu_dec"] = None                      # free the memory again
                _send(sock, {"ok": True, "results": res})
            else:
                _send(sock, {"ok": False, "error": f"unknown op {op}"})
        except Exception as e:
            _send(sock, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})


def _mel_l1(a, b, np, sr=SAMPLE_RATE, n=1024, hop=240, n_mels=80):
    """Mean absolute log-mel difference (dB) over the non-silent frames: ~0 = identical."""
    m = min(len(a), len(b))
    if m < n * 2:
        return 0.0 if np.allclose(a[:m], b[:m], atol=1e-3) else 99.0
    a, b = a[:m].astype(np.float64), b[:m].astype(np.float64)
    f = np.linspace(0, sr / 2, n // 2 + 1)
    mel = lambda x: 2595 * np.log10(1 + x / 700)
    imel = lambda x: 700 * (10 ** (x / 2595) - 1)
    pts = imel(np.linspace(mel(40), mel(sr / 2 - 200), n_mels + 2))
    fb = np.array([np.clip(np.minimum((f - pts[i]) / (pts[i + 1] - pts[i]), (pts[i + 2] - f) / (pts[i + 2] - pts[i + 1])), 0, None)
                   for i in range(n_mels)])
    w = np.hanning(n)
    def lm(x):
        fr = np.array([x[i:i + n] * w for i in range(0, len(x) - n, hop)])
        return 10 * np.log10(fb @ (np.abs(np.fft.rfft(fr, axis=1)) ** 2).T + 1e-8).T
    A, B = lm(a), lm(b)
    loud = A.max(axis=1) > A.max() - 50
    return float(np.abs(A - B)[loud].mean())


# ---------------------------------------------------------------- the client used by tts.py
class GpuError(RuntimeError):
    pass


class Engine:
    """Starts and talks to the helper process; one request at a time."""

    def __init__(self, voice_dir):
        self.dir = gpu_dir(voice_dir)
        self.lock = threading.Lock()
        self.proc = self.sock = None
        self.load_s = 0.0

    def _command(self, port, token):
        if getattr(sys, "frozen", False):
            return [sys.executable, "--voice-worker", str(port), token, self.dir]
        return [sys.executable, os.path.abspath(__file__), "--worker", str(port), token, self.dir]

    def _start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(90)
        token = uuid.uuid4().hex
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(self._command(srv.getsockname()[1], token), creationflags=flags,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while True:
                conn, _ = srv.accept()
                conn.settimeout(90)
                try:
                    h, _ = _recv(conn)
                except Exception:
                    conn.close()
                    continue
                if h.get("hello") == token:
                    break
                conn.close()
            h, _ = _recv(conn)                     # ready (models loaded) or error
            if not h.get("ready"):
                conn.close()
                raise GpuError(h.get("error") or "the graphics-card helper could not start")
            self.sock, self.load_s = conn, h.get("load_s", 0.0)
        except socket.timeout:
            raise GpuError("the graphics-card helper did not start in time")
        except GpuError:
            raise
        except (ConnectionError, OSError):
            raise GpuError("the graphics-card helper stopped while starting")
        except Exception as e:
            raise GpuError(f"the graphics-card helper could not start ({e.__class__.__name__})")
        finally:
            srv.close()
            if self.sock is None:
                self._kill()

    def _kill(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def running(self):
        return self.sock is not None and self.proc is not None and self.proc.poll() is None

    def _call(self, header, payload=b"", timeout=60):
        if not self.running():
            self._kill()
            self._start()
        try:
            self.sock.settimeout(timeout)
            _send(self.sock, header, payload)
            h, data = _recv(self.sock)
        except Exception:
            self._kill()
            raise GpuError("the graphics-card helper stopped unexpectedly")
        if not h.get("ok"):
            raise GpuError(h.get("error") or "the graphics card failed")
        return h, data

    def start(self):
        with self.lock:
            if not self.running():
                self._kill()
                self._start()

    def synth(self, tokens, style, speed):
        import numpy as np
        with self.lock:
            _, data = self._call({"op": "synth", "tokens": [int(t) for t in tokens], "speed": float(speed)},
                                 np.asarray(style, dtype=np.float32).tobytes(), timeout=30 + len(tokens) * 0.2)
        return np.frombuffer(data, dtype=np.float32).copy()

    def probe(self, cases):
        import numpy as np
        styles = np.stack([np.asarray(c["style"], dtype=np.float32).ravel() for c in cases])
        with self.lock:
            h, _ = self._call({"op": "probe", "cases": [{"tokens": [int(t) for t in c["tokens"]]} for c in cases]},
                              styles.tobytes(), timeout=180)
        return h["results"]

    def stop(self):
        with self.lock:
            if self.sock:
                try:
                    _send(self.sock, {"op": "quit"})
                except Exception:
                    pass
            time.sleep(0.1)
            self._kill()


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--worker":
    sys.exit(worker_main(sys.argv[2:]) or 0)
