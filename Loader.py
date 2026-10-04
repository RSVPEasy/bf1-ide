"""
Loader.py - pick a mod, and it swaps that mod's .lvl files into
Star Wars Battlefront (2004)'s GameData\\Data folder, then launches the game.

Folders (set once, remembered):
  Game folder     - the install, e.g. ...\\Star Wars Battlefront (Classic 2004)
  Mods folder     - one sub-folder per mod, each holding that mod's modified
                    files (any layout, e.g. just rep.lvl, or SIDE\\rep.lvl)
  Original files  - untouched copies of the game's files, used to put things
                    back when you switch mods or pick "Original game"

A mod file replaces the game file with the same name under GameData\\Data.
Some names exist more than once (bes1.lvl is both the map and its loading
screen, common.lvl/shell.lvl have one per language): then any folders the mod
keeps above the file decide (BES\\bes1.lvl), otherwise the closest file size.
What's installed is recorded in GameData\\ModLoader\\state.json; a replaced
file that has no copy in the Original files folder is backed up there first.

Usage:  python Loader.py
"""
from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "BF1 Mod Loader"


def app_dir() -> Path:
    """Where the config lives: next to the .exe in a release build (a
    one-file .exe unpacks itself to a temp folder), else next to this script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    """A file bundled into the one-file .exe (the icon), else next to it."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled and (Path(bundled) / name).exists():
        return Path(bundled) / name
    return app_dir() / name


CONFIG_PATH = app_dir() / ".bf1_mod_loader.json"
IDE_SETTINGS_PATH = app_dir() / ".bf1_ide_settings.json"  # only read, to match the editor's light/dark theme
STATE_DIR = "ModLoader"
ORIGINAL_GAME = "Original game (no mods)"
STEAM_GAME_DIR = r"steamapps\common\Star Wars Battlefront (Classic 2004)"


# --------------------------------------------------------------------------
# Planning and applying (no UI - testable on its own)
# --------------------------------------------------------------------------

def _is_game_dir(folder: Path) -> bool:
    return (Path(folder) / "GameData" / "Battlefront.exe").exists()


def find_game_dir() -> Path | None:
    """The game folder: the one this loader sits in (the Steam launch option
    setup), else the one Steam passed on the command line ("loader.exe"
    %command% hands it the game's own .exe path), else the Steam install."""
    if _is_game_dir(app_dir()):
        return app_dir()
    for arg in sys.argv[1:]:
        exe = Path(arg)
        if exe.name.lower() == "battlefront.exe" and _is_game_dir(exe.parent.parent):
            return exe.parent.parent
    candidates = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            candidates.append(Path(winreg.QueryValueEx(key, "SteamPath")[0]))
    except OSError:
        pass
    candidates += [Path(r"C:\Program Files (x86)\Steam"), Path(r"C:\Program Files\Steam")]
    for steam in candidates:
        game = steam / STEAM_GAME_DIR
        if (game / "GameData" / "Battlefront.exe").exists():
            return game
    return None


def data_dir(game_dir: Path) -> Path:
    return Path(game_dir) / "GameData" / "Data"


# --------------------------------------------------------------------------
# Randomizer mods: a mod folder holding randomizer.json is re-rolled from the
# original files each time it's installed. Every number in every unit,
# weapon, ordnance and explosion class (entc/wpnc/ordc/expc) gets a random
# multiplier. Each number is rewritten in place with exactly as many
# characters as before (leading zeros allowed: "075.0" reads as 75), so no
# chunk changes size and the file's structure can't be damaged - which is
# also why a value can only grow as far as its digits allow (300.0 tops out
# at 999.9). Self-contained on purpose: the loader needs none of the editor.
# --------------------------------------------------------------------------

RANDOMIZER_FILE = "randomizer.json"
RANDOMIZER_LAST = "randomizer_last.json"  # the seed and counts of the last roll
LOADER_FILES = {RANDOMIZER_FILE, RANDOMIZER_LAST}  # mod-folder files that are never installed
RANDOMIZER_DEFAULTS = {"files": ["SIDE/*.lvl"], "min": 0.5, "max": 2.0, "mode": "safe", "seed": None}
RANDOMIZER_MODES = ("safe", "wild", "chaos")
_CLASS_TAGS = (b"entc", b"wpnc", b"ordc", b"expc")


