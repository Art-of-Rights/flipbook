"""Qwen3-TTS voices for Flipbook's Read aloud (optional add-on, NVIDIA GPUs only).

Flipbook.exe stays small: everything heavy lives in "addons/qwen" next to the exe and is
downloaded only when the user asks for it (about 5.4 GB, 7.4 GB on disk):

    addons\\qwen\\py\\        private embeddable Python 3.12 with PyTorch (CUDA 12.8) and faster-qwen3-tts
    addons\\qwen\\m\\         the Qwen3-TTS-12Hz-0.6B-CustomVoice model (pinned commit)
    addons\\qwen\\runtime\\   qwen_worker.py + qwen_shims.py (copied from Flipbook on every engine start)
    addons\\qwen\\w\\, t\\    downloads and temp files during install (deleted afterwards)
    addons\\qwen\\installed.json   written last; its presence means the install finished

Speech is made by a separate worker process (qwen_worker.py) running in that private Python, so
torch never enters Flipbook.exe and stopping the worker frees all GPU memory.
Every download is pinned by URL, size and SHA-256. Standard library only (+ numpy, which the exe has).
"""
import hashlib
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile

ADDON_SUBDIR = ("addons", "qwen")
RUNTIME_VERSION = 1            # bump when the pinned list below changes (forces a reinstall)
SAMPLE_RATE = 24000
LANGUAGE = "English"
CREATE_NO_WINDOW = 0x08000000
UA = {"User-Agent": "Flipbook/1.4"}
RUNTIME_FILES = ("qwen_worker.py", "qwen_shims.py")

# Only the two English-native CustomVoice speakers. Source: the model card's speaker table
# (Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice README.md at the pinned commit):
#   Ryan  - "Dynamic male voice with rhythm."  native language English (accent not stated)
#   Aiden - "Sunny American male voice."        native language English
SPEAKERS = [
    ("ryan", "Ryan · man, lively"),
    ("aiden", "Aiden · American man, bright"),
]
SPEAKER_IDS = {s for s, _ in SPEAKERS}

# ---------------------------------------------------------------- pinned downloads
PYTHON = ("https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
          11133606, "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3")
PIP = ("https://bootstrap.pypa.io/pip/zipapp/pip-26.2.2.pyz",
       1762217, "e5ef64ef63ff89a746014ad81658b76c1940385fdcdbc4fcbfa6e3ef1887352d")

