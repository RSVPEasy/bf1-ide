"""Checks bf1_core's Lua compiler against the stock luac 4.0.1 (not shipped - point
--luac at one): every .lua given is compiled by both, in luac's own format
(8-byte size_t and doubles), and the bytes must match exactly.

  python tools/check_lua_compiler.py --luac luac_32.exe file.lua [more.lua | folder ...]
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bf1_core as core  # noqa: E402


def disassemble(data: bytes) -> list:
    """One line per function field, constant and instruction, to show where two chunks differ."""
    _h, main = core.lua_read_chunk(data)
    lines = []

    def walk(fn, path):
        lines.append(f"{path} params={fn.number_of_params} vararg={fn.is_variadic} stack={fn.max_stack_size}")
        lines.append(f"{path} locals={[(l.name, l.start_pc, l.end_pc) for l in fn.locals]}")
        lines.append(f"{path} lines={fn.lines}")
        lines.extend(f"{path} str {s!r}" for s in fn.globals)
        lines.extend(f"{path} num {n!r}" for n in fn.numbers)
        for k, ins in enumerate(fn.instructions):
            op = ins & 63
            lines.append(f"{path} [{k}] {core.OPCODES.get(op)} u={ins >> 6} a={ins >> 15} b={(ins >> 6) & 511}")
        for k, sub in enumerate(fn.functions):
            walk(sub, f"{path}/{k}")

    walk(main, "main")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--luac", required=True)
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args()
    args.luac = str(Path(args.luac).resolve())
    files = []
    for p in map(Path, args.paths):
        files += sorted(p.rglob("*.lua")) if p.is_dir() else [p]
    ok = bad = skipped = 0
    tmp = Path(tempfile.mkdtemp())
    for f in files:
        for strip in (True, False):
            out = tmp / "ref.out"
            r = subprocess.run([args.luac] + (["-s"] if strip else []) + ["-o", str(out), f.name],
                               cwd=f.parent, capture_output=True, text=True)
            if r.returncode:
                skipped += 1
                print(f"skip {f.name}: luac says {r.stderr.strip()[:120]}")
                break
            ref = out.read_bytes()
            try:
                mine = core.lua_compile(f.read_bytes(), "@" + f.name, strip=strip, size_t=8, number_size=8)
            except core.LuaSyntaxError as exc:
                bad += 1
                print(f"FAIL {f.name}: {exc}")
                break
            if mine != ref:
                bad += 1
                i = next((k for k in range(min(len(mine), len(ref))) if mine[k] != ref[k]), min(len(mine), len(ref)))
                print(f"DIFF {f.name} (strip={strip}) at byte {i}: mine {len(mine)} bytes, luac {len(ref)}")
                a, b = disassemble(mine), disassemble(ref)
                k = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
                for line in b[max(0, k - 4):k]:
                    print("      ", line)
                print("  mine", a[k] if k < len(a) else "(end)")
                print("  luac", b[k] if k < len(b) else "(end)")
                break
            ok += 1
    print(f"{ok} matched, {bad} differ, {skipped} skipped")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