def engine_hash(name: str) -> int:
    """The game's FNV-1a name hash (each byte ORed with 0x20)."""
    h = 0x811C9DC5
    for b in name.encode("latin-1", "replace"):
        h = ((h ^ (b | 0x20)) * 0x1000193) & 0xFFFFFFFF
    return h


# Left alone unless chaos is on: numbers that would wreck the camera or the
# physics (springs, collision, skeleton size) rather than make a funny game.
_KEEP = {engine_hash(n) for n in (
    "EyePointOffset", "EyePointCenter", "TrackCenter", "TrackOffset", "TiltValue", "MapScale",
    "FirstPersonFOV", "ThirdPersonFOV", "CollisionScale", "CollisionRootScale", "SkeletonRootScale",
    "AddSpringBody", "BodySpringLength", "LiftSpring", "LiftDamp", "LevelSpring", "LevelDamp",
    "VelocitySpring", "VelocityDamp", "OmegaXSpring", "OmegaXDamp", "OmegaZSpring", "OmegaZDamp",
    "BodyOmegaXSpringFactor", "AimerPitchLimits", "AimerYawLimits", "AimerPitchLimts", "AimerYawLimts",
    "PitchLimits", "YawLimits", "SetAltitude", "GravityScale", "WeaponChannel1", "WeaponChannel2",
    "WeaponChannel3", "WeaponChannel4", "HierarchyLevel", "NumWeapons")}
_NUMBER = __import__("re").compile(r"^-?(\d+\.?\d*|\.\d+)$")

# "safe" mode randomizes only these: what you feel in a match. Counts that can
# size memory when a map loads (debris pieces, salvo sizes...), animation and
# physics tuning, and the ~7% of properties nobody has a name for stay stock.
_SAFE = {engine_hash(n) for n in (
    # units and vehicles
    "MaxHealth", "MaxSpeed", "MaxStrafeSpeed", "MaxTurnSpeed", "Acceleraton", "acceleration", "Deceleration",
    "ForwardSpeed", "ReverseSpeed", "StrafeSpeed", "TurnRate", "Traction", "DropItemProbability",
    "WeaponAmmo1", "WeaponAmmo2", "WeaponAmmo3", "WeaponAmmo4",
    # weapons
    "ShotDelay", "ReloadTime", "RoundsPerClip", "HeatPerShot", "HeatRecoverRate", "HeatThreshold",
    "MaxRange", "MinRange", "OptimalRange", "LockTime", "LockOnRange", "LockOnAngle", "KickStrength",
    "MinSpread", "MaxSpread", "ZoomMin", "ZoomMax", "AutoAimSize",
    # shots
    "Velocity", "MaxDamage", "Damage", "LifeSpan", "Gravity", "Rebound", "LaserLength", "LaserWidth",
    "GlowLength", "BlurLength", "LightRadius", "LightColor", "LaserGlowColor",
    # explosions
    "DamageRadius", "DamageRadiusInner", "DamageRadiusOuter", "Push", "PushRadius", "PushRadiusInner",
    "PushRadiusOuter", "Shake", "ShakeLength", "ShakeRadius", "ShakeRadiusInner", "ShakeRadiusOuter",
    "LightDuration")}


def _allowed(mode: str, prop_hash: int) -> bool:
    """Whether a property gets randomized: safe = the gameplay list only, wild =
    everything but the camera/physics list, chaos = everything."""
    if mode == "chaos":
        return True
    if mode == "wild":
        return prop_hash not in _KEEP
    return prop_hash in _SAFE


def _mode(settings: dict) -> str:
    mode = "chaos" if settings.get("chaos") is True else str(settings.get("mode", "safe")).lower()
    if mode not in RANDOMIZER_MODES:
        raise ValueError(f"randomizer.json: mode must be one of {', '.join(RANDOMIZER_MODES)}, not '{mode}'.")
    return mode


def _fit(value: float, width: int, decimals_ok: bool) -> str | None:
    """`value` written in exactly `width` characters with no leading zeros (a
    leading zero can read as octal in C - "08" is 0 there): extra decimals, or
    leading spaces, which every number reader skips."""
    whole_digits = len(str(int(value)))
    if decimals_ok and whole_digits <= width - 2:
        out = f"{value:.{width - whole_digits - 1}f}"
        if len(out) == width:
            return out
    out = str(int(round(value)))
    return out.rjust(width) if len(out) <= width else None


