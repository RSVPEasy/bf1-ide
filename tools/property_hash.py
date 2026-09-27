"""Check guesses for the game's unnamed property hashes.

Usage:  python tools/property_hash.py Name [Name ...]

Prints each name's hash (FNV-1a, 32-bit, of the lowercased name - the
same hash the game uses), whether the editor already knows it, and whether
it matches one of the hashes listed in docs/UNKNOWN_PROPERTIES.md.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bf1_core as core  # noqa: E402


def main(names: list) -> int:
    if not names:
        print(__doc__)
        return 1
    listed = set(re.findall(r"`(0x[0-9a-f]{8})`", (ROOT / "docs" / "UNKNOWN_PROPERTIES.md").read_text(encoding="utf-8")))
    hits = 0
    for name in names:
        key = f"0x{core.fnv1a32(name):08x}"
        if core.fnv1a32(name) in core.HASH_TO_NAME:
            note = f"already known (as {core.HASH_TO_NAME[core.fnv1a32(name)]})"
        elif key in listed:
            note = "MATCH - this is one of the unknown hashes!"
            hits += 1
        else:
            note = "no match"
        print(f"{name:32s} {key}  {note}")
    return 0 if hits else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
