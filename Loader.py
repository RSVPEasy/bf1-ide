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
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import ctypes
import ctypes.wintypes as W
import re
import struct
import time
from tkinter import messagebox, simpledialog, ttk

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


# Files a mod folder can hold that are never installed (`bf1_cli.py randomize`'s roll report).
RANDOMIZER_LAST = "randomizer_last.json"
LOADER_FILES = {RANDOMIZER_LAST}


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
    style.configure("Treeview", background=colors["field_bg"], fieldbackground=colors["field_bg"],
                    foreground=colors["fg"])
    style.map("Treeview", background=[("selected", colors["select_bg"])],
              foreground=[("selected", colors["select_fg"])])
    style.configure("Treeview.Heading", background=colors["bg"], foreground=colors["fg"])
    style.map("Treeview.Heading", background=[("active", colors["field_bg"])])
    style.configure("TNotebook", background=colors["bg"], bordercolor=colors["border"])
    style.configure("TNotebook.Tab", background=colors["bg"], foreground=colors["fg"])
    style.map("TNotebook.Tab", background=[("selected", colors["field_bg"])],
              foreground=[("selected", colors["fg"])])


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
        ttk.Button(bar, text="Cheats...", command=self._cheats).pack(side="right", padx=(0, 18))
        ttk.Button(bar, text="Make Debug Menu", command=self._make_debug_menu).pack(side="right", padx=(0, 6))
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
        if not lines:
            lines.append("Nothing to change - the game already has these files.")
        self.details.insert("end", "\n".join(lines))
        self.details.configure(state="disabled")

    # -- play ---------------------------------------------------------------------

    def _cheats(self):
        """The cheats / memory window (CheatsWindow), for the running game."""
        if getattr(self, "_cheats_win", None) is not None and self._cheats_win.winfo_exists():
            self._cheats_win.lift()
            return
        self._cheats_win = CheatsWindow(self, self.colors)

    def _make_debug_menu(self):
        """Builds the Debug Menu mod (Fake Console + Free Camera in the pause menu) from Original files."""
        game, mods, orig = self._dirs()
        if not mods or not orig:
            messagebox.showerror(APP_NAME, "Set the Mods folder and Original files first.")
            return
        import bf1_core
        out = Path(mods) / bf1_core.DEBUG_MENU_MOD
        if out.exists() and not messagebox.askyesno(APP_NAME, f"'{out.name}' is already in the Mods folder. "
                                                              "Build it again over the top?"):
            return
        try:
            bf1_core.make_debug_menu(orig, out)
        except (ValueError, OSError) as exc:
            messagebox.showerror(APP_NAME, f"Couldn't build the Debug Menu mod:\n{exc}")
            return
        self.cfg["last_mod"] = out.name
        self.refresh()
        messagebox.showinfo(APP_NAME, "Made the Debug Menu mod - it's selected, press Play.\n\n"
                                      "Pause in a battle for Fake Console and Free Camera. For the console's "
                                      "commands to work, open Cheats... once the game is running and tick "
                                      "'Fake console fix'.")

    def _play(self, launch_game: bool = True):
        if self._busy:
            return
        game, mods, orig = self._dirs()
        if not game or not (Path(game) / "GameData" / "Battlefront.exe").exists():
            messagebox.showerror(APP_NAME, "Set the Game folder first (it contains GameData\\Battlefront.exe).")
            return
        mod = self._selected()
        try:
            the_plan = plan(game, mods, orig, mod)
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
                box["result"] = apply(game, the_plan, orig, progress)
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
            self.status.set(f"Installed {mod or 'original game'} ({r['copied']} copied, {r['skipped']} already there).")
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


# ==================================================================================================
# bf1_memory - merged in from bf1_memory.py
# ==================================================================================================
# bf1_memory.py - live memory access to the running game, a memory scanner, and
# a Gecko-style cheat code engine (the code system Wii/GameCube cheats use,
# adapted to the PC game).
#
# Addresses
# ---------
# The game loads at a different address every run, so codes and the viewer use
# offsets from where Battlefront.exe was loaded ("ba", the base address):
# `Battlefront.exe+4E6D53`, or just `+4E6D53`. Absolute addresses work in the
# viewer as plain hex (`0x00746D53`), and `[+4E6D53]+10` reads a pointer there
# and adds 0x10.
#
# Cheat codes
# -----------
# A cheat is a `$Name` line followed by code lines of two 8-digit hex words,
# like Gecko codes. The first byte picks the code type; the other 6 digits are
# an offset from ba (or from po, the pointer, for the 1x/3x/5x types). An odd
# code type adds 0x1000000 to the offset, as in Gecko.
#
#   00XXXXXX NNNN00VV   write the byte VV, NNNN+1 times
#   02XXXXXX NNNNVVVV   write the 16-bit VVVV, NNNN+1 times
#   04XXXXXX VVVVVVVV   write the 32-bit VVVVVVVV (an int or a float's bits)
#   06XXXXXX NNNNNNNN   write the NNNNNNNN bytes on the following lines, in order
#   10..16              the same, at po + XXXXXX
#   20XXXXXX VVVVVVVV   if the 32-bit value at XXXXXX == V ... (until E0/E2)
#   22 / 24 / 26        if != / > / < (unsigned)
#   30..36              the same tests at po + XXXXXX
#   48XXXXXX 00000000   po = the 32-bit value at ba + XXXXXX (follow a pointer)
#   58XXXXXX 00000000   po = the 32-bit value at po + XXXXXX
#   4AXXXXXX 00000000   po = ba + XXXXXX
#   E2000000 000000NN   end the last NN ifs
#   E0000000 00000000   end all ifs, po = ba
#
# Values are written little-endian, the way the PC stores them, so
# `04XXXXXX 000003E8` writes the number 1000. Enabled cheats are re-applied
# several times a second, like Gecko applies codes every frame. A write that
# would change nothing is skipped, and writes into the game's code (patches)
# are allowed. Lines starting with # or * are comments.
#
# Single player only. These codes change the game in memory while it runs;
# nothing on disk changes.


