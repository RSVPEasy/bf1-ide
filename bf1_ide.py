"""
BF1 IDE - a single application for browsing, editing and rebuilding
Star Wars Battlefront (2004) "Zero Engine" .lvl (UCFB container) files,
with a structured editor for ordc/wpnc/expc (Ordnance/Weapon/Explosion)
chunks.

This replaces two standalone scripts (bf1_lvl_unmunger.py and
bf1_ord_parser.py) with one tool. See bf1_core.py's module docstring for
the full list of bugs that were fixed along the way.

Usage:
    python3 bf1_ide.py [file.lvl]      # launch the GUI
    python3 bf1_ide.py --selftest      # run bf1_core's regression tests

DESIGN NOTE ON NESTED "lvl_" CHUNKS
------------------------------------
The original unmunger extracted nested lvl_ chunks into a sibling
"<name>.embedded/" folder on disk purely for browsing, but its rebuild
step actually *used* that folder's contents whether or not you'd edited
anything (bug #5 from the review). This IDE removes that whole disk-based
side-channel: double-click a "lvl_" chunk in the tree and it opens as a
new tab, backed by the *same in-memory chunk object* as the row you
clicked. Edit anything in that tab and it's immediately reflected when
you hit Save on the original file's tab - no extra folder, no separate
rebuild step, no risk of silently reserializing something you never
touched.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, simpledialog

import bf1_core as core

APP_NAME = "BF1 Level Editor"


# --------------------------------------------------------------------------
# Settings (persisted alongside this script) and theming
# --------------------------------------------------------------------------

SETTINGS_PATH = core.app_dir() / ".bf1_ide_settings.json"
DEFAULT_SETTINGS = {
    "theme": "light",
    "confirm_delete": True,
    "auto_backup": True,
    "recent_files": [],
}
MAX_RECENT_FILES = 8


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text())
            if isinstance(data, dict):
                return {**DEFAULT_SETTINGS, **data}
        except Exception:
            pass  # corrupt/unreadable settings file - just start fresh
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    except OSError:
        pass  # e.g. read-only install location - not worth failing over


THEMES = {
    "light": {
        "bg": "#f0f0f0", "fg": "#000000", "field_bg": "#ffffff", "text_bg": "#ffffff",
        "select_bg": "#0a5fc4", "select_fg": "#ffffff", "border": "#b0b0b0", "disabled_fg": "#8a8a8a",
    },
    "dark": {
        "bg": "#2b2b2d", "fg": "#e6e6e6", "field_bg": "#1e1e1e", "text_bg": "#1e1e1e",
        "select_bg": "#3d8be8", "select_fg": "#ffffff", "border": "#454548", "disabled_fg": "#8a8a8a",
    },
}


UI_SCALE = 1.0  # display DPI / 96, set once the Tk root exists (App.__init__)


def px(n: float) -> int:
    """A pixel size scaled for the display, so fixed layout sizes (column
    widths, window sizes, image previews) grow along with the fonts."""
    return max(1, round(n * UI_SCALE))


def enable_dpi_awareness() -> None:
    """Without this, Windows renders the app at 96 DPI and stretches the
    bitmap on scaled displays (125%, 150%, ...), which makes everything
    blurry. Has to run before the Tk root is created."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system-DPI aware
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def apply_theme(root: tk.Tk, theme_name: str) -> None:
    """Re-skins every ttk widget class via one shared Style, plus the root
    window itself. Plain tk widgets (Text, Menu) aren't ttk and don't listen
    to this - see text_colors() below, which is what those query directly
    at creation time (and App.after_theme_change re-queries for ones
    already on screen)."""
    colors = THEMES.get(theme_name, THEMES["light"])
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(bg=colors["bg"])
    style.configure(".", background=colors["bg"], foreground=colors["fg"],
                    fieldbackground=colors["field_bg"], bordercolor=colors["border"],
                    lightcolor=colors["bg"], darkcolor=colors["bg"])
    for cls in ("TFrame", "TLabel", "TCheckbutton", "TPanedwindow", "TNotebook"):
        style.configure(cls, background=colors["bg"], foreground=colors["fg"])
    style.configure("TButton", background=colors["field_bg"], foreground=colors["fg"])
    style.map("TButton", background=[("active", colors["select_bg"])],
              foreground=[("active", colors["select_fg"])])
    for cls in ("TEntry", "TCombobox"):
        style.configure(cls, fieldbackground=colors["field_bg"], foreground=colors["fg"],
                        insertcolor=colors["fg"])
    style.map("TCombobox", fieldbackground=[("readonly", colors["field_bg"])],
              foreground=[("readonly", colors["fg"])])
    style.configure("TNotebook.Tab", background=colors["field_bg"], foreground=colors["fg"])
    style.map("TNotebook.Tab", background=[("selected", colors["select_bg"])],
              foreground=[("selected", colors["select_fg"])])
    from tkinter import font as tkfont
    row_height = tkfont.nametofont("TkDefaultFont").metrics("linespace") + px(6)
    style.configure("Treeview", background=colors["field_bg"], fieldbackground=colors["field_bg"],
                    foreground=colors["fg"], bordercolor=colors["border"], rowheight=row_height)
    style.configure("Treeview.Heading", background=colors["bg"], foreground=colors["fg"])
    style.map("Treeview", background=[("selected", colors["select_bg"])],
              foreground=[("selected", colors["select_fg"])])
    style.configure("TScrollbar", background=colors["field_bg"], troughcolor=colors["bg"],
                    bordercolor=colors["border"])


def text_colors(root: tk.Tk) -> dict:
    """Colors for plain tk widgets (Text, Menu) that ttk styling doesn't
    reach - looked up from whatever theme is currently applied."""
    theme_name = getattr(root, "_bf1_theme", "light")
    return THEMES.get(theme_name, THEMES["light"])


# --------------------------------------------------------------------------
# Undo/redo
# --------------------------------------------------------------------------

class UndoStack:
    """One global undo/redo history for the whole App, not per-tab: chunk
    objects can be shared across a top-level tab and a nested 'lvl_' tab
    opened on the same data (see the module docstring), so an edit made in
    either one needs to be undoable regardless of which tab happens to be
    active when Ctrl+Z is pressed.

    Each entry is (label, undo_fn, redo_fn) - both fns take no arguments and
    are expected to fully reverse/redo one user-visible action themselves
    (including whatever notify_changed()/set_payload() plumbing that needs).
    push() is what a call site uses to record an action *after already
    performing it once* - redo_fn is exactly "do the action again".
    """

    def __init__(self, app: "App"):
        self.app = app
        self._undo: list[tuple[str, object, object]] = []
        self._redo: list[tuple[str, object, object]] = []

    def push(self, label: str, undo_fn, redo_fn) -> None:
        self._undo.append((label, undo_fn, redo_fn))
        self._redo.clear()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> None:
        if not self._undo:
            return
        label, undo_fn, redo_fn = self._undo.pop()
        undo_fn()
        self._redo.append((label, undo_fn, redo_fn))
        self.app.after_undo_redo(f"Undid: {label}")

    def redo(self) -> None:
        if not self._redo:
            return
        label, undo_fn, redo_fn = self._redo.pop()
        redo_fn()
        self._undo.append((label, undo_fn, redo_fn))
        self.app.after_undo_redo(f"Redid: {label}")


# --------------------------------------------------------------------------
# Small reusable dialogs
# --------------------------------------------------------------------------

class PropertyDialog(simpledialog.Dialog):
    """Add/edit a single ordnance property (name-or-0xHASH, value)."""

    def __init__(self, parent, title, initial_name="", initial_value=""):
        self.initial_name = initial_name
        self.initial_value = initial_value
        self.result_name = None
        self.result_value = None
        super().__init__(parent, title)

    def body(self, master):
        ttk.Label(master, text="Property name (or 0xHHHHHHHH for a raw hash):").grid(row=0, column=0, sticky="w", pady=(0, 2))
        self.name_var = tk.StringVar(value=self.initial_name)
        self.name_entry = ttk.Entry(master, textvariable=self.name_var, width=40)
        self.name_entry.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(master, text="Value:").grid(row=2, column=0, sticky="w", pady=(0, 2))
        self.value_var = tk.StringVar(value=self.initial_value)
        self.value_entry = ttk.Entry(master, textvariable=self.value_var, width=40)
        self.value_entry.grid(row=3, column=0, sticky="ew")
        return self.name_entry

    def apply(self):
        self.result_name = self.name_var.get().strip()
        self.result_value = self.value_var.get()


class OdfTextDialog(simpledialog.Dialog):
    """Big free-form text box for bulk-editing a KV chunk (ordc/wpnc/expc/
    entc) as real .odf text - see core.encode_kv_chunk_to_odf_text /
    decode_odf_text_to_kv_payload. OK just returns the raw text; the caller
    is the one that re-parses it (so a parse error can be reported without
    losing the user's edits by closing the dialog)."""

    def __init__(self, parent, initial_text: str):
        self.initial_text = initial_text
        self.result_text = None
        super().__init__(parent, "Edit as ODF Text")

    def body(self, master):
        colors = text_colors(self.winfo_toplevel())
        text = tk.Text(master, width=70, height=28, font=("Courier New", 10), wrap="none",
                       bg=colors["text_bg"], fg=colors["fg"], insertbackground=colors["fg"],
                       selectbackground=colors["select_bg"], selectforeground=colors["select_fg"])
        text.insert("1.0", self.initial_text)
        text.pack(fill="both", expand=True)
        self._text_widget = text
        return text

    def apply(self):
        self.result_text = self._text_widget.get("1.0", "end-1c")


class NewChunkDialog(simpledialog.Dialog):
    """Create a brand-new chunk (ordnance, or generic-from-file)."""

    def __init__(self, parent):
        self.result = None
        super().__init__(parent, "Add New Chunk")

    def body(self, master):
        ttk.Label(master, text="Chunk tag (4 chars, e.g. ordc / wpnc / expc):").grid(row=0, column=0, sticky="w")
        self.tag_var = tk.StringVar(value="ordc")
        ttk.Entry(master, textvariable=self.tag_var, width=10).grid(row=1, column=0, sticky="w", pady=(0, 8))

        ttk.Label(master, text="Display name (used for the filename / NAME block):").grid(row=2, column=0, sticky="w")
        self.name_var = tk.StringVar(value="new_chunk")
        entry = ttk.Entry(master, textvariable=self.name_var, width=30)
        entry.grid(row=3, column=0, sticky="w", pady=(0, 8))

        ttk.Label(master, text="For ordc/wpnc/expc, an empty ordnance chunk is created\n"
                                "(edit its Base/Type/Properties afterwards in the editor).\n"
                                "For any other tag, use 'Import Payload From File' after\n"
                                "creating it to fill in the binary content.").grid(row=4, column=0, sticky="w")
        return entry

    def apply(self):
        tag = self.tag_var.get().strip()
        name = self.name_var.get().strip() or "new_chunk"
        if len(tag) != 4:
            messagebox.showerror("Invalid tag", "Chunk tag must be exactly 4 characters.")
            return
        if tag in core.KV_TAGS:
            payload = core.encode_ordnance_payload(name, "ord", [])
        else:
            payload = b""
        self.result = core.pack_new_chunk(tag, name, payload)


# --------------------------------------------------------------------------
# One tab = one Container (either the top-level file, or a nested lvl_
# chunk opened in place). Multiple tabs can share chunk objects, which is
# exactly how edits in a nested tab propagate back to the top file.
# --------------------------------------------------------------------------

