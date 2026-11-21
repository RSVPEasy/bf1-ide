"""
bf1_recipes.py - recipes that can describe a whole mod, not just class rules.

A recipe's "files" section lists, for each file a mod changes, how to make it from the game's original:

  "files": {
    "SIDE/rep.lvl": {
      "base": "Data/_LVL_PC/SIDE/rep.lvl",       where the original is, under the Original files folder
      "base_sha1": "...", "sha1": "...",          the original it was made from, and what building it gives
      "edits": [
        {"op": "class",   "at": ["lvl_:rep"], "chunk": "entc:rep_inf_clone_trooper", "set": {"MaxHealth": "400"}},
        {"op": "replace", "at": ["lvl_:rep"], "chunk": "modl:rep_inf_pilot", "data": "my_mod.assets/1a2b....modl"},
        {"op": "add",     "at": ["lvl_:rep"], "chunk": "tex_:rep_inf_pilot_glb", "after": "tex_:rep_inf_pilot",
                          "data": "my_mod.assets/3c4d....tex_"},
        {"op": "remove",  "at": ["lvl_:rep"], "chunk": "tex_:old_texture"}
      ]
    },
    "SIDE/brand_new.lvl": {"copy": "my_mod.assets/5e6f....lvl", "sha1": "..."}
  }

A chunk is named "tag:name", with "#2", "#3"... for the second, third... chunk of that tag and name in the same
level; "at" is the chain of nested levels (lvl_ chunks) it sits in, empty for the top of the file. Class edits:
"set" changes every copy of a property, "add" appends [name, value] pairs, "remove" deletes properties by name,
and "props" (with "base" and "type") replaces the whole list. "header" puts back a nested level's first 8 bytes
(some tools leave its size field stale). Binary data sits in files beside the recipe.

make_recipe() writes one from a mod folder by comparing it with the original files, and checks that building it
gives back exactly the mod's files; build_files() builds a recipe's files section.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from typing import Callable, Optional

import bf1_core as core

CLASS_TAGS = ("entc", "wpnc", "ordc", "expc")
FORMAT = 2


class RecipeError(Exception):
    pass


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


# --- naming chunks ------------------------------------------------------------------------

def chunk_label(chunk: core.Chunk) -> str:
    if chunk.tag in CLASS_TAGS:
        try:
            return core.decode_ordnance_payload(chunk.payload)["type"]
        except Exception:
            return ""
    try:
        return chunk.display_name()
    except Exception:
        return ""


def chunk_keys(chunks: list) -> list:
    """'tag:name', then 'tag:name#2', '#3'... for repeats, one per chunk in order."""
    seen, out = {}, []
    for c in chunks:
        base = f"{c.tag}:{chunk_label(c)}"
        n = seen.get(base, 0) + 1
        seen[base] = n
        out.append(base if n == 1 else f"{base}#{n}")
    return out


def _default_pad(data: bytes) -> bytes:
    return b"\x00" * core._pad_len(len(data))


# --- class edits --------------------------------------------------------------------------

def apply_class_edit(payload: bytes, edit: dict) -> bytes:
    d = core.decode_ordnance_payload(payload)
    if "props" in edit:
        props = [tuple(p) for p in edit["props"]]
        return core.encode_ordnance_payload(edit.get("base", d["base"]), edit.get("type", d["type"]), props)
    props = list(d["props"])
    for key, value in (edit.get("set") or {}).items():
        props = [(k, str(value)) if k.lower() == key.lower() else (k, v) for k, v in props]
    gone = {k.lower() for k in edit.get("remove") or []}
    props = [(k, v) for k, v in props if k.lower() not in gone]
    props += [(k, str(v)) for k, v in edit.get("add") or []]
    return core.encode_ordnance_payload(d["base"], d["type"], props)


def class_edit(base_payload: bytes, mod_payload: bytes) -> Optional[dict]:
    """The smallest class edit turning one class into the other exactly, or None if none does."""
    try:
        db, dm = core.decode_ordnance_payload(base_payload), core.decode_ordnance_payload(mod_payload)
    except Exception:
        return None
    tries = []
    if db["base"] == dm["base"] and db["type"] == dm["type"]:
        order, spell = [], {}
        for k, _v in db["props"] + dm["props"]:
            if k.lower() not in spell:
                order.append(k.lower())
            spell.setdefault(k.lower(), k)
        for k, _v in dm["props"]:
            spell[k.lower()] = k
        edit, ok = {"set": {}, "add": [], "remove": []}, True
        for key in order:
            bv = [v for k, v in db["props"] if k.lower() == key]
            mv = [v for k, v in dm["props"] if k.lower() == key]
            if bv == mv:
                continue
            if not bv:
                edit["add"] += [[spell[key], v] for v in mv]
            elif not mv:
                edit["remove"].append(spell[key])
            elif len(bv) == len(mv) and len(set(mv)) == 1:
                edit["set"][spell[key]] = mv[0]
            else:
                ok = False
                break
        if ok:
            tries.append({k: v for k, v in edit.items() if v})
    tries.append({"base": dm["base"], "type": dm["type"], "props": [list(p) for p in dm["props"]]})
    for edit in tries:
        try:
            if apply_class_edit(base_payload, edit) == mod_payload:
                return edit
        except Exception:
            continue
    return None


# --- comparing levels ---------------------------------------------------------------------

class Assets:
    """Binary data for a recipe, in a folder beside it, one file per distinct payload."""

    def __init__(self, folder: Path, relative_to: Path):
        self.folder, self.relative_to = Path(folder), Path(relative_to)
        self.written, self.bytes = 0, 0

    def put(self, data: bytes, ext: str) -> str:
        ext = "".join(ch for ch in ext if ch.isalnum() or ch == "_") or "bin"
        path = self.folder / f"{sha1(data)[:20]}.{ext}"
        if not path.exists():
            self.folder.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.written += 1
            self.bytes += len(data)
        return path.relative_to(self.relative_to).as_posix()


def _chunk_edit(cb: core.Chunk, cm: core.Chunk, key: str, assets: Assets) -> list:
    """Edits (relative to this chunk's level) turning base chunk `cb` into mod chunk `cm`."""
    if cb.pad == cm.pad:
        if cb.tag == "lvl_":
            try:
                nb, nm = cb.get_nested_container(), cm.get_nested_container()
                if nb is not None and nm is not None:
                    sub = diff_body(nb.body, nm.body, assets)
                    trial = core.Chunk("lvl_", cb.payload, cb.pad)
                    apply_body_edits(trial.get_nested_container().body, sub, assets_reader(assets.relative_to))
                    nested = [dict(e, at=[key] + e["at"]) for e in sub]
                    if trial.payload == cm.payload:
                        return nested
                    if len(trial.payload) == len(cm.payload) and trial.payload[8:] == cm.payload[8:]:
                        # the tool that made the mod left the level's size field as it was; the game doesn't mind
                        return nested + [{"op": "header", "at": [], "chunk": key, "hex": cm.payload[:8].hex()}]
            except (RecipeError, ValueError, KeyError, IndexError):
                pass
        elif cb.tag in CLASS_TAGS:
            edit = class_edit(cb.payload, cm.payload)
            if edit is not None:
                return [dict({"op": "class", "at": [], "chunk": key}, **edit)]
    e = {"op": "replace", "at": [], "chunk": key, "data": assets.put(cm.payload, cm.tag.strip())}
    if cm.pad != _default_pad(cm.payload):
        e["pad"] = cm.pad.hex()
    return [e]


def diff_body(bb: core.Body, mb: core.Body, assets: Assets) -> list:
    """Edits turning level body `bb` into `mb`: changes to chunks both have, removals, then additions in order."""
    kb, km = chunk_keys(bb.chunks), chunk_keys(mb.chunks)
    out, adds = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, kb, km, autojunk=False).get_opcodes():
        if tag == "equal":
            for a, b in zip(range(i1, i2), range(j1, j2)):
                cb, cm = bb.chunks[a], mb.chunks[b]
                if cb.payload != cm.payload or cb.pad != cm.pad:
                    out += _chunk_edit(cb, cm, kb[a], assets)
        else:
            out += [{"op": "remove", "at": [], "chunk": kb[a]} for a in range(i1, i2)]
            adds += range(j1, j2)
    for b in adds:
        cm = mb.chunks[b]
        e = {"op": "add", "at": [], "chunk": km[b], "after": km[b - 1] if b else None,
             "data": assets.put(cm.payload, cm.tag.strip())}
        if cm.pad != _default_pad(cm.payload):
            e["pad"] = cm.pad.hex()
        out.append(e)
    return out


