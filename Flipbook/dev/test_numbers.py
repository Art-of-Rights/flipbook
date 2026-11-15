r"""How the AI voice will say numbers (v1.8.13): tts.normalize + the espeak-ng phonemes.

Run:  ..\.venv\Scripts\python.exe dev\test_numbers.py      (from Flipbook\; needs no voice add-on)

Before 1.8.13, phonemize() split "4,000" at its comma and read "four, zero zero zero".
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tts  # noqa: E402

# text -> (expected normalize() output, words the phonemes must contain, words they must not)
CASES = [
    ("4,000", "4000", ["θˈaʊzənd"], ["zˈiəɹoʊ"]),
    ("1,500 men", "1500 men", ["θˈaʊzənd", "hˈʌndɹɪd"], ["wˈʌn,"]),
    ("1,000,000", "1000000", ["mˈɪliən"], ["zˈiəɹoʊ"]),
    ("12,345 souls", "12345 souls", ["θˈaʊzənd"], []),
    ("3.5 feet", "3 point 5 feet", ["pˈɔɪnt"], []),
    # money (v1.8.14): dollars and cents
    ("$1,250.75", "1250 dollars and 75 cents", ["θˈaʊzənd", "dˈɑːlɚz", "sˈɛnts"], ["pˈɔɪnt"]),
    ("$1", "1 dollar", ["dˈɑːlɚ"], ["dˈɑːlɚz"]),
    ("$5.", "5 dollars.", ["dˈɑːlɚz."], []),
    ("$0.50", "50 cents", ["fˈɪfti sˈɛnts"], ["dˈɑːlɚ"]),
    ("$3.01", "3 dollars and 1 cent", ["sˈɛnt"], ["sˈɛnts"]),
    ("$1850 a year", "1850 dollars a year", ["θˈaʊzənd"], ["ˈeɪtiːn fˈɪfti"]),
    ("$4.5 million", "4 point 5 million dollars", ["mˈɪliən dˈɑːlɚz"], []),
    ("$2,000,000", "2000000 dollars", ["mˈɪliən dˈɑːlɚz"], []),
    ("in 1717 he", "in 17 17 he", ["sˈɛvəntˌiːn sˈɛvəntˌiːn"], []),     # years unchanged
    ("In 1850, 3,000 copies", "In 18 50, 3000 copies", ["θˈaʊzənd"], []),
    ("1, 2, 3", "1, 2, 3", ["wˈʌn,"], []),                              # lists unchanged
    ("4,000.", "4000.", ["θˈaʊzənd."], []),
]

es = tts._Espeak()
bad = 0
for text, want, has, hasnt in CASES:
    n = tts.normalize(text)
    ph = es.phonemize(n, "en-us")
    ok = n == want and all(h in ph for h in has) and not any(h in ph for h in hasnt)
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {text!r:24} -> {n!r:24} -> {ph}")
print("all passed" if not bad else f"{bad} FAILED")
sys.exit(1 if bad else 0)