# Wheels (win_amd64 / cp312), each by direct URL. Every hash was checked against the hash
# published by PyPI / download.pytorch.org. Installed with --no-deps: this IS the full set.
# torch 2.10.0+cu128 has SASS for sm_70 ... sm_120. transformers must stay 5.15.1:
# 5.18.0 and 5.22.2 break qwen-tts-hf 0.1.1.post1 (MimiConfig rope_theta / Mimi 'upsample').
WHEELS = [
    ("https://files.pythonhosted.org/packages/9f/d2/c581486aa6c4fbd7394c23c47b83fa1a919d34194e16944241daf9e762dd/accelerate-1.12.0-py3-none-any.whl",
     380935, "3e2091cd341423207e2f084a6654b1efcd250dc326f2a37d6dde446e07cabb11"),
    ("https://files.pythonhosted.org/packages/cd/f5/3dd85d583e55be76cd02b1fc9bbec771b12d63124b01661d9535ccf8721f/annotated_doc-0.0.6-py3-none-any.whl",
     5293, "361ca0950cc7082f53fa69e9328409f40a62c61e57e0a47e1825a08078a58a53"),
    ("https://files.pythonhosted.org/packages/99/91/8acff4f5e50511b911bbccb72b8628a49c68ce14148cd9f6431094859a90/annotated_types-0.8.0-py3-none-any.whl",
     13427, "f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0"),
    ("https://files.pythonhosted.org/packages/a9/16/7757fb84c819accc7d7e9af5291b261e4abb8627ee040f99e7d35ef2457a/anyio-4.16.0-py3-none-any.whl",
     134521, "c66ae634e0b8228396b801741beb3294326d4b6102f7bba4041c0dfb58c29026"),
    ("https://files.pythonhosted.org/packages/32/d1/a3652fe2c19134d0d15922177821d863cbd7e842d53a21bbe7e8e3a1b5ac/certifi-2026.10.23-py3-none-any.whl",
     135082, "e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1"),
    ("https://files.pythonhosted.org/packages/d9/79/615cc094e2fb508cade7de88d3b4f6c4ec2bab695c97bce9153dc65aadf5/cffi-2.1.1-cp312-cp312-win_amd64.whl",
     185919, "f53e442b08449d42821fa4a4fba000095af9f62742a500f978a9f557ec44339a"),
    ("https://files.pythonhosted.org/packages/31/7d/164b7ffc8624fe9efb4221bf9af485d5c1e41b9ae118045d2b6dc5457d8b/click-8.5.1-py3-none-any.whl",
     126163, "8bb5376d2006950386f8c6427613334a2acadc5f559cb80e638d00b4df957e37"),
    ("https://files.pythonhosted.org/packages/d1/d6/3965ed04c63042e047cb6a3e6ed1a63a35087b6a609aa3a15ed8ac56c221/colorama-0.4.6-py2.py3-none-any.whl",
     25335, "4f1d9991f5acc0ca119f9d443620b77f9d6b33703e51011c16baf57afb285fc6"),
    ("https://files.pythonhosted.org/packages/2a/09/f8d8f8f31e4483c10a906437b4ce31bdf3d6d417b73fe33f1a8b59e34228/einops-0.8.2-py3-none-any.whl",
     65638, "54058201ac7087911181bfec4af6091bb59380360f069276601256a76af08193"),
    ("https://files.pythonhosted.org/packages/b3/e5/074b3ca7bb05f2d3564a456bc7620c24e65edeb1288c118a4b0506d55f11/faster_qwen3_tts-0.5.4-py3-none-any.whl",
     43858, "714bfb6d0a70fac177eac48b0a750e04bd597ab9aa86d35c4271aa15d0d1b716"),
    ("https://files.pythonhosted.org/packages/ef/90/5d0096a325aa0f83ba7a3968bfb485b67e32c947bdf9cd0fcb4b485be718/filelock-4.2.10-py3-none-any.whl",
     218040, "9479aaeb4db20b5a53e3da28493edd24720b66b8a0b84030aa56ccdf243d6857"),
    ("https://files.pythonhosted.org/packages/6c/c0/a98505f18594f1bce828bb159cec0fcf9860562f1a2c85913409fc8f3d9e/fsspec-2026.9.0-py3-none-any.whl",
     221738, "8dd6e646e99ea382bd85f97a45e6b526a442d79423a7dc673f1e2756d05fcb5f"),
    ("https://files.pythonhosted.org/packages/04/4b/29cac41a4d98d144bf5f6d33995617b185d14b22401f75ca86f384e87ff1/h11-0.16.0-py3-none-any.whl",
     37515, "63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86"),
    ("https://files.pythonhosted.org/packages/97/4d/2c76a992b370ff77d0b5219275779f8c9a7dabb8ee649deb265dfed84d0f/hf_xet-1.8.0-cp38-abi3-win_amd64.whl",
     3911559, "92188598dedc1e1f5609bd718d4db8687a6c89915553912571e2f587892eecec"),
    ("https://files.pythonhosted.org/packages/7e/f5/f66802a942d491edb555dd61e3a9961140fd64c90bce1eafd741609d334d/httpcore-1.0.9-py3-none-any.whl",
     78784, "2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55"),
    ("https://files.pythonhosted.org/packages/2a/39/e50c7c3a983047577ee07d2a9e53faf5a69493943ec3f6a384bdc792deb2/httpx-0.28.1-py3-none-any.whl",
     73517, "d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad"),
    ("https://files.pythonhosted.org/packages/fc/16/963096d224b80909432dc16561a615fd33d2d13beef3ce4c63fa25e40867/huggingface_hub-1.33.0-py3-none-any.whl",
     846435, "04e434b06e100eddbce9a6e817d72693a7884b10a79bd67ab48080d5c07eb899"),
    ("https://files.pythonhosted.org/packages/58/a2/bb081bab032533a855d44de1d56f8e8426114ff1ba5d1f07a438a0a654f8/idna-3.20-py3-none-any.whl",
     69583, "ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c"),
    ("https://files.pythonhosted.org/packages/62/a1/3d680cbfd5f4b8f15abc1d571870c5fc3e594bb582bc3b64ea099db13e56/jinja2-3.1.6-py3-none-any.whl",
     134899, "85ece4451f492d0c13c5dd7c13a64681a86afae63a5f347908daf103ce6d2f67"),
    ("https://files.pythonhosted.org/packages/c9/31/5f0ac2f1a1c67423c995c731a11cd4ad05d081a2be969d74f868bdb9cc70/markdown_it_py-4.3.0-py3-none-any.whl",
     96951, "7aa1511a46c39138de78d9eba92ce47e63124d8003fc98b3a351909e04239efa"),
    ("https://files.pythonhosted.org/packages/3f/bf/62495e180b7000aaf30000fff849e933f74264638057176cf46852500adc/markupsafe-3.0.4-cp312-cp312-win_amd64.whl",
     14303, "11935df9bf455ed0c04eb87bcd720f02b1fe5e02128a9430f23aed6f93336fc7"),
    ("https://files.pythonhosted.org/packages/b3/38/89ba8ad64ae25be8de66a6d463314cf1eb366222074cfda9ee839c56a4b4/mdurl-0.1.2-py3-none-any.whl",
     9979, "84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8"),
    ("https://files.pythonhosted.org/packages/43/e3/7d92a15f894aa0c9c4b49b8ee9ac9850d6e63b03c9c32c0367a13ae62209/mpmath-1.3.0-py3-none-any.whl",
     536198, "a0b2b9fe80bbcd81a6647ff13108738cfb482d481d826cc0e02f5b35e5c88d2c"),
    ("https://files.pythonhosted.org/packages/7e/cd/fe58041e9011f307c490e3e17dd48cc516448f7c698a3f2d9d9d65d7e6a8/networkx-3.7-py3-none-any.whl",
     2142205, "e3fd2c13a7814cee3746340d8d7f8598a67f16a58bf47fb7f8793fab6efca1b0"),
    ("https://files.pythonhosted.org/packages/12/33/fefe62073dc8acfd0f2b9ed7c003af2f50aa61555e113e6db02b8f79f145/numpy-2.5.4-cp312-cp312-win_amd64.whl",
     12573091, "a7b1b6353e36a7e50de2973a38d705c88ee93adcf120673cee7f45a4a3fa223a"),
    ("https://files.pythonhosted.org/packages/63/34/ba1c580383c9eada3711951fef0795c80b829a078d72188184bcab9dd527/packaging-26.3-py3-none-any.whl",
     129956, "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c"),
    ("https://files.pythonhosted.org/packages/b4/90/e2159492b5426be0c1fef7acba807a03511f97c5f86b3caeda6ad92351a7/psutil-7.2.2-cp37-abi3-win_amd64.whl",
     137737, "eb7e81434c8d223ec4a219b5fc1c47d0417b12be7ea866e24fb5ad6e84b3d988"),
    ("https://files.pythonhosted.org/packages/b6/ae/315bbeee126a769cbde43cf4908acb4b40408d8bf8bbf334dd7368d8dadf/pycparser-3.12-py3-none-any.whl",
     62600, "a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e"),
    ("https://files.pythonhosted.org/packages/72/b8/b864503789ef5c58597d2a7b4adbee62c26440ab1e682581fc6838df8947/pydantic-2.14.3-py3-none-any.whl",
     494071, "3f7b347782ab7e4cf4c798496ae3a82787d9f21ec12676a9c564355eeef8daa8"),
    ("https://files.pythonhosted.org/packages/82/88/d438a8fcf3738e9ff2670f724047ca3893ec899bcd5cdfc077a2aab133c6/pydantic_core-2.50.4-cp312-cp312-win_amd64.whl",
     2264064, "ea2f61b0bc12f3592288503961b764579dea0658852d96a0b3d823614d808821"),
    ("https://files.pythonhosted.org/packages/71/46/17f022dd3e953bf20a04a028a21ec746d942f8d2af30fa0f124fa0e6a684/pygments-2.21.0-py3-none-any.whl",
     1250147, "2363c69b61c4a97c838da3b130dcd6468f4848992b21a82f2a63ec34377137d9"),
    ("https://files.pythonhosted.org/packages/86/bf/899e81e4cce32febab4fb42bb97dcdf66bc135272882d1987881a4b519e9/pyyaml-6.0.3-cp312-cp312-win_amd64.whl",
     154003, "5fcd34e47f6e0b794d17de1b4ff496c00986e1c83f7ab2fb8fcfe9616ff7477b"),
    ("https://files.pythonhosted.org/packages/ed/14/57e0ba0b84535102e08f33f1e14a2e0975229f5b878290343705727a3a33/qwen_tts_hf-0.1.1.post1-py3-none-any.whl",
     115212, "ed5b7aae64cf795243369fb5226173f459a45689ea86db1c6d3eb14fc0c2c01d"),
    ("https://files.pythonhosted.org/packages/c1/ef/e03271355a802f38f7510e31e006667fc8b95b771058d75a36e2afda61b2/regex-2026.11.9-cp312-cp312-win_amd64.whl",
     280891, "ae42ad9825cde4cd374abfce6bc82233b91099170c138b349554813e97da5100"),
    ("https://files.pythonhosted.org/packages/82/3b/64d4899d73f91ba49a8c18a8ff3f0ea8f1c1d75481760df8c68ef5235bf5/rich-15.0.0-py3-none-any.whl",
     310654, "33bd4ef74232fb73fe9279a257718407f169c09b78a87ad3d296f548e27de0bb"),
    ("https://files.pythonhosted.org/packages/1b/6d/3fba214c1e5e0f69991677ec3bc17023f0421776975e1de0c682dca475e2/safetensors-0.8.0-cp310-abi3-win_amd64.whl",
     355540, "096ec1a98435df7beb08853bb5aa9081a84f23d0adc67ed1a0a10550f608373f"),
    ("https://files.pythonhosted.org/packages/ef/55/6dc6d6bb953bdfc52519a0059592c52b6e3b8ad51cf8256dffceb55b0610/setuptools-84.2.1-py3-none-any.whl",
     876263, "ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2"),
    ("https://files.pythonhosted.org/packages/e0/f9/0595336914c5619e5f28a1fb793285925a8cd4b432c9da0a987836c7f822/shellingham-1.5.4-py2.py3-none-any.whl",
     9755, "7ecfff8f2fd72616f7481040475a65b2bf8af90a56c89140852d1120324e8686"),
    ("https://files.pythonhosted.org/packages/ed/97/b39c18ac1df45e755ca22b8b00e872929da5d107998a207a5e4ac831bfda/soundfile-0.14.0-py2.py3-none-win_amd64.whl",
     1021480, "299491d3499460fb1b74bb4bd78b57ffc2d243a5fafa7b6ec1b264875c78453e"),
    ("https://files.pythonhosted.org/packages/a2/09/77d55d46fd61b4a135c444fc97158ef34a095e5681d0a6c10b75bf356191/sympy-1.14.0-py3-none-any.whl",
     6299353, "e091cc3e99d2141a0ba2847328f5479b05d94a6635cb96148ccb3f34671bd8f5"),
    ("https://files.pythonhosted.org/packages/65/71/0670843133a43d43070abeb1949abfdef12a86d490bea9cd9e18e37c5ff7/tokenizers-0.22.2-cp39-abi3-win_amd64.whl",
     2747786, "c9ea31edff2968b44a88f97d784c2f16dc0729b8b143ed004699ebca91f05c48"),
    ("https://download-r2.pytorch.org/whl/cu128/torch-2.10.0%2Bcu128-cp312-cp312-win_amd64.whl",
     2867409626, "fbde8f6a9ec8c76979a0d14df21c10b9e5cab6f0d106a73ca73e2179bc597cae"),
    ("https://files.pythonhosted.org/packages/a7/03/921a3d3c75785aca9ebfbfcabfbc3a1be12e2ab5265deb026d55a5a3f83e/tqdm-4.70.1-py3-none-any.whl",
     80199, "c293e525e6fef9c20e8728fd4612df02a0aa31bb5fe91ecd93e123b1b7bffa73"),
    ("https://files.pythonhosted.org/packages/41/c4/a12e1d9b387fb0c40a57116db82b457e8c771cb419163cda29204d74a595/transformers-5.15.1-py3-none-any.whl",
     11749582, "b7cdf238ff583e3a58dbc7fa34da1aaf091ce063141f65a30538160bd5afe93f"),
    ("https://files.pythonhosted.org/packages/05/ff/51969745f31cea6bcd12151e668febcf979656316e67a7755942267c1273/typer-0.29.1-py3-none-any.whl",
     121064, "00dbc549c13428bec1be55fe1f4d0fb8c6637a310e4f178ef14abe9534ee7b58"),
    ("https://files.pythonhosted.org/packages/49/d3/b8441a820a491ddfc024b0b0cf0393375b75ea13866d9c66727e54c2fc80/typing_extensions-4.16.0-py3-none-any.whl",
     45571, "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8"),
    ("https://files.pythonhosted.org/packages/de/0c/ea2544850a7c135e15033a5c7a8e7211136184b7ae63e2239d557c2b3019/typing_inspection-0.4.5-py3-none-any.whl",
     14874, "bc210535442b39e1bf2805e0c7a17dfd6a4ace760c84c7f7d16245d23a6b1c39"),
]