# --- applying edits -----------------------------------------------------------------------

def assets_reader(folder: Path) -> Callable[[str], bytes]:
    folder = Path(folder)

    def read(rel: str) -> bytes:
        path = folder / rel
        try:
            return path.read_bytes()
        except OSError:
            raise RecipeError(f"The recipe's data file {rel} is missing (it should be beside the recipe).")
    return read


def apply_body_edits(body: core.Body, edits: list, read: Callable[[str], bytes]) -> list:
    """Applies edits to a level body in place. Chunks are found by their names as they were before any edits
    (so earlier edits don't renumber later ones); an addition goes after the chunk named in "after".
    Returns warnings."""
    snaps, warnings = {}, []

    def snap(b):
        if id(b) not in snaps:
            snaps[id(b)] = (b, dict(zip(chunk_keys(b.chunks), b.chunks)))
        return snaps[id(b)][1]

    def body_at(at):
        b = body
        for key in at:
            c = snap(b).get(key)
            if c is None or c.tag != "lvl_":
                raise RecipeError(f"No nested level {key} in {' / '.join(at) or 'the file'}.")
            b = c.get_nested_container().body
        return b

    for e in edits:
        op, key, at = e.get("op"), e.get("chunk", ""), e.get("at") or []
        b = body_at(at)
        where = " / ".join(at + [key])
        if op == "add":
            data = read(e["data"])
            pad = bytes.fromhex(e["pad"]) if "pad" in e else _default_pad(data)
            keys = chunk_keys(b.chunks)
            after = e.get("after")
            if after is None:
                index = 0
            elif after in keys:
                index = keys.index(after) + 1
            else:
                index = len(b.chunks)
                warnings.append(f"{where}: {after} wasn't there to go after, added at the end")
            b.add_chunk(core.Chunk(key[:4], data, pad), index)
            continue
        c = snap(b).get(key)
        if c is None or c.parent_body is not b:
            raise RecipeError(f"No chunk {where} to {op}.")
        if op == "remove":
            b.remove_chunk(c)
        elif op == "replace":
            data = read(e["data"])
            c.pad = bytes.fromhex(e["pad"]) if "pad" in e else _default_pad(data)
            c.set_payload(data)
        elif op == "header":
            head = bytes.fromhex(e["hex"])
            c.set_payload(head + c.payload[len(head):])
        elif op == "class":
            if c.tag not in CLASS_TAGS:
                raise RecipeError(f"{where} isn't a class.")
            c.set_payload(apply_class_edit(c.payload, e))
        else:
            raise RecipeError(f"Unknown edit '{op}' for {where}.")
    return warnings


