"""Import shims for the Qwen3-TTS worker. Runs in the add-on's private Python 3.12, NOT in Flipbook.exe.

qwen_tts (the qwen-tts-hf package) imports sox, onnxruntime, torchaudio and librosa when it is
imported (for its 25 Hz tokenizer and voice cloning), but the 12 Hz CustomVoice model that Flipbook
uses never calls them. Leaving them out saves ~400 MB and a sox source build that fails on plain
Windows. install() imports qwen_tts with small stand-ins for whichever of them are missing.
"""
import importlib.machinery
import importlib.util
import sys
import types


def _missing(name):
    try:
        return importlib.util.find_spec(name) is None
    except (ImportError, ValueError):
        return True


def _unavailable(what):
    def f(*a, **k):
        raise NotImplementedError(f"{what} is not available in Flipbook's Qwen voice runtime "
                                  "(only the 12 Hz CustomVoice model is supported)")
    return f


def _mod(name):
    # A real (origin-less) ModuleSpec is required: torch._dynamo calls importlib.util.find_spec()
    # on names such as "onnxruntime" and raises "onnxruntime.__spec__ is None" for a bare ModuleType.
    m = types.ModuleType(name)
    m.__spec__ = importlib.machinery.ModuleSpec(name, None)
    m.__file__ = None
    return m


def install():
    """Import qwen_tts with temporary stand-ins. Returns the names that were stubbed.

    1. Ask transformers whether librosa/torchaudio/onnxruntime exist BEFORE stubbing, so its cached
       is_*_available() answers stay False (otherwise transformers.audio_utils sees the stand-in
       librosa and tries `import soxr`).
    2. Put the stand-ins in sys.modules and import qwen_tts (its modules keep their references).
    3. Take the stand-ins out again so nothing else mistakes them for real packages.
    """
    from transformers.utils import import_utils as iu
    for fn in ("is_librosa_available", "is_torchaudio_available", "is_speech_available",
               "is_onnxruntime_available"):
        if hasattr(iu, fn):
            getattr(iu, fn)()
    before = set(sys.modules)
    _install_stubs()
    added = sorted(set(sys.modules) - before)
    import qwen_tts  # noqa: F401
    for name in added:
        sys.modules.pop(name, None)
    return added


def _install_stubs():
    if _missing("sox"):
        m = _mod("sox")
        m.Transformer = _unavailable("sox.Transformer")
        sys.modules["sox"] = m
    if _missing("onnxruntime"):
        m = _mod("onnxruntime")
        m.SessionOptions = _unavailable("onnxruntime.SessionOptions")
        m.InferenceSession = _unavailable("onnxruntime.InferenceSession")
        m.GraphOptimizationLevel = types.SimpleNamespace(ORT_ENABLE_ALL=99)
        sys.modules["onnxruntime"] = m
    if _missing("torchaudio"):
        # only used as `import torchaudio.compliance.kaldi as kaldi` (25 Hz x-vector)
        ta, comp, kaldi = _mod("torchaudio"), _mod("torchaudio.compliance"), _mod("torchaudio.compliance.kaldi")
        kaldi.fbank = _unavailable("torchaudio.compliance.kaldi.fbank")
        comp.kaldi = kaldi
        ta.compliance = comp
        sys.modules.update({"torchaudio": ta, "torchaudio.compliance": comp,
                            "torchaudio.compliance.kaldi": kaldi})
    if _missing("librosa"):
        m = _mod("librosa")
        filters = _mod("librosa.filters")
        filters.mel = _unavailable("librosa.filters.mel")
        m.filters = filters
        m.load = _unavailable("librosa.load")
        m.resample = _unavailable("librosa.resample")
        sys.modules["librosa"] = m
        sys.modules["librosa.filters"] = filters