def _reroll(token: str, rng, low: float, high: float, chaos: bool) -> str:
    """One number, randomized and written back in exactly len(token) characters."""
    if not _NUMBER.match(token):
        return token
    x = float(token)
    is_int = "." not in token
    if not chaos and (x <= 0 or (is_int and x <= 1)):
        return token  # 0/1 flags and "none/infinite" markers
    sign, mag = ("-", token[1:]) if token.startswith("-") else ("", token)
    if chaos and x == 0:
        x = rng.uniform(0.0, 1.0)
    new = abs(x) * math.exp(rng.uniform(math.log(low), math.log(high)))
    width = len(mag)
    if mag.startswith("."):  # ".5": stays below 1
        out = f"{min(new, 0.999999):.{width - 1}f}"[1:]
    else:
        largest = 10 ** (width if is_int else max(1, width - 2)) - 1  # biggest whole number that fits
        new = min(new, largest)
        if is_int:
            new = max(1 if abs(x) >= 1 else 0, round(new))
        out = _fit(new, width, not is_int)
    return sign + out if out is not None and len(out) == width else token


def _randomize_props(data: bytearray, start: int, end: int, rng, low, high, mode) -> int:
    """Rerolls the PROP values of one class chunk's payload; returns how many numbers changed."""
    changed, pos = 0, start
    while pos + 8 <= end:
        tag, size = bytes(data[pos:pos + 4]), struct.unpack_from("<I", data, pos + 4)[0]
        block = pos + 8
        if block + size > end:
            break
        if tag == b"PROP" and size > 4 and _allowed(mode, struct.unpack_from("<I", data, block)[0]):
            vstart = block + 4
            nul = data.find(b"\0", vstart, block + size)
            vend = nul if nul != -1 else block + size
            old = bytes(data[vstart:vend]).decode("latin-1")
            tokens = old.split(" ")
            # a colour ("92 136 250 100"): every channel has to stay 0-255
            is_color = 3 <= len(tokens) <= 4 and all(t.isdigit() and int(t) <= 255 for t in tokens)
            rolled = [_reroll(t, rng, low, high, mode == "chaos") for t in tokens]
            if is_color:
                rolled = [r if not r.isdigit() or int(r) <= 255 else "255".rjust(len(r)) for r in rolled]
            new = " ".join(rolled)
            if len(new) == len(old) and new != old:
                data[vstart:vend] = new.encode("latin-1")
                changed += sum(1 for a, b in zip(old.split(" "), new.split(" ")) if a != b)
        pos = block + size + (-(block + size)) % 4
    return changed


def _body_start(data: bytearray, start: int, end: int) -> int:
    """Where a chunk list begins: the first offset that reads as a chunk header."""
    for off in range(start, min(end - 8, start + 128) + 1):
        tag = data[off:off + 4]
        if all(32 <= b < 127 for b in tag) and struct.unpack_from("<I", data, off + 4)[0] <= end - off - 8:
            return off
    return start


def _randomize_chunks(data: bytearray, start: int, end: int, rng, low, high, mode) -> int:
    changed, pos = 0, _body_start(data, start, end)
    while pos + 8 <= end:
        tag, size = bytes(data[pos:pos + 4]), struct.unpack_from("<I", data, pos + 4)[0]
        body, body_end = pos + 8, pos + 8 + size
        if not all(32 <= b < 127 for b in tag) or body_end > end:
            break
        if tag in _CLASS_TAGS:
            changed += _randomize_props(data, body, body_end, rng, low, high, mode)
        elif tag == b"lvl_" and size >= 8:
            # a nested level: [name hash][size of the rest] then its chunks, or a whole ucfb
            if struct.unpack_from("<I", data, body + 4)[0] == size - 8:
                changed += _randomize_chunks(data, body + 8, body_end, rng, low, high, mode)
            elif data[body:body + 4] == b"ucfb":
                changed += _randomize_chunks(data, body + 8, body_end, rng, low, high, mode)
        pos = body_end + (-body_end) % 4
    return changed


