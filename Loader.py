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
import math
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
    subprocess.Popen([str(exe), "/nointro", "/noframelock"], cwd=str(exe.parent))

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
        ttk.Button(bar, text="New Class...", command=self._new_class).pack(side="right", padx=(0, 6))
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

    def _new_class(self):
        """Builds a mod adding a class cloned from an existing one (see bf1_core.make_class_mod)."""
        _game, mods, orig = self._dirs()
        if not mods or not orig:
            messagebox.showerror(APP_NAME, "Set the Mods folder and Original files first.")
            return
        win = tk.Toplevel(self)
        win.title("New Class")
        win.transient(self)
        fields = {}
        rows = (("source", "Copy this class:", "imp_inf_storm_trooper"), ("name", "New class name:", "imp_inf_elite"),
                ("display", "On-screen name:", "ELITE STORMTROOPER"), ("replace", "Takes the slot of (optional):", ""),
                ("missions", "Only these missions (optional):", ""), ("mod", "Mod folder:", "New Classes"))
        for i, (key, label, default) in enumerate(rows):
            ttk.Label(win, text=label).grid(row=i, column=0, sticky="w", padx=10, pady=2)
            fields[key] = tk.StringVar(value=default)
            ttk.Entry(win, textvariable=fields[key], width=36).grid(row=i, column=1, padx=(0, 10), pady=2)
        ttk.Label(win, text="Changes (Name = value, one per line):").grid(row=6, column=0, columnspan=2, sticky="w", padx=10)
        box = tk.Text(win, width=50, height=5)
        box.grid(row=7, column=0, columnspan=2, padx=10)
        box.insert("1.0", "MaxHealth = 1500")
        own = tk.BooleanVar(value=True)
        ttk.Checkbutton(win, text="Give it its own model and texture, to import a .glb onto it alone",
                        variable=own).grid(row=8, column=0, columnspan=2, sticky="w", padx=10, pady=4)
        ttk.Label(win, text="A team can have at most 5 classes, the hero aside; a 6th crashes the game. Most teams are "
                            "full, so name a class to take the slot of, like imp_inf_dark_trooper.",
                  foreground="gray", wraplength=px(420)).grid(row=9, column=0, columnspan=2, sticky="w", padx=10)
        status = tk.StringVar()
        ttk.Label(win, textvariable=status, foreground="gray", wraplength=px(420)).grid(
            row=10, column=0, columnspan=2, sticky="w", padx=10, pady=4)
        buttons = ttk.Frame(win)
        buttons.grid(row=11, column=0, columnspan=2, sticky="e", padx=10, pady=(0, 10))

        def done(box_):
            if "error" in box_:
                status.set("")
                build.configure(state="normal")
                messagebox.showerror(APP_NAME, f"Couldn't make the class:\n{box_['error']}", parent=win)
                return
            r = box_["result"]
            self.cfg["last_mod"] = fields["mod"].get().strip()
            self.refresh()
            text = f"Added {fields['name'].get().strip()} to {r['file']}.\n\nOn the team in: {', '.join(r['missions']) or 'no missions'}."
            if r["full"]:
                text += (f"\n\nNot added to {', '.join(r['full'])}: those teams already have 5 classes. "
                         "Name a class to take the slot of.")
            win.destroy()
            messagebox.showinfo(APP_NAME, text + "\n\nThe mod is selected, press Play.")

        def go():
            changes = []
            for line in box.get("1.0", "end").splitlines():
                if line.strip():
                    key, eq, value = line.partition("=")
                    if not eq:
                        messagebox.showerror(APP_NAME, f"'{line}' isn't 'Name = value'.", parent=win)
                        return
                    changes.append((key.strip(), value.strip()))
            import bf1_core
            get = lambda k: fields[k].get().strip()
            missions = [m.strip() for m in get("missions").split(",") if m.strip()] or None
            result = {}

            def work():
                try:
                    result["result"] = bf1_core.make_class_mod(
                        orig, Path(mods) / get("mod"), get("source"), get("name"), get("display") or None,
                        changes, None, missions, get("replace") or None, own.get())
                except (ValueError, OSError) as exc:
                    result["error"] = exc

            worker = threading.Thread(target=work, daemon=True)
            worker.start()
            build.configure(state="disabled")
            status.set("Building - this reads the big side files, so it takes a little while...")

            def poll():
                if worker.is_alive():
                    win.after(100, poll)
                else:
                    done(result)

            win.after(100, poll)

        build = ttk.Button(buttons, text="Build mod", command=go)
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right")
        build.pack(side="right", padx=6)
        win.grab_set()

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
        self._read_sections()

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

    def read_safe(self, addr: int, n: int) -> list:
        """n bytes from addr as a list of ints, page by page, with None for bytes that
        can't be read - so one unmapped page doesn't hide the readable ones around it."""
        data = self.read(addr, n)
        if len(data) == n:
            return list(data)
        out, a, end = [], addr, addr + n
        while a < end:
            stop = min(end, (a // 0x1000 + 1) * 0x1000)
            b = self.read(a, stop - a)
            out.extend(b)
            out.extend([None] * (stop - a - len(b)))
            a = stop
        return out

    def regions(self, writable_only=True, scope=None):
        """(start, size) of committed memory regions. scope: 'writable' (the default),
        'all' (everything readable) or 'exe' (Battlefront.exe's own sections)."""
        scope = scope or ("writable" if writable_only else "all")
        addr, mbi = 0, MEMORY_BASIC_INFORMATION()
        while addr < 0x7FFF0000:
            if not _k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            start, size = mbi.BaseAddress or 0, mbi.RegionSize
            prot = mbi.Protect & 0xFF
            ok = mbi.State == MEM_COMMIT and not (mbi.Protect & PAGE_GUARD) and prot not in (0, 0x01)
            if scope == "writable":
                ok = ok and prot in WRITABLE
            elif scope == "exe":
                ok = ok and self.base <= start < self.base + self.image_size
            if ok:
                yield start, size
            addr = start + size

    PROTECTION = {0x01: "no access", 0x02: "read", 0x04: "read/write", 0x08: "write-copy", 0x10: "execute",
                  0x20: "execute/read", 0x40: "execute/read/write", 0x80: "execute/write-copy"}

    def where(self, addr: int) -> str:
        """What an address is: 'Battlefront.exe .text (execute/read)', 'heap (read/write)'..."""
        mbi = MEMORY_BASIC_INFORMATION()
        if not _k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) \
                or mbi.State != MEM_COMMIT:
            return "not mapped"
        prot = self.PROTECTION.get(mbi.Protect & 0xFF, f"protect {mbi.Protect:#x}")
        if self.base <= addr < self.base + self.image_size:
            rva = addr - self.base
            sec = next((n for n, start, size in self.sections if start <= rva < start + max(size, 1)), "header")
            return f"Battlefront.exe {sec} ({prot})"
        kind = {0x1000000: "image", 0x40000: "mapped", 0x20000: "heap"}.get(mbi.Type, "memory")
        return f"{kind} ({prot})"

    def _read_sections(self):
        head = self.read(self.base, 0x1000)
        self.sections, self.image_size = [], 0x2400000
        if len(head) < 0x40:
            return
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if pe + 0xF8 > len(head) or head[pe:pe + 4] != b"PE\0\0":
            return
        count = struct.unpack_from("<H", head, pe + 6)[0]
        opt = struct.unpack_from("<H", head, pe + 20)[0]
        self.image_size = struct.unpack_from("<I", head, pe + 0x50)[0] or self.image_size
        p = pe + 24 + opt
        for _ in range(count):
            if p + 40 > len(head):
                break
            name = head[p:p + 8].rstrip(b"\0").decode("latin1", "replace")
            vsize, rva = struct.unpack_from("<II", head, p + 8)
            self.sections.append((name, rva, vsize))
            p += 40

    def string_at(self, addr: int, limit: int = 64, wide: bool = False):
        """The printable text at addr (ASCII, or UTF-16 if wide), or None."""
        data = self.read(addr, limit * (2 if wide else 1))
        if wide:
            chars = []
            for i in range(0, len(data) - 1, 2):
                c = data[i] | data[i + 1] << 8
                if c == 0:
                    break
                if not (32 <= c < 0xD800) or c == 0x7F:
                    return None
                chars.append(chr(c))
        else:
            chars = []
            for b in data:
                if b == 0:
                    break
                if not (32 <= b < 127 or b in (9, 10, 13)):
                    return None
                chars.append(chr(b))
        return "".join(chars) if len(chars) >= 3 else None

    # -- addresses

    def describe(self, addr: int) -> str:
        if self.base <= addr < self.base + getattr(self, "image_size", 0x2400000):
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

TEXT_KINDS = ("text", "text (UTF-16)", "bytes")
SCAN_KINDS = tuple(MEM_TYPES) + TEXT_KINDS
FIRST_MODES = ("exact", "bigger than", "smaller than", "between", "unknown")
NEXT_MODES = ("exact", "changed", "unchanged", "increased", "decreased", "increased by", "decreased by",
              "bigger than", "smaller than", "between")
TEXT_NEXT_MODES = ("exact", "unchanged", "changed")
SCOPES = {"writable memory": "writable", "all memory": "all", "Battlefront.exe only": "exe"}


def byte_pattern(kind: str, text: str, case: bool = True):
    """(compiled regex, length in bytes) for a text or byte-pattern search. Byte patterns are
    hex with ?? for any byte: `8B 0D ?? ?? ?? 00`."""
    if kind == "bytes":
        parts = text.replace(",", " ").split()
        if not parts:
            raise GameMemoryError("Type some bytes, e.g. 8B 0D ?? ?? 6D 00")
        rx = b""
        for p in parts:
            if p in ("?", "??"):
                rx += b"."
            elif re.fullmatch(r"[0-9A-Fa-f]{2}", p):
                rx += re.escape(bytes([int(p, 16)]))
            else:
                raise GameMemoryError(f"'{p}' isn't a byte - use two hex digits, or ?? for any byte.")
        return re.compile(rx, re.S), len(parts)
    if not text:
        raise GameMemoryError("Type the text to look for.")
    raw = text.encode("utf-16-le" if kind == "text (UTF-16)" else "latin1", "replace")
    flags = re.S | (0 if case else re.I)
    return re.compile(re.escape(raw), flags), len(raw)


class Scanner:
    """Cheat-Engine-style search: a first scan (a value, a range, or 'unknown' to snapshot
    everything), then next scans that narrow the results down. Also finds text (ASCII or
    UTF-16) and byte patterns with ?? wildcards. `scope` picks the memory searched: writable
    (where game values live), all readable memory, or Battlefront.exe's own sections."""

    MAX_UNKNOWN_BYTES = 768 * 1024 * 1024
    MAX_TEXT_HITS = 100_000

    def __init__(self, game: Game, kind: str, scope: str = "writable", case: bool = True):
        import numpy as np
        self.np = np
        self.game, self.kind, self.scope, self.case = game, kind, scope, case
        self.text = kind in TEXT_KINDS
        if not self.text:
            self.dtype = {"byte": np.uint8, "2 bytes": np.uint16, "4 bytes": np.int32, "float": np.float32,
                          "double": np.float64}[kind]
            self.size = MEM_TYPES[kind][1]
        self.results = None   # numbers: [(region start, numpy offsets or None, numpy previous values)]
        self.hits = None      # text: [(address, bytes found)]
        self.pattern = None
        self.history = []     # earlier results, for undo

    # -- helpers

    def _region_values(self, start, size):
        data = self.game.read(start, size)
        n = len(data) // self.size
        return self.np.frombuffer(data[:n * self.size], dtype=self.dtype) if n else None

    def _value(self, text):
        return mem_unpack(self.kind, mem_pack(self.kind, text))

    def _equal(self, values, target):
        if self.kind in ("float", "double"):
            return self.np.abs(values - target) <= max(abs(target) * 1e-4, 1e-3)
        return values == target

    def _match(self, mode, cur, prev, a, b):
        np = self.np
        if mode == "exact":
            return self._equal(cur, a)
        if mode == "bigger than":
            return cur > a
        if mode == "smaller than":
            return cur < a
        if mode == "between":
            lo, hi = min(a, b), max(a, b)
            return (cur >= lo) & (cur <= hi)
        if mode == "changed":
            return cur != prev
        if mode == "unchanged":
            return cur == prev
        if mode == "increased":
            return cur > prev
        if mode == "decreased":
            return cur < prev
        diff = (cur.astype(np.float64) - prev.astype(np.float64)) * (1 if mode == "increased by" else -1)
        if self.kind in ("float", "double"):
            return np.abs(diff - a) <= max(abs(a) * 1e-4, 1e-3)
        return diff == a

    def _targets(self, mode, value, value2):
        needs = {"exact": 1, "bigger than": 1, "smaller than": 1, "between": 2, "increased by": 1, "decreased by": 1}
        n = needs.get(mode, 0)
        if n >= 1 and not str(value).strip():
            raise GameMemoryError(f"'{mode}' needs a value.")
        if n == 2 and not str(value2).strip():
            raise GameMemoryError("'between' needs two values.")
        a = self._value(value) if n >= 1 else None
        b = self._value(value2) if n == 2 else None
        return a, b

    # -- scans

    def first(self, mode: str, value: str = "", progress=None, value2: str = "") -> int:
        regions = list(self.game.regions(scope=self.scope))
        total = sum(s for _, s in regions) or 1
        self.history = []
        if self.text:
            self.pattern, self.plen = byte_pattern(self.kind, value, self.case)
            self.hits, done = [], 0
            for start, size in regions:
                data = self.game.read(start, size)
                done += size
                if progress:
                    progress(done / total)
                for m in self.pattern.finditer(data):
                    self.hits.append((start + m.start(), m.group()))
                    if len(self.hits) >= self.MAX_TEXT_HITS:
                        return self.count()
            return self.count()
        np = self.np
        a, b = self._targets(mode, value, value2) if mode != "unknown" else (None, None)
        if mode == "unknown" and total > self.MAX_UNKNOWN_BYTES:
            raise GameMemoryError(f"An unknown-value scan would need {total // 2**20} MB - search for a value "
                                  "or a range first, or pick 'writable memory'.")
        self.results, done = [], 0
        for start, size in regions:
            vals = self._region_values(start, size)
            done += size
            if progress:
                progress(done / total)
            if vals is None:
                continue
            if mode == "unknown":
                self.results.append((start, None, vals.copy()))
            else:
                idx = np.nonzero(self._match(mode, vals, None, a, b))[0]
                if len(idx):
                    self.results.append((start, idx, vals[idx].copy()))
        return self.count()

    def next(self, mode: str, value: str = "", value2: str = "") -> int:
        if self.text:
            return self._next_text(mode, value)
        np = self.np
        if self.results is None:
            raise GameMemoryError("Do a first scan.")
        a, b = self._targets(mode, value, value2)
        out = []
        for start, idx, prev in self.results:
            if idx is None:
                vals = self._region_values(start, len(prev) * self.size)
                if vals is None or len(vals) != len(prev):
                    continue
                cur, offsets = vals, np.arange(len(vals))
            else:
                if len(idx) < 4096:
                    cur = np.array([mem_unpack(self.kind, self.game.read(start + int(i) * self.size, self.size)) or 0
                                    for i in idx], dtype=self.dtype)
                else:
                    vals = self._region_values(start, int(idx.max() + 1) * self.size)
                    if vals is None or len(vals) <= int(idx.max()):
                        continue
                    cur = vals[idx]
                offsets = idx
            sel = np.nonzero(self._match(mode, cur, prev, a, b))[0]
            if len(sel):
                out.append((start, offsets[sel], cur[sel].copy()))
        self.history = [self.results]
        self.results = out
        return self.count()

    def _next_text(self, mode, value):
        if self.hits is None:
            raise GameMemoryError("Do a first scan.")
        if mode == "exact":
            pattern, plen = byte_pattern(self.kind, value, self.case)
        elif mode in ("unchanged", "changed"):
            pattern, plen = None, self.plen
        else:
            raise GameMemoryError(f"'{mode}' is for numbers - text searches can use exact, unchanged or changed.")
        out = []
        for addr, prev in self.hits:
            cur = self.game.read(addr, plen)
            keep = (pattern.fullmatch(cur) is not None if mode == "exact"
                    else (cur == prev) == (mode == "unchanged"))
            if keep:
                out.append((addr, cur))
        self.history = [self.hits]
        self.hits = out
        if mode == "exact":
            self.pattern, self.plen = pattern, plen
        return self.count()

    def undo(self) -> int:
        if not self.history:
            raise GameMemoryError("Nothing to undo.")
        if self.text:
            self.hits = self.history.pop()
        else:
            self.results = self.history.pop()
        return self.count()

    def count(self) -> int:
        if self.text:
            return len(self.hits or [])
        return sum(len(prev) for _s, _i, prev in self.results or [])

    def addresses(self, limit=1000):
        """[(address, value at the last scan)]."""
        if self.text:
            return [(a, self.show(b)) for a, b in (self.hits or [])[:limit]]
        out = []
        for start, idx, prev in self.results or []:
            offs = idx if idx is not None else range(len(prev))
            for k, o in enumerate(offs):
                out.append((start + int(o) * self.size, prev[k].item()))
                if len(out) >= limit:
                    return out
        return out

    def show(self, raw: bytes) -> str:
        if self.kind == "bytes":
            return raw.hex(" ").upper()
        if self.kind == "text (UTF-16)":
            return raw.decode("utf-16-le", "replace")
        return raw.decode("latin1")

    def read_now(self, addr):
        if self.text:
            return self.show(self.game.read(addr, self.plen))
        v = mem_unpack(self.kind, self.game.read(addr, self.size))
        return f"{v:.6g}" if isinstance(v, float) else v


# --- disassembly -------------------------------------------------------------------------

def disassemble(game: Game, addr: int, count: int = 40) -> list:
    """[(address, bytes, mnemonic, operands, note)] for `count` instructions from addr. The note
    names call/jump targets and the strings or exe data an instruction points at."""
    try:
        import capstone
    except ImportError:
        raise GameMemoryError("Showing assembly needs capstone (pip install capstone).") from None
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    raw = game.read_safe(addr, count * 15 + 16)
    if raw and raw[0] is None:
        raise GameMemoryError(f"Can't read {game.describe(addr)}.")
    good = bytes(b for b in (raw[:raw.index(None)] if None in raw else raw))
    out = []
    for ins in md.disasm(good, addr):
        out.append((ins.address, ins.bytes.hex(" ").upper(), ins.mnemonic, ins.op_str, _note(game, ins.op_str)))
        if len(out) >= count:
            break
    if not out:
        out.append((addr, f"{good[0]:02X}" if good else "??", "db", f"0x{good[0]:02x}" if good else "?", ""))
    return out


def _note(game: Game, ops: str) -> str:
    notes = []
    for m in re.finditer(r"0x([0-9a-f]{5,8})", ops):
        a = int(m.group(1), 16)
        if not (game.base <= a < game.base + game.image_size):
            continue
        p = struct.unpack("<I", game.read(a, 4).ljust(4, b"\0"))[0]
        pointed = game.string_at(p) if game.base <= p < game.base + game.image_size else None
        s = None if pointed else (game.string_at(a) or game.string_at(a, wide=True))
        if pointed and len(pointed) >= 3:      # a table of string pointers: show what it points at
            notes.append(f"{game.describe(a)} -> \"{pointed[:40]}\"")
        elif s and len(s) >= 4:
            notes.append(f"{game.describe(a)} \"{s[:40]}\"")
        else:
            notes.append(game.describe(a))
    return "; ".join(notes)


def instruction_before(game: Game, addr: int) -> int:
    """The start of the instruction before addr (x86 has no fixed size, so this
    decodes forward from a little earlier and keeps the boundary that lands on addr)."""
    try:
        import capstone
    except ImportError:
        return addr - 1
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    for back in (32, 24, 16, 48, 64):
        data = game.read(addr - back, back)
        prev = None
        for ins in md.disasm(data, addr - back):
            if ins.address == addr:
                break
            prev = ins.address
            if ins.address + ins.size == addr:
                return ins.address
        if prev is not None and prev < addr:
            continue
    return addr - 1


# --- find what writes / accesses an address (a hardware breakpoint) ----------------------------
#
# Like Cheat Engine's "find out what writes to this address": attach to the game as a debugger
# for a moment, put a CPU data breakpoint on the address in every thread, and count the
# instructions that trigger it. The breakpoint is cleared in every thread before detaching, and
# the game is never closed when the tool detaches. 32-bit game, 64-bit tool: the WOW64 calls.

class _WOW64_FLOATING_SAVE_AREA(ctypes.Structure):
    _fields_ = [("ControlWord", W.DWORD), ("StatusWord", W.DWORD), ("TagWord", W.DWORD),
                ("ErrorOffset", W.DWORD), ("ErrorSelector", W.DWORD), ("DataOffset", W.DWORD),
                ("DataSelector", W.DWORD), ("RegisterArea", ctypes.c_ubyte * 80), ("Cr0NpxState", W.DWORD)]


class _WOW64_CONTEXT(ctypes.Structure):
    _fields_ = [("ContextFlags", W.DWORD), ("Dr0", W.DWORD), ("Dr1", W.DWORD), ("Dr2", W.DWORD),
                ("Dr3", W.DWORD), ("Dr6", W.DWORD), ("Dr7", W.DWORD), ("FloatSave", _WOW64_FLOATING_SAVE_AREA),
                ("SegGs", W.DWORD), ("SegFs", W.DWORD), ("SegEs", W.DWORD), ("SegDs", W.DWORD),
                ("Edi", W.DWORD), ("Esi", W.DWORD), ("Ebx", W.DWORD), ("Edx", W.DWORD), ("Ecx", W.DWORD),
                ("Eax", W.DWORD), ("Ebp", W.DWORD), ("Eip", W.DWORD), ("SegCs", W.DWORD), ("EFlags", W.DWORD),
                ("Esp", W.DWORD), ("SegSs", W.DWORD), ("ExtendedRegisters", ctypes.c_ubyte * 512)]


class _EXCEPTION_RECORD(ctypes.Structure):
    _fields_ = [("ExceptionCode", W.DWORD), ("ExceptionFlags", W.DWORD), ("ExceptionRecord", ctypes.c_void_p),
                ("ExceptionAddress", ctypes.c_void_p), ("NumberParameters", W.DWORD),
                ("ExceptionInformation", ctypes.c_size_t * 15)]


class _DEBUG_EVENT(ctypes.Structure):
    class _U(ctypes.Union):
        class _Exc(ctypes.Structure):
            _fields_ = [("ExceptionRecord", _EXCEPTION_RECORD), ("dwFirstChance", W.DWORD)]

        class _CreateThread(ctypes.Structure):
            _fields_ = [("hThread", W.HANDLE), ("lpThreadLocalBase", ctypes.c_void_p),
                        ("lpStartAddress", ctypes.c_void_p)]

        class _CreateProcess(ctypes.Structure):
            _fields_ = [("hFile", W.HANDLE), ("hProcess", W.HANDLE), ("hThread", W.HANDLE),
                        ("lpBaseOfImage", ctypes.c_void_p), ("dwDebugInfoFileOffset", W.DWORD),
                        ("nDebugInfoSize", W.DWORD), ("lpThreadLocalBase", ctypes.c_void_p),
                        ("lpStartAddress", ctypes.c_void_p), ("lpImageName", ctypes.c_void_p),
                        ("fUnicode", W.WORD)]

        class _LoadDll(ctypes.Structure):
            _fields_ = [("hFile", W.HANDLE), ("lpBaseOfDll", ctypes.c_void_p), ("dwDebugInfoFileOffset", W.DWORD),
                        ("nDebugInfoSize", W.DWORD), ("lpImageName", ctypes.c_void_p), ("fUnicode", W.WORD)]

        _fields_ = [("Exception", _Exc), ("CreateThread", _CreateThread), ("CreateProcessInfo", _CreateProcess),
                    ("LoadDll", _LoadDll), ("_pad", ctypes.c_ubyte * 160)]

    _fields_ = [("dwDebugEventCode", W.DWORD), ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD), ("u", _U)]