class WorkspaceTab(ttk.Frame):
    def __init__(self, notebook: ttk.Notebook, app: "App", container: core.Container,
                 title: str, file_path: Path | None):
        super().__init__(notebook)
        self.notebook = notebook
        self.app = app
        self.container = container
        self.title = title
        self.file_path = file_path  # only set for the top-level file tab

        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        tree_frame = ttk.Frame(paned)
        detail_frame = ttk.Frame(paned)
        paned.add(tree_frame, weight=1)
        paned.add(detail_frame, weight=2)

        # Find bar (Ctrl+F): hidden until requested. All non-empty fields must
        # match (case-insensitive "contains"); leaving a field empty ignores it.
        self.find_tag_var = tk.StringVar()
        self.find_name_var = tk.StringVar()
        self.find_payload_var = tk.StringVar()
        self.find_status_var = tk.StringVar()
        self.find_frame = ttk.Frame(tree_frame)
        ttk.Label(self.find_frame, text="Type:").grid(row=0, column=0, sticky="w", padx=(4, 2))
        self.find_tag_entry = ttk.Entry(self.find_frame, textvariable=self.find_tag_var, width=8)
        self.find_tag_entry.grid(row=0, column=1, sticky="w")
        ttk.Label(self.find_frame, text="Name:").grid(row=0, column=2, sticky="w", padx=(8, 2))
        self.find_name_entry = ttk.Entry(self.find_frame, textvariable=self.find_name_var, width=16)
        self.find_name_entry.grid(row=0, column=3, sticky="ew")
        ttk.Label(self.find_frame, text="Data:").grid(row=0, column=4, sticky="w", padx=(8, 2))
        self.find_payload_entry = ttk.Entry(self.find_frame, textvariable=self.find_payload_var, width=12)
        self.find_payload_entry.grid(row=0, column=5, sticky="ew")
        ttk.Button(self.find_frame, text="X", width=2, command=self.hide_find).grid(row=0, column=6, padx=4)
        ttk.Label(self.find_frame, textvariable=self.find_status_var).grid(
            row=1, column=0, columnspan=7, sticky="w", padx=4)
        self.find_frame.columnconfigure(3, weight=1)
        self.find_frame.columnconfigure(5, weight=1)
        for var in (self.find_tag_var, self.find_name_var, self.find_payload_var):
            var.trace_add("write", lambda *_: self.refresh_tree(keep_detail=True))
        for entry in (self.find_tag_entry, self.find_name_entry, self.find_payload_entry):
            entry.bind("<Escape>", lambda e: self.hide_find())
            entry.bind("<Return>", lambda e: (self.tree.focus_set(), "break")[1])

        columns = ("tag", "name", "size")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="#")
        self.tree.heading("tag", text="Tag")
        self.tree.heading("name", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.column("#0", width=px(50), stretch=False)
        self.tree.column("tag", width=px(60), stretch=False)
        self.tree.column("name", width=px(180), minwidth=px(120))
        self.tree.column("size", width=px(80), stretch=False)
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        # grid (not pack) so both scrollbars always stay visible however the
        # pane is sized - row 1 is the only row that stretches.
        self.tree.grid(row=1, column=0, sticky="nsew")
        vsb.grid(row=1, column=1, sticky="ns")
        hsb.grid(row=2, column=0, sticky="ew")
        tree_frame.rowconfigure(1, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.find_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        self.find_frame.grid_remove()

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)
        # Plain Up/Down keep the Treeview's normal "move selection" behavior.
        # Alt+Up/Alt+Down (and Ctrl+Up/Ctrl+Down, for platforms that eat Alt)
        # move the selected chunk itself in the chunk order.
        self.tree.bind("<Alt-Up>", lambda e: self._move_selected(-1))
        self.tree.bind("<Alt-Down>", lambda e: self._move_selected(1))
        self.tree.bind("<Control-Up>", lambda e: self._move_selected(-1))
        self.tree.bind("<Control-Down>", lambda e: self._move_selected(1))
        # Copy/paste a chunk (duplicates tag+payload as a brand new chunk
        # object; never shares state with the original, so editing one
        # after pasting never touches the other).
        self.tree.bind("<Control-c>", lambda e: self._copy_selected())
        self.tree.bind("<Control-v>", lambda e: self._paste_selected())

        self.detail_frame = detail_frame
        self._detail_widget = None
        self._iid_to_chunk: dict[str, core.Chunk] = {}

        menu_colors = text_colors(self.winfo_toplevel())
        self._context_menu = tk.Menu(self, tearoff=0, bg=menu_colors["field_bg"], fg=menu_colors["fg"],
                                     activebackground=menu_colors["select_bg"],
                                     activeforeground=menu_colors["select_fg"])
        self._context_menu.add_command(label="Open Nested Level in New Tab", command=self._open_selected_nested)
        self._context_menu.add_command(label="Export Chunk Bytes (raw)...", command=self._export_selected)
        self._context_menu.add_command(label="Replace Payload From File (raw)...", command=self._replace_selected)
        self._context_menu.add_command(label="Export As Usable File...", command=self._export_usable_selected)
        self._context_menu.add_command(label="Import From Usable File...", command=self._import_usable_selected)
        self._context_menu.add_command(label="Import Character / Vehicle (.glb)...",
                                       command=self._import_character_selected)
        self._context_menu.add_command(label="Move Fire Points...", command=self._move_points_selected)
        self._context_menu.add_command(label="Find References...", command=self._find_references_selected)
        self._context_menu.add_separator()
        self._context_menu.add_command(label="Copy Chunk", command=self._copy_selected, accelerator="Ctrl+C")
        self._context_menu.add_command(label="Paste Chunk After Selected", command=self._paste_selected, accelerator="Ctrl+V")
        self._context_menu.add_separator()
        self._context_menu.add_command(label="Move to Index...", command=self._move_to_index)
        self._context_menu.add_separator()
        self._context_menu.add_command(label="Delete Chunk", command=self._delete_selected)

        self.refresh_tree()

    # -- tree population ---------------------------------------------------

    def _matches_filter(self, chunk: core.Chunk, name: str) -> bool:
        return core.matches_filter(chunk, self.find_tag_var.get(), self.find_name_var.get(),
                                   self.find_payload_var.get(), name=name)

    def refresh_tree(self, keep_detail: bool = False):
        selected = self._selected_chunk()
        self.tree.delete(*self.tree.get_children())
        self._iid_to_chunk.clear()
        filtering = bool(self.find_tag_var.get().strip() or self.find_name_var.get().strip()
                         or self.find_payload_var.get().strip())
        shown = 0
        reselect = None
        for i, chunk in enumerate(self.container.body.chunks):
            name = chunk.display_name()
            if filtering and not self._matches_filter(chunk, name):
                continue
            iid = f"c{i}"
            self.tree.insert("", "end", iid=iid, text=str(i),
                              values=(chunk.tag, name, len(chunk.payload)))
            self._iid_to_chunk[iid] = chunk
            shown += 1
            if chunk is selected:
                reselect = iid
        total = len(self.container.body.chunks)
        self.find_status_var.set(f"{shown} of {total} chunks match" if filtering else f"{total} chunks")
        if reselect is not None:
            if keep_detail:
                # Selection events are queued, so a plain flag would be reset
                # too early - clear it once the event queue has drained.
                self._suppress_select = True
                self.after_idle(lambda: setattr(self, "_suppress_select", False))
            self.tree.selection_set(reselect)
            self.tree.see(reselect)

    def show_find(self):
        self.find_frame.grid()
        self.find_name_entry.focus_set()
        self.find_name_entry.selection_range(0, "end")

    def hide_find(self):
        for var in (self.find_tag_var, self.find_name_var, self.find_payload_var):
            var.set("")
        self.find_frame.grid_remove()
        self.tree.focus_set()

    def _selected_chunk(self) -> core.Chunk | None:
        """The first selected chunk, in tree order. Used by every
        single-chunk action; those guard separately when more than one
        chunk is actually selected (see _require_single)."""
        chunks = self._selected_chunks()
        return chunks[0] if chunks else None

    def _selected_chunks(self) -> list[core.Chunk]:
        """All selected chunks, in tree (display) order - not selection
        order, so bulk actions read top-to-bottom regardless of how the
        selection was built (shift-click, ctrl-click, drag)."""
        sel = set(self.tree.selection())
        return [self._iid_to_chunk[iid] for iid in self.tree.get_children("") if iid in sel]

    def _require_single(self, action: str) -> core.Chunk | None:
        """Guard for actions that only make sense on exactly one chunk
        (Replace Payload, Import From Usable File, Open Nested, Move to
        Index, Copy). Multi-select is for the bulk actions (Export, Delete)
        - everything else just tells you to narrow the selection."""
        chunks = self._selected_chunks()
        if len(chunks) > 1:
            messagebox.showinfo("Multiple chunks selected", f"Select exactly one chunk to {action}.")
            return None
        return chunks[0] if chunks else None

    # -- selection / detail panel -------------------------------------------

    def _clear_detail(self):
        if self._detail_widget is not None:
            self._detail_widget.destroy()
            self._detail_widget = None

    def _on_select(self, _event=None):
        if getattr(self, "_suppress_select", False):
            return
        chunks = self._selected_chunks()
        self._clear_detail()
        if len(chunks) > 1:
            self._detail_widget = MultiSelectionPanel(self.detail_frame, self.app, self, chunks)
            self._detail_widget.pack(fill="both", expand=True)
            return
        chunk = chunks[0] if chunks else None
        if chunk is None:
            return
        if chunk.tag in core.KV_TAGS:
            self._detail_widget = OrdnanceEditor(self.detail_frame, self.app, chunk, self.refresh_tree)
        elif chunk.tag == "lvl_":
            self._detail_widget = LevelChunkPanel(self.detail_frame, self.app, self, chunk)
        elif chunk.tag in core.STRUCTURED_BINARY_TAGS:
            self._detail_widget = StructuredBinaryViewer(self.detail_frame, self.app, chunk)
        elif chunk.tag == "tex_":
            self._detail_widget = TexturePreview(self.detail_frame, self.app, chunk)
        else:
            self._detail_widget = HexViewer(self.detail_frame, self.app, chunk, self.refresh_tree)
        self._detail_widget.pack(fill="both", expand=True)

    def _on_double_click(self, _event=None):
        chunk = self._selected_chunk()
        if chunk is not None and chunk.tag == "lvl_":
            self._open_selected_nested()

    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            # Right-clicking inside an existing multi-selection keeps it
            # (so "Delete Chunk" etc. act on all of it); right-clicking
            # outside one collapses to just the row under the cursor, same
            # as most file managers.
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self._context_menu.tk_popup(event.x_root, event.y_root)

    # -- context menu actions ------------------------------------------------

    def _open_selected_nested(self):
        chunk = self._require_single("open as a nested workspace")
        if chunk is None:
            return
        if chunk.tag != "lvl_":
            messagebox.showinfo("Not a level chunk", "Only 'lvl_' chunks can be opened as a nested workspace.")
            return
        nested = chunk.get_nested_container()
        title = f"{self.title} > {chunk.display_name() or 'lvl_'}"
        self.app.open_container_tab(nested, title, file_path=None)

    def _export_selected(self):
        chunks = self._selected_chunks()
        if not chunks:
            return
        if len(chunks) == 1:
            chunk = chunks[0]
            default_name = (core.sanitize_name(chunk.display_name()) or f"chunk_{chunk.tag}") + chunk.extension()
            path = filedialog.asksaveasfilename(initialfile=default_name, title="Export chunk bytes")
            if not path:
                return
            Path(path).write_bytes(chunk.raw_chunk_bytes())
            self.app.log(f"Exported chunk '{chunk.display_name() or chunk.tag}' -> {path}")
            return
        folder = filedialog.askdirectory(title=f"Export {len(chunks)} chunks to folder (raw bytes)")
        if not folder:
            return
        for chunk in chunks:
            dest = _unique_path(Path(folder), core.sanitize_name(chunk.display_name()) or f"chunk_{chunk.tag}",
                                chunk.extension())
            dest.write_bytes(chunk.raw_chunk_bytes())
        self.app.log(f"Exported {len(chunks)} chunks (raw bytes) -> {folder}")

    def _replace_selected(self):
        chunk = self._require_single("replace its payload")
        if chunk is None:
            return
        path = filedialog.askopenfilename(title="Replace chunk payload from file")
        if not path:
            return
        data = Path(path).read_bytes()
        if data[:4] == chunk.tag.encode("ascii", "replace") and len(data) >= 8:
            # looks like a full raw chunk (tag+len+payload) - take just the payload
            import struct
            length = struct.unpack_from("<I", data, 4)[0]
            data = data[8:8 + length]
        name = chunk.display_name() or chunk.tag
        old_payload = chunk.payload
        chunk.set_payload(data)
        self.refresh_tree()
        self.app.set_dirty()
        self.app.record_undo(f"Replace payload of '{name}'",
                             undo_fn=lambda: chunk.set_payload(old_payload),
                             redo_fn=lambda: chunk.set_payload(data))
        self.app.log(f"Replaced payload of chunk '{name}' from {path} ({len(data)} bytes)")

    def _export_usable_selected(self):
        """Exports the chunk in the most 'usable' format for its type:
        tex_ -> .png/.dds, scr_ -> .luac (raw Lua 5.1 bytecode), skel -> a
        small .json bone dump, modl/gmod/coll -> a full .glb (via the
        bundled swbf-unmunge.exe - see core.export_model_bundle_to_file's
        docstring for why that one shells out instead of a native decode).
        Falls back to the same raw framed bytes as 'Export Chunk Bytes' for
        any tag without a dedicated converter. With more than one chunk
        selected, exports all of them into a chosen folder instead of
        prompting for a filename per chunk."""
        chunks = self._selected_chunks()
        if not chunks:
            return
        if len(chunks) == 1:
            chunk = chunks[0]
            default_name = (core.sanitize_name(chunk.display_name()) or f"chunk_{chunk.tag}") + chunk.extension()
            path = filedialog.asksaveasfilename(
                initialfile=default_name, title="Export as usable file",
                filetypes=[("PNG image", "*.png"), ("DDS texture", "*.dds"), ("glTF binary", "*.glb"),
                           ("Compiled Lua", "*.luac"), ("ODF text", "*.odf"), ("JSON", "*.json"),
                           ("All files", "*.*")],
            )
            if not path:
                return
            try:
                written = self._export_one_usable(chunk, path)
            except Exception as exc:
                messagebox.showerror("Export failed", str(exc))
                return
            self.app.log(f"Exported chunk '{chunk.display_name() or chunk.tag}' -> {written}")
            return
        folder = filedialog.askdirectory(title=f"Export {len(chunks)} chunks to folder (usable files)")
        if not folder:
            return
        failed = []
        for chunk in chunks:
            stem = core.sanitize_name(chunk.display_name()) or f"chunk_{chunk.tag}"
            dest = _unique_path(Path(folder), stem, chunk.extension())
            try:
                self._export_one_usable(chunk, dest)
            except Exception as exc:
                failed.append(f"{chunk.display_name() or chunk.tag}: {exc}")
        self.app.log(f"Exported {len(chunks) - len(failed)}/{len(chunks)} chunks (usable files) -> {folder}")
        if failed:
            messagebox.showwarning("Some exports failed", "\n".join(failed))

    def _export_one_usable(self, chunk: core.Chunk, path) -> Path:
        if chunk.tag in core.MODEL_TAGS:
            return core.export_model_bundle_to_file(self.container.body.chunks, chunk, path)
        return core.export_chunk_to_file(chunk, path)

    def _import_usable_selected(self):
        """Inverse of 'Export As Usable File...': a .png/.dds becomes a new
        tex_ payload (re-encoded to DXT), a .luac becomes a new scr_
        payload, a .glb replaces a modl chunk's geometry (see
        bf1_glb_import.py's docstring for exactly what that does and
        doesn't touch - materials/textures are kept, gmod/coll/skel are
        left alone), anything else is treated as a raw payload/framed
        chunk exactly like 'Replace Payload From File'. gmod/coll/skel
        still have no import path - export-only, since swbf-unmunge itself
        doesn't have a way to rebuild them either."""
        chunk = self._require_single("import into it")
        if chunk is None:
            return
        if chunk.tag in ("gmod", "coll", "skel"):
            messagebox.showinfo(
                "Not supported",
                f"'{chunk.tag}' chunks are export-only: there's no tool available to rebuild "
                "them from an edited file, only to unmunge them into one.")
            return
        if chunk.tag == "modl":
            import bf1_skinned_import as sk
            if sk.is_skinned_model(chunk.payload):
                self._import_character_selected()
                return
            path = filedialog.askopenfilename(
                title="Import .glb to replace this model's geometry",
                filetypes=[("glTF binary", "*.glb"), ("All files", "*.*")],
            )
            if not path:
                return
            try:
                import bf1_glb_import as glb_import
                new_payload = glb_import.import_glb_into_model(
                    self.container.body.chunks, chunk, Path(path).read_bytes())
            except Exception as exc:
                messagebox.showerror("Import failed", str(exc))
                return
            old_payload = chunk.payload
            chunk.set_payload(new_payload)
            self.refresh_tree()
            self.app.set_dirty()
            self.app.record_undo(f"Replace geometry of '{chunk.display_name()}'",
                                 undo_fn=lambda: chunk.set_payload(old_payload),
                                 redo_fn=lambda: chunk.set_payload(new_payload))
            self.app.log(f"Replaced geometry of modl chunk '{chunk.display_name()}' from {path}")
            return
        path = filedialog.askopenfilename(
            title="Import from usable file",
            filetypes=[("Image files", "*.png *.dds *.bmp *.tga *.jpg *.jpeg"),
                       ("Compiled Lua bytecode", "*.luac"), ("ODF text", "*.odf"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            new_payload = core.import_file_to_chunk_payload(chunk.tag, path)
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc))
            return
        name = chunk.display_name() or chunk.tag
        old_payload = chunk.payload
        chunk.set_payload(new_payload)
        self.refresh_tree()
        self.app.set_dirty()
        self.app.record_undo(f"Import into '{name}'",
                             undo_fn=lambda: chunk.set_payload(old_payload),
                             redo_fn=lambda: chunk.set_payload(new_payload))
        self.app.log(f"Imported payload of chunk '{name}' from {path} ({len(new_payload)} bytes)")

    def _import_character_selected(self):
        """Soldier bodies and vehicles (modl chunks whose segments carry
        per-vertex bone data) - see SkinnedImportDialog."""
        chunk = self._require_single("import a model into it")
        if chunk is None:
            return
        import bf1_skinned_import as sk
        if chunk.tag != "modl" or not sk.is_skinned_model(chunk.payload):
            messagebox.showinfo(
                "Not a character or vehicle model",
                "Select a character's or vehicle's 'modl' chunk - one with a skeleton, like rep_inf_trooper "
                "or rep_hover_fightertank. (Weapons and props use 'Import From Usable File...' instead.)")
            return
        name = chunk.display_name()
        for suffix in sk.LOD_SUFFIXES:
            base = name[: -len(suffix)]
            if name.endswith(suffix) and any(ch.tag == "modl" and ch.display_name() == base
                                             for ch in chunk.parent_body.chunks):
                messagebox.showinfo(
                    "This is the low-detail version",
                    f"'{name}' is the low-detail (LOD) version of '{base}'. Import into '{base}' instead - "
                    "its import replaces this one as well.")
                return
        path = filedialog.askopenfilename(title=f"Import .glb as '{name}'",
                                          filetypes=[("glTF binary", "*.glb"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.config(cursor="watch")
            self.update_idletasks()
            SkinnedImportDialog(self.app, self, chunk, path)
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc))
        finally:
            self.config(cursor="")

    def _move_points_selected(self):
        """Opens FirePointEditor on a character/vehicle model as it is now
        (e.g. one imported earlier) and writes the moved points into its
        skeleton - and its LOD's - as one undo step."""
        chunk = self._require_single("move its fire points")
        if chunk is None:
            return
        import bf1_skinned_import as sk
        name = chunk.display_name()
        siblings = chunk.parent_body.chunks
        bones = sk.find_skeleton(siblings, name) if chunk.tag == "modl" else None
        if bones is None:
            messagebox.showinfo("No fire points",
                                "Select a character's or vehicle's 'modl' chunk - one with a skeleton, like "
                                "rep_inf_trooper or rep_hover_fightertank.")
            return
        for suffix in sk.LOD_SUFFIXES:
            base = name[: -len(suffix)]
            if name.endswith(suffix) and any(ch.tag == "modl" and ch.display_name() == base for ch in siblings):
                messagebox.showinfo("This is the low-detail version",
                                    f"'{name}' is the low-detail (LOD) version of '{base}'. Move the points on "
                                    f"'{base}' instead - its LOD gets the same moves.")
                return
        points = sk.hard_points(bones, siblings, name)
        if not points:
            messagebox.showinfo("No fire points", f"'{name}' has no fire points or other hardpoints.")
            return
        texture = None
        tex_chunk = sk.find_texture(siblings, sk.model_texture(chunk.payload))
        if tex_chunk is not None:
            decoded = core.decode_texture_chunk_to_rgba(tex_chunk.payload)
            if decoded is not None:
                from PIL import Image
                texture = Image.frombytes("RGBA", (decoded["width"], decoded["height"]), decoded["rgba"])
        lod = sk.find_lod(siblings, name)
        models = [name] + ([lod.display_name()] if lod is not None else [])

        def done(moves: dict):
            if not moves:
                return
            try:
                changes = point_move_changes(sk, siblings, models, moves)
            except Exception as exc:
                messagebox.showerror("Move fire points failed", str(exc))
                return
            for ch, _old, new in changes:
                ch.set_payload(new)
            self.app.record_undo(f"Move fire points of '{name}'",
                                 undo_fn=lambda: [ch.set_payload(old) for ch, old, _ in changes],
                                 redo_fn=lambda: [ch.set_payload(new) for ch, _, new in changes])
            self.app.set_dirty()
            self.refresh_tree()
            self._on_select()
            self.app.log(f"Moved {len(moves)} point(s) on '{name}': {', '.join(sorted(moves))}")

        FirePointEditor(self.app, chunk.payload, bones, texture, points, {}, done, sk.is_character(bones))

    def _find_references_selected(self):
        """Finds every other chunk (in any .lvl in a folder) whose payload
        contains this chunk's name - not a curated list of "known reference
        fields" (there are dozens across ordc/wpnc/expc/entc alone, and any
        such list would inevitably miss some, plus fields in chunk types
        this module doesn't model at all, like fx__). Since a reference is
        just that other chunk's property value storing this chunk's name as
        plain ascii text, a byte-level search catches all of them, known or
        not - it's the same engine 'Find in Files' data-search uses, just
        pre-filled and auto-run."""
        chunk = self._require_single("find references to it")
        if chunk is None:
            return
        if chunk.tag in core.KV_TAGS:
            # Other properties reference an ordc/wpnc/expc/entc chunk by its
            # TYPE (e.g. WeaponName="all_weap_..."), not by any NAME block -
            # these chunks don't have one.
            name = core.decode_ordnance_payload(chunk.payload)["type"]
        else:
            name = chunk.display_name()
        if not name:
            messagebox.showinfo("No name", "This chunk has no identifiable name to search for.")
            return
        self.app.find_references(name)

    def _delete_selected(self):
        chunks = self._selected_chunks()
        if not chunks:
            return
        body = self.container.body
        if self.app.settings.get("confirm_delete", True):
            if len(chunks) == 1:
                label = f"Delete chunk '{chunks[0].display_name() or chunks[0].tag}'? (Ctrl+Z will undo this.)"
            else:
                label = f"Delete {len(chunks)} selected chunks? (Ctrl+Z will undo this.)"
            if not messagebox.askyesno("Delete chunk" if len(chunks) == 1 else "Delete chunks", label):
                return
        # Original indices, ascending - re-inserting in this order at these
        # exact indices reconstructs the original list exactly (each insert
        # accounts for every earlier one having already shifted things back
        # into place; nothing with a higher original index has been touched
        # yet), so this works for any subset, not just contiguous runs.
        entries = sorted(((body.chunks.index(ch), ch) for ch in chunks), key=lambda e: e[0])
        for _, ch in entries:
            body.remove_chunk(ch)
        self.refresh_tree()
        self._clear_detail()
        self.app.set_dirty()

        def undo_all():
            for index, ch in entries:
                body.add_chunk(ch, index)

        def redo_all():
            for _, ch in entries:
                body.remove_chunk(ch)

        label_text = (f"Delete '{chunks[0].display_name() or chunks[0].tag}'" if len(chunks) == 1
                      else f"Delete {len(chunks)} chunks")
        self.app.record_undo(label_text, undo_fn=undo_all, redo_fn=redo_all)
        self.app.log(f"Deleted {len(chunks)} chunk(s)")

    def add_chunk(self, chunk: core.Chunk):
        body = self.container.body
        body.add_chunk(chunk)
        self.refresh_tree()
        self.app.set_dirty()
        self.app.record_undo(f"Add chunk '{chunk.tag}'",
                             undo_fn=lambda: body.remove_chunk(chunk),
                             redo_fn=lambda: body.add_chunk(chunk))
        self.app.log(f"Added new chunk '{chunk.tag}'")

    def _select_index(self, index: int) -> None:
        """Selects + scrolls to the chunk row at `index` after a
        refresh_tree() call (row iids are always 'c<index>')."""
        iid = f"c{index}"
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)

    # -- reordering (Alt/Ctrl+Up/Down and "Move to Index...") ---------------

    def _move_selected(self, delta: int):
        """Moves the selected chunk one step earlier/later. Bound to
        Alt+Up/Down and Ctrl+Up/Down (see __init__)."""
        chunk = self._selected_chunk()
        if chunk is None:
            return "break"
        body = self.container.body
        old_index = body.chunks.index(chunk)
        new_index = body.move_chunk(chunk, delta)
        self.refresh_tree()
        self._select_index(new_index)
        self.app.set_dirty()
        if new_index != old_index:
            self.app.record_undo(f"Move '{chunk.display_name() or chunk.tag}'",
                                 undo_fn=lambda: body.move_chunk(chunk, old_index - new_index),
                                 redo_fn=lambda: body.move_chunk(chunk, new_index - old_index))
        self.app.log(f"Moved chunk '{chunk.display_name() or chunk.tag}' to index {new_index}")
        return "break"  # don't let the plain Up/Down selection-move binding also fire

    def _move_to_index(self):
        chunk = self._require_single("move it")
        if chunk is None:
            return
        chunks = self.container.body.chunks
        old_index = chunks.index(chunk)
        last = len(chunks) - 1
        new_index = simpledialog.askinteger(
            "Move to Index",
            f"New index for chunk '{chunk.display_name() or chunk.tag}' (0-{last}):",
            parent=self, initialvalue=old_index, minvalue=0, maxvalue=last,
        )
        if new_index is None or new_index == old_index:
            return
        body = self.container.body
        actual_index = body.move_chunk(chunk, new_index - old_index)
        self.refresh_tree()
        self._select_index(actual_index)
        self.app.set_dirty()
        if actual_index != old_index:
            self.app.record_undo(f"Move '{chunk.display_name() or chunk.tag}'",
                                 undo_fn=lambda: body.move_chunk(chunk, old_index - actual_index),
                                 redo_fn=lambda: body.move_chunk(chunk, actual_index - old_index))
        self.app.log(f"Moved chunk '{chunk.display_name() or chunk.tag}' from index {old_index} to {actual_index}")

    # -- copy / paste ---------------------------------------------------------

    def _copy_selected(self):
        chunk = self._require_single("copy it")
        if chunk is None:
            return "break"
        # Snapshot tag+payload into a fresh, independent Chunk so later
        # edits to the original (or to a pasted copy) never cross-affect
        # the other. Deliberately not the same object as chunk.get_
        # nested_container()'s cache - pasting a 'lvl_' chunk re-parses its
        # own nested container lazily, same as opening any other chunk.
        self.app.chunk_clipboard = core.Chunk(chunk.tag, chunk.payload)
        self.app.log(f"Copied chunk '{chunk.display_name() or chunk.tag}' ({chunk.tag}) to clipboard")
        return "break"

    def _paste_selected(self):
        clip = getattr(self.app, "chunk_clipboard", None)
        if clip is None:
            messagebox.showinfo("Clipboard empty", "Copy a chunk first (Ctrl+C or right-click > Copy Chunk).")
            return "break"
        # Independent copy each paste, so pasting twice doesn't insert the
        # same live object into the chunk list in two places.
        new_chunk = core.Chunk(clip.tag, clip.payload)
        selected = self._selected_chunk()
        if selected is not None:
            index = self.container.body.chunks.index(selected) + 1
        else:
            index = None  # nothing selected -> append at the end
        body = self.container.body
        body.add_chunk(new_chunk, index)
        self.refresh_tree()
        self._select_index(index if index is not None else len(self.container.body.chunks) - 1)
        self.app.set_dirty()
        self.app.record_undo(f"Paste chunk '{new_chunk.tag}'",
                             undo_fn=lambda: body.remove_chunk(new_chunk),
                             redo_fn=lambda: body.add_chunk(new_chunk, index))
        self.app.log(f"Pasted chunk '{new_chunk.tag}' at index "
                      f"{index if index is not None else len(self.container.body.chunks) - 1}")
        return "break"


def _unique_path(folder: Path, stem: str, suffix: str) -> Path:
    """<folder>/<stem><suffix>, or <stem>_2<suffix>, _3, ... if that's taken
    - chunk display names aren't guaranteed unique (ordc/expc/wpnc have
    none at all), so bulk export needs this to avoid silently overwriting
    one exported file with the next."""
    candidate = folder / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{n}{suffix}"
        n += 1
    return candidate


class MultiSelectionPanel(ttk.Frame):
    """Shown in place of a single-chunk editor when more than one chunk is
    selected: a summary plus the same bulk actions available from the
    right-click menu, so you don't have to go find them."""

    def __init__(self, master, app: "App", owning_tab: WorkspaceTab, chunks: list[core.Chunk]):
        super().__init__(master)
        self.app = app
        self.owning_tab = owning_tab
        self.chunks = chunks

        total_bytes = sum(len(ch.payload) for ch in chunks)
        tag_counts: dict[str, int] = {}
        for ch in chunks:
            tag_counts[ch.tag] = tag_counts.get(ch.tag, 0) + 1
        breakdown = ", ".join(f"{n}x {tag}" for tag, n in sorted(tag_counts.items()))

        ttk.Label(self, text=f"{len(chunks)} chunks selected  -  {total_bytes:,} bytes total",
                  font=("TkDefaultFont", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 2))
        ttk.Label(self, text=breakdown).pack(anchor="w", padx=10, pady=(0, 10))

        btns = ttk.Frame(self)
        btns.pack(anchor="w", padx=10)
        ttk.Button(btns, text="Export Raw Bytes to Folder...",
                   command=owning_tab._export_selected).pack(side="left")
        ttk.Button(btns, text="Export As Usable Files to Folder...",
                   command=owning_tab._export_usable_selected).pack(side="left", padx=6)
        ttk.Button(btns, text="Delete All Selected", command=owning_tab._delete_selected).pack(side="left")


class LevelChunkPanel(ttk.Frame):
    """Shown when a 'lvl_' chunk is selected but not yet opened as a tab."""

    def __init__(self, master, app: "App", owning_tab: WorkspaceTab, chunk: core.Chunk):
        super().__init__(master)
        self.app = app
        self.owning_tab = owning_tab
        self.chunk = chunk

        ttk.Label(self, text=f"Nested level chunk: {chunk.display_name() or '(unnamed)'}",
                  font=("TkDefaultFont", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
        ttk.Label(self, text=f"{len(chunk.payload)} bytes payload.").pack(anchor="w", padx=10)
        ttk.Button(self, text="Open Nested Level in New Tab",
                   command=owning_tab._open_selected_nested).pack(anchor="w", padx=10, pady=10)


class TexturePreview(ttk.Frame):
    """Read-only decoded thumbnail for tex_ chunks, so you can tell what a
    texture actually looks like without exporting it first. Falls back to a
    plain info line (no image) for a fourcc this build can't decode to RGBA
    (e.g. no Pillow, or a compressed format bf1_core doesn't handle) -
    'Export As Usable File...' still works either way, since it also falls
    back to .dds in that case."""

    def __init__(self, master, app: "App", chunk: core.Chunk):
        super().__init__(master)
        self.app = app
        self.chunk = chunk
        self._photo = None  # keep a reference - Tk drops the image otherwise

        info = core.decode_texture_chunk(chunk.payload)
        fmt = info["formats"][0] if info["formats"] else None
        header_text = f"Texture '{info['name'] or '(unnamed)'}'"
        if fmt is not None:
            header_text += (f"  -  {fmt['width']}x{fmt['height']}  -  {fmt['fourcc']}  -  "
                            f"{fmt['mip_count']} mip level(s), {len(info['formats'])} format(s)")
        ttk.Label(self, text=header_text, font=("TkDefaultFont", 10, "bold")).pack(anchor="w", padx=8, pady=6)

        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        decoded = core.decode_texture_chunk_to_rgba(chunk.payload)
        if decoded is None:
            reason = "Pillow isn't installed" if not _pillow_available() else \
                f"fourcc {fmt['fourcc'] if fmt else '?'!r} isn't a decodable format in this build"
            ttk.Label(canvas_frame, text=f"(No preview available - {reason}.)").pack(anchor="w")
            return

        try:
            from PIL import Image, ImageTk
        except ImportError:
            ttk.Label(canvas_frame, text="(No preview available - Pillow isn't installed.)").pack(anchor="w")
            return

        img = Image.frombytes("RGBA", (decoded["width"], decoded["height"]), decoded["rgba"])
        display = img
        longest = max(img.width, img.height)
        if longest < px(128):
            scale = max(1, px(256) // max(1, longest))
            display = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
        elif longest > px(512):
            scale = px(512) / longest
            display = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)

        # Checkerboard behind alpha, same idea as any image editor's transparency grid.
        checker = Image.new("RGB", display.size)
        tile = 8
        for y in range(0, display.height, tile):
            for x in range(0, display.width, tile):
                shade = 200 if ((x // tile) + (y // tile)) % 2 == 0 else 160
                checker.paste((shade, shade, shade), (x, y, x + tile, y + tile))
        composited = Image.alpha_composite(checker.convert("RGBA"), display)

        self._photo = ImageTk.PhotoImage(composited)
        ttk.Label(canvas_frame, image=self._photo).pack(anchor="w")


def _pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


class HexViewer(ttk.Frame):
    """Read-only hex+ascii view for chunk types we don't have a structured
    editor for, plus a way to swap the payload from a file."""

    def __init__(self, master, app: "App", chunk: core.Chunk, on_change):
        super().__init__(master)
        self.app = app
        self.chunk = chunk
        self.on_change = on_change

        header = ttk.Frame(self)
        header.pack(fill="x", padx=8, pady=6)
        ttk.Label(header, text=f"Chunk '{chunk.tag}'  -  {chunk.display_name() or '(unnamed)'}  -  {len(chunk.payload)} bytes",
                  font=("TkDefaultFont", 10, "bold")).pack(side="left")

        colors = text_colors(self.winfo_toplevel())
        text = tk.Text(self, wrap="none", height=24, font=("Courier New", 10),
                       bg=colors["text_bg"], fg=colors["fg"], insertbackground=colors["fg"],
                       selectbackground=colors["select_bg"], selectforeground=colors["select_fg"])
        vsb = ttk.Scrollbar(self, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=vsb.set)
        text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        vsb.pack(side="right", fill="y", pady=(0, 8))

        text.insert("1.0", self._hexdump(chunk.payload))
        text.configure(state="disabled")

    @staticmethod
    def _hexdump(data: bytes, max_bytes: int = 8192) -> str:
        if not data:
            return "(empty payload)"
        shown = data[:max_bytes]
        lines = []
        for i in range(0, len(shown), 16):
            row = shown[i:i + 16]
            hex_part = " ".join(f"{b:02x}" for b in row)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            lines.append(f"{i:08x}  {hex_part:<47}  {ascii_part}")
        if len(data) > max_bytes:
            lines.append(f"... ({len(data) - max_bytes} more bytes not shown)")
        return "\n".join(lines)


class StructuredBinaryViewer(ttk.Frame):
    """Read-only tree view for zaa_/zaf_ chunks - a best-effort structural
    breakdown (see core.decode_structured_binary_tree's docstring for why
    it's view-only: these aren't key/value data like ordc/wpnc/expc/entc,
    they're a different binary format this module only partly understands,
    and neither does swbf-unmunge.exe). Whatever the decoder couldn't
    confidently parse shows up as an 'unparsed tail' node instead of being
    silently dropped or guessed at."""

    def __init__(self, master, app: "App", chunk: core.Chunk):
        super().__init__(master)
        self.app = app
        self.chunk = chunk

        info = core.decode_structured_binary_tree(chunk.payload)

        header = ttk.Frame(self)
        header.pack(fill="x", padx=8, pady=6)
        ttk.Label(header, text=f"Chunk '{chunk.tag}'  -  {info['name'] or '(unnamed)'}  -  {len(chunk.payload)} bytes "
                                "(structure view only - see 'Export As Usable File...' for a full JSON dump)",
                  font=("TkDefaultFont", 10, "bold")).pack(side="left")

        columns = ("length", "detail")
        self.tree = ttk.Treeview(self, columns=columns, show="tree headings", height=18)
        self.tree.heading("#0", text="Tag")
        self.tree.heading("length", text="Length")
        self.tree.heading("detail", text="Offset / preview")
        self.tree.column("#0", width=px(160))
        self.tree.column("length", width=px(80), stretch=False)
        self.tree.column("detail", width=px(400))
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        vsb.pack(side="right", fill="y", pady=(0, 8))

        self._populate("", info["tree"])
        for iid in self.tree.get_children(""):
            self.tree.item(iid, open=True)

    def _populate(self, parent_iid: str, nodes: list[dict]) -> None:
        for node in nodes:
            if "unparsed_tail_offset" in node:
                self.tree.insert(parent_iid, "end", text="(unparsed tail)",
                                  values=(node["unparsed_tail_length"],
                                          f"@{node['unparsed_tail_offset']}  {node['unparsed_tail_preview']}..."))
                continue
            detail = f"@{node['offset']}"
            if "payload_preview" in node:
                detail += f"  {node['payload_preview']}"
            iid = self.tree.insert(parent_iid, "end", text=node["tag"], values=(node["length"], detail))
            if "children" in node:
                self._populate(iid, node["children"])


class OrdnanceEditor(ttk.Frame):
    """Structured editor for ordc/wpnc/expc chunks."""

    def __init__(self, master, app: "App", chunk: core.Chunk, on_change):
        super().__init__(master)
        self.app = app
        self.chunk = chunk
        self.on_change = on_change

        info = core.decode_ordnance_payload(chunk.payload)

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text=f"Ordnance chunk ({chunk.tag})", font=("TkDefaultFont", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(top, text="Base:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.base_var = tk.StringVar(value=info["base"])
        ttk.Entry(top, textvariable=self.base_var, width=30).grid(row=1, column=1, sticky="w", pady=(6, 0))

        ttk.Label(top, text="ClassLabel/Type:").grid(row=2, column=0, sticky="w")
        self.type_var = tk.StringVar(value=info["type"])
        ttk.Entry(top, textvariable=self.type_var, width=30).grid(row=2, column=1, sticky="w")

        ttk.Label(top, text="Tag:").grid(row=3, column=0, sticky="w")
        self.tag_var = tk.StringVar(value=chunk.tag)
        tag_combo = ttk.Combobox(top, textvariable=self.tag_var, values=sorted(core.KV_TAGS), width=10, state="readonly")
        tag_combo.grid(row=3, column=1, sticky="w")

        ttk.Label(self, text="Properties (unresolved hashes show as 0xHHHHHHHH and round-trip exactly):").pack(anchor="w", padx=8, pady=(6, 0))

        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=8, pady=4)
        self.props_tree = ttk.Treeview(table_frame, columns=("name", "value"), show="headings", selectmode="browse", height=10)
        self.props_tree.heading("name", text="Name / Hash")
        self.props_tree.heading("value", text="Value")
        self.props_tree.column("name", width=px(220))
        self.props_tree.column("value", width=px(220))
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.props_tree.yview)
        self.props_tree.configure(yscrollcommand=vsb.set)
        self.props_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.props_tree.bind("<Double-1>", lambda e: self._edit_property())

        self._props = list(info["props"])
        self._refresh_props_tree()

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Button(btns, text="Add Property", command=self._add_property).pack(side="left")
        ttk.Button(btns, text="Edit Selected", command=self._edit_property).pack(side="left", padx=4)
        ttk.Button(btns, text="Delete Selected", command=self._delete_property).pack(side="left")
        ttk.Button(btns, text="Edit as ODF Text...", command=self._edit_as_text).pack(side="left", padx=(16, 0))

        ttk.Button(self, text="Apply Changes", command=self._apply).pack(anchor="e", padx=8, pady=8)

    def _edit_as_text(self):
        """Bulk-edit Base/Type/all properties at once as real .odf text -
        same staging area as the property table (base_var/type_var/_props),
        just a different editor for it. Still doesn't touch the chunk
        itself until the outer 'Apply Changes' button is clicked."""
        payload = core.encode_ordnance_payload(self.base_var.get(), self.type_var.get(), self._props)
        text = core.encode_kv_chunk_to_odf_text(self.tag_var.get(), payload)
        dlg = OdfTextDialog(self, text)
        if dlg.result_text is None:
            return
        try:
            new_payload = core.decode_odf_text_to_kv_payload(dlg.result_text, fallback_type=self.type_var.get())
        except Exception as exc:
            messagebox.showerror("Couldn't parse ODF text", str(exc))
            return
        info = core.decode_ordnance_payload(new_payload)
        self.base_var.set(info["base"])
        self.type_var.set(info["type"])
        self._props = list(info["props"])
        self._refresh_props_tree()

    def _refresh_props_tree(self):
        self.props_tree.delete(*self.props_tree.get_children())
        for i, (name, value) in enumerate(self._props):
            self.props_tree.insert("", "end", iid=str(i), values=(name, value))

    def _selected_index(self) -> int | None:
        sel = self.props_tree.selection()
        if not sel:
            return None
        return int(sel[0])

    def _add_property(self):
        dlg = PropertyDialog(self, "Add Property")
        if dlg.result_name:
            self._props.append((dlg.result_name, dlg.result_value or ""))
            self._refresh_props_tree()

    def _edit_property(self):
        idx = self._selected_index()
        if idx is None:
            return
        name, value = self._props[idx]
        dlg = PropertyDialog(self, "Edit Property", name, value)
        if dlg.result_name:
            self._props[idx] = (dlg.result_name, dlg.result_value or "")
            self._refresh_props_tree()

    def _delete_property(self):
        idx = self._selected_index()
        if idx is None:
            return
        del self._props[idx]
        self._refresh_props_tree()

    def _apply(self):
        tag = self.tag_var.get().strip()
        if len(tag) != 4:
            messagebox.showerror("Invalid tag", "Chunk tag must be exactly 4 characters.")
            return
        new_payload = core.encode_ordnance_payload(self.base_var.get(), self.type_var.get(), self._props)
        chunk = self.chunk
        old_tag, old_payload = chunk.tag, chunk.payload

        def apply_state(use_tag, use_payload):
            chunk.tag = use_tag
            chunk.set_payload(use_payload)

        apply_state(tag, new_payload)
        self.on_change()
        self.app.set_dirty()
        self.app.record_undo(f"Edit ordnance chunk '{self.base_var.get()}'",
                             undo_fn=lambda: apply_state(old_tag, old_payload),
                             redo_fn=lambda: apply_state(tag, new_payload))
        self.app.log(f"Applied changes to ordnance chunk '{self.base_var.get()}' ({tag})")


class FindInFilesDialog(tk.Toplevel):
    """Cross-file search: scans every .lvl in a folder (recursing into every
    nested 'lvl_' chunk, same as the per-tab find bar) for chunks matching
    the type/name/data filters, and lets you jump straight to a result.

    Runs synchronously rather than in a background thread - Tk objects
    aren't safe to touch from another thread, and parsing even a 100+MB
    level only takes a couple of seconds (see bf1_core.py's parser), so a
    plain loop with an update_idletasks() + status label between files
    keeps the dialog responsive (repaints, and the Cancel button works)
    without the complexity/risk of real threading."""

    def __init__(self, app: "App", default_folder: Path):
        super().__init__(app)
        self.app = app
        self.title("Find in Files")
        self.geometry(f"{px(780)}x{px(480)}")
        self._cancel = False
        self._results: list[tuple[Path, tuple, int]] = []

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=8)

        ttk.Label(top, text="Folder:").grid(row=0, column=0, sticky="w")
        self.folder_var = tk.StringVar(value=str(default_folder))
        ttk.Entry(top, textvariable=self.folder_var, width=50).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="Browse...", command=self._browse).grid(row=0, column=2)
        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Include subfolders", variable=self.recursive_var).grid(
            row=0, column=3, padx=(8, 0))

        ttk.Label(top, text="Type:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.tag_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.tag_var, width=10).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(top, text="Name:").grid(row=2, column=0, sticky="w")
        self.name_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.name_var, width=30).grid(row=2, column=1, sticky="w")
        ttk.Label(top, text="Data:").grid(row=3, column=0, sticky="w")
        self.data_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.data_var, width=30).grid(row=3, column=1, sticky="w")

        btn_row = ttk.Frame(top)
        btn_row.grid(row=1, column=2, columnspan=2, rowspan=3, sticky="n", padx=(8, 0))
        self.search_btn = ttk.Button(btn_row, text="Search", command=self._start_search)
        self.search_btn.pack(fill="x")
        self.cancel_btn = ttk.Button(btn_row, text="Cancel", command=self._request_cancel, state="disabled")
        self.cancel_btn.pack(fill="x", pady=(4, 0))

        top.columnconfigure(1, weight=1)

        self.status_var = tk.StringVar(value="Enter search terms and click Search.")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(fill="x", padx=8)

        columns = ("file", "path", "tag", "name", "size")
        self.results_tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="browse")
        for col, label, width in [("file", "File", 120), ("path", "Location", 220),
                                  ("tag", "Tag", 50), ("name", "Name", 180), ("size", "Size", 70)]:
            self.results_tree.heading(col, text=label)
            self.results_tree.column(col, width=px(width), stretch=(col in ("path", "name")))
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.results_tree.yview)
        self.results_tree.configure(yscrollcommand=vsb.set)
        self.results_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        vsb.pack(side="right", fill="y", pady=8)
        self.results_tree.bind("<Double-1>", self._on_result_double_click)

        self.bind("<Return>", lambda e: self._start_search())
        self.transient(app)

    def _browse(self):
        folder = filedialog.askdirectory(initialdir=self.folder_var.get() or ".")
        if folder:
            self.folder_var.set(folder)

    def _request_cancel(self):
        self._cancel = True

    def _start_search(self):
        folder = Path(self.folder_var.get())
        if not folder.is_dir():
            messagebox.showerror("Find in Files", f"Not a folder: {folder}")
            return
        tag_q, name_q, data_q = self.tag_var.get().strip(), self.name_var.get().strip(), self.data_var.get().strip()
        if not (tag_q or name_q or data_q):
            messagebox.showinfo("Find in Files", "Enter at least one of Type/Name/Data to search for.")
            return

        pattern = "**/*.lvl" if self.recursive_var.get() else "*.lvl"
        files = sorted(folder.glob(pattern))
        self.results_tree.delete(*self.results_tree.get_children())
        self._results.clear()
        self._cancel = False
        self.search_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        try:
            self._run_search(files, tag_q, name_q, data_q)
        finally:
            self.search_btn.config(state="normal")
            self.cancel_btn.config(state="disabled")

    def _run_search(self, files: list[Path], tag_q: str, name_q: str, data_q: str):
        total_matches = 0
        for i, path in enumerate(files):
            if self._cancel:
                self.status_var.set(f"Cancelled after {i}/{len(files)} file(s) - {total_matches} match(es).")
                return
            self.status_var.set(f"Scanning {path.name} ({i + 1}/{len(files)})...")
            self.update_idletasks()
            try:
                data = path.read_bytes()
                container = core.parse_container(data)
            except Exception as exc:
                self.status_var.set(f"Skipped {path.name} (couldn't parse: {exc})")
                self.update_idletasks()
                continue
            for idx_path, index, chunk, name_path in core.find_in_container(container, tag_q, name_q, data_q):
                result_id = str(len(self._results))
                self._results.append((path, idx_path, index))
                location = "/" + "/".join(name_path) if name_path else "/"
                self.results_tree.insert("", "end", iid=result_id, values=(
                    path.name, location, chunk.tag, chunk.display_name(), len(chunk.payload)))
                total_matches += 1
            if total_matches and total_matches % 50 == 0:
                self.update_idletasks()  # keep the results list painting on long scans
        self.status_var.set(f"Done - {total_matches} match(es) in {len(files)} file(s).")

    def _on_result_double_click(self, _event=None):
        sel = self.results_tree.selection()
        if not sel:
            return
        path, idx_path, index = self._results[int(sel[0])]
        self.app.navigate_to_result(path, idx_path, index)