def randomize_level(data: bytes, seed: int, low: float = 0.5, high: float = 2.0, mode: str = "safe") -> tuple:
    """(new bytes, numbers changed) - same length as `data`, only class values differ."""
    import random
    if data[:4] != b"ucfb":
        raise ValueError("not a .lvl file")
    out = bytearray(data)
    changed = _randomize_chunks(out, 8, min(len(out), 8 + struct.unpack_from("<I", out, 4)[0]),
                                random.Random(seed), low, high, mode)
    return bytes(out), changed


def randomizer_settings(mod_dir: Path) -> dict | None:
    path = Path(mod_dir) / RANDOMIZER_FILE
    if not path.is_file():
        return None
    settings = dict(RANDOMIZER_DEFAULTS)
    settings.update(json.loads(path.read_text(encoding="utf-8-sig") or "{}"))
    return settings


def prepare_mod(mods_dir: Path, mod: str | None, originals_dir: Path | None) -> dict | None:
    """For a randomizer mod: rolls new files from the originals into the mod
    folder (same relative paths) and returns {seed, files, values}; None for
    an ordinary mod."""
    if not mod:
        return None
    mod_dir = Path(mods_dir) / mod
    settings = randomizer_settings(mod_dir)
    if settings is None:
        return None
    if not originals_dir or not Path(originals_dir).is_dir():
        raise ValueError("A randomizer mod needs the Original files folder - it re-rolls from untouched copies.")
    import random
    seed = settings["seed"] if settings["seed"] is not None else random.randrange(1, 1_000_000)
    base = Path(originals_dir) / "Data" / "_LVL_PC"
    if not base.is_dir():
        base = Path(originals_dir)
    sources = sorted({p for pattern in settings["files"] for p in base.glob(pattern) if p.is_file()})
    if not sources:
        raise ValueError(f"randomizer.json's files ({', '.join(settings['files'])}) match nothing in {base}.")
    total, written = 0, []
    for i, src in enumerate(sources):
        data, n = randomize_level(src.read_bytes(), seed * 1000 + i, float(settings["min"]),
                                  float(settings["max"]), _mode(settings))
        target = mod_dir / src.relative_to(base)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        total += n
        written.append(str(src.relative_to(base)))
    result = {"seed": seed, "files": written, "values": total, "min": settings["min"], "max": settings["max"],
              "mode": _mode(settings)}
    (mod_dir / RANDOMIZER_LAST).write_text(json.dumps(result, indent=2))
    return result


def list_mods(mods_dir: Path, originals_dir: Path | None = None) -> list[str]:
    if not mods_dir or not Path(mods_dir).is_dir():
        return []
    skip = Path(originals_dir).resolve() if originals_dir else None
    out = []
    for d in sorted(Path(mods_dir).iterdir(), key=lambda p: p.name.lower()):
        if d.is_dir() and not d.name.startswith(".") and d.resolve() != skip and any(f.is_file() for f in d.rglob("*")):
            out.append(d.name)
    return out


def _index(root: Path) -> dict[str, list[Path]]:
    """{lower-case file name: [paths]} for every file under root."""
    out: dict[str, list[Path]] = {}
    for p in Path(root).rglob("*"):
        if p.is_file():
            out.setdefault(p.name.lower(), []).append(p)
    return out


def _suffix_score(candidate: Path, hint_parts: tuple) -> int:
    """How many trailing folder names of `candidate` match `hint_parts`."""
    parts = [s.lower() for s in candidate.parent.parts]
    score = 0
    for want, have in zip(reversed([h.lower() for h in hint_parts]), reversed(parts)):
        if want != have:
            break
        score += 1
    return score


def pick(candidates: list[Path], hint_parts: tuple, size: int) -> tuple[Path, bool]:
    """(best match, was it ambiguous) - most matching folder names first,
    then the closest file size."""
    if len(candidates) == 1:
        return candidates[0], False
    ranked = sorted(candidates, key=lambda c: (-_suffix_score(c, hint_parts), abs(c.stat().st_size - size)))
    return ranked[0], True