_DBG_CONTINUE, _DBG_EXCEPTION_NOT_HANDLED = 0x00010002, 0x80010001
_EXCEPTION_DEBUG_EVENT, _CREATE_THREAD_DEBUG_EVENT, _CREATE_PROCESS_DEBUG_EVENT = 1, 2, 3
_EXIT_THREAD_DEBUG_EVENT, _EXIT_PROCESS_DEBUG_EVENT, _LOAD_DLL_DEBUG_EVENT = 4, 5, 6
_SINGLE_STEPS = (0x80000004, 0x4000001E)          # EXCEPTION_SINGLE_STEP, STATUS_WX86_SINGLE_STEP
_BREAKPOINTS = (0x80000003, 0x4000001F)           # EXCEPTION_BREAKPOINT, STATUS_WX86_BREAKPOINT
_WOW64_CONTEXT_DEBUG_REGISTERS = 0x00010010
_WOW64_CONTEXT_CONTROL = 0x00010001
_THREAD_ALL = 0x001F03FF


def _dr7(access: str, size: int) -> int:
    rw = {"execute": 0b00, "write": 0b01, "access": 0b11}[access]
    ln = {1: 0b00, 2: 0b01, 4: 0b11}[size]
    return 0x1 | (rw << 16) | (ln << 18)


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ThreadID", W.DWORD),
                ("th32OwnerProcessID", W.DWORD), ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
                ("dwFlags", W.DWORD)]