MODEL_REPO = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
MODEL_REV = "85e237c12c027371202489a0ec509ded67b5e4b5"
MODEL_FILES = [   # (path in repo, size, sha256); README.md / .gitattributes are not needed
    ("config.json", 4908, "81aca2b6fac304944d8acf345272d8a9a727d5fc2e2e66b222ab4729340c7455"),
    ("generation_config.json", 245, "f1b90b4513f3b34c62851049e2492d7b4c5940daf1276f89c82b8ef04127f3aa"),
    ("merges.txt", 1671839, "599bab54075088774b1733fde865d5bd747cbcc7a547c5bc12610e874e26f5e3"),
    ("model.safetensors", 1811626576, "bc3c7e785eb961179c25450d1acff03f839e0002f2f3a5aeb67b5735c0fa2adb"),
    ("preprocessor_config.json", 127, "efdde1022ea9d76928bf7a9cd53139138f5ba2e466e837f08f6105ab1af1c119"),
    ("tokenizer_config.json", 7344, "dc3c31c3bdaedd5016382bb3cbe07323026775ad51f5a4fb564505992ae4a670"),
    ("vocab.json", 2776833, "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
    ("speech_tokenizer/config.json", 2336, "ee65bb901c876664ab8707c487157aa1a6ee57c65969b28fb5ec9dc211e68167"),
    ("speech_tokenizer/configuration.json", 76, "6bc26d64eb5024b4d1dab5a52371958b429256d6c9d59787f1f5294a54e0cebd"),
    ("speech_tokenizer/model.safetensors", 682293092, "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258"),
    ("speech_tokenizer/preprocessor_config.json", 234, "fcb3805e597e786d4067706e602f6688524640f8d3396790e2e09b5942fcbdfb"),
]


def _wheel_name(url):
    return urllib.parse.unquote(url.rsplit("/", 1)[1])      # torch-2.10.0%2Bcu128... -> +cu128


def _items():
    """Every file to download: dicts with url, size, sha256, rel (path inside the add-on), label."""
    out = [dict(url=PYTHON[0], size=PYTHON[1], sha256=PYTHON[2], rel="w\\" + _wheel_name(PYTHON[0]),
                label="Downloading Python"),
           dict(url=PIP[0], size=PIP[1], sha256=PIP[2], rel="w\\" + _wheel_name(PIP[0]),
                label="Downloading the package installer")]
    for url, size, sha in WHEELS:
        name = _wheel_name(url)
        label = ("Downloading PyTorch with CUDA (2.9 GB)" if name.startswith("torch-")
                 else "Downloading the voice engine libraries")
        out.append(dict(url=url, size=size, sha256=sha, rel="w\\" + name, label=label))
    for path, size, sha in MODEL_FILES:
        out.append(dict(url=f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REV}/{path}",
                        size=size, sha256=sha, rel="m\\" + path.replace("/", "\\"),
                        label="Downloading the Qwen voice model (2.5 GB)"))
    return out


