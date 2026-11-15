# Flipbook

A PDF reader for Windows that reads any PDF like a real book: two pages side by side, turned with a 3D
page-flip animation. It runs entirely on your computer and works offline. Nothing is uploaded.

## Features

- **Book view** with page-flip animation, drag to turn, a slider to jump anywhere, and a close-up view.
- **Table of contents** built for every book, and **page links**: click a printed contents line or a
  link to go to its page.
- **Bookmarks** with ribbon colours, and Flipbook remembers where you stopped reading.
- **Read aloud** with captions and a reading highlight, using the Windows voices or an optional
  natural, human-sounding AI voice (Kokoro, runs on your PC; a graphics card can speed it up).
- **Word look-up**: right-click any word for its meaning, word origin (etymology) and pronunciation.
- **Legal dictionary**: a switch between Etymology and ⚖ Legal. Black's Law Dictionary, 2nd edition
  (1910, public domain) is a free download; if you own the 4th or 5th edition as PDFs, Flipbook can
  read them on your PC.
- **Your own dictionaries**: add any dictionary you have as a PDF, legal or general. Flipbook works out
  how its entries are laid out by itself.
- **Text recognition (OCR)** for scanned books, light and dark themes, full screen, keyboard shortcuts.

The full user manual is inside the app: click **ⓘ About** in the tools bar.

## Download

Get `Flipbook.exe` from the [Releases](../../releases) page. It is a single file: no installer and no
Python needed. Put it in any folder you can write to (for example Documents\Flipbook) and run it.

- **Windows SmartScreen:** Flipbook.exe is not code-signed, so Windows may warn you the first time.
  Click "More info" > "Run anyway".
- **Check your download:** each release has a `Flipbook - SHA-256 checksum.txt` file. In PowerShell,
  in the folder with the exe, run `Get-FileHash .\Flipbook.exe -Algorithm SHA256` and compare the
  result with the checksum in that file.

## System requirements

- Windows 10 or 11, 64-bit.
- Microsoft Edge WebView2 Runtime (already part of Windows 11 and up-to-date Windows 10).
- About 66 MB for the app. Optional add-ons are downloaded only if you choose them, in 🧩 Add-ons.

## Add-ons and your own books

Optional add-ons (the AI voice, the Dictionary, Black's 2nd edition) are downloaded from their original
sources and checked against fixed checksums. Dictionaries you add from your own PDFs are built on your
PC and never leave it. **No dictionary text and no books are included in this repository**: the Black's
4th and 5th editions, and any dictionary you add, are copyrighted works that each user must own.

## Building from source

Flipbook is Python 3.13 (with pywebview and Edge WebView2) and one HTML page, `Flipbook/flipbook.html`.

1. Install Python 3.13 (64-bit) from python.org.
2. In the repository folder, create the environment next to the `Flipbook` folder and install the
   pinned packages:

   ```powershell
   py -3.13 -m venv .venv
   .venv\Scripts\python.exe -m pip install --only-binary ":all:" --no-binary proxy_tools -r Flipbook\dev\requirements-dev.txt
   ```

3. Run it from source: `.venv\Scripts\python.exe Flipbook\app.py`
4. Build the exe: run `Flipbook\build.bat` (it uses PyInstaller and `Flipbook.spec`).

Tests and developer tools are in `Flipbook/dev/` (for example `test_security.py`, `test_mydict.py`,
`test_numbers.py`). `test_mydict.py --real <folder>` also tries your own dictionary PDFs, listed in
`Flipbook/dev/real_books.json` (not included; the test explains its format).

## Licence

Flipbook bundles third-party software, including eSpeak NG (GNU GPL version 3), PDF.js and
Tesseract.js (Apache 2.0), pywebview and pypdf (BSD), and others. The complete list with each licence is
in the manual, section 18 ("Third-party software and licenses"), shown in the app under ⓘ About.
See the `LICENSE` file for Flipbook's own licence.