@dataclass
class Plan:
    mod: str
    copies: list = field(default_factory=list)      # (source, target in game, ambiguous?)
    restores: list = field(default_factory=list)    # (source original or backup, target in game)
    unmatched: list = field(default_factory=list)   # mod files with no same-named game file
    missing_original: list = field(default_factory=list)  # targets with no original copy (will be backed up)


def load_state(game_dir: Path) -> dict:
    path = Path(game_dir) / "GameData" / STATE_DIR / "state.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"mod": None, "files": {}}


def save_state(game_dir: Path, state: dict) -> None:
    folder = Path(game_dir) / "GameData" / STATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(json.dumps(state, indent=2))


def _original_for(target: Path, game_dir: Path, originals: dict[str, list[Path]]) -> Path | None:
    """The untouched copy of a game file: same name in the Original files
    folder, matched by the folders it sits in (then size)."""
    cands = originals.get(target.name.lower(), [])
    if not cands:
        return None
    rel_parts = target.relative_to(data_dir(game_dir)).parent.parts
    best, _ = pick(cands, rel_parts, target.stat().st_size if target.exists() else 0)
    return best


def plan(game_dir: Path, mods_dir: Path, originals_dir: Path | None, mod: str | None) -> Plan:
    """What applying `mod` (None = original game) would do, without touching anything."""
    game_dir = Path(game_dir)
    game_files = _index(data_dir(game_dir))
    originals = _index(originals_dir) if originals_dir and Path(originals_dir).is_dir() else {}
    state = load_state(game_dir)
    result = Plan(mod or ORIGINAL_GAME)

    wanted: dict[Path, Path] = {}
    if mod:
        root = Path(mods_dir) / mod
        for src in sorted(root.rglob("*")):
            if not src.is_file() or src.name in LOADER_FILES:
                continue
            cands = game_files.get(src.name.lower(), [])
            if not cands:
                result.unmatched.append(src)
                continue
            target, ambiguous = pick(cands, src.relative_to(root).parent.parts, src.stat().st_size)
            wanted[target] = src
            result.copies.append((src, target, ambiguous))

    # everything the currently installed mod replaced that the new one doesn't
    for rel, info in state.get("files", {}).items():
        target = data_dir(game_dir) / rel
        if target in wanted:
            continue
        backup = info.get("backup")
        source = Path(backup) if backup else _original_for(target, game_dir, originals)
        if source is not None and source.exists():
            result.restores.append((source, target))
        else:
            result.missing_original.append(target)

    # anything else that doesn't match its original (e.g. a mod copied in by
    # hand before the loader was used) goes back too, so the game ends up as
    # exactly "original files + the chosen mod"
    restoring = {t for _, t in result.restores}
    for cands in game_files.values():
        for target in cands:
            if target in wanted or target in restoring:
                continue
            original = _original_for(target, game_dir, originals)
            if original is not None and original.stat().st_size != target.stat().st_size:
                result.restores.append((original, target))

    for _, target, _ in result.copies:
        rel = str(target.relative_to(data_dir(game_dir)))
        if rel not in state.get("files", {}) and _original_for(target, game_dir, originals) is None:
            result.missing_original.append(target)
    return result


def _same(a: Path, b: Path) -> bool:
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return sa.st_size == sb.st_size and int(sa.st_mtime) == int(sb.st_mtime)


def apply(game_dir: Path, the_plan: Plan, originals_dir: Path | None, progress=lambda done, total, name: None) -> dict:
    """Carries out a Plan. Restores first, then copies the mod in. Files that
    are already identical (same size and date) are skipped. Returns counts."""
    game_dir = Path(game_dir)
    state = load_state(game_dir)
    files = state.setdefault("files", {})
    originals = _index(originals_dir) if originals_dir and Path(originals_dir).is_dir() else {}
    backup_root = game_dir / "GameData" / STATE_DIR / "backup"
    steps = [("restore", s, t) for s, t in the_plan.restores] + [("copy", s, t) for s, t, _ in the_plan.copies]
    copied = skipped = 0
    for i, (kind, src, target) in enumerate(steps):
        progress(i, len(steps), target.name)
        rel = str(target.relative_to(data_dir(game_dir)))
        if kind == "copy" and rel not in files:
            entry = {}
            if _original_for(target, game_dir, originals) is None and target.exists():
                backup = backup_root / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    shutil.copy2(target, backup)
                entry["backup"] = str(backup)
            files[rel] = entry
        if _same(src, target):
            skipped += 1
        else:
            shutil.copy2(src, target)
            copied += 1
        if kind == "restore":
            files.pop(rel, None)
        save_state(game_dir, state)  # after every file, so an interrupted run can still be undone
    state["mod"] = None if the_plan.mod == ORIGINAL_GAME else the_plan.mod
    save_state(game_dir, state)
    progress(len(steps), len(steps), "")
    return {"copied": copied, "skipped": skipped}


