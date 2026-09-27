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
            if not src.is_file():
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
        if not lines:
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


if __name__ == "__main__":
    LoaderApp().mainloop()