DOWNLOAD_BYTES = PYTHON[1] + PIP[1] + sum(w[1] for w in WHEELS) + sum(m[1] for m in MODEL_FILES)
PY_BYTES = 4_920_000_000       # measured: installed private Python incl. the .pyc files it writes
DISK_BYTES = PY_BYTES + sum(m[1] for m in MODEL_FILES) + 1_000_000
DEEPEST_PY_PATH = 150          # longest file path below addons\qwen\py\ (measured 144, transformers .pyc)


def addon_dir(base):
    return os.path.join(base, *ADDON_SUBDIR)


def _p(base, *parts):
    return os.path.join(addon_dir(base), *parts)


def installed(base):
    try:
        with open(_p(base, "installed.json"), encoding="utf-8") as f:
            if json.load(f).get("runtime") != RUNTIME_VERSION:
                return False
    except (OSError, ValueError):
        return False
    if not os.path.isfile(_p(base, "py", "python.exe")):
        return False
    return all(os.path.isfile(_p(base, "m", *p.split("/"))) and
               os.path.getsize(_p(base, "m", *p.split("/"))) == s for p, s, _ in MODEL_FILES)


# ---------------------------------------------------------------- GPU check
# torch 2.10.0+cu128 ships kernels for compute capability 7.0+ (sm_70..sm_120), but we require 8.0
# (RTX 30-series / A-series and newer): the model needs bfloat16, which only 8.0+ has. Tested on
# an RTX 3070 Laptop: float16 crashes ("device-side assert", the sampler gets NaN/inf); float32
# works but is ~40% slower (RTF 1.2 vs 2.0 there) and needs ~5.1 GiB, so a 7.5 card (RTX 20xx,
# GTX 16xx) would be about real time at best. The worker still falls back to float32 below 8.0.
MIN_COMPUTE = (8, 0)
# Measured on an RTX 3070 Laptop: ~2.0 GiB weights, ~3.0 GiB peak allocated, ~3.3 GiB reserved,
# plus ~0.4 GiB CUDA context. 6 GB cards report 5.9-6.0 GiB total, hence 5800 MiB (not 6144):
# that still leaves ~1.5 GB for Windows and other apps.
MIN_VRAM_MB = 5800
# The cu128 wheels bundle the CUDA 12.8 runtime. NVIDIA documents 570.65 as the Windows driver for
# CUDA 12.8, but CUDA minor-version compatibility lets any CUDA 12.x driver (Windows >= 528.33, the
# CUDA 12.0 GA driver) run a 12.8 runtime as long as no PTX JIT is needed, and the torch wheel has
# native (SASS) kernels for every supported GPU. NOT tested on a pre-570 driver.
MIN_DRIVER = (528, 33)
DOC_DRIVER = "570.65"

_gpu = None
_gpu_lock = threading.Lock()


def _nvidia_smi():
    p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
    return p if os.path.isfile(p) else shutil.which("nvidia-smi")