def build_file(base_bytes: Optional[bytes], spec: dict, read: Callable[[str], bytes]) -> tuple:
    """(bytes, warnings) for one entry of a recipe's files section."""
    if "copy" in spec:
        return read(spec["copy"]), []
    if base_bytes is None:
        raise RecipeError("needs its original file")
    container = core.parse_container(base_bytes)
    warnings = apply_body_edits(container.body, spec.get("edits") or [], read)
    return container.raw_bytes(), warnings


# --- mods and original files --------------------------------------------------------------

def _index(root: Path) -> dict:
    out = {}
    for p in Path(root).rglob("*"):
        if p.is_file():
            out.setdefault(p.name.lower(), []).append(p)
    return out


def find_original(originals_root: Path, rel: str, index: Optional[dict] = None) -> Optional[Path]:
    """The original for a mod's file: same name, the most matching folders (as the Mod Loader picks)."""
    rel = Path(rel)
    cands = (index if index is not None else _index(originals_root)).get(rel.name.lower(), [])
    if not cands:
        return None
    hint = [s.lower() for s in rel.parent.parts]

    def score(c):
        parts, n = [s.lower() for s in c.parent.parts], 0
        for a, b in zip(reversed(parts), reversed(hint)):
            if a != b:
                break
            n += 1
        return n
    return sorted(cands, key=lambda c: (-score(c), len(c.parts)))[0]


SKIP_FILES = {"randomizer_last.json", "desktop.ini", "thumbs.db"}