def _setup_debug_api():
    k = _k32
    k.Wow64GetThreadContext.argtypes = [W.HANDLE, ctypes.POINTER(_WOW64_CONTEXT)]
    k.Wow64SetThreadContext.argtypes = [W.HANDLE, ctypes.POINTER(_WOW64_CONTEXT)]
    k.WaitForDebugEvent.argtypes = [ctypes.POINTER(_DEBUG_EVENT), W.DWORD]
    k.ContinueDebugEvent.argtypes = [W.DWORD, W.DWORD, W.DWORD]
    k.OpenThread.restype = W.HANDLE
    k.OpenThread.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.CreateToolhelp32Snapshot.restype = W.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    k.Thread32First.argtypes = [W.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    k.Thread32Next.argtypes = [W.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    k.SuspendThread.argtypes = [W.HANDLE]
    k.ResumeThread.argtypes = [W.HANDLE]
    k.CloseHandle.argtypes = [W.HANDLE]
    return k


def find_writers(game: Game, addr: int, seconds: float = 2.0, access: str = "write", size: int = 4,
                 max_hits: int = 5000) -> dict:
    """{instruction address: times it hit} for the instructions that write (or, with
    access='access', read or write) `size` bytes at addr during `seconds`.

    Safety: the game's own exceptions are always passed back to it unhandled (they're part of
    how it runs); the breakpoint is cleared in every thread - and checked - before detaching;
    and detaching never closes the game."""
    if size not in (1, 2, 4) or addr % size:
        raise GameMemoryError(f"A hardware breakpoint watches 1, 2 or 4 aligned bytes - 0x{addr:08X} isn't "
                              f"aligned to {size}.")
    k = _setup_debug_api()
    if not k.DebugActiveProcess(game.pid):
        raise GameMemoryError(f"Couldn't attach to the game (error {ctypes.get_last_error()}) - is another "
                              "debugger attached? Running as administrator can help.")
    k.DebugSetProcessKillOnExit(False)
    threads = {}       # thread id -> handle
    own = set()        # handles we opened (debug-event handles are closed by the system)
    hits = {}
    state = {"armed": False, "seen_attach": False}

    def set_dr(h, on) -> bool:
        ctx = _WOW64_CONTEXT()
        ctx.ContextFlags = _WOW64_CONTEXT_DEBUG_REGISTERS
        if not k.Wow64GetThreadContext(h, ctypes.byref(ctx)):
            return True                      # not a 32-bit thread (or gone): nothing to do
        ctx.Dr0 = addr if on else 0
        ctx.Dr7 = _dr7(access, size) if on else 0
        ctx.Dr6 = 0
        k.Wow64SetThreadContext(h, ctypes.byref(ctx))
        check = _WOW64_CONTEXT()
        check.ContextFlags = _WOW64_CONTEXT_DEBUG_REGISTERS
        if not k.Wow64GetThreadContext(h, ctypes.byref(check)):
            return True
        return (check.Dr7 & 0xFF) == (1 if on else 0)

    def collect_threads():
        snap = k.CreateToolhelp32Snapshot(0x4, 0)   # TH32CS_SNAPTHREAD
        if not snap or snap == W.HANDLE(-1).value:
            return
        te = _THREADENTRY32()
        te.dwSize = ctypes.sizeof(te)
        ok = k.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == game.pid and te.th32ThreadID not in threads:
                h = k.OpenThread(_THREAD_ALL, False, te.th32ThreadID)
                if h:
                    threads[te.th32ThreadID] = h
                    own.add(h)
            ok = k.Thread32Next(snap, ctypes.byref(te))
        k.CloseHandle(snap)

    def handle(ev) -> int:
        """Deal with one debug event; returns how to continue it."""
        code = ev.dwDebugEventCode
        if code == _CREATE_PROCESS_DEBUG_EVENT:
            threads[ev.dwThreadId] = ev.u.CreateProcessInfo.hThread
            if ev.u.CreateProcessInfo.hFile:
                k.CloseHandle(ev.u.CreateProcessInfo.hFile)
        elif code == _CREATE_THREAD_DEBUG_EVENT:
            threads[ev.dwThreadId] = ev.u.CreateThread.hThread
            if state["armed"]:
                set_dr(ev.u.CreateThread.hThread, True)
        elif code == _EXIT_THREAD_DEBUG_EVENT:
            threads.pop(ev.dwThreadId, None)
        elif code == _LOAD_DLL_DEBUG_EVENT:
            if ev.u.LoadDll.hFile:
                k.CloseHandle(ev.u.LoadDll.hFile)
        elif code == _EXCEPTION_DEBUG_EVENT:
            exc = ev.u.Exception.ExceptionRecord.ExceptionCode
            if exc in _BREAKPOINTS and not state["seen_attach"]:
                state["seen_attach"] = True          # the attach breakpoint (ours)
                return _DBG_CONTINUE
            if exc in _BREAKPOINTS and ev.u.Exception.dwFirstChance and code == _EXCEPTION_DEBUG_EVENT \
                    and exc == 0x4000001F:
                return _DBG_CONTINUE                 # WOW64's own copy of the attach breakpoint
            if exc in _SINGLE_STEPS:
                # The game never single-steps itself, so every one of these is our breakpoint - even
                # when WOW64 reports Dr6 empty or the thread's context can't be read. Handing one to
                # the game would crash it. The event's own address is the instruction after the write.
                eip = ev.u.Exception.ExceptionRecord.ExceptionAddress or 0
                hits[eip] = hits.get(eip, 0) + 1
                h = threads.get(ev.dwThreadId)
                if h:
                    ctx = _WOW64_CONTEXT()
                    ctx.ContextFlags = _WOW64_CONTEXT_DEBUG_REGISTERS
                    if k.Wow64GetThreadContext(h, ctypes.byref(ctx)):
                        ctx.Dr6 = 0
                        k.Wow64SetThreadContext(h, ctypes.byref(ctx))
                return _DBG_CONTINUE
            return _DBG_EXCEPTION_NOT_HANDLED        # anything else is the game's own: let it handle it
        elif code == _EXIT_PROCESS_DEBUG_EVENT:
            state["gone"] = True
        return _DBG_CONTINUE

    ev = _DEBUG_EVENT()
    deadline = time.time() + 5          # time to see the attach breakpoint
    try:
        while time.time() < deadline and not state.get("gone"):
            if not k.WaitForDebugEvent(ctypes.byref(ev), 100):
                continue
            cont = handle(ev)
            if state["seen_attach"] and not state["armed"]:
                # every thread is stopped at a debug event: arm them all now
                collect_threads()
                for h in threads.values():
                    set_dr(h, True)
                state["armed"] = True
                deadline = time.time() + seconds
            k.ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, cont)
            if sum(hits.values()) >= max_hits:
                break
        if state.get("gone"):
            raise GameMemoryError("The game closed.")
    finally:
        if not state.get("gone"):
            # disarm every thread while it's suspended, and check it took
            collect_threads()
            stuck = []
            for tid, h in list(threads.items()):
                if k.SuspendThread(h) == 0xFFFFFFFF:
                    continue
                for _ in range(3):
                    if set_dr(h, False):
                        break
                else:
                    stuck.append(tid)
                k.ResumeThread(h)
            # finish anything already queued - with the same rules as above - then detach
            while k.WaitForDebugEvent(ctypes.byref(ev), 100):
                k.ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, handle(ev))
            k.DebugActiveProcessStop(game.pid)
            FIND_WRITERS_LAST.clear()
            FIND_WRITERS_LAST.update(threads=len(threads), stuck=stuck, hits=sum(hits.values()))
        for h in own:
            k.CloseHandle(h)
    # a data breakpoint stops after the instruction: report the instruction itself
    return {instruction_before(game, eip): n for eip, n in sorted(hits.items(), key=lambda x: -x[1])}


