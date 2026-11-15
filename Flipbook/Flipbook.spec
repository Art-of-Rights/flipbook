# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Flipbook (onefile, windowed). Build with build.bat.
import os
import espeakng_loader
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

F = os.path.abspath(SPECPATH)

# pyinstaller-hooks-contrib already ships hook-webview.py (lib/ data + dlls),
# hook-clr.py and hook-clr_loader.py; these calls are harmless duplicates
# (TOC de-duplicates) that guarantee webview/lib/*.dll incl. runtimes/*/WebView2Loader.dll.
datas = [
    (os.path.join(F, 'flipbook.html'), '.'),
    (os.path.join(F, 'vendor'), 'vendor'),
    (os.path.join(F, 'flipbook.ico'), '.'),   # window icon at every size (v1.8.4; app.py passes it to webview.start)
    (os.path.join(F, 'manual.txt'), '.'),     # the user manual, shown only in ⌨ Commands > About (no README.txt since 1.8.1)
]
datas += collect_data_files('webview')
binaries = collect_dynamic_libs('webview')

# --- Read aloud: espeak-ng (pronunciation for the AI voice), English data only ---
ES = os.path.dirname(espeakng_loader.__file__)
ESD = os.path.join(ES, 'espeak-ng-data')
binaries += [(os.path.join(ES, 'espeak-ng.dll'), 'espeakng_loader')]
for name in ('phondata', 'phondata-manifest', 'phonindex', 'phontab', 'intonations', 'en_dict'):
    datas.append((os.path.join(ESD, name), 'espeakng_loader/espeak-ng-data'))
datas += [(os.path.join(ESD, 'lang'), 'espeakng_loader/espeak-ng-data/lang'),
          (os.path.join(ESD, 'voices'), 'espeakng_loader/espeak-ng-data/voices')]
# (the voice model itself is NOT bundled: it is the optional add-on downloaded by the app)

# --- graphics card (v1.4): onnxruntime-directml ships DirectML.dll next to its capi; make sure it is in
binaries += collect_dynamic_libs('onnxruntime')
# --- Qwen3-TTS add-on: these two scripts run inside the add-on's own private Python (copied there by qwen.py)
for name in ('qwen_worker.py', 'qwen_shims.py'):
    if os.path.isfile(os.path.join(F, name)):
        datas.append((os.path.join(F, name), '.'))

hiddenimports = [
    'kokoro_gpu',
    'qwen',
    'onnx',                     # converts the GPU voice model once on the user's PC
    'onnx.utils',
    'onnx.checker',
    'onnx.numpy_helper',
    'serve',
    'tts',
    'dictionary',
    'legal',                    # legal dictionary (v1.8.17); reads the user's own Black's PDFs with pypdf
    'mydict',                   # the user's own dictionaries from PDF files (v1.8.18)
    'pypdf',
    'sqlite3',
    'webview.platforms.edgechromium',
    'webview.platforms.winforms',
    'clr',
    'clr_loader',
    'pythonnet',
    'onnxruntime',
    'numpy',
]

a = Analysis(
    [os.path.join(F, 'app.py')],
    pathex=[F],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', '_tkinter', 'PIL', 'matplotlib', 'scipy', 'pandas',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'qtpy', 'gi', 'cefpython3',
        'IPython', 'pytest', 'espeakng_loader', 'sympy', 'onnxruntime.training',
        'onnxruntime.transformers', 'onnxruntime.quantization', 'onnxruntime.tools',
        'onnx.reference', 'onnx.backend', 'onnx.defs.tests', 'torch',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Flipbook',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(F, 'flipbook.ico'),
    version=os.path.join(F, 'version_info.txt'),
)