def make_recipe(mod_dir: Path, originals_root: Path, out_json: Path, name: Optional[str] = None,
                description: Optional[str] = None, progress: Callable[[str], None] = lambda s: None) -> dict:
    """Writes a recipe that rebuilds `mod_dir` from the original files, and checks it does. Returns a report."""
    mod_dir, originals_root, out_json = Path(mod_dir), Path(originals_root), Path(out_json)
    if not mod_dir.is_dir() or not any(p.is_file() for p in mod_dir.rglob("*")):
        raise RecipeError(f"No mod files in {mod_dir}.")
    assets =Assets(out_json.parent / f"{out_json.stem}.assets", out_json.parent)
    read = assets_reader(out_json.parent)
    index = _index(originals_root)
    files, report = {}, {"recipe": str(out_json), "files": [], "unchanged": []}
    for src in sorted(p for p in mod_dir.rglob("*") if p.is_file()):
        if src.name.lower() in SKIP_FILES:
            continue
        rel = src.relative_to(mod_dir).as_posix()
        progress(rel)
        data = src.read_bytes()
        orig = find_original(originals_root, rel, index)
        row = {"file": rel}
        if orig is None:
            files[rel] = {"copy": assets.put(data, src.suffix.lstrip(".") or "bin"), "sha1": sha1(data)}
            row.update(kind="new file, copied whole")
            report["files"].append(row)
            continue
        base = orig.read_bytes()
        if base == data:
            report["unchanged"].append(rel)
            continue
        spec = {"base": orig.relative_to(originals_root).as_posix(), "base_sha1": sha1(base), "sha1": sha1(data)}
        edits = None
        if base[:4] == core.MAGIC and data[:4] == core.MAGIC:
            try:
                mod_c, base_c = core.parse_container(data), core.parse_container(base)
                if mod_c.has_magic == base_c.has_magic and mod_c.body.prefix == base_c.body.prefix \
                        and mod_c.body.trailing == base_c.body.trailing:
                    edits = diff_body(base_c.body, mod_c.body, assets)
                    built, _w = build_file(base, {"edits": edits}, read)
                    if built != data:
                        edits = None
            except (RecipeError, ValueError, KeyError, IndexError) as exc:
                row["note"] = f"couldn't compare chunk by chunk ({exc})"
                edits = None
        if edits is None:
            spec["copy"] = assets.put(data, src.suffix.lstrip(".") or "bin")
            row.update(kind="changed, copied whole" + (" (not a level)" if base[:4] != core.MAGIC else ""))
        else:
            spec["edits"] = edits
            row.update(kind="edits", **summarize(edits))
        files[rel] = spec
        report["files"].append(row)
    recipe = {"name": name or mod_dir.name, "format": FORMAT,
              "description": description or f"Rebuilds the {mod_dir.name} mod from the original game files.",
              "rules": [], "files": files}
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(recipe, indent=2) + "\n", encoding="utf-8")
    report.update(data_files=assets.written, data_bytes=assets.bytes)
    return report


def summarize(edits: list) -> dict:
    """Counts of edits by kind, and class names touched."""
    counts, classes, chunks = {}, [], {}
    for e in edits:
        counts[e["op"]] = counts.get(e["op"], 0) + 1
        if e["op"] == "class":
            classes.append(e["chunk"])
        else:
            tag = e["chunk"][:4]
            chunks.setdefault(e["op"], {}).setdefault(tag, 0)
            chunks[e["op"]][tag] += 1
    return {"edits": counts, "classes": sorted(set(classes)), "chunks": chunks}


def build_files(recipe: dict, recipe_dir: Path, originals_root: Optional[Path]) -> list:
    """[(relative path, bytes, warnings)] for a recipe's files section."""
    read = assets_reader(recipe_dir)
    index = _index(originals_root) if originals_root else {}
    out = []
    for rel, spec in (recipe.get("files") or {}).items():
        warnings, base = [], None
        if "copy" not in spec:
            if originals_root is None:
                raise RecipeError(f"{rel}: set the Original files folder (or pass --base) to build it.")
            orig = Path(originals_root) / spec["base"] if spec.get("base") else None
            if orig is None or not orig.is_file():
                orig = find_original(originals_root, spec.get("base") or rel, index)
            if orig is None:
                raise RecipeError(f"{rel}: no original {Path(spec.get('base') or rel).name} in {originals_root}.")
            base = orig.read_bytes()
            if spec.get("base_sha1") and sha1(base) != spec["base_sha1"]:
                warnings.append(f"the original {orig.name} isn't the copy this recipe was made from; "
                                "edits are applied by name, so check the result")
        try:
            data, w = build_file(base, spec, read)
        except RecipeError as exc:
            raise RecipeError(f"{rel}: {exc}")
        warnings += w
        if spec.get("sha1") and sha1(data) != spec["sha1"] and not (spec.get("base_sha1") and sha1(base or b"") != spec["base_sha1"]):
            warnings.append("the result isn't byte-for-byte what the recipe recorded")
        out.append((rel, data, warnings))
    return out