def gpu_check():
    """Can this PC run Qwen voices? Cached. Never raises."""
    global _gpu
    with _gpu_lock:
        if _gpu is None:
            try:
                _gpu = _gpu_check()
            except Exception as e:
                _gpu = {"ok": False, "reason": f"Couldn't check the graphics card ({e}).",
                        "name": "", "vram_mb": 0, "driver": "", "compute": "", "uuid": ""}
        return dict(_gpu)


def _gpu_check():
    res = {"ok": False, "reason": "", "name": "", "vram_mb": 0, "driver": "", "compute": "", "uuid": ""}
    smi = _nvidia_smi()
    if not smi:
        res["reason"] = "Qwen voices need an NVIDIA graphics card, and none was found on this PC."
        return res

    def query(fields):
        r = subprocess.run([smi, "--query-gpu=" + fields, "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return None
        return [[c.strip() for c in line.split(",")] for line in r.stdout.strip().splitlines() if line.strip()]

    rows = query("name,memory.total,driver_version,compute_cap,uuid")
    if rows is None:                       # drivers before ~510 don't know compute_cap
        rows = [r[:3] + ["", r[3]] for r in (query("name,memory.total,driver_version,uuid") or [])]
    if not rows:
        res["reason"] = "Qwen voices need an NVIDIA graphics card, but its driver didn't answer (nvidia-smi failed)."
        return res
    best = None
    for name, mem, drv, cc, uuid in rows:
        g = {"name": name, "vram_mb": int(float(mem)) if mem.replace(".", "").isdigit() else 0,
             "driver": drv, "compute": cc, "uuid": uuid}
        if best is None or g["vram_mb"] > best["vram_mb"]:
            best = g
    res.update(best)
    try:
        drv = tuple(int(x) for x in res["driver"].split(".")[:2])
    except ValueError:
        drv = (0, 0)
    try:
        cc = tuple(int(x) for x in res["compute"].split(".")[:2])
    except ValueError:
        cc = None
    gb = res["vram_mb"] / 1024
    if drv < MIN_DRIVER:
        res["reason"] = (f"Your NVIDIA driver ({res['driver'] or 'unknown'}) is too old for Qwen voices. "
                         f"Update it from nvidia.com (version {DOC_DRIVER} or newer is best), then restart Flipbook.")
    elif cc is not None and cc < MIN_COMPUTE:
        res["reason"] = (f"Your {res['name']} is too old for Qwen voices. They need an NVIDIA "
                         "RTX 30-series or newer graphics card.")
    elif res["vram_mb"] < MIN_VRAM_MB:
        res["reason"] = (f"Qwen voices need a graphics card with at least 6 GB of memory; "
                         f"your {res['name']} has {gb:.0f} GB.")
    else:
        res["ok"] = True
    return res


# ---------------------------------------------------------------- install
class Stale(Exception):
    """The reader moved on; this speech is no longer wanted (map to tts.Stale)."""


class _Cancelled(Exception):
    pass


def _long_paths_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def _clean_env(base):
    """Environment for the private Python: nothing from Flipbook.exe's own (PyInstaller) Python."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("PYTHON", "_MEIPASS", "_PYI", "TCL_", "TK_"))}
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if p and not p.startswith(mei))
    t = _p(base, "t")
    env.update(TMP=t, TEMP=t, PIP_NO_CACHE_DIR="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
               PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
               HF_HOME=_p(base, "hf"))
    return env


def _mirror_dirs():
    # TEST ONLY: FLIPBOOK_QWEN_MIRROR="dir1;dir2" serves files from local copies (by file name,
    # or by repo path for model files) instead of the internet. Checksums still apply.
    return [d for d in os.environ.get("FLIPBOOK_QWEN_MIRROR", "").split(";") if d]


def _copy_runtime(base):
    src = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None
    src = src or os.path.dirname(os.path.abspath(__file__))
    dst = _p(base, "runtime")
    os.makedirs(dst, exist_ok=True)
    for name in RUNTIME_FILES:
        with open(os.path.join(src, name), "rb") as f:
            data = f.read()
        target = os.path.join(dst, name)
        try:
            with open(target, "rb") as f:
                if f.read() == data:
                    continue
        except OSError:
            pass
        with open(target + ".tmp", "wb") as f:
            f.write(data)
        os.replace(target + ".tmp", target)


def _rmtree(path, tries=10):
    def onerror(func, p, exc):
        try:
            os.chmod(p, 0o666)
            func(p)
        except OSError:
            pass
    for _ in range(tries):
        if not os.path.exists(path):
            return True
        shutil.rmtree(path, onerror=onerror)
        if not os.path.exists(path):
            return True
        time.sleep(0.5)
    return not os.path.exists(path)


class Installer:
    def __init__(self, base):
        self.base = base
        self.lock = threading.Lock()
        self.state = "idle"        # idle | downloading | verifying | installing | done | error | cancelled
        self.done = 0
        self.error = ""
        self.phase = ""
        self.cancel_flag = False
        self.thread = None
        self.proc = None

    def status(self):
        return {"state": self.state, "done": self.done, "total": DOWNLOAD_BYTES,
                "error": self.error, "phase": self.phase}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.state, self.error, self.cancel_flag, self.phase = "downloading", "", False, "Starting"
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def cancel(self):
        self.cancel_flag = True
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def _check(self):
        if self.cancel_flag:
            raise _Cancelled()

    def _run(self):
        d = addon_dir(self.base)
        try:
            os.makedirs(d, exist_ok=True)
            for sub in ("w", "m", "t"):
                os.makedirs(os.path.join(d, sub), exist_ok=True)
        except OSError as e:
            self.state, self.error = "error", f"Can't write to the Flipbook folder ({e.strerror}). Move Flipbook to a folder you can write to, such as Documents."
            return
        try:
            self._preflight(d)
            self._download_all(d)
            self._install(d)
            self.done, self.state, self.phase = DOWNLOAD_BYTES, "done", "Done"
        except _Cancelled:
            self.state, self.phase = "cancelled", "Cancelled"
        except Exception as e:  # network errors, checksum mismatch, disk full, pip failure ...
            self.state, self.error = "error", str(e) or e.__class__.__name__
        finally:
            self.proc = None

    def _preflight(self, d):
        if len(d) + 4 + DEEPEST_PY_PATH > 255 and not _long_paths_enabled():
            raise RuntimeError("The Flipbook folder's path is too long for the Qwen voice files "
                               f"({len(d)} characters). Move Flipbook to a shorter path, such as C:\\Flipbook, and try again.")
        have = sum(min(_size(os.path.join(d, it["rel"])), it["size"]) +
                   min(_size(os.path.join(d, it["rel"]) + ".part"), it["size"]) for it in _items())
        need = (DOWNLOAD_BYTES - have) + PY_BYTES + 500_000_000
        free = shutil.disk_usage(d).free
        if free < need:
            raise RuntimeError(f"Not enough free disk space: the Qwen voice needs about {need / 1e9:.1f} GB "
                               f"during installation, and the drive has {free / 1e9:.1f} GB free.")

    # -- downloading
    def _download_all(self, d):
        finished = 0
        for it in _items():
            self._check()
            dest = os.path.join(d, it["rel"])
            if os.path.isfile(dest) and os.path.getsize(dest) == it["size"]:
                finished += it["size"]
                self.done = finished
                continue
            self.phase = it["label"]
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            self._fetch(it, dest, finished)
            finished += it["size"]
            self.done = finished

    def _open(self, it, have):
        """Return (response-like, resumed). Local mirror (tests) or HTTP with Range."""
        for m in _mirror_dirs():
            rel = it["rel"][2:]                  # strip "w\" / "m\"
            for cand in (os.path.join(m, rel), os.path.join(m, os.path.basename(rel))):
                if os.path.isfile(cand) and os.path.getsize(cand) == it["size"]:
                    f = open(cand, "rb")
                    f.seek(have)
                    return f, True
        req = urllib.request.Request(it["url"], headers=UA)
        if have:
            req.add_header("Range", f"bytes={have}-")
        r = urllib.request.urlopen(req, timeout=30)
        return r, (not have) or r.status == 206

    def _fetch(self, it, dest, before):
        part = dest + ".part"
        have = _size(part)
        if have > it["size"]:
            os.remove(part)
            have = 0
        h = hashlib.sha256()
        if have:
            self.state = "verifying"
            with open(part, "rb") as fp:
                for chunk in iter(lambda: fp.read(1 << 20), b""):
                    self._check()
                    h.update(chunk)
            self.state = "downloading"
        tries = 0
        while have < it["size"]:
            self._check()
            try:
                r, resumed = self._open(it, have)
                with r, open(part, "ab") as out:
                    if have and not resumed:      # server ignored the resume request: start over
                        out.truncate(0)
                        have, h = 0, hashlib.sha256()
                    while have < it["size"]:
                        self._check()
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        out.write(chunk)
                        h.update(chunk)
                        have += len(chunk)
                        self.done = before + have
                        tries = 0
            except _Cancelled:
                raise
            except Exception as e:
                tries += 1
                if tries > 5:
                    raise RuntimeError(f"The download stopped ({e}). Check the internet connection and try again; it will continue where it left off.")
                for _ in range(4 * tries):
                    self._check()
                    time.sleep(0.5)
                have = _size(part)
                if have:
                    h = hashlib.sha256()
                    with open(part, "rb") as fp:
                        for chunk in iter(lambda: fp.read(1 << 20), b""):
                            h.update(chunk)
        self.state = "verifying"
        if have != it["size"] or h.hexdigest() != it["sha256"]:
            os.remove(part)
            raise RuntimeError(f"{os.path.basename(dest)} didn't match its checksum, so it was deleted. Please try again.")
        os.replace(part, dest)
        self.state = "downloading"

    # -- installing
    def _run_logged(self, args, logname, timeout):
        log = open(os.path.join(addon_dir(self.base), logname), "ab")
        log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(args)}\n".encode())
        log.flush()
        try:
            self.proc = subprocess.Popen(args, cwd=addon_dir(self.base), env=_clean_env(self.base),
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                         creationflags=CREATE_NO_WINDOW)
            t0 = time.time()
            while self.proc.poll() is None:
                if self.cancel_flag:
                    self.proc.kill()
                    self.proc.wait(10)
                    raise _Cancelled()
                if time.time() - t0 > timeout:
                    self.proc.kill()
                    raise RuntimeError("The installation step took too long and was stopped.")
                time.sleep(0.3)
            if self.cancel_flag:                   # cancel() killed it directly
                raise _Cancelled()
            return self.proc.returncode
        finally:
            log.close()

    def _log_tail(self, logname):
        try:
            with open(os.path.join(addon_dir(self.base), logname), "rb") as f:
                f.seek(max(0, os.path.getsize(f.name) - 20000))
                lines = [l.strip() for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
        except OSError:
            return ""
        errs = [l for l in lines if l.startswith("ERROR") or "Error" in l]
        return (errs or lines or [""])[-1][:300]

    def _install(self, d):
        self.state = "installing"
        py = os.path.join(d, "py")
        w = os.path.join(d, "w")
        self.phase = "Unpacking Python"
        try:
            os.remove(os.path.join(d, "installed.json"))      # a reinstall isn't "installed" until it ends
        except OSError:
            pass
        if _engine is not None and _engine.base == self.base:
            _engine.stop()                                     # its python.exe would be locked
        if os.path.exists(py) and not _rmtree(py):
            raise RuntimeError("Couldn't replace the old Qwen voice files (some are in use). Restart Flipbook and try again.")
        with zipfile.ZipFile(os.path.join(w, _wheel_name(PYTHON[0]))) as z:
            z.extractall(py)
        pth = [n for n in os.listdir(py) if n.endswith("._pth")]
        stem = pth[0][:-5] if pth else "python312"
        with open(os.path.join(py, stem + "._pth"), "w", encoding="ascii", newline="\r\n") as f:
            f.write(f"{stem}.zip\n.\nLib\\site-packages\n\nimport site\n")
        os.makedirs(os.path.join(py, "Lib", "site-packages"), exist_ok=True)
        self._check()

        lock = os.path.join(w, "lock.txt")
        with open(lock, "w", encoding="ascii", newline="\n") as f:
            for url, _, sha in WHEELS:
                name, ver = _wheel_name(url).split("-")[:2]
                f.write(f"{name}=={ver} --hash=sha256:{sha}\n")
        self.phase = "Installing PyTorch and the voice engine (this takes a few minutes)"
        code = self._run_logged([os.path.join(py, "python.exe"), os.path.join(w, _wheel_name(PIP[0])),
                                 "install", "--no-index", "--find-links", w, "--require-hashes", "--no-deps",
                                 "--only-binary=:all:", "--no-warn-script-location", "-r", lock],
                                "install.log", 3600)
        if code != 0:
            raise RuntimeError("Installing the voice engine failed: " + (self._log_tail("install.log") or f"exit code {code}"))

        self.phase = "Checking the installation"
        _copy_runtime(self.base)
        code = self._run_logged([os.path.join(py, "python.exe"), "-c",
                                 "import torch, transformers, faster_qwen3_tts, soundfile;"
                                 "print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"],
                                "install.log", 600)
        if code != 0:
            raise RuntimeError("The voice engine was installed but doesn't start: " + self._log_tail("install.log"))

        self.phase = "Cleaning up"
        _rmtree(w)
        _rmtree(os.path.join(d, "t"))
        with open(os.path.join(d, "installed.json"), "w", encoding="utf-8") as f:
            json.dump({"runtime": RUNTIME_VERSION, "model": f"{MODEL_REPO}@{MODEL_REV}",
                       "date": time.strftime("%Y-%m-%d %H:%M:%S")}, f)


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def remove(base):
    """Stop the engine and delete addons\\qwen (retrying files that are briefly locked)."""
    e = _engine
    if e is not None:
        e.stop()
    d = addon_dir(base)
    if os.path.isdir(d):
        if not _rmtree(d, tries=20):
            raise RuntimeError("Some Qwen voice files are in use and couldn't be deleted. Restart Flipbook and try again.")
    try:
        os.rmdir(os.path.dirname(d))      # "addons", if now empty
    except OSError:
        pass


# ---------------------------------------------------------------- speech
MAX_CHARS = 800         # per worker request: prompt + generated frames must fit the 2048-slot cache
CHARS_PER_SEC = 14.0    # typical for these speakers (measured 12-20)
READY_TIMEOUT = 300     # model load + CUDA graph capture: ~5-25 s normally
SYNTH_TIMEOUT = 300


def _seed(text):
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:4], "little") & 0x7FFFFFFF


def max_new_tokens(text):
    # 12 codec frames per second; allow ~len/8 s (about 2x normal speech) + 2 s
    return min(1700, int(12 * (len(text) / 8 + 2)))


def _split(text):
    if len(text) <= MAX_CHARS:
        return [text]
    pieces = re.split(r"(?<=[.!?;:])\s+", text)
    out, cur = [], ""
    for p in pieces:
        while len(p) > MAX_CHARS:                       # one huge "sentence": cut at a comma or space
            cut = max(p.rfind(", ", 0, MAX_CHARS), p.rfind(" ", 0, MAX_CHARS))
            cut = cut if cut > MAX_CHARS // 2 else MAX_CHARS
            head, p = p[:cut + 1].strip(), p[cut + 1:].strip()
            if cur:
                out.append(cur)
                cur = ""
            out.append(head)
        if cur and len(cur) + 1 + len(p) > MAX_CHARS:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        out.append(cur)
    return [c for c in out if c]


class _Dead(Exception):
    pass


class Engine:
    def __init__(self, base):
        self.base = base
        self.cv = threading.Condition()
        self.req_lock = threading.Lock()
        self._state = "stopped"      # stopped | starting | ready | error
        self.error = ""
        self.load_seconds = 0.0
        self.info = {}
        self.proc = None
        self.frames = None
        self.log = None

    def state(self):
        with self.cv:
            if self._state == "ready" and (self.proc is None or self.proc.poll() is not None):
                self._state, self.error = "error", "The Qwen voice engine stopped unexpectedly."
            return {"state": self._state, "error": self.error, "load_seconds": self.load_seconds}

    # -- process management
    def start(self):
        with self.cv:
            if self._state in ("starting", "ready") and self.proc is not None and self.proc.poll() is None:
                return
            if self._state == "starting":
                return
            self._kill_locked()
            self._state, self.error, self.load_seconds = "starting", "", 0.0
            threading.Thread(target=self._launch, daemon=True).start()

    def _launch(self):
        try:
            if not installed(self.base):
                raise RuntimeError("The Qwen voice add-on isn't installed.")
            _copy_runtime(self.base)
            logp = _p(self.base, "worker.log")
            if _size(logp) > 2_000_000:
                os.replace(logp, logp + ".1")
            log = open(logp, "ab")
            log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} start\n".encode())
            log.flush()
            env = _clean_env(self.base)
            g = gpu_check()
            if g.get("uuid"):
                env["CUDA_VISIBLE_DEVICES"] = g["uuid"]
            proc = subprocess.Popen(
                [_p(self.base, "py", "python.exe"), "-u", _p(self.base, "runtime", "qwen_worker.py"),
                 _p(self.base, "m"), str(os.getpid())],
                cwd=addon_dir(self.base), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=log, creationflags=CREATE_NO_WINDOW)
            frames = queue.Queue()
            threading.Thread(target=self._reader, args=(proc, frames), daemon=True).start()
            with self.cv:
                if self._state != "starting":          # stop() was called meanwhile
                    proc.kill()
                    log.close()
                    return
                self.proc, self.frames, self.log = proc, frames, log
            msg = self._next(frames, proc, READY_TIMEOUT)
            hdr = json.loads(msg.decode("utf-8"))
            if not hdr.get("ready"):
                raise RuntimeError(hdr.get("error") or "The Qwen voice engine failed to start.")
            with self.cv:
                if self.proc is proc:
                    self._state, self.load_seconds, self.info = "ready", float(hdr.get("load_seconds", 0)), hdr
                self.cv.notify_all()
        except _Dead:
            self._fail("The Qwen voice engine stopped while loading. See addons\\qwen\\worker.log for details.",
                       only_if_starting=True)
        except Exception as e:
            self._fail(str(e) or e.__class__.__name__, only_if_starting=True)

    def _fail(self, msg, only_if_starting=False):
        with self.cv:
            if only_if_starting and self._state != "starting":
                return                                 # stop() won the race; stay stopped
            self._kill_locked()
            self._state, self.error = "error", msg
            self.cv.notify_all()

    @staticmethod
    def _reader(proc, frames):
        f = proc.stdout
        try:
            while True:
                head = f.read(4)
                if len(head) < 4:
                    break
                (n,) = struct.unpack("<I", head)
                data = f.read(n)
                if len(data) < n:
                    break
                frames.put(data)
        except (OSError, ValueError):
            pass
        frames.put(None)

    @staticmethod
    def _next(frames, proc, timeout):
        t0 = time.time()
        while True:
            try:
                data = frames.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None and frames.empty():
                    raise _Dead()
                if time.time() - t0 > timeout:
                    raise RuntimeError("The Qwen voice engine stopped responding.")
                continue
            if data is None:
                raise _Dead()
            return data

    def _kill_locked(self):
        p, self.proc, self.frames = self.proc, None, None
        if p is not None and p.poll() is None:
            try:
                p.stdin.write(struct.pack("<I", 14) + b'{"op": "quit"}')
                p.stdin.flush()
                p.wait(3)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            if p.poll() is None:
                try:
                    p.kill()
                    p.wait(5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if self.log:
            try:
                self.log.close()
            except OSError:
                pass
            self.log = None

    def stop(self):
        with self.cv:
            self._kill_locked()
            self._state, self.error = "stopped", ""
            self.cv.notify_all()

    # -- speech
    def _wait_ready(self, cancelled):
        self.start()
        t0 = time.time()
        with self.cv:
            while self._state == "starting":
                if cancelled and cancelled():
                    raise Stale()
                if time.time() - t0 > READY_TIMEOUT + 30:
                    raise RuntimeError("The Qwen voice engine took too long to start.")
                self.cv.wait(0.25)
            if self._state != "ready":
                raise RuntimeError(self.error or "The Qwen voice engine isn't running.")
            return self.proc, self.frames

    def _request(self, text, speaker, seed, cancelled):
        import numpy as np
        while True:                                  # one request at a time; give up if no longer wanted
            if cancelled and cancelled():
                raise Stale()
            if self.req_lock.acquire(timeout=0.2):
                break
        try:
            proc, frames = self._wait_ready(cancelled)
            if proc is None or proc.poll() is not None:
                self._fail("The Qwen voice engine stopped unexpectedly.")
                proc, frames = self._wait_ready(cancelled)
            if cancelled and cancelled():
                raise Stale()
            req = json.dumps({"op": "synth", "text": text, "speaker": speaker, "language": LANGUAGE,
                              "seed": seed, "max_new_tokens": max_new_tokens(text)}).encode("utf-8")
            try:
                proc.stdin.write(struct.pack("<I", len(req)) + req)
                proc.stdin.flush()
                hdr = json.loads(self._next(frames, proc, SYNTH_TIMEOUT).decode("utf-8"))
                if not hdr.get("ok"):
                    raise RuntimeError("The Qwen voice couldn't read this sentence: " + str(hdr.get("error")))
                audio = np.frombuffer(self._next(frames, proc, 60), dtype="<f4").astype(np.float32)
            except (_Dead, OSError, ValueError):
                self._fail("The Qwen voice engine stopped unexpectedly. It will restart on the next sentence.")
                raise RuntimeError("The Qwen voice engine stopped unexpectedly. It will restart on the next sentence.")
            except RuntimeError as e:
                if "stopped responding" in str(e):
                    self._fail(str(e))
                raise
            return audio
        finally:
            self.req_lock.release()

    def synth(self, text, speaker, cancelled=None):
        """float32 mono 24 kHz numpy array for text. Raises Stale or RuntimeError."""
        import numpy as np
        speaker = (speaker or "").lower()
        if speaker not in SPEAKER_IDS:
            raise RuntimeError(f"Unknown Qwen speaker {speaker!r}.")
        text = " ".join((text or "").split())
        if not re.search(r"\w", text):
            return np.zeros(0, dtype=np.float32)
        parts = [self._synth_one(c, speaker, cancelled) for c in _split(text)]
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def _synth_one(self, text, speaker, cancelled):
        expect = len(text) / CHARS_PER_SEC
        lo, hi = 0.35 * expect, 2.5 * max(expect, 1.2)   # floor: very short lines carry fixed pauses
        seed = _seed(text)
        a = self._request(text, speaker, seed, cancelled)
        dur = a.size / SAMPLE_RATE
        if lo <= dur <= hi:
            return a
        b = self._request(text, speaker, (seed * 7919 + 1) & 0x7FFFFFFF, cancelled)
        bd = b.size / SAMPLE_RATE
        off = lambda d: abs(math.log(max(d, 0.05) / max(expect, 0.3)))
        return b if off(bd) < off(dur) else a


_engine = None
_engine_lock = threading.Lock()


def get_engine(base):
    global _engine
    with _engine_lock:
        if _engine is not None and _engine.base != base:
            _engine.stop()
            _engine = None
        if _engine is None:
            _engine = Engine(base)
        return _engine
