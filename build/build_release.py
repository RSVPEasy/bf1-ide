"""Builds the release .exes - the same steps the GitHub release workflow runs.

Usage:  python build/build_release.py        (needs: pip install pyinstaller)

1. copies the tools' .py files to build/stripped/ with comments and
   docstrings removed (build/strip_comments.py)
2. PyInstaller turns each program into a single .exe in dist/:
   - "BF1 Level Editor.exe" (bf1_ide.py), with swbf-unmunge.exe and the icon
     bundled inside
   - "BF1 Mod Loader.exe" (Loader.py)
3. copies the licenses next to them
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRIPPED = ROOT / "build" / "stripped"
WORK = ROOT / "build" / "pyinstaller"
DIST = ROOT / "dist"

PROGRAMS = [  # (script, exe name, extra bundled files)
    ("bf1_ide.py", "BF1 Level Editor", ["swbf-unmunge.exe", "icon.ico"]),
    ("Loader.py", "BF1 Mod Loader", ["icon.ico"]),
]


# Packages PyInstaller would otherwise sweep in because Pillow/scipy/numpy can
# optionally use them - none are needed, and they make the build slow and big.
EXCLUDE = ["matplotlib", "pandas", "IPython", "jupyter", "notebook", "pytest", "setuptools", "pkg_resources",
           "imageio", "dateutil", "gi", "certifi", "sympy", "numba", "PyQt5", "PyQt6", "PySide2", "PySide6",
           "wx", "cv2", "docutils", "sphinx", "yaml", "jinja2", "pygments",
           # scipy's optional array backends - a machine with these installed would
           # otherwise pack gigabytes of machine-learning libraries into the editor
           "torch", "torchvision", "torchaudio", "tensorflow", "tensorboard", "jax", "jaxlib", "cupy", "dask",
           "sparse", "sklearn", "transformers", "onnxruntime"]


def run(*args):
    print(">", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], check=True)


def main() -> int:
    for folder in (STRIPPED, WORK, DIST):
        shutil.rmtree(folder, ignore_errors=True)
    run(sys.executable, ROOT / "build" / "strip_comments.py", ROOT, STRIPPED)
    for script, name, extras in PROGRAMS:
        missing = [f for f in extras if not (ROOT / f).exists()]
        if missing:
            raise SystemExit(f"Missing {', '.join(missing)} - put it next to {script}.")
        args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
                "--name", name, "--icon", ROOT / "icon.ico",
                "--distpath", DIST, "--workpath", WORK, "--specpath", WORK,
                "--paths", STRIPPED]
        for module in EXCLUDE:
            args += ["--exclude-module", module]
        for f in extras:
            args += ["--add-data", f"{ROOT / f}{';' if sys.platform == 'win32' else ':'}."]
        run(*args, STRIPPED / script)
    shutil.copy(ROOT / "third_party" / "swbf-unmunge-LICENSE.txt", DIST)
    if (ROOT / "LICENSE").exists():
        shutil.copy(ROOT / "LICENSE", DIST / "LICENSE.txt")
    print("\nBuilt:")
    for f in sorted(DIST.iterdir()):
        print(f"  {f.name}  ({f.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