class SettingsDialog(tk.Toplevel):
    """Preferences that persist across sessions (see SETTINGS_PATH) -
    applied live on OK, no restart needed."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.resizable(False, False)
        self.transient(app)

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Appearance", font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, sticky="w", columnspan=2)
        ttk.Label(body, text="Theme:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.theme_var = tk.StringVar(value=app.settings.get("theme", "light"))
        theme_combo = ttk.Combobox(body, textvariable=self.theme_var, values=["light", "dark"],
                                   state="readonly", width=12)
        theme_combo.grid(row=1, column=1, sticky="w", pady=(4, 0))

        ttk.Label(body, text="Behavior", font=("TkDefaultFont", 10, "bold")).grid(
            row=2, column=0, sticky="w", pady=(14, 0), columnspan=2)
        self.confirm_delete_var = tk.BooleanVar(value=app.settings.get("confirm_delete", True))
        ttk.Checkbutton(body, text="Confirm before deleting chunks",
                        variable=self.confirm_delete_var).grid(row=3, column=0, sticky="w", columnspan=2, pady=(4, 0))
        self.auto_backup_var = tk.BooleanVar(value=app.settings.get("auto_backup", True))
        ttk.Checkbutton(body, text="Back up the original file the first time I save over it",
                        variable=self.auto_backup_var).grid(row=4, column=0, sticky="w", columnspan=2)

        btns = ttk.Frame(body)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", pady=(18, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="OK", command=self._apply).pack(side="right", padx=(0, 6))

        self.bind("<Return>", lambda e: self._apply())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _apply(self):
        app = self.app
        theme_changed = self.theme_var.get() != app.settings.get("theme")
        app.settings["theme"] = self.theme_var.get()
        app.settings["confirm_delete"] = self.confirm_delete_var.get()
        app.settings["auto_backup"] = self.auto_backup_var.get()
        save_settings(app.settings)
        if theme_changed:
            app._bf1_theme = app.settings["theme"]
            app.apply_current_theme()
        self.destroy()


class FirePointEditor(tk.Toplevel):
    """Drag a model's fire points (and optionally its other hardpoints -
    seats, damage smoke...) to new spots on front, side and top views.
    Green = on the model, red = floating off it (shots appear out of thin
    air). Right-click a point to put it back where it was. `on_done` gets
    {bone name: new bind-pose world position} for every point that moved."""

    # which world axes a view's screen x / screen y move (see sk._VIEWS)
    VIEW_AXES = {"Front": (0, 1), "Side": (2, 1), "Top": (0, 2)}
    FIRE_ON, FIRE_OFF, OTHER, SELECTED = "#50dc6e", "#ff4646", "#6fb3ff", "#ffd23a"

    def __init__(self, parent: tk.Misc, payload: bytes, bones: list, texture, points: list,
                 moves: dict, on_done, character: bool = False):
        import bf1_skinned_import as sk
        super().__init__(parent)
        self.sk, self.on_done = sk, on_done
        self.original = {name: pos for name, pos, _ in points}
        self.fire = {name for name, _, is_fire in points if is_fire}
        self.pos = {name: tuple(moves.get(name, pos)) for name, pos, _ in points}
        self.frames, self.selected, self._drag = [], None, None
        self._verts, self._limit = None, 0.15
        self.title("Fire points")
        self.transient(parent)
        self.W, self.H = px(300), px(380)

        body = ttk.Frame(self, padding=px(10))
        body.pack(fill="both", expand=True)
        text = ("Drag a point to move it - each view moves it in the two directions you can see. Green is on "
                "the model, red is floating off it (shots would come out of thin air). Right-click a point "
                "to put it back.")
        if character:
            text += (" Soldiers hold their gun at 'hp_weapons' in the hand; it follows the hand's animation, so "
                     "if the gun sits wrong the arm angle is usually the better fix.")
        ttk.Label(body, text=text, wraplength=px(880)).pack(anchor="w", pady=(0, 6))
        colors = text_colors(self.winfo_toplevel())
        self.canvas = tk.Canvas(body, width=self.W * 3, height=self.H + 18, highlightthickness=0,
                                bg=colors["bg"], cursor="hand2")
        self.canvas.pack()
        self.canvas.create_text(self.W * 3 // 2, self.H // 2, text="Rendering...", fill="gray")
        self.info_var = tk.StringVar(value="Click a point to select it.")
        ttk.Label(body, textvariable=self.info_var, foreground="gray").pack(anchor="w", pady=(6, 0))
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(8, 0))
        self.others_var = tk.BooleanVar(value=not self.fire)
        if len(self.fire) < len(points):
            ttk.Checkbutton(bar, text="Show other hardpoints (seats, damage, smoke...)", variable=self.others_var,
                            command=self._redraw).pack(side="left")
        ttk.Button(bar, text="Snap red points onto the model", command=self._snap_off).pack(side="left",
                                                                                          padx=(12, 0))
        ttk.Button(bar, text="Reset all", command=self._reset_all).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Done", command=self._done).pack(side="right", padx=(0, 6))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None) or self._redraw())
        self.canvas.bind("<Button-3>", self._reset_point)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

        import threading
        box = {}

        def render():
            try:
                tris = sk.model_triangles(payload, bones)
                verts = [p for t in tris for p in t[:3]]
                size = max(max(p[k] for p in verts) - min(p[k] for p in verts) for k in range(3)) if verts else 1.0
                box["verts"], box["limit"] = verts, max(0.15, size * 0.05)
                # frame every point (moved or not) so none starts off-screen, plus
                # a margin so a point at the model's edge can still be dragged out
                frame_points = [(p, "", (0, 0, 0)) for p in list(self.original.values()) + list(self.pos.values())]
                if verts:
                    lo = [min(p[k] for p in verts) for k in range(3)]
                    hi = [max(p[k] for p in verts) for k in range(3)]
                    pad = [(hi[k] - lo[k]) * 0.12 + 0.1 for k in range(3)]
                    frame_points += [((lo[0] - pad[0], lo[1] - pad[1], lo[2] - pad[2]), "", (0, 0, 0)),
                                     ((hi[0] + pad[0], hi[1] + pad[1], hi[2] + pad[2]), "", (0, 0, 0))]
                box["out"] = sk.render_views([(v, v, tris, True) for v in self.VIEW_AXES], texture,
                                             (self.W, self.H), frame_points, draw_markers=False)
            except Exception as exc:
                box["error"] = exc

        worker = threading.Thread(target=render, daemon=True)
        worker.start()

        def poll():
            if not self.winfo_exists():
                return
            if worker.is_alive():
                self.after(60, poll)
                return
            if "error" in box:
                messagebox.showerror("Fire points", str(box["error"]), parent=self)
                self.destroy()
                return
            from PIL import ImageTk
            img, self.frames = box["out"]
            self._verts, self._limit = box["verts"], box["limit"]
            try:
                import numpy as np
                self._verts = np.asarray(self._verts)
            except ImportError:
                pass
            self._photo = ImageTk.PhotoImage(img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
            self._redraw()

        self.after(60, poll)

    # -- geometry -----------------------------------------------------------------

    def _distance(self, p) -> float:
        if self._verts is None or len(self._verts) == 0:
            return 0.0
        if hasattr(self._verts, "shape"):
            import numpy as np
            return float(np.sqrt(((self._verts - np.asarray(p)) ** 2).sum(axis=1)).min())
        return min(math.dist(p, q) for q in self._verts)

    def _nearest(self, p) -> tuple:
        if hasattr(self._verts, "shape"):
            import numpy as np
            return tuple(float(v) for v in self._verts[int(((self._verts - np.asarray(p)) ** 2).sum(axis=1).argmin())])
        return min(self._verts, key=lambda q: math.dist(p, q))

    def _visible(self) -> list:
        return [n for n in self.pos if n in self.fire or self.others_var.get()]

    def _to_pixel(self, panel: int, p) -> tuple:
        view, scale, ox, oy = self.frames[panel]
        a, b, _ = self.sk._VIEWS[view](p)
        return ox + a * scale, oy - b * scale

    # -- drawing ------------------------------------------------------------------

    def _redraw(self):
        self.canvas.delete("pt")
        if not self.frames:
            return
        for name in self._visible():
            p = self.pos[name]
            if name == self.selected:
                color = self.SELECTED
            elif name in self.fire:
                color = self.FIRE_OFF if self._distance(p) > self._limit else self.FIRE_ON
            else:
                color = self.OTHER
            for panel in range(len(self.frames)):
                x, y = self._to_pixel(panel, p)
                if name in self.original and p != self.original[name]:  # a faint line back to where it was
                    x0, y0 = self._to_pixel(panel, self.original[name])
                    self.canvas.create_line(x0, y0, x, y, fill=color, dash=(2, 3), tags="pt")
                r = px(5)
                self.canvas.create_oval(x - r, y - r, x + r, y + r, outline=color, width=2, tags="pt")
                self.canvas.create_line(x - r - 3, y, x + r + 3, y, fill=color, tags="pt")
                self.canvas.create_line(x, y - r - 3, x, y + r + 3, fill=color, tags="pt")
                self.canvas.create_text(x + r + 3, y - r - 3, text=name, fill=color, anchor="sw",
                                        font=("TkDefaultFont", 8), tags="pt")
        self._update_info()

    def _update_info(self):
        name = self.selected
        if name is None:
            moved = sum(1 for n, p in self.pos.items() if p != self.original[n])
            self.info_var.set(f"Click a point to select it.  {moved} point{'s' if moved != 1 else ''} moved.")
            return
        p = self.pos[name]
        state = ""
        if name in self.fire:
            state = "  - off the model" if self._distance(p) > self._limit else "  - on the model"
        self.info_var.set(f"{name}: x {p[0]:.2f}, y {p[1]:.2f}, z {p[2]:.2f}{state}")

    # -- mouse --------------------------------------------------------------------

    def _hit(self, event):
        """(point name, panel) under the mouse, or (None, panel)."""
        if not self.frames:
            return None, 0
        panel = max(0, min(len(self.frames) - 1, int(event.x // self.W)))
        best, best_d = None, px(10) ** 2
        for name in self._visible():
            x, y = self._to_pixel(panel, self.pos[name])
            d = (x - event.x) ** 2 + (y - event.y) ** 2
            if d <= best_d:
                best, best_d = name, d
        return best, panel

    def _press(self, event):
        name, panel = self._hit(event)
        self.selected = name
        self._drag = (name, panel) if name else None
        self._redraw()

    def _motion(self, event):
        if not self._drag:
            return
        name, panel = self._drag
        view, scale, ox, oy = self.frames[panel]
        ax, ay = self.VIEW_AXES[view]
        p = list(self.pos[name])
        x = max(panel * self.W, min((panel + 1) * self.W - 1, event.x))
        p[ax], p[ay] = (x - ox) / scale, (oy - event.y) / scale
        self.pos[name] = tuple(p)
        self._redraw()

    def _reset_point(self, event):
        name, _ = self._hit(event)
        if name:
            self.pos[name] = self.original[name]
            self.selected = name
            self._redraw()

    def _reset_all(self):
        self.pos = dict(self.original)
        self._redraw()

    def _snap_off(self):
        if self._verts is None:
            return
        snapped = [n for n in self._visible() if n in self.fire and self._distance(self.pos[n]) > self._limit]
        for name in snapped:
            self.pos[name] = self._nearest(self.pos[name])
        self._redraw()
        if not snapped:
            self.info_var.set("No red points to snap - every fire point is already on the model.")

    def _done(self):
        self.on_done({n: p for n, p in self.pos.items() if p != self.original[n]})
        self.destroy()


def point_move_changes(sk, siblings: list, models: list, moves: dict) -> list:
    """[(skel chunk, old payload, new payload)] that put the named points at
    their new positions in the skeleton of every model in `models` (a
    soldier's LOD has its own skeleton; a vehicle's LOD shares the main one,
    so it's only changed once)."""
    changes, seen = [], set()
    for name in models:
        skel = sk.find_skeleton_chunk(siblings, name)
        if skel is None or id(skel) in seen:
            continue
        seen.add(id(skel))
        _, bones = sk.decode_skeleton_chunk(skel.payload)
        wanted = {n: p for n, p in moves.items() if any(b.name == n for b in bones)}
        if wanted:
            changes.append((skel, skel.payload, sk.encode_skeleton_transforms(skel.payload,
                                                                              sk.move_bones(bones, wanted))))
    return changes


class SkinnedImportDialog(tk.Toplevel):
    """Replaces a skinned model - a soldier or a vehicle - (and its
    low-detail LOD) with a .glb, via bf1_skinned_import. Everything is
    pre-filled: the vertex budget (the .glb is automatically simplified if
    it's bigger), size and placement (fitted into the original model's own
    extents), arm angle for characters, a texture atlas (the game gives a
    model one texture here, so every texture in the .glb gets packed into
    the one being replaced) and which textures are hair. Preview renders the
    result from the rebuilt bytes before anything is written; Import applies
    it as one undo step."""

    NO_TEXTURE = "(leave textures alone)"
    BUDGET_MAX = 60000  # the budget spinboxes' limit

    def __init__(self, app: "App", tab: "WorkspaceTab", model: core.Chunk, glb_path: str):
        import bf1_skinned_import as sk
        name = model.display_name()
        siblings = model.parent_body.chunks
        bones = sk.find_skeleton(siblings, name)
        if bones is None:
            raise sk.SkinnedImportError(f"No skeleton ('skel' chunk) for '{name}' was found next to it.")
        lod = sk.find_lod(siblings, name)
        # soldier LODs carry their own smaller skeleton; vehicle LODs ("...LOWD")
        # index straight into the main model's
        lod_bones = (sk.find_skeleton(siblings, lod.display_name()) or bones) if lod is not None else None
        glb = Path(glb_path).read_bytes()
        source = sk.load_glb_mesh(glb)
        usage = sk.texture_usage(source)
        images = sk.fill_missing_images(sk.load_glb_images(glb), usage)
        character = sk.is_character(bones)
        bounds = sk.template_bounds(model.payload, bones)
        axis = "Height" if character else "Largest side"
        fitted = sk.fit_to_bounds(source, *bounds, axis)
        arm = sk.estimate_arm_drop(fitted, bones, sk.bind_world_matrices(bones)) if character else 0.0
        hair = sk.guess_loose_groups(fitted) if character else set()
        coll, prim = sk.find_collision(siblings, name)
        fire_points = sk.weapon_points(bones, siblings, name)

        super().__init__(app)
        self.sk, self.app, self.tab, self.model = sk, app, tab, model
        self.bones, self.siblings, self.bounds, self.character = bones, siblings, bounds, character
        self.lod, self.lod_bones = lod, lod_bones
        self.coll, self.prim, self.fire_points = coll, prim, fire_points
        self.hard_points = sk.hard_points(bones, siblings, name)
        self.point_moves = {}  # bone name -> new world position, from FirePointEditor
        self.tested_vertices = sk.tested_vertices(bones)
        self.has_shadow = sk.model_has_shadow(model.payload) or (lod is not None and sk.model_has_shadow(lod.payload))
        self.glb_path = Path(glb_path)
        self.source, self.usage, self.images = source, usage, images
        # the whole .glb; self.source/self.usage are it minus deleted textures
        self.full_source, self.full_usage = source, usage
        self.deleted = set()
        self.textures = {ch.display_name(): ch for ch in siblings if ch.tag == "tex_"}
        self._atlas_cache = {}
        self._result = None
        self._photos = []

        self.title(f"Import {'Character' if character else 'Vehicle'} - {name}")
        self.transient(app)
        self._build_ui(name, axis, arm, hair)
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.after(50, self._preview)

    def _extent(self, axis: str) -> float:
        lo, hi = self.bounds
        k = self.sk.FIT_AXES[axis]
        if k is None:
            return max(hi[i] - lo[i] for i in range(3))
        return hi[k] - lo[k]

    # -- layout ------------------------------------------------------------

    def _build_ui(self, name: str, axis: str, arm: float, hair: set):
        sk = self.sk
        body = ttk.Frame(self, padding=px(12))
        body.pack(fill="both", expand=True)
        src_v, src_t = len(self.source.positions), len(self.source.tris) // 3
        users = sk.classes_using(self.siblings, name, sk.MODEL_PROPERTIES)
        ttk.Label(body, text=f"Replace '{name}' with {self.glb_path.name}",
                  font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(body, text=f"Source: {src_v:,} vertices, {src_t:,} triangles, {len(self.usage)} texture(s).  "
                             f"Classes using this model: {', '.join(users) or 'none found'}",
                  wraplength=px(1000)).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 8))

        form = ttk.Frame(body)
        form.grid(row=2, column=0, sticky="nw", padx=(0, 16))
        row = 0

        ttk.Label(form, text="Texture to replace:").grid(row=row, column=0, sticky="w")
        current = sk.find_texture(self.siblings, sk.model_texture(self.model.payload))
        self.texture_var = tk.StringVar(value=current.display_name() if current is not None else self.NO_TEXTURE)
        ttk.Combobox(form, textvariable=self.texture_var, state="readonly", width=30,
                     values=[self.NO_TEXTURE] + sorted(self.textures)).grid(row=row, column=1, sticky="w")
        row += 1
        self.texture_hint = ttk.Label(form, text="", wraplength=px(330), foreground="gray")
        self.texture_hint.grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.texture_var.trace_add("write", lambda *_: self._update_texture_hint())
        self._update_texture_hint()
        row += 1

        self.axis_var = tk.StringVar(value=axis)
        self.size_var = tk.StringVar(value=f"{self._extent(axis):.2f}")
        self.turn_vars = [tk.IntVar(value=0) for _ in range(3)]  # degrees about X, Y, Z - the 3D view's sliders
        self.arm_var = tk.StringVar(value=f"{arm:.0f}" if self.character else "0")
        self.budget_var = tk.StringVar(value=str(min(len(self.source.positions), self.BUDGET_MAX)))  # full quality by default

        ttk.Label(form, text="Fit size along:").grid(row=row, column=0, sticky="w", pady=2)
        axis_combo = ttk.Combobox(form, textvariable=self.axis_var, values=list(sk.FIT_AXES), state="readonly",
                                  width=18)
        axis_combo.grid(row=row, column=1, sticky="w", pady=2)
        axis_combo.bind("<<ComboboxSelected>>",
                        lambda e: self.size_var.set(f"{self._extent(self.axis_var.get()):.2f}"))
        row += 1
        for label, var, lo, hi, step, enabled in (
                ("Size (game units):", self.size_var, 0.05, 500.0, 0.05, True),
                ("Arm angle below T-pose (deg):", self.arm_var, -90, 90, 1, self.character),
                ("Vertex budget:", self.budget_var, 100, 60000, 100, True)):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Spinbox(form, textvariable=var, from_=lo, to=hi, increment=step, width=10,
                        state="normal" if enabled else "disabled").grid(row=row, column=1, sticky="w", pady=2)
            row += 1
        self.budget_hint = ttk.Label(form, text="", foreground="gray", wraplength=px(330))
        self.budget_hint.grid(row=row, column=0, columnspan=2, sticky="w")
        self.budget_var.trace_add("write", lambda *_: self._update_budget_hint())
        row += 1

        self.lod_var = tk.BooleanVar(value=self.lod is not None)
        self.lod_budget_var = tk.StringVar(
            value=str(min(len(self.source.positions), self.BUDGET_MAX)) if self.lod else "0")
        lod_text = f"Also replace LOD model '{self.lod.display_name()}'" if self.lod else "No LOD model found"
        ttk.Checkbutton(form, text=lod_text, variable=self.lod_var,
                        state="normal" if self.lod else "disabled").grid(row=row, column=0, columnspan=2,
                                                                         sticky="w", pady=(8, 0))
        row += 1
        ttk.Label(form, text="LOD vertex budget:").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Spinbox(form, textvariable=self.lod_budget_var, from_=50, to=60000, increment=100, width=10,
                    state="normal" if self.lod else "disabled").grid(row=row, column=1, sticky="w", pady=2)
        row += 1
        ttk.Label(form, text="Normal gameplay mostly shows the LOD model, so it's worth replacing too.",
                  foreground="gray", wraplength=px(330)).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        self._update_budget_hint()

        found = []
        if self.coll is not None:
            found.append("collision mesh")
        if self.prim is not None:
            shapes = sum(1 for s in core.parse_body(self.prim.payload).chunks if s.tag == "DATA")
            found.append(f"{shapes} collision shape{'s' if shapes != 1 else ''}")
        self.collision_var = tk.BooleanVar(value=bool(found))
        ttk.Checkbutton(form, text=f"Rebuild collision to fit the new model ({' + '.join(found)})" if found
                        else "No collision found for this model", variable=self.collision_var,
                        state="normal" if found else "disabled").grid(row=row, column=0, columnspan=2,
                                                                      sticky="w", pady=(8, 0))
        row += 1
        self.shadow_var = tk.BooleanVar(value=self.has_shadow)
        ttk.Checkbutton(form, text="Remove the old model's shadow (it has the old shape)" if self.has_shadow
                        else "Model has no shadow data", variable=self.shadow_var,
                        state="normal" if self.has_shadow else "disabled").grid(row=row, column=0, columnspan=2,
                                                                                sticky="w")

        tex_frame = ttk.Frame(body)
        tex_frame.grid(row=2, column=1, sticky="nsew")
        header = "Textures in the .glb  (Delete = leave out every part using it"
        header += "; tick hair so it follows the head/torso, not the arms)" if self.character else ")"
        ttk.Label(tex_frame, text=header, wraplength=px(360)).pack(anchor="w")
        colors = text_colors(self.app)
        outer = ttk.Frame(tex_frame)
        outer.pack(fill="both", expand=True, pady=(4, 0))
        canvas = tk.Canvas(outer, width=px(360), height=px(230), highlightthickness=0, bg=colors["bg"])
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        wheel = lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        from PIL import Image, ImageTk
        total = sum(self.usage.values()) or 1
        self.hair_vars = {}
        self.delete_vars = {}
        t = px(40)
        for r, g in enumerate(sorted(self.usage, key=lambda g: -self.usage[g])):
            thumb = Image.new("RGB", (t, t), (90, 90, 90))
            src = self.images[g].copy()
            src.thumbnail((t, t))
            thumb.paste(src.convert("RGB"), ((t - src.width) // 2, (t - src.height) // 2))
            photo = ImageTk.PhotoImage(thumb)
            self._photos.append(photo)
            ttk.Label(inner, image=photo).grid(row=r, column=0, padx=(0, 6), pady=2)
            label = "untextured" if g < 0 else f"texture {g}"
            ttk.Label(inner, text=f"{label}\n{self.usage[g]:,} verts ({100 * self.usage[g] / total:.0f}%)").grid(
                row=r, column=1, sticky="w")
            var = tk.BooleanVar(value=g in hair)
            self.hair_vars[g] = var
            if self.character:
                ttk.Checkbutton(inner, text="Hair", variable=var).grid(row=r, column=2, padx=(12, 0))
            delete = tk.BooleanVar(value=False)
            self.delete_vars[g] = delete
            ttk.Checkbutton(inner, text="Delete", variable=delete,
                            command=self._deletions_changed).grid(row=r, column=3, padx=(12, 0))

        self._build_orientation(body)

        self.preview_label = ttk.Label(body, text="")
        self.preview_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(12, 4))
        self.stats_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.stats_var, wraplength=px(1000), justify="left").grid(
            row=4, column=0, columnspan=3, sticky="w")

        bar = ttk.Frame(body)
        bar.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.status_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.status_var, foreground="gray").pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        self._action_buttons = [ttk.Button(bar, text="Import", command=self._import),
                                ttk.Button(bar, text="Preview", command=self._preview)]
        if self.hard_points:
            self._action_buttons.append(ttk.Button(bar, text="Fire points...", command=self._edit_points))
        for button in self._action_buttons:
            button.pack(side="right", padx=(0, 6))

    # -- live 3D orientation view --------------------------------------------------

    AXIS_COLORS = (("X", "#e05252"), ("Y", "#4fbf5f"), ("Z", "#4f86f0"))

    def _build_orientation(self, body):
        frame = ttk.Frame(body)
        frame.grid(row=2, column=2, sticky="nw", padx=(16, 0))
        ttk.Label(frame, text="Orientation  (drag the view to look around)").pack(anchor="w")
        self._orient_data = self.sk.orientation_triangles(self.source, self.images)
        self._camera = [35.0, 22.0]
        self._orient_photo = None
        self._orient_pending = False
        self.orient_label = ttk.Label(frame, cursor="fleur")
        self.orient_label.pack(pady=(4, 6))
        self.orient_label.bind("<ButtonPress-1>", lambda e: setattr(self, "_drag_at", (e.x, e.y)))
        self.orient_label.bind("<B1-Motion>", self._orbit)
        colors = text_colors(self.app)
        for (axis_name, color), var in zip(self.AXIS_COLORS, self.turn_vars):
            row = ttk.Frame(frame)
            row.pack(fill="x")
            tk.Label(row, text=axis_name, fg=color, bg=colors["bg"], font=("TkDefaultFont", 10, "bold"),
                     width=2).pack(side="left")
            tk.Scale(row, from_=-180, to=180, orient="horizontal", resolution=1, variable=var,
                     length=px(250), troughcolor=color, bg=colors["bg"], fg=colors["fg"],
                     activebackground=color, highlightthickness=0, sliderrelief="flat",
                     command=lambda _v: self._schedule_orientation()).pack(side="left", fill="x", expand=True)
        bottom = ttk.Frame(frame)
        bottom.pack(fill="x", pady=(4, 0))
        ttk.Button(bottom, text="Reset", command=self._reset_turns).pack(side="left")
        ttk.Label(frame, text="Red tips it forward/back (180 flips an upside-down model), green turns it to "
                              "face another way, blue rolls it. Point the model's nose along the yellow "
                              "arrow - that's forward in the game. The grey box is the original model.",
                  foreground="gray", wraplength=px(300)).pack(anchor="w", pady=(6, 0))
        for var in (self.size_var, self.axis_var):
            var.trace_add("write", lambda *_: self._schedule_orientation())
        self._schedule_orientation()

    def _reset_turns(self):
        for var in self.turn_vars:
            var.set(0)
        self._schedule_orientation()

    def _orbit(self, event):
        x0, y0 = getattr(self, "_drag_at", (event.x, event.y))
        self._drag_at = (event.x, event.y)
        self._camera[0] += (event.x - x0) * 0.6
        self._camera[1] = max(-85.0, min(85.0, self._camera[1] + (event.y - y0) * 0.6))
        self._schedule_orientation()

    def _schedule_orientation(self):
        """Coalesces slider/drag events into one redraw per idle moment."""
        if not self._orient_pending:
            self._orient_pending = True
            self.after(15, self._draw_orientation)

    def _draw_orientation(self):
        self._orient_pending = False
        if not self.winfo_exists():
            return
        try:
            size = float(self.size_var.get())
        except (ValueError, tk.TclError):
            size = None
        turns = tuple(float(v.get()) for v in self.turn_vars)
        points, tris = self._orient_data
        from PIL import ImageTk
        img = self.sk.render_orientation(points, tris, turns, *self.bounds, self.axis_var.get(), size,
                                         self._camera[0], self._camera[1], (px(300), px(270)))
        self._orient_photo = ImageTk.PhotoImage(img)
        self.orient_label.configure(image=self._orient_photo)

    def _update_texture_hint(self):
        tex = self.texture_var.get()
        if tex == self.NO_TEXTURE:
            self.texture_hint.configure(text="The model keeps its current texture, and the .glb's UVs are used "
                                             "as-is - only right if they were made for that texture.")
            return
        sk = self.sk
        name = self.model.display_name()
        lod_name = self.lod.display_name() if self.lod else None
        models = [m for m in sk.models_textured_with(self.siblings, tex) if m not in (name, lod_name)]
        classes = sk.classes_using(self.siblings, tex, sk.TEXTURE_PROPERTIES)
        text = f"All the .glb's textures get packed into '{tex}', and the model is pointed at it. "
        if models or classes:
            text += "It's also used by " + "; ".join(
                part for part in (f"models: {', '.join(models)}" if models else "",
                                  f"classes (OverrideTexture): {', '.join(classes)}" if classes else "") if part)
            text += " - those change too."
        else:
            text += "Nothing else uses it."
        self.texture_hint.configure(text=text)

    def _update_budget_hint(self):
        src = len(self.source.positions)
        try:
            budget = int(float(self.budget_var.get()))
        except ValueError:
            self.budget_hint.configure(text="")
            return
        kind = "soldier" if self.character else "vehicle"
        if src > budget:
            text = f"The .glb has {src:,} vertices - it will be simplified automatically to about {budget:,}."
        else:
            text = f"The .glb has {src:,} vertices - within budget, no simplification needed."
        text += " Parts too big for the game are split into extra segments automatically."
        if min(src, budget) > self.tested_vertices:
            text += (f" Note: the biggest {kind} tested in-game so far is {self.tested_vertices:,} vertices - "
                     f"check this one in-game.")
        self.budget_hint.configure(text=text)

    def _deletions_changed(self):
        """Rebuilds the source mesh without the textures ticked Delete - the
        atlas then only packs the textures that are left, the fit measures
        what's left, and the default budgets follow the new size."""
        deleted = {g for g, v in self.delete_vars.items() if v.get()}
        if len(deleted) == len(self.full_usage):
            messagebox.showinfo("Delete texture", "That would delete the whole model - at least one texture has "
                                                  "to stay.", parent=self)
            for g, v in self.delete_vars.items():
                v.set(g in self.deleted)
            return
        old_count = len(self.source.positions)
        self.deleted = deleted
        self.source = self.sk.drop_groups(self.full_source, deleted)
        self.usage = self.sk.texture_usage(self.source)
        self._atlas_cache = {}
        new_count = min(len(self.source.positions), self.BUDGET_MAX)
        for var in (self.budget_var, self.lod_budget_var):  # untouched defaults follow the new size
            if var.get() == str(min(old_count, self.BUDGET_MAX)):
                var.set(str(new_count))
        self._update_budget_hint()
        self._orient_data = self.sk.orientation_triangles(self.source, self.images)
        self._schedule_orientation()
        n = len(deleted)
        self.status_var.set(f"{n} texture{'s' if n != 1 else ''} deleted - {len(self.source.positions):,} vertices "
                            f"left. Press Preview to see it." if n else "")

    # -- building ------------------------------------------------------------

    def _params(self) -> dict:
        try:
            params = {
                "texture": self.texture_var.get(),
                "axis": self.axis_var.get(),
                "size": float(self.size_var.get()),
                "rotate": tuple(float(v.get()) for v in self.turn_vars),
                "arm": float(self.arm_var.get()) if self.character else 0.0,
                "budget": int(float(self.budget_var.get())),
                "lod": bool(self.lod_var.get()) and self.lod is not None,
                "lod_budget": int(float(self.lod_budget_var.get())),
                "hair": tuple(sorted(g for g, v in self.hair_vars.items() if v.get())) if self.character else (),
                "deleted": tuple(sorted(self.deleted)),
                "collision": bool(self.collision_var.get()),
                "strip_shadow": bool(self.shadow_var.get()),
            }
        except ValueError:
            raise ValueError("Size, turn angles, arm angle and vertex budgets need to be numbers.")
        if params["size"] <= 0:
            raise ValueError("Size has to be greater than zero.")
        return params

    def _atlas_size(self, tex_name: str) -> int:
        info = core.decode_texture_chunk(self.textures[tex_name].payload)
        if info["formats"]:
            f = info["formats"][0]
            if f["width"] == f["height"] and f["width"] >= 64:
                return f["width"]
        return 512

    def _textured_source(self, tex: str | None) -> tuple:
        """(mesh, atlas bytes or None, atlas size) - the .glb with its UVs
        remapped into the atlas for `tex`, cached per atlas size."""
        if tex is None:
            return self.source, None, 0
        size = self._atlas_size(tex)
        if size not in self._atlas_cache:
            layout = self.sk.auto_atlas_layout(self.usage, self.images, size)
            self._atlas_cache[size] = self.sk.build_atlas(self.source, self.images, layout, size)
        mesh, atlas = self._atlas_cache[size]
        return mesh, atlas, size

    def _build(self, params: dict) -> dict:
        if self._result is not None and self._result["params"] == params:
            return self._result
        sk = self.sk
        tex = None if params["texture"] == self.NO_TEXTURE else params["texture"]
        mesh, atlas, size = self._textured_source(tex)
        mesh = sk.fit_to_bounds(mesh, *self.bounds, params["axis"], params["size"], params["rotate"])
        limits = {g: sk.TORSO_BONES for g in params["hair"]}
        main, stats = sk.build_character_model(self.model.payload, self.bones, mesh, params["budget"],
                                               params["arm"], limits, tex)
        lod, lod_stats = None, None
        if params["lod"]:
            lod, lod_stats = sk.build_character_model(self.lod.payload, self.lod_bones, mesh, params["lod_budget"],
                                                      params["arm"], limits, tex)
        coll = prim = None
        coll_stats, prim_count = None, 0
        if params["collision"]:
            if self.coll is not None:
                coll, coll_stats = sk.rebuild_collision_mesh(
                    self.coll.payload, self.bones, sk.model_triangles(main, self.bones),
                    sk.auto_collision_budget(self.coll.payload))
            if self.prim is not None:
                prim, prim_count = sk.refit_primitives(self.prim.payload, self.bones, self.bounds,
                                                       sk.model_bounds(main, self.bones))
        if params["strip_shadow"]:
            main = sk.strip_shadows(main)
            if lod is not None:
                lod = sk.strip_shadows(lod)
        self._result = {"params": params, "texture": tex, "atlas": atlas, "size": size,
                        "main": main, "stats": stats, "lod_payload": lod, "lod_stats": lod_stats,
                        "coll": coll, "coll_stats": coll_stats, "prim": prim, "prim_count": prim_count}
        return self._result

    def _current_fire_points(self) -> list:
        """The fire points with any moves from the Fire points window applied."""
        return [(name, self.point_moves.get(name, pos)) for name, pos in self.fire_points]

    def _run_in_background(self, busy_text: str, work, done, failure_title: str):
        """Runs `work()` (pure data crunching - it must not touch Tk) on a
        worker thread so the window keeps responding (a few seconds of
        simplifying/skinning/texture encoding on the UI thread gets the window
        flagged "Not Responding"), then calls `done(result)` back on the UI
        thread."""
        import threading
        self.status_var.set(busy_text)
        self.configure(cursor="watch")
        for button in self._action_buttons:
            button.configure(state="disabled")
        box = {}

        def run():
            try:
                box["result"] = work()
            except Exception as exc:  # reported on the UI thread below
                box["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()

        def poll():
            if not self.winfo_exists():
                return
            if worker.is_alive():
                self.after(60, poll)
                return
            self.configure(cursor="")
            for button in self._action_buttons:
                button.configure(state="normal")
            if "error" in box:
                self.status_var.set("")
                messagebox.showerror(failure_title, str(box["error"]), parent=self)
                return
            done(box["result"])

        self.after(60, poll)

    def _preview(self):
        if not self.winfo_exists():
            return
        try:
            params = self._params()
        except ValueError as exc:
            messagebox.showerror("Preview failed", str(exc), parent=self)
            return
        panel = (px(170), px(240))

        def work():
            result = self._build(params)
            from PIL import Image
            atlas = (Image.frombytes("RGBA", (result["size"], result["size"]), result["atlas"])
                     if result["atlas"] is not None else None)
            points = self._current_fire_points()
            fire = self.sk.weapon_point_report(points, result["main"], self.bones)
            markers = [(pos, name, (255, 70, 70) if off else (80, 220, 110))
                       for (name, pos), (_, _, off) in zip(points, fire)]
            return result, fire, self.sk.render_preview(result["main"], self.bones, atlas, panel, markers)

        self._run_in_background("Building preview...", work, self._show_preview, "Preview failed")

    def _show_preview(self, outcome):
        from PIL import ImageTk
        result, fire, img = outcome
        photo = ImageTk.PhotoImage(img)
        self._photos.append(photo)
        self.preview_label.configure(image=photo)
        s = result["stats"]
        lines = [f"Model: {s['vertices']:,} vertices, {s['tris']:,} triangles "
                 f"({len(self.model.payload):,} -> {s['bytes']:,} bytes)"
                 + (f", arm angle {s['arm_drop']:.0f} deg." if self.character else ".")]
        if result["lod_stats"]:
            ls = result["lod_stats"]
            lines[0] += (f"   LOD: {ls['vertices']:,} vertices, {ls['tris']:,} triangles "
                         f"({len(self.lod.payload):,} -> {ls['bytes']:,} bytes).")
        model_segments = {"model": self.sk.model_segment_count(self.model.payload),
                          "LOD": self.sk.model_segment_count(self.lod.payload) if self.lod is not None else 0}
        split = [f"{name} into {st['segments']}" for name, st in (("model", s), ("LOD", result["lod_stats"]))
                 if st and st["segments"] > model_segments[name]]
        if split:
            lines.append("Split into more segments to stay under the game's per-segment limit: "
                         + ", ".join(split) + " segments.")
        parts = []
        if result["coll_stats"]:
            parts.append(f"collision mesh rebuilt ({result['coll_stats']['triangles']} triangles)")
        if result["prim_count"]:
            parts.append(f"{result['prim_count']} collision shapes refitted")
        if result["params"]["strip_shadow"] and self.has_shadow:
            parts.append("old shadow removed")
        if parts:
            lines.append("Also: " + ", ".join(parts) + ".")
        if fire:
            off = [name for name, _, is_off in fire if is_off]
            moved = f" ({len(self.point_moves)} moved)" if self.point_moves else ""
            if off:
                lines.append(f"Fire points off the new model (red in the preview - shots will come from there): "
                             f"{', '.join(off)}. Use 'Fire points...' to move them{moved}.")
            else:
                lines.append(f"All {len(fire)} fire points sit on the new model (green){moved}.")
        self.stats_var.set("\n".join(lines))
        self.status_var.set("Preview shows the rebuilt model decoded from its new bytes.")

    # -- fire points ----------------------------------------------------------

    def _edit_points(self):
        """Builds the model with the current settings (cached if unchanged),
        then opens the Fire points window on it."""
        try:
            params = self._params()
        except ValueError as exc:
            messagebox.showerror("Fire points", str(exc), parent=self)
            return

        def work():
            result = self._build(params)
            from PIL import Image
            atlas = (Image.frombytes("RGBA", (result["size"], result["size"]), result["atlas"])
                     if result["atlas"] is not None else None)
            return result, atlas

        def done(outcome):
            result, atlas = outcome
            self.status_var.set("")
            FirePointEditor(self, result["main"], self.bones, atlas, self.hard_points, self.point_moves,
                            self._points_changed, self.character)

        self._run_in_background("Building model...", work, done, "Fire points")

    def _points_changed(self, moves: dict):
        self.point_moves = moves
        self._preview()

    def _import(self):
        try:
            params = self._params()
        except ValueError as exc:
            messagebox.showerror("Import failed", str(exc), parent=self)
            return

        def work():
            result = self._build(params)
            changes = [(self.model, self.model.payload, result["main"])]
            if result["lod_payload"] is not None:
                changes.append((self.lod, self.lod.payload, result["lod_payload"]))
            if result["coll"] is not None:
                changes.append((self.coll, self.coll.payload, result["coll"]))
            if result["prim"] is not None:
                changes.append((self.prim, self.prim.payload, result["prim"]))
            if self.point_moves:
                names = [self.model.display_name()] + ([self.lod.display_name()] if self.lod is not None else [])
                changes += point_move_changes(self.sk, self.siblings, names, self.point_moves)
            if result["texture"] is not None:
                tex_chunk = self.textures[result["texture"]]
                size = result["size"]
                changes.append((tex_chunk, tex_chunk.payload,
                                core.encode_texture_chunk(result["texture"], size, size, result["atlas"])))
            return changes

        self._run_in_background("Building model and encoding texture...", work, self._apply, "Import failed")

    def _apply(self, changes):
        for chunk, _old, new in changes:
            chunk.set_payload(new)

        def undo():
            for ch, old, _new in changes:
                ch.set_payload(old)

        def redo():
            for ch, _old, new in changes:
                ch.set_payload(new)

        name = self.model.display_name()
        self.app.record_undo(f"Import model into '{name}'", undo_fn=undo, redo_fn=redo)
        self.app.set_dirty()
        self.tab.refresh_tree()
        self.tab._on_select()
        replaced = ", ".join(ch.display_name() for ch, _, _ in changes)
        self.app.log(f"Imported {self.glb_path.name} -> {replaced}")
        self.destroy()


class MediaDialog(tk.Toplevel):
    """Browses a movie file (.mvs - Bink videos) or sound bank (.bnk - PCM
    samples) via bf1_media: play/open an entry, extract some or all of them
    (.bik / .wav). Names the game only stores as hashes show as the hash."""

    def __init__(self, app: "App", path: str):
        import bf1_media
        self.media = bf1_media.read_media(path)  # raises MediaError on a bad file - caller reports it
        super().__init__(app)
        self.app, self.bf1_media = app, bf1_media
        self._temp_dir = None
        movies = self.media.kind == "movies"
        self.title(f"{Path(path).name} - {len(self.media.entries)} {'movies' if movies else 'sounds'}")
        self.geometry(f"{px(620)}x{px(520)}")
        self.transient(app)

        body = ttk.Frame(self, padding=px(10))
        body.pack(fill="both", expand=True)
        hint = ("Bink videos (.bik) - they play in VLC or RAD Video Tools." if movies else
                "16-bit sound samples, extracted as .wav.")
        ttk.Label(body, text=f"{hint} The game stores most names only as a hash, so those show as the hash.",
                  wraplength=px(590), foreground="gray").pack(anchor="w", pady=(0, 6))
        columns = ("name", "length", "detail")
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        for col, text, width in (("name", "Name", 260), ("length", "Length", 110),
                                 ("detail", "Size" if movies else "Sample rate", 140)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=px(width), anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for i, e in enumerate(self.media.entries):
            if movies:
                row = (e.name, f"{e.length / 1e6:.1f} MB", "Bink video")
            else:
                row = (e.name + ("  (alias)" if e.alias_of is not None else ""), f"{e.seconds:.2f} s",
                       f"{e.frequency:,} Hz")
            self.tree.insert("", "end", iid=str(i), values=row)
        self.tree.bind("<Double-1>", lambda e: self._play())

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(8, 0))
        self.status = tk.StringVar(value="Double-click to " + ("open a movie." if movies else "play a sound."))
        ttk.Label(bar, textvariable=self.status, foreground="gray").pack(side="left")
        self.progress = ttk.Progressbar(bar, length=px(120), mode="determinate")
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(8, 0))
        self._buttons = [ttk.Button(buttons, text="Open movie" if movies else "Play", command=self._play),
                         ttk.Button(buttons, text="Extract selected...", command=lambda: self._extract(False)),
                         ttk.Button(buttons, text="Extract all...", command=lambda: self._extract(True))]
        for b in self._buttons:
            b.pack(side="left", padx=(0, 6))
        if not movies:
            ttk.Button(buttons, text="Stop", command=self._stop).pack(side="left")
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Destroy>", lambda e: self._stop() if e.widget is self else None)

    def _selected(self) -> list:
        return [self.media.entries[int(i)] for i in self.tree.selection()]

    def _temp(self) -> Path:
        if self._temp_dir is None:
            import tempfile
            self._temp_dir = Path(tempfile.mkdtemp(prefix="bf1_media_"))
        return self._temp_dir

    def _play(self):
        chosen = self._selected()
        if not chosen:
            return
        entry = chosen[0]
        target = self._temp() / (entry.name + self.media.extension)
        target.write_bytes(self.bf1_media.entry_bytes(self.media, entry))
        if self.media.kind == "sounds":
            try:
                import winsound
                winsound.PlaySound(str(target), winsound.SND_FILENAME | winsound.SND_ASYNC)
                self.status.set(f"Playing {entry.name} ({entry.seconds:.1f} s)")
            except (ImportError, RuntimeError) as exc:
                messagebox.showerror("Can't play", str(exc), parent=self)
            return
        try:
            import os
            os.startfile(target)  # Windows: the app associated with .bik
            self.status.set(f"Opened {entry.name}.bik")
        except (AttributeError, OSError):
            messagebox.showinfo("No video player for .bik",
                                "Nothing on this PC is set up to play Bink (.bik) videos. VLC or RAD Video Tools "
                                "can play them - or use 'Extract selected...' and open the file there.", parent=self)

    def _stop(self):
        try:
            import winsound
            winsound.PlaySound(None, 0)
        except ImportError:
            pass

    def _extract(self, everything: bool):
        chosen = list(self.media.entries) if everything else self._selected()
        if not chosen:
            messagebox.showinfo("Nothing selected", "Select one or more entries first, or use 'Extract all...'.",
                                parent=self)
            return
        folder = filedialog.askdirectory(parent=self, title=f"Extract {len(chosen)} file(s) to folder")
        if not folder:
            return
        import threading
        box = {"done": 0}
        self.progress.pack(side="right")
        self.progress.configure(maximum=len(chosen), value=0)
        for b in self._buttons:
            b.configure(state="disabled")

        def work():
            try:
                box["files"] = self.bf1_media.extract(self.media, folder, chosen,
                                                      lambda done, total: box.__setitem__("done", done))
            except Exception as exc:
                box["error"] = exc

        worker = threading.Thread(target=work, daemon=True)
        worker.start()

        def poll():
            if not self.winfo_exists():
                return
            self.progress.configure(value=box["done"])
            self.status.set(f"Extracting... {box['done']} of {len(chosen)}")
            if worker.is_alive():
                self.after(100, poll)
                return
            for b in self._buttons:
                b.configure(state="normal")
            self.progress.pack_forget()
            if "error" in box:
                self.status.set("")
                messagebox.showerror("Extract failed", str(box["error"]), parent=self)
                return
            self.status.set(f"Extracted {len(box['files'])} file(s) to {folder}")
            self.app.log(f"Extracted {len(box['files'])} file(s) from {self.media.path.name} to {folder}")

        self.after(100, poll)


