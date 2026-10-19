"""The repository's own cancellation checks — what a conventional test suite
asserts about stop(). Run: python selfcheck.py  (prints SELFCHECK <json>).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from stopproof.livedemo import selfcheck_main

if __name__ == "__main__":
    selfcheck_main(Path(__file__).resolve().parent)