GAME_EXE_NAME = "battlefront.exe"
KNOWN_BUILD = (0x5F243336, 0x2224000)   # 1.3 (Steam): PE timestamp, image size - the codes were made for it

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
PROCESS_ACCESS = 0x0008 | 0x0010 | 0x0020 | 0x0400  # VM_OPERATION | VM_READ | VM_WRITE | QUERY_INFORMATION
PAGE_EXECUTE_READWRITE = 0x40
MEM_COMMIT = 0x1000
WRITABLE = {0x04, 0x08, 0x40, 0x80}  # READWRITE, WRITECOPY, EXECUTE_READWRITE, EXECUTE_WRITECOPY
PAGE_GUARD = 0x100


class GameMemoryError(Exception):
    pass


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", W.DWORD), ("PartitionId", W.WORD), ("RegionSize", ctypes.c_size_t),
                ("State", W.DWORD), ("Protect", W.DWORD), ("Type", W.DWORD)]


def find_pid():
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE_NAME}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == GAME_EXE_NAME:
            return int(parts[1])
    return None


class Game:
    """The running game: read, write, scan."""

    def __init__(self, pid: int):
        self.pid = pid
        self.h = _k32.OpenProcess(PROCESS_ACCESS, False, pid)
        if not self.h:
            raise GameMemoryError(f"Can't open the game's process (error {ctypes.get_last_error()}). "
                               "Running the tool as administrator can help.")
        mods = (ctypes.c_void_p * 1024)()
        needed = W.DWORD()
        _psapi.EnumProcessModulesEx(self.h, mods, ctypes.sizeof(mods), ctypes.byref(needed), 0x03)
        name = ctypes.create_unicode_buffer(260)
        self.base = None
        for m in mods[:needed.value // ctypes.sizeof(ctypes.c_void_p)]:
            _psapi.GetModuleBaseNameW(self.h, ctypes.c_void_p(m), name, 260)
            if name.value.lower() == GAME_EXE_NAME:
                self.base = m
                break
        if self.base is None:
            self.close()
            raise GameMemoryError("Couldn't find Battlefront.exe in the game's process.")
        head = self.read(self.base, 0x400)
        pe = struct.unpack_from("<I", head, 0x3C)[0] if len(head) >= 0x40 else 0
        self.build = (struct.unpack_from("<I", head, pe + 8)[0], struct.unpack_from("<I", head, pe + 0x50)[0])             if pe and pe + 0x54 <= len(head) else (0, 0)
        self.known_build = self.build == KNOWN_BUILD

    @classmethod
    def attach(cls):
        pid = find_pid()
        if pid is None:
            raise GameMemoryError("The game isn't running.")
        return cls(pid)

    def alive(self) -> bool:
        code = W.DWORD()
        return bool(_k32.GetExitCodeProcess(self.h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE

    def close(self):
        if self.h:
            _k32.CloseHandle(self.h)
            self.h = None

    # -- raw access

    def read(self, addr: int, n: int) -> bytes:
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t()
        _k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got))
        return buf.raw[:got.value]

    def write(self, addr: int, data: bytes) -> None:
        if self.read(addr, len(data)) == data:
            return
        done = ctypes.c_size_t()
        p = ctypes.c_void_p(addr)
        if _k32.WriteProcessMemory(self.h, p, data, len(data), ctypes.byref(done)) and done.value == len(data):
            return
        old = W.DWORD()  # code pages are read-only: unprotect, write, restore
        if not _k32.VirtualProtectEx(self.h, p, len(data), PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
            raise GameMemoryError(f"Can't write at 0x{addr:08X} (error {ctypes.get_last_error()}).")
        ok = _k32.WriteProcessMemory(self.h, p, data, len(data), ctypes.byref(done))
        _k32.VirtualProtectEx(self.h, p, len(data), old.value, ctypes.byref(W.DWORD()))
        _k32.FlushInstructionCache(self.h, p, len(data))
        if not ok:
            raise GameMemoryError(f"Can't write at 0x{addr:08X} (error {ctypes.get_last_error()}).")

    def u32(self, addr: int) -> int:
        b = self.read(addr, 4)
        if len(b) < 4:
            raise GameMemoryError(f"Can't read 0x{addr:08X}.")
        return struct.unpack("<I", b)[0]

    def regions(self, writable_only=True):
        """(start, size) of committed memory regions."""
        addr, mbi = 0, MEMORY_BASIC_INFORMATION()
        while addr < 0x7FFF0000:
            if not _k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            start, size = mbi.BaseAddress or 0, mbi.RegionSize
            if mbi.State == MEM_COMMIT and not (mbi.Protect & PAGE_GUARD) and \
                    (not writable_only or (mbi.Protect & 0xFF) in WRITABLE):
                yield start, size
            addr = start + size

    # -- addresses

    def describe(self, addr: int) -> str:
        if self.base <= addr < self.base + 0x2400000:
            return f"Battlefront.exe+{addr - self.base:X}"
        return f"{addr:08X}"

    def resolve(self, text: str) -> int:
        """`+4E6D53`, `Battlefront.exe+4E6D53`, `0x746D53`, `[+4E6D53]+10`."""
        t = text.strip().replace(" ", "")
        if not t:
            raise GameMemoryError("No address.")
        m = re.fullmatch(r"\[(.+)\]([+-][0-9A-Fa-fx]+)?", t)
        if m:
            ptr = self.u32(self.resolve(m.group(1)))
            return ptr + (int(m.group(2).replace("0x", ""), 16) if m.group(2) else 0)
        t = re.sub(r"(?i)^battlefront\.exe", "", t)
        try:
            if t.startswith("+"):
                return self.base + int(t[1:], 16)
            return int(t, 16)
        except ValueError:
            raise GameMemoryError(f"'{text}' isn't an address (try +4E6D53 or 0x00746D53).") from None


# --- values ------------------------------------------------------------------------------

MEM_TYPES = {"byte": ("<B", 1), "2 bytes": ("<H", 2), "4 bytes": ("<i", 4), "float": ("<f", 4), "double": ("<d", 8)}


def mem_pack(kind: str, text: str) -> bytes:
    fmt, _ = MEM_TYPES[kind]
    v = float(text) if kind in ("float", "double") else int(text, 0)
    if kind == "4 bytes" and v > 0x7FFFFFFF:
        v -= 1 << 32
    return struct.pack(fmt, v)


def mem_unpack(kind: str, data: bytes):
    fmt, size = MEM_TYPES[kind]
    return struct.unpack(fmt, data[:size])[0] if len(data) >= size else None


# --- the scanner -------------------------------------------------------------------------

class Scanner:
    """Cheat-Engine-style search: a first scan (an exact value, or 'unknown' to snapshot
    everything), then next scans that narrow the results down."""

    MAX_UNKNOWN_BYTES = 768 * 1024 * 1024

    def __init__(self, game: Game, kind: str):
        import numpy as np
        self.np = np
        self.game, self.kind = game, kind
        self.dtype = {"byte": np.uint8, "2 bytes": np.uint16, "4 bytes": np.int32, "float": np.float32,
                      "double": np.float64}[kind]
        self.size = MEM_TYPES[kind][1]
        self.results = None   # list of (region start, numpy offsets, numpy previous values)

    def _region_values(self, start, size):
        data = self.game.read(start, size)
        n = len(data) // self.size
        return self.np.frombuffer(data[:n * self.size], dtype=self.dtype) if n else None

    def _equal(self, values, target):
        if self.kind in ("float", "double"):
            return self.np.abs(values - target) <= max(abs(target) * 1e-4, 1e-3)
        return values == target

    def first(self, mode: str, value: str = "", progress=None) -> int:
        np = self.np
        target = mem_unpack(self.kind, mem_pack(self.kind, value)) if mode == "exact" else None
        regions = list(self.game.regions())
        total = sum(s for _, s in regions)
        if mode == "unknown" and total > self.MAX_UNKNOWN_BYTES:
            raise GameMemoryError(f"An unknown-value scan would need {total // 2**20} MB - search for an exact value first.")
        self.results = []
        done = 0
        for start, size in regions:
            vals = self._region_values(start, size)
            done += size
            if progress:
                progress(done / total)
            if vals is None:
                continue
            if mode == "exact":
                idx = np.nonzero(self._equal(vals, target))[0]
                if len(idx):
                    self.results.append((start, idx, vals[idx].copy()))
            else:
                self.results.append((start, None, vals.copy()))
        return self.count()

    def next(self, mode: str, value: str = "") -> int:
        np = self.np
        if self.results is None:
            raise GameMemoryError("Do a first scan.")
        target = mem_unpack(self.kind, mem_pack(self.kind, value)) if mode == "exact" else None
        out = []
        for start, idx, prev in self.results:
            if idx is None:
                vals = self._region_values(start, len(prev) * self.size)
                if vals is None or len(vals) != len(prev):
                    continue
                cur, offsets = vals, np.arange(len(vals))
            else:
                cur = np.array([mem_unpack(self.kind, self.game.read(start + int(i) * self.size, self.size)) or 0
                                for i in idx], dtype=self.dtype) if len(idx) < 4096 else \
                    self._region_values(start, int(idx.max() + 1) * self.size)[idx]
                offsets = idx
            keep = {"exact": lambda: self._equal(cur, target), "changed": lambda: cur != prev,
                    "unchanged": lambda: cur == prev, "increased": lambda: cur > prev,
                    "decreased": lambda: cur < prev}[mode]()
            sel = np.nonzero(keep)[0]
            if len(sel):
                out.append((start, offsets[sel], cur[sel].copy()))
        self.results = out
        return self.count()

    def count(self) -> int:
        return sum(len(prev) for _s, _i, prev in self.results or [])

    def addresses(self, limit=1000):
        out = []
        for start, idx, prev in self.results or []:
            offs = idx if idx is not None else range(len(prev))
            for k, o in enumerate(offs):
                out.append((start + int(o) * self.size, prev[k].item()))
                if len(out) >= limit:
                    return out
        return out


# --- cheat codes -------------------------------------------------------------------------

@dataclass
class Cheat:
    name: str
    lines: list = field(default_factory=list)   # [(word1, word2)]
    note: str = ""
    enabled: bool = False

    def text(self) -> str:
        return "\n".join(f"{a:08X} {b:08X}" for a, b in self.lines)


class CheatCodeError(Exception):
    pass


def parse_code_lines(text: str) -> list:
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#")[0].strip()
        if not line or line.startswith("*"):
            continue
        words = re.findall(r"[0-9A-Fa-f]{8}", line)
        if len(words) != 2 or len(line.replace(" ", "")) != 16:
            raise CheatCodeError(f"Line {n}: '{raw.strip()}' isn't a code line (two 8-digit hex numbers).")
        lines.append((int(words[0], 16), int(words[1], 16)))
    return lines


def parse_cheats(text: str) -> list:
    """Cheats from a cheats file: `$Name` lines, each followed by its code lines."""
    cheats, cur = [], None
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("$"):
            cur = Cheat(s[1:].strip())
            cheats.append(cur)
        elif s.startswith("*") and cur is not None and not cur.lines:
            cur.note += (" " if cur.note else "") + s[1:].strip()
        elif s and cur is not None and not s.startswith("#") and not s.startswith("*"):
            cur.lines += parse_code_lines(s)
    return cheats


def format_cheats(cheats) -> str:
    out = ["# BF1 cheat codes - Gecko-style, see Loader.py for the code types.", ""]
    for c in cheats:
        out.append(f"${c.name}")
        if c.note:
            out.append(f"* {c.note}")
        out += [f"{a:08X} {b:08X}" for a, b in c.lines]
        out.append("")
    return "\n".join(out)


def run_code(game: Game, lines: list, originals: dict | None = None) -> None:
    """Applies one cheat's code lines once. With `originals`, the bytes each write
    replaces are remembered there the first time ({address: bytes}), so the cheat
    can be undone (restore())."""
    def write(addr, data):
        if originals is not None and addr not in originals:
            originals[addr] = game.read(addr, len(data))
        game.write(addr, data)

    ba = game.base
    po = ba
    skip = []        # a stack of "this if is false"
    i = 0
    while i < len(lines):
        w1, w2 = lines[i]
        t = w1 >> 24
        off = (w1 & 0xFFFFFF) | ((t & 1) << 24)
        t &= 0xFE
        i += 1
        if t == 0xE0:
            skip, po = [], ba
            continue
        if t == 0xE2:
            n = (w2 & 0xFF) or 1
            skip = skip[:-n] if n <= len(skip) else []
            continue
        if t == 0x06 or t == 0x16:   # byte data follows, even when skipped
            count = w2
            nlines = (count + 7) // 8
            data = b"".join(struct.pack(">II", *lines[i + k]) for k in range(nlines))[:count]
            i += nlines
            if not any(skip):
                write((po if t & 0x10 else ba) + off, data)
            continue
        if 0x20 <= t <= 0x37:
            if any(skip):
                skip.append(True)
                continue
            addr = (po if t >= 0x30 else ba) + off
            v = game.u32(addr)
            op = t & 0x0E
            ok = {0x0: v == w2, 0x2: v != w2, 0x4: v > w2, 0x6: v < w2}.get(op, False)
            skip.append(not ok)
            continue
        if any(skip):
            continue
        base = po if t & 0x10 else ba
        if t in (0x00, 0x10):
            write(base + off, bytes([w2 & 0xFF]) * ((w2 >> 16) + 1))
        elif t in (0x02, 0x12):
            write(base + off, struct.pack("<H", w2 & 0xFFFF) * ((w2 >> 16) + 1))
        elif t in (0x04, 0x14):
            write(base + off, struct.pack("<I", w2))
        elif t == 0x48:
            po = game.u32(ba + off)
        elif t == 0x58:
            po = game.u32(po + off)
        elif t == 0x4A:
            po = ba + off
        else:
            raise CheatCodeError(f"Code type {w1 >> 24:02X} isn't supported.")


# Built in: verified against Battlefront.exe 1.3 (Steam). The debug flags are the
# variables the developers' console commands switch; the console fix is the patch
# that makes the pause menu's Fake Console work (see patch_console).
BUILT_IN = [
    ("Fake console fix", "Creates the debug console the Fake Console's commands print to.",
     "060A5A60 0000000A\nE8BBA31D 00E90600\n00000000 00000000\n0617B393 00000005\nE8C8A6F2 FF000000"),
    ("Invincible", "The debug 'invincible' flag.", "004E6D53 00000001"),
    ("Unlimited ammo", "The debug 'unlimitedammo' flag.", "004E6D58 00000001"),
    ("Show framerate", "The debug 'framerate' display.", "00583A25 00000001"),
    ("Show polygon count", "The debug 'showpolycount' display.", "004E91F0 00000001"),
    ("Render collision", "Draws collision meshes (debug 'rendercollision').", "004E6C63 00000001"),
    ("Show flyer heights", "The debug 'showflyerheights' display.", "004A1F84 00000001"),
]


def restore_cheat(game: Game, originals: dict) -> None:
    """Puts back what a cheat's writes replaced (newest first)."""
    for addr, data in reversed(list(originals.items())):
        try:
            game.write(addr, data)
        except GameMemoryError:
            pass
    originals.clear()


def default_cheats() -> list:
    return [Cheat(name, parse_code_lines(code), note) for name, note, code in BUILT_IN]


class CheatFile:
    """cheats.txt (the codes) and cheats_enabled.json (which are on), next to the tool."""

    def __init__(self, folder: Path):
        self.path = Path(folder) / "cheats.txt"
        self.state_path = Path(folder) / "cheats_enabled.json"

    def load(self) -> list:
        if not self.path.exists():
            cheats = default_cheats()
            self.save(cheats)
        else:
            cheats = parse_cheats(self.path.read_text(encoding="utf-8"))
        try:
            on = set(json.loads(self.state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            on = set()
        for c in cheats:
            c.enabled = c.name in on
        return cheats

    def save(self, cheats) -> None:
        self.path.write_text(format_cheats(cheats), encoding="utf-8")
        self.state_path.write_text(json.dumps([c.name for c in cheats if c.enabled]), encoding="utf-8")


class CheatRunner:
    """Applies the enabled cheats several times a second in a background thread,
    attaching to the game whenever it's running."""

    def __init__(self, interval=0.1):
        self.interval = interval
        self.cheats = []
        self.game = None
        self.status = "Waiting for the game..."
        self.errors = {}
        self.originals = {}   # cheat name -> {address: bytes it replaced}, to undo it when switched off
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()

    def set_cheats(self, cheats):
        with self.lock:
            self.cheats = list(cheats)
            self.errors.clear()

    def _loop(self):
        last_try = 0
        while not self._stop.is_set():
            if self.game is not None and not self.game.alive():
                self.game.close()
                self.game = None
                self.originals.clear()
                self.status = "Game closed - waiting for it to start..."
            if self.game is None and time.time() - last_try > 2:
                last_try = time.time()
                try:
                    self.game = Game.attach()
                    self.status = f"Attached: Battlefront.exe at 0x{self.game.base:08X}"
                    if not self.game.known_build:
                        self.status += " - not the 1.3 Steam exe the codes were made for: cheats are off"
                except GameMemoryError:
                    pass
            if self.game is not None and self.game.known_build:
                with self.lock:
                    active = [c for c in self.cheats if c.enabled]
                    names = {c.name for c in active}
                for name in [n for n in self.originals if n not in names]:  # switched off: undo
                    restore_cheat(self.game, self.originals.pop(name))
                for c in active:
                    try:
                        run_code(self.game, c.lines, self.originals.setdefault(c.name, {}))
                        self.errors.pop(c.name, None)
                    except (GameMemoryError, CheatCodeError, struct.error) as exc:
                        self.errors[c.name] = str(exc)
            self._stop.wait(self.interval)


# ==================================================================================================
# bf1_patch - merged in from bf1_patch.py
# ==================================================================================================
# bf1_patch.py - the fake console fix, applied to the running game.
#
# The pause menu's Fake Console runs commands through ScriptCB_DoConsoleCmd,
# which hands them to the debug command system along with the debug console
# (the text window commands print their results to). The release build never
# creates that console - the function that does is still in the exe but
# nothing calls it - so every command printed to a null console and crashed.
#
# The fix puts a two-instruction stub in 16 bytes of unused padding:
#
#     call ConsoleCreate      ; creates the console if there isn't one
#     jmp  GetConsole         ; the original getter: returns it
#
# and points the one call in ScriptCB_DoConsoleCmd that fetches the console at
# the stub. Nothing else changes. It's the "Fake console fix" cheat code in
# bf1_memory.BUILT_IN; this module just waits for the game to be ready and
# applies it once. Battlefront.exe 1.3 (Steam) only; nothing on disk changes.



RVA_CALL_SITE = 0x17B393   # ScriptCB_DoConsoleCmd: call GetConsole
ORIGINAL_CALL = bytes.fromhex("e8859ee8ff")  # call GetConsole's thunk (+521D)


class PatchError(Exception):
    pass


def console_code():
    return next(parse_code_lines(code) for name, _note, code in BUILT_IN if name == "Fake console fix")


def patch_console(pid=None, wait: float = 120) -> str:
    """Waits (up to `wait` seconds) for the game to start and decrypt its code, then
    applies the fix. Returns what it did."""
    deadline = time.time() + wait
    game = None
    while game is None:
        try:
            game = Game(pid) if pid else Game.attach()
        except GameMemoryError:
            if time.time() > deadline:
                raise PatchError("The game isn't running.") from None
            time.sleep(1)
    try:
        if not game.known_build:
            raise PatchError("This isn't Battlefront.exe 1.3 (Steam), the version the fix was made for - not patching.")
        code = console_code()
        patched_call = bytes.fromhex("e8c8a6f2ff")
        while True:  # Steam's DRM wrapper decrypts the code a moment after the game starts
            site = game.read(game.base + RVA_CALL_SITE, 5)
            if site == patched_call:
                run_code(game, code)
                return "already patched"
            if site == ORIGINAL_CALL:
                break
            if time.time() > deadline:
                raise PatchError("The game's code isn't ready (or doesn't match) - not patching.")
            time.sleep(1)
        run_code(game, code)
        return f"patched (game at 0x{game.base:08x})"
    finally:
        game.close()


# ==================================================================================================
# bf1_cheats - merged in from bf1_cheats.py
# ==================================================================================================
# bf1_cheats.py - BF1 Cheats: a window beside the running game with
#
#   Cheats   Gecko-style cheat codes, switched on and off while you play
#   Memory   a live hex view of the game's memory, with editing
#   Search   a scanner for finding values (health, ammo, ...) like Cheat Engine
#
# The codes and their format are in bf1_memory.py; they're saved to cheats.txt
# next to the tool. Opened from the Mod Loader's Cheats button, or on its own.
# Battlefront.exe 1.3 (Steam), single player. Nothing on disk changes.



HEX_ROWS = 24


class CheatsWindow(tk.Toplevel):
    def __init__(self, master, colors=None):
        super().__init__(master)
        self.title("BF1 Cheats")
        self.colors = colors or THEMES[editor_theme()]
        scale = self.winfo_fpixels("1i") / 96.0
        self.px = lambda n: max(1, round(n * scale))
        try:
            self.iconbitmap(default=str(resource_path("icon.ico")))
        except tk.TclError:
            pass
        self.configure(bg=self.colors["bg"])
        self.file = CheatFile(app_dir())
        try:
            self.cheats = self.file.load()
        except (OSError, CheatCodeError) as exc:
            messagebox.showerror("BF1 Cheats", f"Couldn't read cheats.txt: {exc}", parent=self)
            self.cheats = default_cheats()
        self.runner = CheatRunner()
        self.runner.set_cheats(self.cheats)
        self.runner.start()
        self.scanner = None
        self.mem_addr = None
        self.mem_prev = b""
        self.sel_addr = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(200, self._tick)

    # -- layout --------------------------------------------------------------

    def _build(self):
        px, c = self.px, self.colors
        top = ttk.Frame(self, padding=(px(10), px(8), px(10), 0))
        top.pack(fill="x")
        self.status = tk.StringVar(value=self.runner.status)
        ttk.Label(top, textvariable=self.status).pack(side="left")
        self.on_top = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Stay on top", variable=self.on_top,
                        command=lambda: self.attributes("-topmost", self.on_top.get())).pack(side="right")
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=px(10), pady=px(8))
        self.nb = nb
        nb.add(self._cheats_tab(nb), text="Cheats")
        self.mem_tab = self._memory_tab(nb)
        nb.add(self.mem_tab, text="Memory")
        nb.add(self._search_tab(nb), text="Search")

    def _text(self, parent, **kw):
        c = self.colors
        return tk.Text(parent, bg=c["field_bg"], fg=c["fg"], insertbackground=c["fg"], highlightthickness=0,
                       selectbackground=c["select_bg"], selectforeground=c["select_fg"], font=("Consolas", 10), **kw)

    # -- cheats tab ------------------------------------------------------------

    def _cheats_tab(self, nb):
        f = ttk.Frame(nb, padding=self.px(8))
        ttk.Label(f, text="Click a cheat's box to switch it on or off - it applies live while the game runs.",
                  foreground="gray").pack(anchor="w")
        self.tree = ttk.Treeview(f, columns=("on", "name", "note"), show="headings", height=14, selectmode="browse")
        for col, text, w in (("on", "On", 40), ("name", "Cheat", 220), ("note", "Notes", 380)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=self.px(w), stretch=col == "note", anchor="center" if col == "on" else "w")
        self.tree.pack(fill="both", expand=True, pady=self.px(6))
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<Double-1>", lambda e: self._edit_cheat())
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        for text, fn in (("New...", self._new_cheat), ("Edit...", self._edit_cheat), ("Delete", self._delete_cheat),
                         ("All off", self._all_off), ("Open cheats.txt", self._open_file)):
            ttk.Button(bar, text=text, command=fn).pack(side="left", padx=(0, 6))
        self._fill_cheats()
        return f

    def _fill_cheats(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for i, ch in enumerate(self.cheats):
            note = self.runner.errors.get(ch.name) or ch.note
            self.tree.insert("", "end", iid=str(i), values=("✔" if ch.enabled else "", ch.name, note))
        if sel and sel[0] in self.tree.get_children():
            self.tree.selection_set(sel)

    def _save(self):
        self.runner.set_cheats(self.cheats)
        try:
            self.file.save(self.cheats)
        except OSError as exc:
            messagebox.showerror("BF1 Cheats", f"Couldn't save cheats.txt: {exc}", parent=self)
        self._fill_cheats()

    def _tree_click(self, event):
        row = self.tree.identify_row(event.y)
        if row and self.tree.identify_column(event.x) == "#1":
            ch = self.cheats[int(row)]
            ch.enabled = not ch.enabled
            self._save()
            return "break"

    def _all_off(self):
        for ch in self.cheats:
            ch.enabled = False
        self._save()

    def _selected_cheat(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _cheat_dialog(self, title, name="", note="", code=""):
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.configure(bg=self.colors["bg"])
        body = ttk.Frame(win, padding=self.px(10))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Name:").grid(row=0, column=0, sticky="w")
        name_var = tk.StringVar(value=name)
        ttk.Entry(body, textvariable=name_var, width=50).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(body, text="Notes:").grid(row=1, column=0, sticky="w")
        note_var = tk.StringVar(value=note)
        ttk.Entry(body, textvariable=note_var, width=50).grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(body, text="Code:").grid(row=2, column=0, sticky="nw")
        box = self._text(body, width=40, height=10)
        box.grid(row=2, column=1, sticky="nsew", pady=2)
        box.insert("1.0", code)
        ttk.Label(body, text="Lines like  004E6D53 00000001  - see the code types in the Help button.",
                  foreground="gray").grid(row=3, column=1, sticky="w")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(2, weight=1)
        result = {}

        def ok():
            try:
                lines = parse_code_lines(box.get("1.0", "end"))
            except CheatCodeError as exc:
                messagebox.showerror("Code", str(exc), parent=win)
                return
            if not name_var.get().strip():
                messagebox.showerror("Code", "Give the cheat a name.", parent=win)
                return
            result["v"] = (name_var.get().strip(), note_var.get().strip(), lines)
            win.destroy()

        row = ttk.Frame(body)
        row.grid(row=4, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(row, text="Help", command=lambda: messagebox.showinfo("Code types", CODE_HELP, parent=win)
                   ).pack(side="left", padx=(0, 20))
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(row, text="OK", command=ok).pack(side="right", padx=6)
        win.grab_set()
        self.wait_window(win)
        return result.get("v")

    def _new_cheat(self, code="", name="", note=""):
        got = self._cheat_dialog("New cheat", name, note, code)
        if got:
            self.cheats.append(Cheat(got[0], got[2], got[1]))
            self._save()
            self.nb.select(0)

    def _edit_cheat(self):
        i = self._selected_cheat()
        if i is None:
            return
        ch = self.cheats[i]
        got = self._cheat_dialog("Edit cheat", ch.name, ch.note, ch.text())
        if got:
            ch.name, ch.note, ch.lines = got[0], got[1], got[2]
            self._save()

    def _delete_cheat(self):
        i = self._selected_cheat()
        if i is not None and messagebox.askyesno("Delete", f"Delete '{self.cheats[i].name}'?", parent=self):
            del self.cheats[i]
            self._save()

    def _open_file(self):
        try:
            os.startfile(str(self.file.path))
        except OSError:
            messagebox.showinfo("BF1 Cheats", f"The codes are in {self.file.path}", parent=self)

    # -- memory tab ------------------------------------------------------------

    def _memory_tab(self, nb):
        px = self.px
        f = ttk.Frame(nb, padding=px(8))
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Address:").pack(side="left")
        self.addr_var = tk.StringVar(value="+4E6D50")
        e = ttk.Entry(row, textvariable=self.addr_var, width=28)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda ev: self._goto())
        ttk.Button(row, text="Go", command=self._goto).pack(side="left")
        ttk.Button(row, text="▲", width=3, command=lambda: self._scroll(-HEX_ROWS * 16)).pack(side="left", padx=(12, 0))
        ttk.Button(row, text="▼", width=3, command=lambda: self._scroll(HEX_ROWS * 16)).pack(side="left", padx=2)
        ttk.Label(row, text="e.g. +4E6D53, 0x00746D53, [+6D9420]+10", foreground="gray").pack(side="left", padx=10)
        self.hex = self._text(f, width=86, height=HEX_ROWS, wrap="none", cursor="arrow")
        self.hex.pack(fill="both", expand=True, pady=px(6))
        self.hex.tag_configure("changed", foreground="#e8a33d")
        self.hex.tag_configure("sel_byte", background=self.colors["select_bg"], foreground=self.colors["select_fg"])
        self.hex.bind("<Button-1>", self._hex_click)
        self.hex.bind("<MouseWheel>", lambda ev: (self._scroll(-16 * 4 if ev.delta > 0 else 16 * 4), "break")[1])
        self.hex.configure(state="disabled")
        info = ttk.Frame(f)
        info.pack(fill="x")
        self.sel_info = tk.StringVar(value="Click a byte to inspect it.")
        ttk.Label(info, textvariable=self.sel_info, font=("Consolas", 9)).pack(anchor="w")
        w = ttk.Frame(f)
        w.pack(fill="x", pady=(6, 0))
        ttk.Label(w, text="Value:").pack(side="left")
        self.write_type = tk.StringVar(value="byte")
        ttk.Combobox(w, textvariable=self.write_type, values=list(MEM_TYPES), width=8, state="readonly").pack(side="left", padx=4)
        self.write_val = tk.StringVar()
        ttk.Entry(w, textvariable=self.write_val, width=14).pack(side="left")
        ttk.Button(w, text="Write once", command=self._write_once).pack(side="left", padx=6)
        ttk.Button(w, text="Make cheat (keep it set)...", command=self._make_cheat).pack(side="left")
        return f

    def _game(self):
        return self.runner.game

    def _goto(self):
        g = self._game()
        if g is None:
            messagebox.showinfo("Memory", "Start the game first.", parent=self)
            return
        try:
            self.mem_addr = g.resolve(self.addr_var.get()) & ~0xF
        except GameMemoryError as exc:
            messagebox.showerror("Memory", str(exc), parent=self)
            return
        self.mem_prev = b""
        self._refresh_hex()

    def _scroll(self, delta):
        if self.mem_addr is not None:
            self.mem_addr = max(0, self.mem_addr + delta)
            g = self._game()
            if g:
                self.addr_var.set(g.describe(self.mem_addr))
            self.mem_prev = b""
            self._refresh_hex()

    def _refresh_hex(self):
        g = self._game()
        if g is None or self.mem_addr is None:
            return
        data = g.read(self.mem_addr, HEX_ROWS * 16)
        t = self.hex
        t.configure(state="normal")
        t.delete("1.0", "end")
        changed = []
        for r in range(HEX_ROWS):
            a = self.mem_addr + r * 16
            chunk = data[r * 16:r * 16 + 16]
            label = g.describe(a).rjust(22)
            hexes = " ".join(f"{b:02X}" for b in chunk) if chunk else "?? " * 16
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            t.insert("end", f"{label}  {hexes:<48}  {text}\n")
            for k, b in enumerate(chunk):
                p = r * 16 + k
                if self.mem_prev and p < len(self.mem_prev) and self.mem_prev[p] != b:
                    changed.append((r, k))
        for r, k in changed:
            col = 24 + k * 3
            t.tag_add("changed", f"{r + 1}.{col}", f"{r + 1}.{col + 2}")
        if self.sel_addr is not None and self.mem_addr <= self.sel_addr < self.mem_addr + HEX_ROWS * 16:
            off = self.sel_addr - self.mem_addr
            r, k = divmod(off, 16)
            t.tag_add("sel_byte", f"{r + 1}.{24 + k * 3}", f"{r + 1}.{26 + k * 3}")
            self._show_selection(g)
        t.configure(state="disabled")
        self.mem_prev = data

    def _hex_click(self, event):
        idx = self.hex.index(f"@{event.x},{event.y}")
        line, col = (int(x) for x in idx.split("."))
        if col < 24 or col >= 24 + 48 or self.mem_addr is None:
            return "break"
        k = (col - 24) // 3
        self.sel_addr = self.mem_addr + (line - 1) * 16 + k
        self._refresh_hex()
        return "break"

    def _show_selection(self, g):
        d = g.read(self.sel_addr, 8)
        vals = []
        for kind in ("byte", "2 bytes", "4 bytes", "float"):
            v = mem_unpack(kind, d)
            if v is not None:
                vals.append(f"{kind}={v:.6g}" if kind == "float" else f"{kind}={v}")
        self.sel_info.set(f"{g.describe(self.sel_addr)}:  " + "   ".join(vals))

    def _write_once(self):
        g = self._game()
        if g is None or self.sel_addr is None:
            messagebox.showinfo("Memory", "Click the byte to write at first.", parent=self)
            return
        try:
            g.write(self.sel_addr, mem_pack(self.write_type.get(), self.write_val.get()))
        except (ValueError, struct.error, GameMemoryError) as exc:
            messagebox.showerror("Memory", f"Couldn't write: {exc}", parent=self)
            return
        self._refresh_hex()

    def _make_cheat(self, addr=None, kind=None, value=None):
        g = self._game()
        addr = self.sel_addr if addr is None else addr
        if g is None or addr is None:
            messagebox.showinfo("Memory", "Click the byte the cheat should set first.", parent=self)
            return
        kind = kind or self.write_type.get()
        value = self.write_val.get() if value is None else value
        try:
            data = mem_pack(kind, value)
        except (ValueError, struct.error) as exc:
            messagebox.showerror("Memory", f"'{value}' isn't a {kind} value: {exc}", parent=self)
            return
        off = addr - g.base
        if not 0 <= off < 0x2000000:
            messagebox.showinfo("Memory", "That address isn't inside Battlefront.exe - it's memory the game allocates "
                                "while running (a soldier, a vehicle...), which moves every time, so a fixed code "
                                "can't reach it. Values in the exe's own data (Battlefront.exe+...) stay put.",
                                parent=self)
            return
        code = code_for_write(off, data)
        self._new_cheat(code=code, name=f"Set {g.describe(addr)} to {value}", note=f"{kind} {value}")

    # -- search tab ------------------------------------------------------------

    def _search_tab(self, nb):
        px = self.px
        f = ttk.Frame(nb, padding=px(8))
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Type:").pack(side="left")
        self.scan_type = tk.StringVar(value="4 bytes")
        ttk.Combobox(row, textvariable=self.scan_type, values=list(MEM_TYPES), width=8, state="readonly").pack(side="left", padx=4)
        ttk.Label(row, text="Value:").pack(side="left", padx=(8, 0))
        self.scan_val = tk.StringVar()
        ttk.Entry(row, textvariable=self.scan_val, width=14).pack(side="left", padx=4)
        self.first_btn = ttk.Button(row, text="First scan", command=lambda: self._scan(first=True, mode="exact"))
        self.first_btn.pack(side="left", padx=4)
        ttk.Button(row, text="Unknown value", command=lambda: self._scan(first=True, mode="unknown")).pack(side="left")
        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="Next scan:").pack(side="left")
        self.next_mode = tk.StringVar(value="exact")
        ttk.Combobox(row2, textvariable=self.next_mode, width=10, state="readonly",
                     values=("exact", "changed", "unchanged", "increased", "decreased")).pack(side="left", padx=4)
        self.next_btn = ttk.Button(row2, text="Next scan", command=lambda: self._scan(first=False))
        self.next_btn.pack(side="left")
        ttk.Button(row2, text="New search", command=self._reset_scan).pack(side="left", padx=6)
        self.scan_status = tk.StringVar(value="Search for a value you can see in game (ammo, health...), change it "
                                              "in game, and search again until a few addresses are left.")
        ttk.Label(f, textvariable=self.scan_status, foreground="gray", wraplength=px(680)).pack(anchor="w", pady=4)
        self.scan_progress = ttk.Progressbar(f, mode="determinate", maximum=1.0)
        self.scan_progress.pack(fill="x")
        self.results = ttk.Treeview(f, columns=("addr", "value", "prev"), show="headings", height=12)
        for col, text, w in (("addr", "Address", 220), ("value", "Value now", 160), ("prev", "At last scan", 160)):
            self.results.heading(col, text=text)
            self.results.column(col, width=px(w))
        self.results.pack(fill="both", expand=True, pady=px(6))
        self.results.bind("<Double-1>", lambda e: self._result_to_memory())
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="View in memory", command=self._result_to_memory).pack(side="left")
        ttk.Button(bar, text="Make cheat...", command=self._result_cheat).pack(side="left", padx=6)
        self._result_addrs = []
        return f

    def _reset_scan(self):
        self.scanner = None
        self.results.delete(*self.results.get_children())
        self._result_addrs = []
        self.scan_status.set("New search.")

    def _scan(self, first, mode=None):
        g = self._game()
        if g is None:
            messagebox.showinfo("Search", "Start the game first.", parent=self)
            return
        mode = mode or self.next_mode.get()
        if mode == "exact" and not self.scan_val.get().strip():
            messagebox.showinfo("Search", "Type the value to look for.", parent=self)
            return
        if first or self.scanner is None:
            try:
                self.scanner = Scanner(g, self.scan_type.get())
            except ImportError:
                messagebox.showerror("Search", "Searching needs numpy (pip install numpy).", parent=self)
                return
            first = True
        self.first_btn.state(["disabled"])
        self.next_btn.state(["disabled"])
        self.scan_status.set("Scanning...")
        result = {}

        def work():
            try:
                if first:
                    result["n"] = self.scanner.first(mode, self.scan_val.get(), progress=lambda p: result.update(p=p))
                else:
                    result["n"] = self.scanner.next(mode, self.scan_val.get())
            except (GameMemoryError, ValueError, struct.error) as exc:
                result["err"] = str(exc)
            result["done"] = True

        threading.Thread(target=work, daemon=True).start()

        def poll():
            self.scan_progress["value"] = result.get("p", 0)
            if not result.get("done"):
                self.after(100, poll)
                return
            self.first_btn.state(["!disabled"])
            self.next_btn.state(["!disabled"])
            self.scan_progress["value"] = 0
            if "err" in result:
                self.scan_status.set(result["err"])
                return
            n = result["n"]
            self.scan_status.set(f"{n:,} addresses match." + (" Showing the first 1,000." if n > 1000 else ""))
            self._show_results()

        poll()

    def _show_results(self):
        g = self._game()
        self.results.delete(*self.results.get_children())
        self._result_addrs = self.scanner.addresses(1000) if self.scanner else []
        for i, (a, prev) in enumerate(self._result_addrs):
            self.results.insert("", "end", iid=str(i), values=(g.describe(a), "", prev))
        self._refresh_results()

    def _refresh_results(self):
        g = self._game()
        if g is None or not self._result_addrs or self.scanner is None:
            return
        kind = self.scanner.kind
        size = MEM_TYPES[kind][1]
        for i, (a, prev) in enumerate(self._result_addrs[:200]):
            v = mem_unpack(kind, g.read(a, size))
            if self.results.exists(str(i)):
                self.results.set(str(i), "value", f"{v:.6g}" if isinstance(v, float) else v)

    def _picked_result(self):
        sel = self.results.selection()
        return self._result_addrs[int(sel[0])][0] if sel else None

    def _result_to_memory(self):
        a = self._picked_result()
        g = self._game()
        if a is None or g is None:
            return
        self.addr_var.set(g.describe(a))
        self.sel_addr = a
        self.write_type.set(self.scanner.kind)
        self._goto()
        self.nb.select(self.mem_tab)

    def _result_cheat(self):
        a = self._picked_result()
        if a is None:
            return
        g = self._game()
        cur = mem_unpack(self.scanner.kind, g.read(a, MEM_TYPES[self.scanner.kind][1]))
        value = simpledialog.askstring("Make cheat", f"Keep {g.describe(a)} at which value?",
                                       initialvalue=str(cur), parent=self)
        if value is not None:
            self._make_cheat(addr=a, kind=self.scanner.kind, value=value)

    # -- loop ------------------------------------------------------------------

    def _tick(self):
        if not self.winfo_exists():
            return
        self.status.set(self.runner.status)
        if any(self.runner.errors.get(c.name) for c in self.cheats):
            self._fill_cheats()
        try:
            if str(self.nb.select()) == str(self.mem_tab):
                self._refresh_hex()
            else:
                self._refresh_results()
        except tk.TclError:
            pass
        self.after(500, self._tick)

    def _close(self):
        self.runner.stop()
        self.destroy()


CODE_HELP = """Each line is two 8-digit hex numbers. XXXXXX is an offset from where
Battlefront.exe loaded (as shown in the Memory tab: Battlefront.exe+XXXXXX).

00XXXXXX 000000VV   write the byte VV
02XXXXXX 0000VVVV   write the 16-bit VVVV
04XXXXXX VVVVVVVV   write the 32-bit VVVVVVVV (int, or a float's bits)
06XXXXXX 0000000N   write the N bytes on the next lines, in order
20XXXXXX VVVVVVVV   if the 32-bit value there == V (22 !=, 24 >, 26 <)
48XXXXXX 00000000   follow the pointer stored there (po)
10/12/14/16/30-36   the same as 00-26, at po + XXXXXX
E0000000 00000000   end all ifs

Values are little-endian numbers: 04XXXXXX 000003E8 writes 1000.
Enabled cheats are applied several times a second."""


def code_for_write(off: int, data: bytes) -> str:
    """Code lines that keep `data` at ba+off."""
    def t(base_type):
        return f"{base_type | (1 if off >= 0x1000000 else 0):02X}{off & 0xFFFFFF:06X}"
    if len(data) == 1:
        return f"{t(0x00)} {data[0]:08X}"
    if len(data) == 2:
        return f"{t(0x02)} {struct.unpack('<H', data)[0]:08X}"
    if len(data) == 4:
        return f"{t(0x04)} {struct.unpack('<I', data)[0]:08X}"
    lines = [f"{t(0x06)} {len(data):08X}"]
    padded = data + bytes(-len(data) % 8)
    lines += [f"{padded[i:i + 4].hex().upper()} {padded[i + 4:i + 8].hex().upper()}" for i in range(0, len(padded), 8)]
    return "\n".join(lines)


def cheats_main() -> int:
    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    apply_theme(root, THEMES[editor_theme()])
    win = CheatsWindow(root)
    win.protocol("WM_DELETE_WINDOW", lambda: (win._close(), root.destroy()))
    root.mainloop()
    return 0


if __name__ == "__main__":
    if "--cheats" in sys.argv:
        sys.exit(cheats_main())
    LoaderApp().mainloop()