FIND_WRITERS_LAST = {}   # threads armed, threads that wouldn't disarm, hits - from the last run


# --- pointer paths (so a cheat can reach memory that moves) ------------------------------------

def pointer_paths(game: Game, target: int, max_offset: int = 0x1000, levels: int = 2, limit: int = 12,
                  progress=None) -> list:
    """Chains from Battlefront.exe's own data to `target`: [(static offset, [offsets...])], where
    target = [[ba + static] + o1] + o2 ... The exe's data never moves, so a cheat that follows the
    chain finds the value again after a restart (if the game builds the same objects). Shortest
    chains and smallest offsets first."""
    import numpy as np
    statics = []      # (address, value) of every pointer-sized word in the exe's writable data
    for s, n in game.regions(scope="exe"):
        if "write" not in game.where(s):     # real pointers live in the data, not in code bytes
            continue
        d = game.read(s, n)
        a = np.frombuffer(d[:len(d) // 4 * 4], dtype=np.uint32)
        statics.append((s, a))
    s_addr = np.concatenate([s + 4 * np.arange(len(a), dtype=np.int64) for s, a in statics]) if statics else np.array([])
    s_val = np.concatenate([a.astype(np.int64) for _s, a in statics]) if statics else np.array([])
    order = np.argsort(s_val)
    s_val_sorted, s_addr_sorted = s_val[order], s_addr[order]

    def statics_pointing_near(node):
        lo = np.searchsorted(s_val_sorted, node - max_offset, side="left")
        hi = np.searchsorted(s_val_sorted, node, side="right")
        return [(int(s_addr_sorted[i]), node - int(s_val_sorted[i])) for i in range(lo, min(hi, lo + 50))]

    paths = []
    for sa, off in statics_pointing_near(target):            # one level: [ba+S]+off
        paths.append((sa - game.base, [off]))
    if levels >= 2:
        # heap words pointing just below the target, then statics pointing at those
        nodes = []
        regions = list(game.regions(scope="writable"))
        total = sum(n for _s, n in regions) or 1
        done = 0
        for s, n in regions:
            d = game.read(s, n)
            a = np.frombuffer(d[:len(d) // 4 * 4], dtype=np.uint32).astype(np.int64)
            idx = np.nonzero((a <= target) & (a >= target - max_offset))[0]
            for i in idx[:5000]:
                addr = s + 4 * int(i)
                if not (game.base <= addr < game.base + game.image_size):
                    nodes.append((addr, target - int(a[i])))
            done += n
            if progress:
                progress(done / total)
            if len(nodes) > 20000:
                break
        for node, off2 in nodes:
            for sa, off1 in statics_pointing_near(node):
                paths.append((sa - game.base, [off1, off2]))
    seen, out = set(), []
    for p in sorted(paths, key=lambda p: (len(p[1]), sum(p[1]))):
        key = (p[0], tuple(p[1]))
        if key in seen:
            continue
        seen.add(key)
        try:
            if follow_path(game, *p) == target:
                out.append(p)
        except GameMemoryError:
            pass
        if len(out) >= limit:
            break
    return out


def follow_path(game: Game, static: int, offsets: list) -> int:
    a = game.u32(game.base + static)
    for o in offsets[:-1]:
        a = game.u32(a + o)
    return a + offsets[-1]


def path_expr(static: int, offsets: list) -> str:
    """The address expression for a chain: [[+S]+o1]+o2."""
    e = f"+{static:X}"
    for o in offsets[:-1]:
        e = f"[{e}]+{o:X}"
    return f"[{e}]+{offsets[-1]:X}"


def code_for_pointer_write(static: int, offsets: list, data: bytes) -> str:
    """Code lines that follow the chain, then keep `data` there."""
    def t(kind, off):
        return f"{kind | (1 if off >= 0x1000000 else 0):02X}{off & 0xFFFFFF:06X}"
    lines = [f"{t(0x48, static)} 00000000"]
    for o in offsets[:-1]:
        lines.append(f"{t(0x58, o)} 00000000")
    last = offsets[-1]
    if len(data) == 1:
        lines.append(f"{t(0x10, last)} {data[0]:08X}")
    elif len(data) == 2:
        lines.append(f"{t(0x12, last)} {struct.unpack('<H', data)[0]:08X}")
    elif len(data) == 4:
        lines.append(f"{t(0x14, last)} {struct.unpack('<I', data)[0]:08X}")
    else:
        lines.append(f"{t(0x16, last)} {len(data):08X}")
        padded = data + bytes(-len(data) % 8)
        lines += [f"{padded[i:i + 4].hex().upper()} {padded[i + 4:i + 8].hex().upper()}" for i in range(0, len(padded), 8)]
    lines.append("E0000000 00000000")
    return "\n".join(lines)


# --- the watch list ----------------------------------------------------------------------

WATCH_KINDS = tuple(MEM_TYPES) + ("text",)


@dataclass
class Watch:
    name: str
    expr: str            # an address expression: +4E6D53, 0x00746D53, [+6D9420]+10
    kind: str = "4 bytes"
    frozen: bool = False
    value: str = ""      # what to keep it at when frozen

    def read(self, game: Game):
        addr = game.resolve(self.expr)
        if self.kind == "text":
            return game.string_at(addr, 64) or ""
        v = mem_unpack(self.kind, game.read(addr, MEM_TYPES[self.kind][1]))
        if v is None:
            raise GameMemoryError("unreadable")
        return f"{v:.6g}" if isinstance(v, float) else str(v)

    def write(self, game: Game, text: str):
        addr = game.resolve(self.expr)
        data = text.encode("latin1") + b"\0" if self.kind == "text" else mem_pack(self.kind, text)
        game.write(addr, data)


class WatchFile:
    """The watch list, saved as watchlist.json next to the tool."""

    def __init__(self, folder: Path):
        self.path = Path(folder) / "watchlist.json"

    def load(self) -> list:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [Watch(**{k: r[k] for k in ("name", "expr", "kind", "frozen", "value") if k in r})
                for r in rows if isinstance(r, dict) and "expr" in r]

    def save(self, watches: list):
        self.path.write_text(json.dumps([w.__dict__ for w in watches], indent=1), encoding="utf-8")


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
    # From the console variables the exe registers (name, address, type) - see the Memory tab.
    ("Show everyone on the minimap", "The debug 'map.displayall' flag: every unit on the map, enemies too.",
     "004FD9B0 00000001"),
    ("Fly higher", "Raises the flying ceiling ('ai.playermaxflyheight', normally 200) to 10000.",
     "044A0B1C 461C4000\n044A0B18 C61C4000"),
    ("Autopilot", "The debug 'aimode' flag: the AI takes over your soldier.", "004A0AB0 00000001"),
    ("Easy enemies", "Enemy AI skill ('ai.EnemySkillLevel') set to 0.", "044AC084 00000000"),
    ("Elite enemies", "Enemy AI skill ('ai.EnemySkillLevel') set to 20.", "044AC084 00000014"),
    ("Elite allies", "Your team's AI skill ('ai.PlayerSkillLevel') set to 20.", "044AC08C 00000014"),
    ("Hide first-person weapon", "First-person weapon scale ('FirstPerson.Scale') set to 0.", "044780DC 00000000"),
    ("Giant first-person weapon", "First-person weapon scale ('FirstPerson.Scale') set to 3.", "044780DC 40400000"),
    ("AI camera", "The debug 'ai.cam' camera.", "044A0B04 00000001"),
    ("Show AI paths", "The debug 'ai.showallpaths' display.", "004A0AB4 00000001"),
    ("Show AI hints", "The debug 'ai.showhints' display.", "004A0AB2 00000001"),
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
            have = {c.name for c in cheats}
            new = [c for c in default_cheats() if c.name not in have]   # built-ins added since it was made
            if new:
                cheats += new
                try:
                    self.path.write_text(format_cheats(cheats), encoding="utf-8")
                except OSError:
                    pass
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
        self.frozen = []      # Watch entries kept at their value (the watch list's Freeze)
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

    def set_frozen(self, watches):
        with self.lock:
            self.frozen = [w for w in watches if w.frozen and w.value != ""]

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
            if self.game is not None:
                with self.lock:
                    frozen = list(self.frozen)
                for w in frozen:
                    try:
                        w.write(self.game, w.value)
                    except (GameMemoryError, ValueError, struct.error):
                        pass   # a pointer path that doesn't lead anywhere yet (no soldier spawned...)
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
        self.watch_tab_frame = self._watch_tab(nb)
        nb.add(self.watch_tab_frame, text="Watch")

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

    VIEWS = ("Hex", "4-byte numbers", "Floats", "Assembly")
    _LABEL_W = 24        # columns before the data: the address, then two spaces
    _FIELD_W = 15        # one number in the numbers / floats views

    def _memory_tab(self, nb):
        px = self.px
        f = ttk.Frame(nb, padding=px(8))
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text="◀", width=3, command=lambda: self._history(-1)).pack(side="left")
        ttk.Button(row, text="▶", width=3, command=lambda: self._history(1)).pack(side="left", padx=(2, 8))
        ttk.Label(row, text="Address:").pack(side="left")
        self.addr_var = tk.StringVar(value="+4E6D50")
        e = ttk.Entry(row, textvariable=self.addr_var, width=26)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda ev: self._goto())
        self.addr_entry = e
        ttk.Button(row, text="Go", command=self._goto).pack(side="left")
        ttk.Label(row, text="View:").pack(side="left", padx=(12, 2))
        self.view = tk.StringVar(value="Hex")
        cb = ttk.Combobox(row, textvariable=self.view, values=self.VIEWS, width=15, state="readonly")
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda ev: self._view_changed())
        ttk.Button(row, text="▲", width=3, command=lambda: self._page(-1)).pack(side="left", padx=(12, 0))
        ttk.Button(row, text="▼", width=3, command=lambda: self._page(1)).pack(side="left", padx=2)
        row_b = ttk.Frame(f)
        row_b.pack(fill="x", pady=(4, 0))
        ttk.Label(row_b, text="Bookmarks:").pack(side="left")
        self.bm_var = tk.StringVar()
        self.bm_box = ttk.Combobox(row_b, textvariable=self.bm_var, width=40, state="readonly")
        self.bm_box.pack(side="left", padx=4)
        self.bm_box.bind("<<ComboboxSelected>>", lambda ev: self._goto_bookmark())
        ttk.Button(row_b, text="Add bookmark", command=self._add_bookmark).pack(side="left")
        ttk.Button(row_b, text="Remove", command=self._remove_bookmark).pack(side="left", padx=4)
        ttk.Label(row_b, text="+4E6D53 = Battlefront.exe+4E6D53, 0x... = absolute, [+6D9420]+10 follows a pointer",
                  foreground="gray").pack(side="left", padx=10)
        self.region_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.region_var, foreground="gray").pack(anchor="w", pady=(4, 0))
        self.hex = self._text(f, width=104, height=HEX_ROWS, wrap="none", cursor="arrow")
        self.hex.pack(fill="both", expand=True, pady=px(4))
        self.hex.tag_configure("changed", foreground="#e8a33d")
        self.hex.tag_configure("unreadable", foreground="#6b6e78")
        self.hex.tag_configure("note", foreground="#7aaaff")
        self.hex.tag_configure("sel_byte", background=self.colors["select_bg"], foreground=self.colors["select_fg"])
        self.hex.bind("<Button-1>", self._hex_click)
        self.hex.bind("<Double-1>", self._hex_double)
        self.hex.bind("<MouseWheel>", lambda ev: (self._rows(-4 if ev.delta > 0 else 4), "break")[1])
        for key, fn in (("<Prior>", lambda: self._page(-1)), ("<Next>", lambda: self._page(1)),
                        ("<Up>", lambda: self._rows(-1)), ("<Down>", lambda: self._rows(1)),
                        ("<Alt-Left>", lambda: self._history(-1)), ("<Alt-Right>", lambda: self._history(1))):
            self.hex.bind(key, lambda ev, fn=fn: (fn(), "break")[1])
        self.bind("<Control-g>", lambda ev: (self.addr_entry.focus_set(), self.addr_entry.select_range(0, "end")))
        self.hex.configure(state="disabled")
        self.sel_info = tk.StringVar(value="Click a byte (or an instruction) to inspect it. Double-click to edit it, "
                                           "or to follow a jump or call.")
        ttk.Label(f, textvariable=self.sel_info, font=("Consolas", 9), justify="left").pack(anchor="w")
        tools = ttk.Frame(f)
        tools.pack(fill="x", pady=(4, 0))
        for text, fn in (("Follow pointer", self._follow_pointer), ("Show as assembly", self._show_asm_here),
                         ("Copy address", self._copy_address), ("Add to watch list", self._watch_selected)):
            # "What writes here?" (find_writers) is left out: in testing it crashed the game when the
            # watched address was a soldier's animation data during a battle.
            ttk.Button(tools, text=text, command=fn).pack(side="left", padx=(0, 6))
        w = ttk.Frame(f)
        w.pack(fill="x", pady=(6, 0))
        ttk.Label(w, text="Value:").pack(side="left")
        self.write_type = tk.StringVar(value="byte")
        ttk.Combobox(w, textvariable=self.write_type, values=list(MEM_TYPES) + ["text", "bytes"], width=8,
                     state="readonly").pack(side="left", padx=4)
        self.write_val = tk.StringVar()
        ttk.Entry(w, textvariable=self.write_val, width=24).pack(side="left")
        ttk.Button(w, text="Write once", command=self._write_once).pack(side="left", padx=6)
        ttk.Button(w, text="Make cheat (keep it set)...", command=self._make_cheat).pack(side="left")
        self.hist, self.hist_i = [], -1
        self.line_addrs, self.asm_lines, self.asm_next = [], [], None
        self.bm_path = app_dir() / "memory_bookmarks.json"
        self._load_bookmarks()
        return f

    def _game(self):
        return self.runner.game

    def _need_game(self, title="Memory"):
        g = self._game()
        if g is None:
            messagebox.showinfo(title, "Start the game first - this window attaches to it by itself.", parent=self)
        return g

    # navigation

    def _goto(self):
        g = self._need_game()
        if g is None:
            return
        try:
            addr = g.resolve(self.addr_var.get())
        except GameMemoryError as exc:
            messagebox.showerror("Memory", str(exc), parent=self)
            return
        self._go(addr, select=addr)

    def _go(self, addr, select=None, push=True, view=None):
        g = self._game()
        if g is None:
            return
        if view:
            self.view.set(view)
        addr = max(0, addr)
        self.mem_addr = addr if self.view.get() == "Assembly" else addr & ~0xF
        if select is not None:
            self.sel_addr = select
        if push:
            self.hist = self.hist[:self.hist_i + 1] + [(self.mem_addr, self.view.get(), self.sel_addr)]
            self.hist = self.hist[-100:]
            self.hist_i = len(self.hist) - 1
        self.addr_var.set(g.describe(self.sel_addr if self.sel_addr is not None else self.mem_addr))
        self.region_var.set(f"{g.describe(self.mem_addr)}: {g.where(self.mem_addr)}")
        self.mem_prev = []
        self._refresh_hex()

    def _history(self, step):
        i = self.hist_i + step
        if 0 <= i < len(self.hist):
            self.hist_i = i
            addr, view, sel = self.hist[i]
            self.view.set(view)
            self._go(addr, select=sel, push=False)

    def _view_changed(self):
        if self.mem_addr is not None:
            target = self.sel_addr if self.sel_addr is not None else self.mem_addr
            self._go(target, select=self.sel_addr)

    def _page(self, step):
        self._rows(step * HEX_ROWS)

    def _rows(self, step):
        if self.mem_addr is None or self._game() is None:
            return
        g = self._game()
        if self.view.get() == "Assembly":
            addr = self.mem_addr
            if step > 0:
                if step >= HEX_ROWS and self.asm_next:
                    addr = self.asm_next
                elif len(self.line_addrs) > step:
                    addr = self.line_addrs[step]
            else:
                for _ in range(-step):
                    addr = instruction_before(g, addr)
            self._go(addr, push=False)
        else:
            self._go(self.mem_addr + 16 * step, push=False)

    # drawing

    def _refresh_hex(self):
        g = self._game()
        if g is None or self.mem_addr is None:
            return
        if self.view.get() == "Assembly":
            self._refresh_asm(g)
            return
        data = g.read_safe(self.mem_addr, HEX_ROWS * 16)
        prev = self.mem_prev if len(self.mem_prev) == len(data) else []
        t = self.hex
        t.configure(state="normal")
        t.delete("1.0", "end")
        view = self.view.get()
        L, FW = self._LABEL_W, self._FIELD_W
        tags = []   # (tag, line, col, width)
        for r in range(HEX_ROWS):
            a = self.mem_addr + r * 16
            chunk = data[r * 16:r * 16 + 16]
            label = g.describe(a).rjust(L - 2) + "  "
            if view == "Hex":
                cells = []
                for k, b in enumerate(chunk):
                    cells.append("??" if b is None else f"{b:02X}")
                    if b is None:
                        tags.append(("unreadable", r, L + 3 * k, 2))
                    elif prev and prev[r * 16 + k] != b:
                        tags.append(("changed", r, L + 3 * k, 2))
                text = "".join("." if b is None or not 32 <= b < 127 else chr(b) for b in chunk)
                t.insert("end", label + " ".join(cells).ljust(48) + "  " + text + "\n")
            else:
                fields = []
                for k in range(4):
                    four = chunk[4 * k:4 * k + 4]
                    if None in four or len(four) < 4:
                        fields.append("??".rjust(FW - 1))
                        tags.append(("unreadable", r, L + FW * k, FW - 1))
                        continue
                    raw = bytes(four)
                    if view == "Floats":
                        v = struct.unpack("<f", raw)[0]
                        fields.append((f"{v:.6g}" if math.isfinite(v) else str(v)).rjust(FW - 1))
                    else:
                        fields.append(str(struct.unpack("<i", raw)[0]).rjust(FW - 1))
                    if prev and prev[r * 16 + 4 * k:r * 16 + 4 * k + 4] != four:
                        tags.append(("changed", r, L + FW * k, FW - 1))
                t.insert("end", label + " ".join(fields) + "\n")
        for tag, r, col, w in tags:
            t.tag_add(tag, f"{r + 1}.{col}", f"{r + 1}.{col + w}")
        if self.sel_addr is not None and self.mem_addr <= self.sel_addr < self.mem_addr + HEX_ROWS * 16:
            off = self.sel_addr - self.mem_addr
            r, k = divmod(off, 16)
            if view == "Hex":
                t.tag_add("sel_byte", f"{r + 1}.{L + k * 3}", f"{r + 1}.{L + k * 3 + 2}")
            else:
                c = L + FW * (k // 4)
                t.tag_add("sel_byte", f"{r + 1}.{c}", f"{r + 1}.{c + FW - 1}")
        if self.sel_addr is not None:
            self._show_selection(g)
        t.configure(state="disabled")
        self.mem_prev = data

    def _refresh_asm(self, g):
        t = self.hex
        t.configure(state="normal")
        t.delete("1.0", "end")
        try:
            rows = disassemble(g, self.mem_addr, HEX_ROWS)
        except GameMemoryError as exc:
            t.insert("end", str(exc))
            t.configure(state="disabled")
            self.line_addrs, self.asm_lines = [], []
            return
        self.line_addrs = [r[0] for r in rows]
        self.asm_lines = rows
        last = rows[-1]
        self.asm_next = last[0] + len(last[1].split())
        for i, (a, hexb, mn, ops, note) in enumerate(rows):
            line = f"{g.describe(a).rjust(self._LABEL_W - 2)}  {hexb[:20]:<21} {mn:<7} {ops}"
            t.insert("end", line)
            if note:
                t.insert("end", f"   ; {note}", "note")
            t.insert("end", "\n")
            if a == self.sel_addr:
                t.tag_add("sel_byte", f"{i + 1}.0", f"{i + 1}.end")
        t.configure(state="disabled")
        if self.sel_addr is not None:
            self._show_selection(g)

    # clicks

    def _hit(self, event):
        """The address under the mouse, or None."""
        idx = self.hex.index(f"@{event.x},{event.y}")
        line, col = (int(x) for x in idx.split("."))
        if self.mem_addr is None or line > HEX_ROWS:
            return None
        if self.view.get() == "Assembly":
            return self.line_addrs[line - 1] if line - 1 < len(self.line_addrs) else None
        L = self._LABEL_W
        if self.view.get() == "Hex":
            if not L <= col < L + 47:
                return None
            return self.mem_addr + (line - 1) * 16 + (col - L) // 3
        k = (col - L) // self._FIELD_W
        if not (col >= L and 0 <= k < 4):
            return None
        return self.mem_addr + (line - 1) * 16 + 4 * k

    def _hex_click(self, event):
        self.hex.focus_set()
        a = self._hit(event)
        if a is not None:
            self.sel_addr = a
            g = self._game()
            if g:
                self.addr_var.set(g.describe(a))
            self._refresh_hex()
        return "break"

    def _hex_double(self, event):
        g = self._game()
        a = self._hit(event)
        if g is None or a is None:
            return "break"
        self.sel_addr = a
        if self.view.get() == "Assembly":
            row = next((r for r in self.asm_lines if r[0] == a), None)
            m = re.search(r"0x([0-9a-f]+)", row[3]) if row else None
            if m:
                target = int(m.group(1), 16)
                code = row[2].startswith(("j", "call", "loop"))
                self._go(target, select=target, view="Assembly" if code else "Hex")
            return "break"
        if self.view.get() == "Hex":
            cur = g.read(a, 1)
            text = simpledialog.askstring("Edit memory", f"New bytes at {g.describe(a)} (hex, e.g. 90 90):",
                                          initialvalue=cur.hex().upper(), parent=self)
            if text:
                try:
                    g.write(a, bytes.fromhex(text.replace(" ", "")))
                except (ValueError, GameMemoryError) as exc:
                    messagebox.showerror("Memory", f"Couldn't write: {exc}", parent=self)
        else:
            kind = "float" if self.view.get() == "Floats" else "4 bytes"
            cur = mem_unpack(kind, g.read(a, 4))
            text = simpledialog.askstring("Edit memory", f"New {kind} value at {g.describe(a)}:",
                                          initialvalue=str(cur), parent=self)
            if text:
                try:
                    g.write(a, mem_pack(kind, text))
                except (ValueError, struct.error, GameMemoryError) as exc:
                    messagebox.showerror("Memory", f"Couldn't write: {exc}", parent=self)
        self._refresh_hex()
        return "break"

    def _show_selection(self, g):
        a = self.sel_addr
        d = g.read_safe(a, 8)
        if None in d[:1]:
            self.sel_info.set(f"{g.describe(a)}: can't be read ({g.where(a)})")
            return
        raw = bytes(b or 0 for b in d)
        u8, i8 = raw[0], struct.unpack("<b", raw[:1])[0]
        u16, i16 = struct.unpack("<H", raw[:2])[0], struct.unpack("<h", raw[:2])[0]
        u32, i32 = struct.unpack("<I", raw[:4])[0], struct.unpack("<i", raw[:4])[0]
        f32, f64 = struct.unpack("<f", raw[:4])[0], struct.unpack("<d", raw[:8])[0]
        lines = [f"{g.describe(a)}  ({g.where(a)})",
                 f"byte {u8} / {i8}    2 bytes {u16} / {i16}    4 bytes {u32} / {i32} (0x{u32:08X})",
                 f"float {f32:.7g}    double {f64:.10g}"]
        target = g.where(u32)
        if target != "not mapped":
            s = g.string_at(u32) or g.string_at(u32, wide=True)
            lines.append(f"pointer -> {g.describe(u32)}  {target}" + (f'  "{s[:50]}"' if s else ""))
        s = g.string_at(a)
        w = g.string_at(a, wide=True)
        if s or w:
            lines.append("text here: " + (f'"{s[:60]}"' if s else "") + (f'  UTF-16 "{w[:40]}"' if w else ""))
        self.sel_info.set("\n".join(lines))

    # buttons

    def _follow_pointer(self):
        g = self._need_game()
        if g is None or self.sel_addr is None:
            return
        p = struct.unpack("<I", g.read(self.sel_addr, 4).ljust(4, b"\0"))[0]
        if g.where(p) == "not mapped":
            messagebox.showinfo("Memory", f"0x{p:08X} isn't a valid address.", parent=self)
            return
        self._go(p, select=p)

    def _show_asm_here(self):
        if self.sel_addr is not None or self.mem_addr is not None:
            a = self.sel_addr if self.sel_addr is not None else self.mem_addr
            self._go(a, select=a, view="Assembly")

    def _copy_address(self):
        g = self._game()
        if g and self.sel_addr is not None:
            self.clipboard_clear()
            self.clipboard_append(g.describe(self.sel_addr))

    def _watch_selected(self):
        g = self._game()
        if g is None or self.sel_addr is None:
            return
        kind = self.write_type.get() if self.write_type.get() in WATCH_KINDS else "4 bytes"
        self._add_watch(Watch(g.describe(self.sel_addr), g.describe(self.sel_addr), kind))

    def _find_writers(self, access):
        g = self._need_game()
        if g is None or self.sel_addr is None:
            if g is not None:
                messagebox.showinfo("Memory", "Click the value to watch first.", parent=self)
            return
        addr = self.sel_addr & ~3
        verb = "write to" if access == "write" else "read or write"
        if not messagebox.askokcancel(
                "Find what " + ("writes" if access == "write" else "reads/writes"),
                f"For the next 3 seconds, every instruction that tries to {verb} {g.describe(addr)} "
                "(4 bytes) is recorded, using a CPU breakpoint. The tool attaches to the game as a "
                "debugger for that time; it may hitch for a moment.\n\nDo whatever changes the value in "
                "game during those 3 seconds.", parent=self):
            return
        result = {}

        def work():
            try:
                result["hits"] = find_writers(g, addr, seconds=3.0, access=access)
            except GameMemoryError as exc:
                result["err"] = str(exc)
            result["done"] = True

        threading.Thread(target=work, daemon=True).start()
        self.sel_info.set(f"Watching {g.describe(addr)} for 3 seconds...")

        def poll():
            if not result.get("done"):
                self.after(150, poll)
                return
            if "err" in result:
                messagebox.showerror("Memory", result["err"], parent=self)
                return
            self._writers_window(g, addr, access, result["hits"])

        poll()

    def _writers_window(self, g, addr, access, hits):
        win = tk.Toplevel(self)
        win.title(f"What {'writes' if access == 'write' else 'reads/writes'} {g.describe(addr)}")
        win.configure(bg=self.colors["bg"])
        f = ttk.Frame(win, padding=self.px(8))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=(f"{sum(hits.values())} hits from {len(hits)} instructions. Double-click one to see "
                           "its code." if hits else "Nothing touched it in those 3 seconds - try again while "
                           "doing something in game that changes it.")).pack(anchor="w")
        tree = ttk.Treeview(f, columns=("addr", "count", "asm"), show="headings", height=12)
        for col, text, w in (("addr", "Instruction", 190), ("count", "Times", 70), ("asm", "Code", 420)):
            tree.heading(col, text=text)
            tree.column(col, width=self.px(w))
        tree.pack(fill="both", expand=True, pady=self.px(6))
        rows = list(hits.items())
        for i, (a, n) in enumerate(rows):
            try:
                r = disassemble(g, a, 1)[0]
                asm = f"{r[2]} {r[3]}"
            except GameMemoryError:
                asm = "?"
            tree.insert("", "end", iid=str(i), values=(g.describe(a), n, asm))

        def open_it(_e=None):
            sel = tree.selection()
            if sel:
                a = rows[int(sel[0])][0]
                self._go(a, select=a, view="Assembly")
                self.nb.select(self.mem_tab)

        tree.bind("<Double-1>", open_it)

    def _write_data(self):
        kind, text = self.write_type.get(), self.write_val.get()
        if kind == "text":
            return text.encode("latin1", "replace") + b"\0"
        if kind == "bytes":
            return bytes.fromhex(text.replace(" ", ""))
        return mem_pack(kind, text)

    def _write_once(self):
        g = self._game()
        if g is None or self.sel_addr is None:
            messagebox.showinfo("Memory", "Click the byte to write at first.", parent=self)
            return
        try:
            g.write(self.sel_addr, self._write_data())
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
        try:
            if kind is None:
                kind, value = self.write_type.get(), self.write_val.get()
                if not value.strip() and kind in MEM_TYPES:   # nothing typed: keep what's there now
                    v = mem_unpack(kind, g.read(addr, MEM_TYPES[kind][1]))
                    value = f"{v:.6g}" if isinstance(v, float) else str(v)
                    self.write_val.set(value)
                data = self._write_data()
            else:
                data = mem_pack(kind, value)
        except (ValueError, struct.error) as exc:
            messagebox.showerror("Memory", f"'{value}' isn't a {kind} value: {exc}", parent=self)
            return
        name, note = f"Set {g.describe(addr)} to {value}", f"{kind} {value}"
        off = addr - g.base
        if 0 <= off < g.image_size:
            self._new_cheat(code=code_for_write(off, data), name=name, note=note)
            return
        # memory the game allocates while running moves around: find a pointer chain to it
        self.sel_info.set("That address moves each time the game runs - looking for a pointer chain to it...")
        result = {}

        def work():
            try:
                result["paths"] = pointer_paths(g, addr, progress=lambda p: result.update(p=p))
            except GameMemoryError as exc:
                result["err"] = str(exc)
            result["done"] = True

        threading.Thread(target=work, daemon=True).start()

        def poll():
            if not result.get("done"):
                self.sel_info.set(f"Looking for a pointer chain to {g.describe(addr)}... "
                                  f"{int(100 * result.get('p', 0))}%")
                self.after(200, poll)
                return
            paths = result.get("paths") or []
            if not paths:
                if messagebox.askyesno(
                        "Make cheat", f"{result.get('err') or 'No pointer chain from Battlefront.exe leads to it.'}"
                        "\n\nA cheat code can't reach this address. Freeze it in the watch list instead? That "
                        "keeps it set until the game closes.", parent=self):
                    w = Watch(name, g.describe(addr), kind if kind in WATCH_KINDS else "4 bytes", True, value)
                    self._add_watch(w)
                return
            self._choose_path(g, paths, data, name, note)

        poll()

    def _choose_path(self, g, paths, data, name, note):
        win = tk.Toplevel(self)
        win.title("Make cheat - pick a pointer chain")
        win.configure(bg=self.colors["bg"])
        f = ttk.Frame(win, padding=self.px(10))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="These chains start in Battlefront.exe's own data and lead to the value now. The "
                          "shortest is usually the safest. If a code stops working after a restart, try "
                          "another chain.", wraplength=self.px(560), foreground="gray").pack(anchor="w")
        lb = tk.Listbox(f, height=min(12, len(paths)), width=60, bg=self.colors["field_bg"], fg=self.colors["fg"],
                        selectbackground=self.colors["select_bg"], highlightthickness=0, font=("Consolas", 10))
        for s, offs in paths:
            lb.insert("end", path_expr(s, offs))
        lb.selection_set(0)
        lb.pack(fill="both", expand=True, pady=self.px(6))

        def ok(_e=None):
            sel = lb.curselection()
            if not sel:
                return
            s, offs = paths[sel[0]]
            win.destroy()
            self._new_cheat(code=code_for_pointer_write(s, offs, data), name=name,
                            note=f"{note}, via {path_expr(s, offs)}")

        lb.bind("<Double-1>", ok)
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(row, text="Make cheat", command=ok).pack(side="right", padx=6)

    # bookmarks

    def _load_bookmarks(self):
        try:
            self.bookmarks = [b for b in json.loads(self.bm_path.read_text(encoding="utf-8"))
                              if isinstance(b, dict) and "expr" in b]
        except (OSError, ValueError):
            self.bookmarks = []
        self.bm_box["values"] = [f"{b.get('name', '')} - {b['expr']}" for b in self.bookmarks]

    def _save_bookmarks(self):
        try:
            self.bm_path.write_text(json.dumps(self.bookmarks, indent=1), encoding="utf-8")
        except OSError:
            pass
        self.bm_box["values"] = [f"{b.get('name', '')} - {b['expr']}" for b in self.bookmarks]

    def _add_bookmark(self):
        g = self._game()
        a = self.sel_addr if self.sel_addr is not None else self.mem_addr
        if g is None or a is None:
            return
        name = simpledialog.askstring("Bookmark", f"Name for {g.describe(a)}:", parent=self)
        if name:
            self.bookmarks.append({"name": name, "expr": g.describe(a)})
            self._save_bookmarks()

    def _remove_bookmark(self):
        i = self.bm_box.current()
        if i >= 0:
            del self.bookmarks[i]
            self.bm_var.set("")
            self._save_bookmarks()

    def _goto_bookmark(self):
        i = self.bm_box.current()
        if i >= 0:
            self.addr_var.set(self.bookmarks[i]["expr"])
            self._goto()

    # -- search tab ------------------------------------------------------------

    def _search_tab(self, nb):
        px = self.px
        f = ttk.Frame(nb, padding=px(8))
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Type:").pack(side="left")
        self.scan_type = tk.StringVar(value="4 bytes")
        cb = ttk.Combobox(row, textvariable=self.scan_type, values=SCAN_KINDS, width=13, state="readonly")
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda ev: self._scan_type_changed())
        ttk.Label(row, text="Search in:").pack(side="left", padx=(10, 0))
        self.scan_scope = tk.StringVar(value="writable memory")
        ttk.Combobox(row, textvariable=self.scan_scope, values=list(SCOPES), width=20,
                     state="readonly").pack(side="left", padx=4)
        self.scan_case = tk.BooleanVar(value=False)
        self.case_box = ttk.Checkbutton(row, text="Match case", variable=self.scan_case)
        self.case_box.pack(side="left", padx=8)
        row1 = ttk.Frame(f)
        row1.pack(fill="x", pady=(6, 0))
        self.first_mode = tk.StringVar(value="exact")
        self.first_mode_box = ttk.Combobox(row1, textvariable=self.first_mode, values=FIRST_MODES, width=12,
                                           state="readonly")
        self.first_mode_box.pack(side="left")
        ttk.Label(row1, text="Value:").pack(side="left", padx=(8, 0))
        self.scan_val = tk.StringVar()
        ttk.Entry(row1, textvariable=self.scan_val, width=28).pack(side="left", padx=4)
        ttk.Label(row1, text="and").pack(side="left")
        self.scan_val2 = tk.StringVar()
        ttk.Entry(row1, textvariable=self.scan_val2, width=10).pack(side="left", padx=4)
        self.first_btn = ttk.Button(row1, text="First scan", command=lambda: self._scan(first=True))
        self.first_btn.pack(side="left", padx=4)
        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="Next scan:").pack(side="left")
        self.next_mode = tk.StringVar(value="exact")
        self.next_mode_box = ttk.Combobox(row2, textvariable=self.next_mode, width=12, state="readonly",
                                          values=NEXT_MODES)
        self.next_mode_box.pack(side="left", padx=4)
        self.next_btn = ttk.Button(row2, text="Next scan", command=lambda: self._scan(first=False))
        self.next_btn.pack(side="left")
        ttk.Button(row2, text="Undo scan", command=self._undo_scan).pack(side="left", padx=6)
        ttk.Button(row2, text="New search", command=self._reset_scan).pack(side="left")
        self.scan_status = tk.StringVar(value="Numbers: search for a value you can see in game (ammo, health...), "
                                              "change it in game, and search again until a few addresses are left. "
                                              "Text and bytes: find strings or code, e.g. 8B 0D ?? ?? ?? 00.")
        ttk.Label(f, textvariable=self.scan_status, foreground="gray", wraplength=px(760)).pack(anchor="w", pady=4)
        self.scan_progress = ttk.Progressbar(f, mode="determinate", maximum=1.0)
        self.scan_progress.pack(fill="x")
        self.results = ttk.Treeview(f, columns=("addr", "value", "prev", "where"), show="headings", height=12)
        for col, text, w in (("addr", "Address", 190), ("value", "Value now", 170), ("prev", "At last scan", 170),
                             ("where", "Where", 230)):
            self.results.heading(col, text=text)
            self.results.column(col, width=px(w))
        self.results.pack(fill="both", expand=True, pady=px(6))
        self.results.bind("<Double-1>", lambda e: self._result_to_memory())
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        for text, fn in (("View in memory", self._result_to_memory), ("Show as assembly", self._result_to_asm),
                         ("Add to watch list", self._result_watch), ("Make cheat...", self._result_cheat)):
            ttk.Button(bar, text=text, command=fn).pack(side="left", padx=(0, 6))
        self._result_addrs = []
        return f

    def _scan_type_changed(self):
        text = self.scan_type.get() in TEXT_KINDS
        self.first_mode_box["values"] = ("exact",) if text else FIRST_MODES
        self.next_mode_box["values"] = TEXT_NEXT_MODES if text else NEXT_MODES
        self.first_mode.set("exact")
        self.next_mode.set("exact" if not text else "unchanged")
        if text:
            self.scan_scope.set("all memory")   # strings live in the exe's read-only data too
        self._reset_scan()

    def _reset_scan(self):
        self.scanner = None
        self.results.delete(*self.results.get_children())
        self._result_addrs = []
        self.scan_status.set("New search.")

    def _undo_scan(self):
        if self.scanner is None:
            return
        try:
            n = self.scanner.undo()
        except GameMemoryError as exc:
            self.scan_status.set(str(exc))
            return
        self.scan_status.set(f"Back to {n:,} addresses.")
        self._show_results()

    def _scan(self, first, mode=None):
        g = self._need_game("Search")
        if g is None:
            return
        if first or self.scanner is None:
            try:
                self.scanner = Scanner(g, self.scan_type.get(), SCOPES[self.scan_scope.get()], self.scan_case.get())
            except ImportError:
                messagebox.showerror("Search", "Searching needs numpy (pip install numpy).", parent=self)
                return
            first = True
        mode = mode or (self.first_mode.get() if first else self.next_mode.get())
        self.first_btn.state(["disabled"])
        self.next_btn.state(["disabled"])
        self.scan_status.set("Scanning...")
        result = {}
        v1, v2 = self.scan_val.get(), self.scan_val2.get()

        def work():
            try:
                if first:
                    result["n"] = self.scanner.first(mode, v1, progress=lambda p: result.update(p=p), value2=v2)
                else:
                    result["n"] = self.scanner.next(mode, v1, value2=v2)
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
            where = g.where(a) if g and i < 200 else ""
            self.results.insert("", "end", iid=str(i), values=(g.describe(a), "", prev, where))
        self._refresh_results()

    def _refresh_results(self):
        g = self._game()
        if g is None or not self._result_addrs or self.scanner is None:
            return
        for i, (a, _prev) in enumerate(self._result_addrs[:200]):
            if self.results.exists(str(i)):
                try:
                    self.results.set(str(i), "value", self.scanner.read_now(a))
                except (GameMemoryError, struct.error):
                    self.results.set(str(i), "value", "??")

    def _picked_result(self):
        sel = self.results.selection()
        return self._result_addrs[int(sel[0])][0] if sel else None

    def _result_to_memory(self, view=None):
        a = self._picked_result()
        g = self._game()
        if a is None or g is None:
            return
        if self.scanner and not self.scanner.text:
            self.write_type.set(self.scanner.kind)
        self._go(a, select=a, view=view or ("Hex" if self.view.get() == "Assembly" else None))
        self.nb.select(self.mem_tab)

    def _result_to_asm(self):
        self._result_to_memory(view="Assembly")

    def _result_watch(self):
        a = self._picked_result()
        g = self._game()
        if a is None or g is None:
            return
        kind = "text" if self.scanner.text else self.scanner.kind
        self._add_watch(Watch(g.describe(a), g.describe(a), kind))

    def _result_cheat(self):
        a = self._picked_result()
        if a is None or self.scanner is None:
            messagebox.showinfo("Make cheat", "Pick a result first.", parent=self)
            return
        if self.scanner.text:
            messagebox.showinfo("Make cheat", "For text or bytes, use View in memory, then type the new text or "
                                "bytes in the Memory tab's Value box (type 'text' or 'bytes') and press Make cheat "
                                "there.", parent=self)
            return
        g = self._game()
        cur = mem_unpack(self.scanner.kind, g.read(a, MEM_TYPES[self.scanner.kind][1]))
        value = simpledialog.askstring("Make cheat", f"Keep {g.describe(a)} at which value?",
                                       initialvalue=str(cur), parent=self)
        if value is not None:
            self._make_cheat(addr=a, kind=self.scanner.kind, value=value)

    # -- watch tab -------------------------------------------------------------

    def _watch_tab(self, nb):
        px = self.px
        f = ttk.Frame(nb, padding=px(8))
        ttk.Label(f, text="Addresses to keep an eye on. Click the first column to freeze one at its value "
                          "(it's rewritten several times a second); double-click a value to change it. "
                          "Pointer paths like [+6D9420]+10 are followed every time, so they survive restarts.",
                  foreground="gray", wraplength=px(760)).pack(anchor="w")
        self.watch_tree = ttk.Treeview(f, columns=("frozen", "name", "addr", "type", "value"), show="headings",
                                       height=14, selectmode="browse")
        for col, text, w in (("frozen", "Freeze", 60), ("name", "Name", 220), ("addr", "Address", 200),
                             ("type", "Type", 80), ("value", "Value", 200)):
            self.watch_tree.heading(col, text=text)
            self.watch_tree.column(col, width=px(w), anchor="center" if col == "frozen" else "w")
        self.watch_tree.pack(fill="both", expand=True, pady=px(6))
        self.watch_tree.bind("<Button-1>", self._watch_click)
        self.watch_tree.bind("<Double-1>", self._watch_double)
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        for text, fn in (("Add...", lambda: self._watch_dialog()), ("Edit...", self._watch_edit),
                         ("Set value...", self._watch_set), ("View in memory", self._watch_view),
                         ("Delete", self._watch_delete)):
            ttk.Button(bar, text=text, command=fn).pack(side="left", padx=(0, 6))
        self.watch_file = WatchFile(app_dir())
        self.watches = self.watch_file.load()
        self.runner.set_frozen(self.watches)
        self._fill_watches()
        return f

    def _fill_watches(self):
        sel = self.watch_tree.selection()
        self.watch_tree.delete(*self.watch_tree.get_children())
        for i, w in enumerate(self.watches):
            self.watch_tree.insert("", "end", iid=str(i), values=("✔" if w.frozen else "", w.name, w.expr, w.kind, ""))
        if sel and self.watch_tree.exists(sel[0]):
            self.watch_tree.selection_set(sel)
        self._refresh_watches()

    def _refresh_watches(self):
        g = self._game()
        for i, w in enumerate(self.watches):
            if not self.watch_tree.exists(str(i)):
                continue
            try:
                v = w.read(g) if g else "(game not running)"
            except (GameMemoryError, ValueError, struct.error):
                v = "??"
            self.watch_tree.set(str(i), "value", v)

    def _save_watches(self):
        try:
            self.watch_file.save(self.watches)
        except OSError as exc:
            messagebox.showerror("Watch list", f"Couldn't save watchlist.json: {exc}", parent=self)
        self.runner.set_frozen(self.watches)
        self._fill_watches()

    def _add_watch(self, w):
        self.watches.append(w)
        self._save_watches()
        self.nb.select(self.watch_tab_frame)

    def _picked_watch(self):
        sel = self.watch_tree.selection()
        return int(sel[0]) if sel else None

    def _watch_click(self, event):
        row = self.watch_tree.identify_row(event.y)
        if row and self.watch_tree.identify_column(event.x) == "#1":
            w = self.watches[int(row)]
            w.frozen = not w.frozen
            if w.frozen:
                try:
                    w.value = w.read(self._game()) if self._game() else w.value
                except (GameMemoryError, ValueError, struct.error):
                    pass
            self._save_watches()
            return "break"

    def _watch_double(self, event):
        row = self.watch_tree.identify_row(event.y)
        if not row:
            return
        self.watch_tree.selection_set(row)
        if self.watch_tree.identify_column(event.x) == "#5":
            self._watch_set()
        else:
            self._watch_edit()
        return "break"

    def _watch_dialog(self, w=None):
        win = tk.Toplevel(self)
        win.title("Watch")
        win.transient(self)
        win.configure(bg=self.colors["bg"])
        body = ttk.Frame(win, padding=self.px(10))
        body.pack(fill="both")
        name = tk.StringVar(value=w.name if w else "")
        expr = tk.StringVar(value=w.expr if w else (self.addr_var.get() if self.sel_addr is not None else ""))
        kind = tk.StringVar(value=w.kind if w else "4 bytes")
        for r, (label, widget) in enumerate((
                ("Name:", ttk.Entry(body, textvariable=name, width=36)),
                ("Address:", ttk.Entry(body, textvariable=expr, width=36)),
                ("Type:", ttk.Combobox(body, textvariable=kind, values=WATCH_KINDS, width=12, state="readonly")))):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(body, text="+4E6D53, 0x00746D53, or [+6D9420]+10 to follow a pointer", foreground="gray").grid(
            row=3, column=0, columnspan=2, sticky="w")

        def ok():
            if not expr.get().strip():
                return
            if w is None:
                self.watches.append(Watch(name.get() or expr.get(), expr.get().strip(), kind.get()))
            else:
                w.name, w.expr, w.kind = name.get() or expr.get(), expr.get().strip(), kind.get()
            win.destroy()
            self._save_watches()

        row = ttk.Frame(body)
        row.grid(row=4, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(row, text="OK", command=ok).pack(side="right", padx=6)
        win.bind("<Return>", lambda e: ok())

    def _watch_edit(self):
        i = self._picked_watch()
        if i is not None:
            self._watch_dialog(self.watches[i])

    def _watch_set(self):
        i = self._picked_watch()
        g = self._need_game("Watch list")
        if i is None or g is None:
            return
        w = self.watches[i]
        try:
            cur = w.read(g)
        except (GameMemoryError, ValueError, struct.error):
            cur = ""
        text = simpledialog.askstring("Set value", f"New value for {w.name}:", initialvalue=cur, parent=self)
        if text is None:
            return
        try:
            w.write(g, text)
        except (GameMemoryError, ValueError, struct.error) as exc:
            messagebox.showerror("Watch list", f"Couldn't write: {exc}", parent=self)
            return
        if w.frozen:
            w.value = text
            self._save_watches()
        self._refresh_watches()

    def _watch_view(self):
        i = self._picked_watch()
        g = self._need_game("Watch list")
        if i is None or g is None:
            return
        try:
            a = g.resolve(self.watches[i].expr)
        except GameMemoryError as exc:
            messagebox.showerror("Watch list", str(exc), parent=self)
            return
        self._go(a, select=a)
        self.nb.select(self.mem_tab)

    def _watch_delete(self):
        i = self._picked_watch()
        if i is not None:
            del self.watches[i]
            self._save_watches()

    # -- loop ------------------------------------------------------------------

    def _tick(self):
        if not self.winfo_exists():
            return
        self.status.set(self.runner.status)
        if any(self.runner.errors.get(c.name) for c in self.cheats):
            self._fill_cheats()
        try:
            tab = str(self.nb.select())
            if tab == str(self.mem_tab):
                if self.view.get() != "Assembly":
                    self._refresh_hex()
            elif tab == str(self.watch_tab_frame):
                self._refresh_watches()
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