# --------------------------------------------------------------------------
# Application shell
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self, initial_path: str | None = None):
        global UI_SCALE
        enable_dpi_awareness()
        super().__init__()
        UI_SCALE = self.winfo_fpixels("1i") / 96.0
        self.settings = load_settings()
        self._bf1_theme = self.settings.get("theme", "light")
        self.title(APP_NAME)
        try:
            self.iconbitmap(default=str(core.resource_path("icon.ico")))
        except tk.TclError:
            pass  # no icon file - Tk's default feather
        self.geometry(f"{min(px(1100), int(self.winfo_screenwidth() * 0.9))}x"
                      f"{min(px(720), int(self.winfo_screenheight() * 0.85))}")
        apply_theme(self, self._bf1_theme)

        self._dirty = False
        self._top_container: core.Container | None = None
        self._top_path: Path | None = None
        # Shared across all tabs, so a chunk can be copied in one tab
        # (e.g. the top-level file) and pasted into another (e.g. a nested
        # 'lvl_' tab), and vice versa.
        self.chunk_clipboard: core.Chunk | None = None
        self.undo_stack = UndoStack(self)

        self._build_menu()

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)

        log_frame = ttk.Frame(self)
        log_frame.pack(fill="x", side="bottom")
        ttk.Label(log_frame, text="Log:").pack(anchor="w", padx=4)
        colors = text_colors(self)
        self.log_text = tk.Text(log_frame, height=6, state="disabled", font=("Courier New", 9),
                                bg=colors["text_bg"], fg=colors["fg"],
                                selectbackground=colors["select_bg"], selectforeground=colors["select_fg"])
        self.log_text.pack(fill="x", padx=4, pady=(0, 4))

        self.status = tk.StringVar(value="No file open.")
        ttk.Label(self, textvariable=self.status, anchor="w", relief="sunken").pack(fill="x", side="bottom")

        self.log(f"{APP_NAME} ready. File > Open to load a .lvl file.")
        if initial_path:
            self.open_file(initial_path)

    # -- menu -----------------------------------------------------------

    def _build_menu(self):
        colors = text_colors(self)
        menu_kwargs = dict(bg=colors["field_bg"], fg=colors["fg"], activebackground=colors["select_bg"],
                           activeforeground=colors["select_fg"])
        menubar = tk.Menu(self, **menu_kwargs)

        file_menu = tk.Menu(menubar, tearoff=0, **menu_kwargs)
        file_menu.add_command(label="Open .lvl...", command=self.open_file_dialog)
        file_menu.add_command(label="Open Movies / Sounds (.mvs, .bnk)...", command=self.open_media_dialog)
        self.recent_menu = tk.Menu(file_menu, tearoff=0, **menu_kwargs)
        file_menu.add_cascade(label="Open Recent", menu=self.recent_menu)
        file_menu.add_command(label="Save", command=self.save, accelerator="Ctrl+S")
        file_menu.add_command(label="Save As...", command=self.save_as)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.destroy)
        menubar.add_cascade(label="File", menu=file_menu)
        self._refresh_recent_menu()

        edit_menu = tk.Menu(menubar, tearoff=0, **menu_kwargs)
        edit_menu.add_command(label="Undo", command=lambda: self.undo_stack.undo(), accelerator="Ctrl+Z")
        edit_menu.add_command(label="Redo", command=lambda: self.undo_stack.redo(), accelerator="Ctrl+Y")
        edit_menu.add_separator()
        edit_menu.add_command(label="Find Chunks...", command=lambda: self._dispatch_to_tab("show_find"), accelerator="Ctrl+F")
        edit_menu.add_command(label="Find in Files...", command=self.open_find_in_files, accelerator="Ctrl+Shift+F")
        edit_menu.add_separator()
        edit_menu.add_command(label="Add New Chunk...", command=self.add_new_chunk)
        edit_menu.add_separator()
        edit_menu.add_command(label="Copy Chunk", command=lambda: self._dispatch_to_tab("_copy_selected"), accelerator="Ctrl+C")
        edit_menu.add_command(label="Paste Chunk After Selected", command=lambda: self._dispatch_to_tab("_paste_selected"), accelerator="Ctrl+V")
        edit_menu.add_command(label="Move Chunk to Index...", command=lambda: self._dispatch_to_tab("_move_to_index"))
        menubar.add_cascade(label="Edit", menu=edit_menu)

        view_menu = tk.Menu(menubar, tearoff=0, **menu_kwargs)
        view_menu.add_command(label="Settings...", command=self.open_settings)
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0, **menu_kwargs)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

    def _refresh_recent_menu(self):
        self.recent_menu.delete(0, "end")
        recents = self.settings.get("recent_files", [])
        if not recents:
            self.recent_menu.add_command(label="(No recent files)", state="disabled")
            return
        for path_str in recents:
            self.recent_menu.add_command(label=path_str, command=lambda p=path_str: self.open_file(p))
        self.recent_menu.add_separator()
        self.recent_menu.add_command(label="Clear Recent Files", command=self._clear_recent_files)

    def _remember_recent_file(self, path: Path) -> None:
        path_str = str(path.resolve())
        recents = [p for p in self.settings.get("recent_files", []) if p != path_str]
        recents.insert(0, path_str)
        self.settings["recent_files"] = recents[:MAX_RECENT_FILES]
        save_settings(self.settings)
        self._refresh_recent_menu()

    def _clear_recent_files(self):
        self.settings["recent_files"] = []
        save_settings(self.settings)
        self._refresh_recent_menu()

    def open_settings(self):
        SettingsDialog(self)

    def apply_current_theme(self) -> None:
        """Re-skins the whole running app after a Settings change: ttk
        widgets via apply_theme()'s shared Style (automatic, no per-widget
        work), plus the handful of plain tk widgets that aren't ttk and
        need their colors set directly - the log, and every open tab's
        detail panel (cleared and rebuilt, which is simplest way to get a
        freshly-colored Text/Menu widget into ones already on screen)."""
        apply_theme(self, self._bf1_theme)
        colors = text_colors(self)
        self.log_text.configure(bg=colors["text_bg"], fg=colors["fg"],
                                selectbackground=colors["select_bg"], selectforeground=colors["select_fg"])
        self._build_menu()
        for tab in self.all_tabs():
            selected = tab._selected_chunk()
            tab._clear_detail()
            if selected is not None:
                tab._on_select()
        self.bind_all("<Control-s>", lambda e: self.save())
        self.bind_all("<Control-f>", lambda e: self._find_shortcut())
        self.bind_all("<Control-F>", lambda e: self.open_find_in_files())
        self.bind_all("<Control-Shift-F>", lambda e: self.open_find_in_files())
        self.bind_all("<Control-z>", lambda e: self.undo_stack.undo())
        self.bind_all("<Control-y>", lambda e: self.undo_stack.redo())
        self.bind_all("<Control-Shift-Z>", lambda e: self.undo_stack.redo())

    def _show_about(self):
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME}\n\n"
            "A browser and editor for Star Wars Battlefront (2004) .lvl files.\n\n"
            "- Browse and edit any chunk, including nested levels, with full\n"
            "  undo/redo and byte-identical rebuilds for anything untouched\n"
            "- Structured property editor for weapons, ordnance, explosions\n"
            "  and vehicles, with a full .odf text round trip\n"
            "- Texture preview with PNG/DDS import and export\n"
            "- Script decompilation to readable Lua\n"
            "- 3D model export/import via glTF, including skinned\n"
            "  characters with automatic texture atlas and simplification\n"
            "- Fire point / hardpoint mover for characters and vehicles\n"
            "- Movie (.mvs) and sound bank (.bnk) extraction to .bik / .wav\n"
            "- Cross-file search and a reference finder"
        )

    # -- logging / status -------------------------------------------------

    def log(self, message: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_dirty(self, dirty: bool = True):
        self._dirty = dirty
        self._update_status()

    def _update_status(self):
        name = str(self._top_path) if self._top_path else "(unsaved)"
        mark = " *" if self._dirty else ""
        self.status.set(f"{name}{mark}")

    # -- file operations ----------------------------------------------------

    def open_file_dialog(self):
        path = filedialog.askopenfilename(
            title="Open BF1 .lvl or chunk file",
            filetypes=[("BF1 files", "*.lvl *.ord *.wpn *.exp *.ordc *.wpnc *.expc"), ("All files", "*.*")],
        )
        if path:
            self.open_file(path)

    def open_media_dialog(self):
        path = filedialog.askopenfilename(
            title="Open a movie file or sound bank",
            filetypes=[("Movies and sound banks", "*.mvs *.bnk"), ("All files", "*.*")],
        )
        if path:
            self.open_media(path)

    def open_media(self, path: str):
        import bf1_media
        try:
            MediaDialog(self, path)
        except (bf1_media.MediaError, OSError, struct.error, StopIteration, KeyError) as exc:
            messagebox.showerror("Can't open", str(exc) or f"{Path(path).name}: unexpected layout")

    def open_file(self, path: str):
        if Path(path).suffix.lower() in (".mvs", ".bnk"):
            self.open_media(path)
            return
        try:
            data = Path(path).read_bytes()
            container = core.parse_container(data)
        except Exception as exc:
            messagebox.showerror("Failed to open", str(exc))
            return
        self._top_container = container
        self._top_path = Path(path)
        self.notebook.forget(*self.notebook.tabs()) if False else None
        for tab_id in self.notebook.tabs():
            self.notebook.forget(tab_id)
        self.undo_stack = UndoStack(self)  # old entries would reference chunks from the closed file
        self.open_container_tab(container, Path(path).name, file_path=self._top_path)
        self.set_dirty(False)
        self._remember_recent_file(self._top_path)
        self.log(f"Opened {path} ({len(data)} bytes, {len(container.body.chunks)} top-level chunks, "
                 f"magic={'ucfb' if container.has_magic else 'none'}).")

    def open_container_tab(self, container: core.Container, title: str, file_path: Path | None):
        tab = WorkspaceTab(self.notebook, self, container, title, file_path)
        self.notebook.add(tab, text=title)
        self.notebook.select(tab)
        return tab

    def open_find_in_files(self):
        default_folder = self._top_path.parent if self._top_path else Path.cwd()
        FindInFilesDialog(self, default_folder)

    def find_references(self, name: str) -> None:
        """Opens Find in Files pre-filled with a Data search for `name` and
        runs it immediately - what 'Find References...' uses. Note this
        (like Find in Files generally) reads files from disk, so it won't
        see not-yet-saved edits in other open tabs; and the source chunk
        itself will often show up in its own results (an ordc/wpnc/expc/
        entc chunk's own TYPE field literally contains its own name) -
        that's expected, not a bug, and easy to spot in the list."""
        default_folder = self._top_path.parent if self._top_path else Path.cwd()
        dlg = FindInFilesDialog(self, default_folder)
        dlg.title(f"References to '{name}'")
        dlg.data_var.set(name)
        dlg._start_search()

    def navigate_to_result(self, file_path: Path, idx_path: tuple, final_index: int) -> None:
        """Opens (or focuses) `file_path`, drills into whichever nested
        'lvl_' tabs `idx_path` names, and selects the chunk at
        `final_index` in the deepest one - what FindInFilesDialog's
        double-click uses to jump to a result."""
        file_path = Path(file_path).resolve()
        tab = next((t for t in self.all_tabs()
                   if t.file_path is not None and t.file_path.resolve() == file_path), None)
        if tab is None:
            self.open_file(str(file_path))
            tab = self._current_tab()
            if tab is None:
                return  # open_file already reported the error

        for idx in idx_path:
            if idx >= len(tab.container.body.chunks):
                messagebox.showerror("Navigation failed",
                                     "This file has changed since the search ran - "
                                     "the chunk isn't where it was found.")
                return
            chunk = tab.container.body.chunks[idx]
            nested = chunk.get_nested_container()
            if nested is None:
                messagebox.showerror("Navigation failed",
                                     "Expected a nested level chunk here, but the file has "
                                     "changed since the search ran.")
                return
            existing = next((t for t in self.all_tabs() if t.container is nested), None)
            tab = existing or self.open_container_tab(
                nested, f"{tab.title} > {chunk.display_name() or 'lvl_'}", file_path=None)

        if final_index >= len(tab.container.body.chunks):
            messagebox.showerror("Navigation failed",
                                 "This file has changed since the search ran - "
                                 "the chunk isn't where it was found.")
            return
        iid = f"c{final_index}"
        if not tab.tree.exists(iid):
            tab.hide_find()  # an active filter in this tab could be hiding the row
        self.notebook.select(tab)
        tab.tree.selection_set(iid)
        tab.tree.focus(iid)
        tab.tree.see(iid)
        tab._on_select()

    def save(self):
        if self._top_container is None:
            return
        if self._top_path is None:
            self.save_as()
            return
        self._write(self._top_path)

    def save_as(self):
        if self._top_container is None:
            return
        path = filedialog.asksaveasfilename(
            title="Save .lvl as",
            defaultextension=".lvl",
            initialfile=self._top_path.name if self._top_path else "output.lvl",
            filetypes=[("BF1 level", "*.lvl"), ("All files", "*.*")],
        )
        if not path:
            return
        self._top_path = Path(path)
        self._write(self._top_path)

    def _write(self, path: Path):
        try:
            backup = self._backup_if_needed(path)
            data = self._top_container.raw_bytes()
            path.write_bytes(data)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.set_dirty(False)
        if backup:
            self.log(f"Backed up pre-session original -> {backup}")
        self.log(f"Saved {len(data)} bytes -> {path}")

    def _backup_if_needed(self, path: Path) -> Path | None:
        """Copies whatever's currently on disk at `path` to `path.bak`
        before the first save to that path overwrites it - but only the
        first: a .bak that already exists (from this session or an earlier
        one) is left alone, since the whole point is to keep the oldest
        known-good copy, not whatever the previous save happened to write."""
        if not self.settings.get("auto_backup", True) or not path.exists():
            return None
        backup_path = path.with_name(path.name + ".bak")
        if backup_path.exists():
            return None
        shutil.copy2(path, backup_path)
        return backup_path

    def add_new_chunk(self):
        tab = self._current_tab()
        if tab is None:
            messagebox.showinfo("No workspace open", "Open a .lvl file first.")
            return
        dlg = NewChunkDialog(self)
        if dlg.result is not None:
            tab.add_chunk(dlg.result)

    def _current_tab(self) -> WorkspaceTab | None:
        current = self.notebook.select()
        if not current:
            return None
        widget = self.nametowidget(current)
        return widget if isinstance(widget, WorkspaceTab) else None

    def all_tabs(self) -> list[WorkspaceTab]:
        widgets = (self.nametowidget(tab_id) for tab_id in self.notebook.tabs())
        return [w for w in widgets if isinstance(w, WorkspaceTab)]

    def record_undo(self, label: str, undo_fn, redo_fn) -> None:
        """Convenience wrapper so call sites don't need `self.app.undo_stack.push(...)`."""
        self.undo_stack.push(label, undo_fn, redo_fn)

    def after_undo_redo(self, message: str) -> None:
        """Called by UndoStack after every undo()/redo(): a chunk that
        changed could be visible in more than one open tab (a top-level tab
        and a nested 'lvl_' tab sharing the same underlying Chunk objects),
        so every tab's list is refreshed, and detail panels are cleared
        since whatever they were showing/editing may no longer be current."""
        for tab in self.all_tabs():
            tab.refresh_tree()
            tab._clear_detail()
        self.set_dirty(True)
        self.log(message)

    def _find_shortcut(self):
        tab = self._current_tab()
        if tab is not None:
            tab.show_find()
        return "break"

    def _dispatch_to_tab(self, method_name: str):
        """Runs a WorkspaceTab method (copy/paste/move) on whichever tab is
        currently active, for the Edit-menu versions of those actions."""
        tab = self._current_tab()
        if tab is None:
            messagebox.showinfo("No workspace open", "Open a .lvl file first.")
            return
        getattr(tab, method_name)()


def run_selftest() -> int:
    import subprocess
    result = subprocess.run([sys.executable, str(Path(__file__).parent / "test_core.py")])
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} - browse and edit .lvl files")
    parser.add_argument("file", nargs="?", help="Optional .lvl (or chunk) file to open on startup")
    parser.add_argument("--selftest", action="store_true", help="Run bf1_core regression tests and exit")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()

    app = App(initial_path=args.file)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