def launch(game_dir: Path) -> None:
    exe = Path(game_dir) / "GameData" / "Battlefront.exe"
    subprocess.Popen([str(exe)], cwd=str(exe.parent))


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


# --- look: the same DPI handling and light/dark colors as the editor, but
# self-contained so the loader needs none of the editor's files

THEMES = {
    "light": {"bg": "#f0f0f0", "fg": "#000000", "field_bg": "#ffffff", "select_bg": "#0a5fc4",
              "select_fg": "#ffffff", "border": "#b0b0b0"},
    "dark": {"bg": "#2b2b2d", "fg": "#e6e6e6", "field_bg": "#1e1e1e", "select_bg": "#3d8be8",
             "select_fg": "#ffffff", "border": "#454548"},
}


def enable_dpi_awareness() -> None:
    """Sharp text on scaled displays (125%, 150%...) - must run before Tk starts."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def editor_theme() -> str:
    try:
        theme = json.loads(IDE_SETTINGS_PATH.read_text(encoding="utf-8")).get("theme", "light")
    except (OSError, ValueError, AttributeError):
        return "light"
    return theme if theme in THEMES else "light"


def apply_theme(root: tk.Tk, colors: dict) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(bg=colors["bg"])
    style.configure(".", background=colors["bg"], foreground=colors["fg"],
                    fieldbackground=colors["field_bg"], bordercolor=colors["border"],
                    lightcolor=colors["bg"], darkcolor=colors["bg"])
    for cls in ("TFrame", "TLabel", "TCheckbutton"):
        style.configure(cls, background=colors["bg"], foreground=colors["fg"])
    style.configure("TButton", background=colors["field_bg"], foreground=colors["fg"])
    style.map("TButton", background=[("active", colors["select_bg"])],
              foreground=[("active", colors["select_fg"])])
    for cls in ("TEntry", "TCombobox"):
        style.configure(cls, fieldbackground=colors["field_bg"], foreground=colors["fg"],
                        insertcolor=colors["fg"])
    style.map("TCombobox", fieldbackground=[("readonly", colors["field_bg"])],
              foreground=[("readonly", colors["fg"])])
    style.configure("TScrollbar", background=colors["field_bg"], troughcolor=colors["bg"],
                    bordercolor=colors["border"])


class LoaderApp(tk.Tk):
    def __init__(self):
        enable_dpi_awareness()
        super().__init__()
        try:
            self.iconbitmap(default=str(resource_path("icon.ico")))
        except tk.TclError:
            pass  # no icon file - Tk's default feather
        scale = self.winfo_fpixels("1i") / 96.0
        self.colors = THEMES[editor_theme()]
        apply_theme(self, self.colors)
        self.px = lambda n: max(1, round(n * scale))
        self.title(APP_NAME)
        self.cfg = load_config()
        if not self.cfg.get("game_dir"):
            found = find_game_dir()
            if found:
                self.cfg["game_dir"] = str(found)
        self._busy = False
        self._build()
        self.refresh()

    # -- layout ----------------------------------------------------------------

    def _build(self):
        px = self.px
        body = ttk.Frame(self, padding=px(12))
        body.pack(fill="both", expand=True)
        self.vars = {}
        for row, (key, label) in enumerate((("game_dir", "Game folder"), ("mods_dir", "Mods folder"),
                                            ("originals_dir", "Original files"))):
            ttk.Label(body, text=label + ":").grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=self.cfg.get(key, ""))
            self.vars[key] = var
            ttk.Entry(body, textvariable=var, width=70).grid(row=row, column=1, sticky="ew", padx=6, pady=2)
            ttk.Button(body, text="Browse...", command=lambda k=key: self._browse(k)).grid(row=row, column=2, pady=2)
        ttk.Label(body, text="Mods folder: one sub-folder per mod, holding that mod's changed .lvl files.  "
                             "Original files: untouched copies of the game's .lvl files, used to undo mods.",
                  foreground="gray", wraplength=px(760)).grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 8))
        body.columnconfigure(1, weight=1)

        lists = ttk.Frame(body)
        lists.grid(row=4, column=0, columnspan=3, sticky="nsew")
        body.rowconfigure(4, weight=1)
        left = ttk.Frame(lists)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Choose what to play:").pack(anchor="w")
        self.mod_list = tk.Listbox(left, width=34, height=14, exportselection=False, activestyle="none",
                                   bg=self.colors["field_bg"], fg=self.colors["fg"],
                                   selectbackground=self.colors["select_bg"], selectforeground=self.colors["select_fg"],
                                   highlightthickness=0, font=("TkDefaultFont", 11))
        self.mod_list.pack(fill="y", expand=True, pady=(4, 0))
        self.mod_list.bind("<<ListboxSelect>>", lambda e: self._show_plan())
        self.mod_list.bind("<Double-1>", lambda e: self._play())
        right = ttk.Frame(lists)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        ttk.Label(right, text="What will happen:").pack(anchor="w")
        self.details = tk.Text(right, width=70, height=14, wrap="none", bg=self.colors["field_bg"],
                               fg=self.colors["fg"], highlightthickness=0, font=("Consolas", 9))
        self.details.pack(fill="both", expand=True, pady=(4, 0))

        bar = ttk.Frame(body)
        bar.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.status = tk.StringVar()
        ttk.Label(bar, textvariable=self.status, foreground="gray").pack(side="left")
        self.progress = ttk.Progressbar(bar, length=px(160), mode="determinate")
        self.progress.pack(side="left", padx=(10, 0))
        self.play_btn = ttk.Button(bar, text="Play", command=self._play)
        self.play_btn.pack(side="right")
        self.apply_btn = ttk.Button(bar, text="Install only", command=lambda: self._play(launch_game=False))
        self.apply_btn.pack(side="right", padx=(0, 6))
        self.close_var = tk.BooleanVar(value=self.cfg.get("close_after_launch", True))
        ttk.Checkbutton(bar, text="Close after launching", variable=self.close_var).pack(side="right", padx=(0, 12))

    def _browse(self, key):
        path = filedialog.askdirectory(title="Choose folder", initialdir=self.vars[key].get() or None)
        if path:
            self.vars[key].set(path)
            self.refresh()

    # -- state ------------------------------------------------------------------

    def _dirs(self):
        game = Path(self.vars["game_dir"].get()) if self.vars["game_dir"].get() else None
        mods = Path(self.vars["mods_dir"].get()) if self.vars["mods_dir"].get() else None
        orig = Path(self.vars["originals_dir"].get()) if self.vars["originals_dir"].get() else None
        return game, mods, orig

    def refresh(self):
        game, mods, orig = self._dirs()
        for key, var in self.vars.items():
            self.cfg[key] = var.get()
        save_config(self.cfg)
        self.mod_list.delete(0, "end")
        self.mod_list.insert("end", ORIGINAL_GAME)
        for name in list_mods(mods, orig):
            self.mod_list.insert("end", name)
        installed = load_state(game).get("mod") if game and data_dir(game).is_dir() else None
        choice = self.cfg.get("last_mod") or installed
        names = self.mod_list.get(0, "end")
        index = names.index(choice) if choice in names else 0
        self.mod_list.selection_set(index)
        self.mod_list.see(index)
        if not game or not (Path(game) / "GameData" / "Battlefront.exe").exists():
            self.status.set("Set the Game folder (the one containing GameData\\Battlefront.exe).")
        else:
            self.status.set(f"Installed now: {installed or 'original game'}")
        self._show_plan()

    def _selected(self) -> str | None:
        sel = self.mod_list.curselection()
        name = self.mod_list.get(sel[0]) if sel else ORIGINAL_GAME
        return None if name == ORIGINAL_GAME else name

    def _show_plan(self):
        game, mods, orig = self._dirs()
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        if not game or not data_dir(game).is_dir():
            self.details.insert("end", "Game folder not set.")
            self.details.configure(state="disabled")
            return
        try:
            p = plan(game, mods, orig, self._selected())
        except Exception as exc:
            self.details.insert("end", f"Can't plan this: {exc}")
            self.details.configure(state="disabled")
            return
        base = data_dir(game)
        lines = []
        for src, target, ambiguous in p.copies:
            lines.append(f"install  {src.name:28s} -> {target.relative_to(base)}" + ("   (picked by folder/size)" if ambiguous else ""))
        for src, target in p.restores:
            lines.append(f"restore  {str(target.relative_to(base)):28s} (back to original)")
        for src in p.unmatched:
            lines.append(f"skip     {src.name:28s} - no file with this name in the game")
        for target in p.missing_original:
            lines.append(f"note     {target.relative_to(base)} has no copy in Original files - it gets backed up first")
        settings = randomizer_settings(Path(mods) / self._selected()) if self._selected() else None
        if settings is not None:
            lines = [f"RANDOMIZER - every Play re-rolls {', '.join(settings['files'])} from the original files:",
                     f"every number in every unit, weapon, ordnance and explosion class x{settings['min']} to "
                     f"x{settings['max']}, mode {settings.get('mode', 'safe')}"
                     + (" (CHAOS: nothing held back)" if settings.get("chaos") is True else "") + ".",
                     f"seed: {settings['seed'] if settings['seed'] is not None else 'new each time'}", ""] + lines
        elif not lines:
            lines.append("Nothing to change - the game already has these files.")
        self.details.insert("end", "\n".join(lines))
        self.details.configure(state="disabled")

    # -- play ---------------------------------------------------------------------

    def _play(self, launch_game: bool = True):
        if self._busy:
            return
        game, mods, orig = self._dirs()
        if not game or not (Path(game) / "GameData" / "Battlefront.exe").exists():
            messagebox.showerror(APP_NAME, "Set the Game folder first (it contains GameData\\Battlefront.exe).")
            return
        mod = self._selected()
        randomizer = bool(mod) and randomizer_settings(Path(mods) / mod) is not None
        try:
            the_plan = None if randomizer else plan(game, mods, orig, mod)  # a randomizer plans after rolling
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.cfg["last_mod"] = mod or ORIGINAL_GAME
        self.cfg["close_after_launch"] = self.close_var.get()
        save_config(self.cfg)
        self._busy = True
        for b in (self.play_btn, self.apply_btn):
            b.configure(state="disabled")
        box = {}

        def progress(done, total, name):
            box["progress"] = (done, total, name)

        def work():
            try:
                plan_now = the_plan
                if randomizer:
                    box["progress"] = (0, 1, "")
                    box["rolled"] = prepare_mod(mods, mod, orig)
                    plan_now = plan(game, mods, orig, mod)
                box["result"] = apply(game, plan_now, orig, progress)
            except Exception as exc:
                box["error"] = exc

        worker = threading.Thread(target=work, daemon=True)
        worker.start()

        def poll():
            if "progress" in box:
                done, total, name = box["progress"]
                self.progress.configure(maximum=max(total, 1), value=done)
                self.status.set(f"Copying {name}..." if name else "Done.")
            if worker.is_alive():
                self.after(80, poll)
                return
            self._busy = False
            for b in (self.play_btn, self.apply_btn):
                b.configure(state="normal")
            if "error" in box:
                messagebox.showerror(APP_NAME, f"Couldn't install: {box['error']}\n\n"
                                               "If the game is running, close it and try again.")
                self.refresh()
                return
            r = box["result"]
            rolled = box.get("rolled")
            self.status.set(f"Installed {mod or 'original game'} ({r['copied']} copied, {r['skipped']} already there)."
                            + (f"  Randomized {rolled['values']:,} values, seed {rolled['seed']}." if rolled else ""))
            if launch_game:
                try:
                    launch(game)
                except OSError as exc:
                    messagebox.showerror(APP_NAME, f"Couldn't start the game: {exc}")
                    return
                if self.close_var.get():
                    self.after(500, self.destroy)
                    return
            self.refresh()

        self.after(80, poll)


if __name__ == "__main__":
    LoaderApp().mainloop()
