"""
bf1_cli.py - command-line access to everything the BF1 Level Editor and
Mod Loader do, built for scripts and AI agents: every command prints plain
text, or JSON with --json, exits non-zero with a one-line error on failure,
and never changes its input file - edits are written to -o/--out.

  python bf1_cli.py --help            every command
  python bf1_cli.py <command> --help  one command's options

Chunks are addressed by REF, in any of these forms:
  9/34                 index path: top-level chunk 9 (a nested lvl_), chunk 34 inside it
  entc:rep_inf_clone_trooper   tag:name, searched through every nested level
  rep_inf_trooper      name alone (an error lists the candidates when it's ambiguous)
`ls` prints each chunk's index path.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

import bf1_core as core
import math
import random
import re
import Loader

CLASS_TAGS = ("entc", "wpnc", "ordc", "expc")


class CliError(Exception):
    pass


# --- output -------------------------------------------------------------------------------

VERSION = "1.2.1"

# --- colour -----------------------------------------------------------------------------
# Plain ANSI escape codes, which Windows 10/11 terminals understand once virtual
# terminal processing is switched on - no extra package. Only used when printing to
# a real terminal without --json: piped output and scripts always get plain text.

COLOR = False
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GREY = (f"\033[{n}m" for n in (91, 92, 93, 94, 95, 96, 90))


def enable_colors() -> bool:
    global COLOR
    import os
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # stdout
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except (AttributeError, OSError):
            return False
    COLOR = True
    return True


def paint(text: str, *styles: str) -> str:
    return "".join(styles) + text + RESET if COLOR and styles else text


def colorize(text: str) -> str:
    """Highlights command output: changes (old -> new), written files, headings."""
    if not COLOR:
        return text
    import re
    out = []
    for line in text.split("\n"):
        if line.startswith("wrote ") or line.startswith("installed ") or line.startswith("extracted "):
            line = paint(line, GREEN)
        elif line.startswith(("recipe:", "seed ", "class ", "installed now", "Installed now")):
            line = paint(line, YELLOW, BOLD)
        elif " -> " in line:
            head, _, new = line.rpartition(" -> ")
            key, sep, old = head.partition(": ")
            line = (paint(key, CYAN) + sep + paint(old, DIM) if sep else paint(head, DIM)) + \
                paint(" -> ", GREY) + paint(new, GREEN)
        elif re.match(r"^\s*[\d/]+\s{2}\w{4}\s", line):  # an ls/classes row: REF  tag  name
            ref, rest = line.split(None, 1)
            pad = line[:len(line) - len(line.lstrip())]
            tag, _, rest2 = rest.partition(" ")
            line = pad + paint(ref, GREY) + "  " + paint(tag, MAGENTA) + " " + rest2
        elif re.match(r"^\s+\w[\w ]*= ", line):  # a property: Key = value
            key, _, value = line.partition(" = ")
            line = paint(key, CYAN) + " = " + value
        out.append(line)
    return "\n".join(out)


def emit(args, data, text: str | None = None) -> None:
    """JSON with --json, otherwise `text` (or a readable dump of `data`)."""
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    elif text is not None:
        print(colorize(text))
    else:
        print(json.dumps(data, indent=2, default=str))


# --- level files and chunk references ---------------------------------------------------

def load(path) -> core.Container:
    path = Path(path)
    if not path.is_file():
        raise CliError(f"No such file: {path}")
    try:
        return core.parse_container(path.read_bytes())
    except Exception as exc:
        raise CliError(f"{path.name} isn't a readable .lvl: {exc}")


def save(container: core.Container, out, args) -> str:
    out = Path(out)
    if out.exists() and not args.force:
        raise CliError(f"{out} already exists - pass --force to overwrite it.")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(container.raw_bytes())
    return str(out)


def chunk_name(chunk: core.Chunk) -> str:
    """A chunk's name - for wpnc/ordc/expc, which have no NAME block, the class name inside it."""
    name = chunk.display_name()
    if not name and chunk.tag in CLASS_TAGS:
        try:
            name = core.decode_ordnance_payload(chunk.payload)["type"]
        except Exception:
            pass
    return name or ""


def walk(container: core.Container):
    """(index path string, chunk, name path) for every chunk, nested levels included."""
    for idx_path, i, chunk, name_path in core.find_in_container(container):
        yield "/".join(str(n) for n in idx_path + (i,)), chunk, "/".join(name_path)


def resolve_all(container: core.Container, ref: str) -> list:
    """Every (index path, chunk) a name REF matches - a level often carries the
    same texture or class once per nested level, and an edit should reach each."""
    if all(part.isdigit() for part in ref.split("/")):
        return [resolve(container, ref)]
    tag, _, name = ref.partition(":") if ":" in ref else ("", "", ref)
    found = [(p, c) for p, c, _ in walk(container)
             if chunk_name(c).lower() == name.lower() and (not tag or c.tag == tag)]
    if not found:
        raise CliError(f"No chunk matches '{ref}'. Use `ls` to see what's there.")
    if len({c.tag for _, c in found}) > 1:
        options = ", ".join(f"{p} ({c.tag})" for p, c in found[:12])
        raise CliError(f"'{ref}' matches chunks of different types: {options}. Use tag:name.")
    return found


def resolve(container: core.Container, ref: str, identical_ok: bool = False) -> tuple:
    """(index path, chunk) for a REF - see the module docstring. With
    `identical_ok`, several matches with byte-identical contents count as one."""
    if not all(part.isdigit() for part in ref.split("/")) and identical_ok:
        found = resolve_all(container, ref)
        if len({c.payload for _, c in found}) == 1:
            return found[0]
    if all(part.isdigit() for part in ref.split("/")):
        node, chunk = container, None
        for n, part in enumerate(ref.split("/")):
            chunks = node.body.chunks
            i = int(part)
            if i >= len(chunks):
                raise CliError(f"{ref}: there's no chunk {i} at that level (it has {len(chunks)}).")
            chunk = chunks[i]
            if n < len(ref.split("/")) - 1:
                if chunk.tag != "lvl_":
                    raise CliError(f"{ref}: chunk {part} is '{chunk.tag}', not a nested level.")
                node = chunk.get_nested_container()
        return ref, chunk
    tag, _, name = ref.partition(":") if ":" in ref else ("", "", ref)
    found = [(p, c) for p, c, _ in walk(container)
             if chunk_name(c) == name and (not tag or c.tag == tag)]
    if not found:
        found = [(p, c) for p, c, _ in walk(container)
                 if chunk_name(c).lower() == name.lower() and (not tag or c.tag == tag)]
    if not found:
        raise CliError(f"No chunk matches '{ref}'. Use `ls` to see what's there.")
    if len(found) > 1:
        options = ", ".join(f"{p} ({c.tag})" for p, c in found[:12])
        raise CliError(f"'{ref}' matches {len(found)} chunks: {options}. Use tag:name or an index path"
                       f" (replace takes --all to change every one).")
    return found[0]


def chunk_row(path: str, chunk: core.Chunk, where: str = "") -> dict:
    return {"ref": path, "tag": chunk.tag, "name": chunk_name(chunk), "size": len(chunk.payload),
            "in": where}


# --- level commands -----------------------------------------------------------------------

def cmd_info(args):
    container = load(args.file)
    rows = list(walk(container))
    tags: dict = {}
    for _, chunk, _ in rows:
        tags[chunk.tag] = tags.get(chunk.tag, 0) + 1
    data = {"file": args.file, "bytes": Path(args.file).stat().st_size,
            "top_level_chunks": len(container.body.chunks), "all_chunks": len(rows),
            "nested_levels": [c.display_name() for c in container.body.chunks if c.tag == "lvl_"],
            "chunks_by_tag": dict(sorted(tags.items(), key=lambda kv: -kv[1]))}
    text = (f"{Path(args.file).name}: {data['bytes']:,} bytes, {len(rows)} chunks "
            f"({data['top_level_chunks']} top-level)\n"
            f"nested levels: {', '.join(data['nested_levels']) or 'none'}\n"
            "chunks: " + ", ".join(f"{t} {n}" for t, n in data["chunks_by_tag"].items()))
    emit(args, data, text)


def cmd_ls(args):
    container = load(args.file)
    rows = []
    for path, chunk, where in walk(container):
        if args.top and "/" in path:
            continue
        if args.tag and chunk.tag != args.tag:
            continue
        if args.name and args.name.lower() not in chunk_name(chunk).lower():
            continue
        rows.append(chunk_row(path, chunk, where))
    text = "\n".join(f"{r['ref']:>8}  {r['tag']:5s} {r['name']:40s} {r['size']:>10,}"
                     + (f"   in {r['in']}" if r['in'] else "") for r in rows) or "(no chunks match)"
    emit(args, rows, text)


def class_info(chunk: core.Chunk) -> dict:
    d = core.decode_ordnance_payload(chunk.payload)
    return {"tag": chunk.tag, "class": d["type"], "base": d["base"],
            "properties": [{"key": k, "value": v} for k, v in d["props"]]}


def cmd_show(args):
    container = load(args.file)
    path, chunk = resolve(container, args.ref)
    data = chunk_row(path, chunk)
    lines = [f"{path}  {chunk.tag}  {chunk_name(chunk)}  {len(chunk.payload):,} bytes"]
    if chunk.tag in CLASS_TAGS:
        info = class_info(chunk)
        data.update(info)
        lines.append(f"class {info['class']} (base {info['base']})")
        lines += [f"  {p['key']} = {p['value']}" for p in info["properties"]]
    elif chunk.tag == "tex_":
        info = core.decode_texture_chunk(chunk.payload)
        data["formats"] = [{k: f[k] for k in ("fourcc", "width", "height") if k in f} for f in info["formats"]]
        lines += [f"  format {f.get('fourcc')} {f.get('width')}x{f.get('height')}" for f in info["formats"]]
    elif chunk.tag == "modl":
        data.update({"vertices": core.model_vertex_count(chunk.payload),
                     "segments": core.model_segment_count(chunk.payload),
                     "skinned": core.is_skinned_model(chunk.payload),
                     "texture": core.model_texture(chunk.payload)})
        bones = core.find_skeleton(chunk.parent_body.chunks, chunk.display_name())
        if bones:
            data["character"] = core.is_character(bones)
            data["fire_points"] = [{"bone": n, "position": p, "is_fire_point": f}
                                   for n, p, f in core.hard_points(bones, chunk.parent_body.chunks,
                                                                 chunk.display_name())]
            data["used_by"] = core.classes_using(chunk.parent_body.chunks, chunk.display_name(),
                                               core.MODEL_PROPERTIES)
        lines += [f"  {k}: {v}" for k, v in data.items() if k not in ("ref", "tag", "name", "size", "in")]
    elif chunk.tag == "lvl_":
        nested = chunk.get_nested_container()
        data["chunks"] = len(nested.body.chunks)
        lines.append(f"  nested level with {len(nested.body.chunks)} chunks - `ls` shows them")
    emit(args, data, "\n".join(lines))


def cmd_classes(args):
    container = load(args.file)
    rows = []
    for path, chunk, where in walk(container):
        if chunk.tag not in CLASS_TAGS:
            continue
        info = class_info(chunk)
        props = {p["key"]: p["value"] for p in info["properties"]}
        rows.append({"ref": path, "tag": chunk.tag, "class": info["class"], "base": info["base"],
                     "label": props.get("Label", ""), "geometry": props.get("GeometryName", ""),
                     "in": where})
    text = "\n".join(f"{r['ref']:>8}  {r['tag']}  {r['class']:36s} {r['base']:14s} {r['geometry']}"
                     for r in rows) or "(no classes)"
    emit(args, rows, text)


def cmd_set(args):
    container = load(args.file)
    path, chunk = resolve(container, args.ref)
    if chunk.tag not in CLASS_TAGS:
        raise CliError(f"{args.ref} is a '{chunk.tag}' chunk - `set` edits class chunks ({', '.join(CLASS_TAGS)}).")
    d = core.decode_ordnance_payload(chunk.payload)
    props = list(d["props"])
    changes = []
    for assignment in args.assignments:
        key, eq, value = assignment.partition("=")
        if not eq or not key:
            raise CliError(f"'{assignment}' isn't KEY=VALUE.")
        hits = [i for i, (k, _) in enumerate(props) if k.lower() == key.lower()]
        if len(hits) > 1 and not args.all:
            raise CliError(f"{key} appears {len(hits)} times in this class (e.g. one per weapon slot) - "
                           f"pass --all to set every one, or edit with `export`/`replace`.")
        if hits:
            for i in hits:
                changes.append({"key": props[i][0], "old": props[i][1], "new": value})
                props[i] = (props[i][0], value)
        else:
            props.append((key, value))
            changes.append({"key": key, "old": None, "new": value})
    for key in args.remove or []:
        before = len(props)
        props = [(k, v) for k, v in props if k.lower() != key.lower()]
        if len(props) == before:
            raise CliError(f"{key} isn't in this class.")
        changes.append({"key": key, "removed": before - len(props)})
    chunk.set_payload(core.encode_ordnance_payload(d["base"], d["type"], props))
    out = save(container, args.out, args)
    emit(args, {"ref": path, "class": d["type"], "changes": changes, "out": out},
         "\n".join(f"{c['key']}: {c.get('old')} -> {c.get('new', 'removed')}" for c in changes) + f"\nwrote {out}")


def cmd_export(args):
    container = load(args.file)
    path, chunk = resolve(container, args.ref, identical_ok=True)
    written = core.export_chunk_to_file(chunk, args.out)
    emit(args, {"ref": path, "tag": chunk.tag, "out": str(written)}, f"wrote {written}")


def cmd_replace(args):
    container = load(args.file)
    targets = resolve_all(container, args.ref) if args.all else [resolve(container, args.ref)]
    tag = targets[0][1].tag
    rows = []
    for path, chunk in targets:
        try:  # a script keeps its name and a config its name hash, whatever the file is called
            new = core.import_file_into_chunk(chunk, args.source)
        except Exception as exc:
            raise CliError(f"Can't use {args.source} for a '{tag}' chunk: {exc}")
        rows.append({"ref": path, "old_size": len(chunk.payload), "new_size": len(new)})
        chunk.set_payload(new)
    out = save(container, args.out, args)
    emit(args, {"tag": tag, "replaced": rows, "out": out},
         "\n".join(f"{r['ref']} ({tag}) replaced: {r['old_size']:,} -> {r['new_size']:,} bytes" for r in rows)
         + f"\nwrote {out}")


def _number_format(original: str):
    """How to write a number back the way `original` was written: int, or N decimals."""
    text = original.strip()
    if "." not in text:
        return lambda x: str(int(round(x)))
    decimals = max(1, len(text.split(".", 1)[1]))

    def write(x: float) -> str:
        # at least the original's decimals, more when the change needs them
        # (0.2 * 0.75 is 0.15, not "0.2")
        s = f"{x:.4f}".rstrip("0")
        s = s + "0" if s.endswith(".") else s
        return s if len(s.split(".")[1]) >= decimals else f"{x:.{decimals}f}"
    return write


def apply_op(value: str, op: str, skip_over: float | None = None) -> str | None:
    """The new value for one property, or None to leave it. Ops: '*1.5' scales,
    '+2' adds, '=text' (or plain text) sets. Scaling works on each number of a
    vector ('8.0 14.0'), leaves anything that isn't purely numbers alone (comments,
    '7.5.0', names), and never touches a value <= 0 (0 and -1 mean 'none' or
    'infinite' in the game) or one above `skip_over`."""
    if op[:1] not in "*+":
        return op[1:] if op.startswith("=") else op
    tokens = value.split()
    try:
        numbers = [float(t) for t in tokens]
    except ValueError:
        return None
    if not numbers or any(n <= 0 for n in numbers) or (skip_over is not None and max(numbers) > skip_over):
        return None
    k = float(op[1:])
    new = [n * k if op[0] == "*" else n + k for n in numbers]
    return " ".join(_number_format(t)(n) for t, n in zip(tokens, new))


def _matches(rule: dict, chunk, info: dict) -> bool:
    import fnmatch
    if rule.get("tags") and chunk.tag not in rule["tags"]:
        return False
    if rule.get("base") and info["base"] not in rule["base"]:
        return False
    if rule.get("classes") and not any(fnmatch.fnmatch(info["class"].lower(), p.lower()) for p in rule["classes"]):
        return False
    if rule.get("exclude") and any(fnmatch.fnmatch(info["class"].lower(), p.lower()) for p in rule["exclude"]):
        return False
    return True


def cmd_apply(args):
    """Applies a recipe of rules (JSON) to class properties across many levels."""
    try:
        recipe = json.loads(Path(args.recipe).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CliError(f"Can't read the recipe {args.recipe}: {exc}")
    rules = recipe.get("rules") or []
    if not rules:
        raise CliError("The recipe has no rules.")
    out_dir = Path(args.out_dir)
    report = {"recipe": recipe.get("name", Path(args.recipe).stem), "files": []}
    for file in args.files:
        container = load(file)
        counts = [0] * len(rules)
        classes_changed = set()
        for path, chunk, _ in walk(container):
            if chunk.tag not in CLASS_TAGS:
                continue
            d = core.decode_ordnance_payload(chunk.payload)
            info = {"base": d["base"], "class": d["type"]}
            props, changed = list(d["props"]), False
            for r, rule in enumerate(rules):
                if not _matches(rule, chunk, info):
                    continue
                for key, op in rule.get("set", {}).items():
                    hits = [i for i, (k, _) in enumerate(props) if k.lower() == key.lower()]
                    if not hits and rule.get("add") and not op[:1] in "*+":
                        props.append((key, op[1:] if op.startswith("=") else op))
                        counts[r] += 1
                        changed = True
                    for i in hits:
                        new = apply_op(props[i][1], op, rule.get("skip_over"))
                        if new is not None and new != props[i][1]:
                            props[i] = (props[i][0], new)
                            counts[r] += 1
                            changed = True
            if changed:
                chunk.set_payload(core.encode_ordnance_payload(d["base"], d["type"], props))
                classes_changed.add(d["type"])
        target = out_dir / Path(file).name
        if counts and sum(counts):
            save(container, target, args)
        report["files"].append({"file": str(file), "out": str(target) if sum(counts) else None,
                                "classes_changed": len(classes_changed),
                                "rules": [{"rule": rule.get("name", f"rule {r + 1}"), "values_changed": n}
                                          for r, (rule, n) in enumerate(zip(rules, counts))]})
    lines = [f"recipe: {report['recipe']}"]
    for f in report["files"]:
        total = sum(r["values_changed"] for r in f["rules"])
        n = f["classes_changed"]
        lines.append(f"{Path(f['file']).name}: {total} value{'s' * (total != 1)} in {n} class{'es' * (n != 1)}"
                     + (f" -> {f['out']}" if f["out"] else " (nothing to change, not written)"))
        lines += [f"    {r['rule']}: {r['values_changed']}" for r in f["rules"] if r["values_changed"]]
    emit(args, report, "\n".join(lines))


def cmd_hash(args):
    rows = []
    for name in args.names:
        h = core.fnv1a32(name)
        rows.append({"name": name, "hash": f"0x{h:08x}", "known_as": core.HASH_TO_NAME.get(h)})
    emit(args, rows, "\n".join(f"{r['name']:32s} {r['hash']}" + (f"  (known: {r['known_as']})" if r['known_as'] else "")
                                for r in rows))


# --- characters and vehicles ---------------------------------------------------------------

def _texture_size(chunk) -> int:
    info = core.decode_texture_chunk(chunk.payload)
    if info["formats"]:
        f = info["formats"][0]
        if f["width"] == f["height"] and f["width"] >= 64:
            return f["width"]
    return 512


def cmd_char(args):
    """The import window's build, headless: same defaults, every option a flag."""
    container = load(args.file)
    path, model = resolve(container, args.model)
    if model.tag != "modl" or not core.is_skinned_model(model.payload):
        raise CliError(f"{args.model} isn't a character or vehicle model (a skinned 'modl').")
    name, siblings = model.display_name(), model.parent_body.chunks
    bones = core.find_skeleton(siblings, name)
    if bones is None:
        raise CliError(f"No skeleton for {name} next to it.")
    lod = None if args.no_lod else core.find_lod(siblings, name)
    lod_bones = (core.find_skeleton(siblings, lod.display_name()) or bones) if lod is not None else None
    glb = Path(args.glb).read_bytes()
    source = core.load_glb_mesh(glb)
    usage = core.texture_usage(source)
    if args.list_textures:
        emit(args, [{"group": g, "vertices": n} for g, n in sorted(usage.items())],
             "\n".join(f"texture {g}: {n:,} vertices" for g, n in sorted(usage.items())))
        return
    if not args.out:
        raise CliError("Importing needs -o/--out (or --list-textures to just look).")
    if args.delete_texture:
        source = core.drop_groups(source, set(args.delete_texture))
        usage = core.texture_usage(source)
        if not usage:
            raise CliError("Deleting those textures leaves nothing of the model.")
    images = core.fill_missing_images(core.load_glb_images(glb), usage)
    character = core.is_character(bones)
    bounds = core.template_bounds(model.payload, bones)
    axis = args.axis or ("Height" if character else "Largest side")
    if axis not in core.FIT_AXES:
        raise CliError(f"--axis must be one of: {', '.join(core.FIT_AXES)}")
    textures = {ch.display_name(): ch for ch in siblings if ch.tag == "tex_"}
    tex_name = None
    if not args.keep_texture:
        if args.texture:
            tex_name = args.texture
        else:
            own = core.find_texture(siblings, core.model_texture(model.payload))
            tex_name = own.display_name() if own is not None else None
        if tex_name is not None and tex_name not in textures:
            raise CliError(f"No texture '{tex_name}' next to the model. Options: {', '.join(sorted(textures))}")
    mesh, atlas, size = source, None, 0
    if tex_name:
        size = _texture_size(textures[tex_name])
        mesh, atlas = core.build_atlas(source, images, core.auto_atlas_layout(usage, images, size), size)
    fit_size = args.size
    rotate = tuple(float(v) for v in args.rotate.split(",")) if args.rotate else (0.0, 0.0, 0.0)
    if len(rotate) != 3:
        raise CliError("--rotate takes three angles: X,Y,Z")
    mesh = core.fit_to_bounds(mesh, *bounds, axis, fit_size, rotate)
    arm = args.arm if args.arm is not None else (
        core.estimate_arm_drop(mesh, bones, core.bind_world_matrices(bones)) if character else 0.0)
    hair = set(args.hair) if args.hair is not None else (core.guess_loose_groups(mesh) if character else set())
    limits = {g: core.TORSO_BONES for g in hair}
    budget = args.budget or len(mesh.positions)
    main, stats = core.build_character_model(model.payload, bones, mesh, budget, arm, limits, tex_name)
    changes, report = [(model, main)], {"model": name, "ref": path, "vertices": stats["vertices"],
                                        "triangles": stats["tris"], "segments": stats["segments"],
                                        "arm_angle": stats.get("arm_drop"), "hair_textures": sorted(hair)}
    if lod is not None:
        lod_payload, lod_stats = core.build_character_model(lod.payload, lod_bones, mesh, args.lod_budget or budget,
                                                          arm, limits, tex_name)
        changes.append((lod, lod_payload))
        report["lod"] = {"model": lod.display_name(), "vertices": lod_stats["vertices"],
                         "segments": lod_stats["segments"]}
    if not args.no_collision:
        coll, prim = core.find_collision(siblings, name)
        if coll is not None:
            new_coll, cstats = core.rebuild_collision_mesh(coll.payload, bones, core.model_triangles(main, bones),
                                                         core.auto_collision_budget(coll.payload))
            changes.append((coll, new_coll))
            report["collision_triangles"] = cstats["triangles"]
        if prim is not None:
            new_prim, count = core.refit_primitives(prim.payload, bones, bounds, core.model_bounds(main, bones))
            changes.append((prim, new_prim))
            report["collision_shapes"] = count
    if not args.keep_shadow:
        changes = [(c, core.strip_shadows(p)) if c.tag == "modl" else (c, p) for c, p in changes]
    if tex_name:
        changes.append((textures[tex_name], core.encode_texture_chunk(tex_name, size, size, atlas)))
        report["texture"] = {"name": tex_name, "size": size}
    fire = core.weapon_point_report(core.weapon_points(bones, siblings, name), main, bones)
    report["fire_points_off_model"] = [n for n, _, off in fire if off]
    if args.preview:
        from PIL import Image
        tex_img = Image.frombytes("RGBA", (size, size), atlas) if atlas is not None else None
        core.render_preview(main, bones, tex_img).save(args.preview)
        report["preview"] = args.preview
    for chunk, payload in changes:
        chunk.set_payload(payload)
    report["out"] = save(container, args.out, args)
    text = (f"{name}: {report['vertices']:,} vertices, {report['triangles']:,} triangles, "
            f"{report['segments']} segments" + (f"; LOD {report['lod']['vertices']:,} vertices" if "lod" in report else "")
            + (f"\nfire points off the model: {', '.join(report['fire_points_off_model'])} (fix with `points`)"
               if report["fire_points_off_model"] else "") + f"\nwrote {report['out']}")
    emit(args, report, text)


def cmd_points(args):
    container = load(args.file)
    path, model = resolve(container, args.model)
    name, siblings = model.display_name(), model.parent_body.chunks
    bones = core.find_skeleton(siblings, name) if model.tag == "modl" else None
    if not bones:
        raise CliError(f"{args.model} has no skeleton, so no fire points.")
    points = {n: (p, f) for n, p, f in core.hard_points(bones, siblings, name)}
    moves = {}
    for spec in args.move or []:
        bone, eq, xyz = spec.partition("=")
        try:
            moves[bone] = tuple(float(v) for v in xyz.split(","))
            assert eq and len(moves[bone]) == 3
        except (ValueError, AssertionError):
            raise CliError(f"'{spec}' isn't BONE=X,Y,Z.")
        if bone not in points:
            raise CliError(f"{bone} isn't a hardpoint of {name}. Options: {', '.join(points)}")
    if args.snap:
        for bone, _, off in core.weapon_point_report([(n, p) for n, (p, f) in points.items() if f], model.payload, bones):
            if off and bone not in moves:
                moves[bone] = core.nearest_surface_point(points[bone][0], model.payload, bones)
    rows = [{"bone": n, "position": moves.get(n, p), "moved": n in moves, "is_fire_point": f}
            for n, (p, f) in points.items()]
    if not moves:
        emit(args, rows, "\n".join(f"{r['bone']:24s} {tuple(round(v, 3) for v in r['position'])}"
                                   + ("  (fire point)" if r["is_fire_point"] else "") for r in rows))
        return
    if not args.out:
        raise CliError("Moving points needs -o/--out.")
    lod = core.find_lod(siblings, name)
    done = set()
    for model_name in [name] + ([lod.display_name()] if lod is not None else []):
        skel = core.find_skeleton_chunk(siblings, model_name)
        if skel is None or id(skel) in done:
            continue
        done.add(id(skel))
        _, skel_bones = core.decode_skeleton_chunk(skel.payload)
        wanted = {n: p for n, p in moves.items() if any(b.name == n for b in skel_bones)}
        if wanted:
            skel.set_payload(core.encode_skeleton_transforms(skel.payload, core.move_bones(skel_bones, wanted)))
    out = save(container, args.out, args)
    emit(args, {"moved": {n: p for n, p in moves.items()}, "out": out},
         "\n".join(f"moved {n} -> {tuple(round(v, 3) for v in p)}" for n, p in moves.items()) + f"\nwrote {out}")


# --- movies and sounds --------------------------------------------------------------------

def _media(path):
    try:
        return core, core.read_media(path)
    except core.MediaError as exc:
        raise CliError(str(exc))


def cmd_media_ls(args):
    m, media = _media(args.file)
    rows = [{"name": e.name, "hash": f"0x{e.hash:08x}", "bytes": e.length, "seconds": round(e.seconds, 3),
             "frequency": e.frequency, "alias_of": f"0x{e.alias_of:08x}" if e.alias_of is not None else None}
            for e in media.entries]
    head = f"{Path(args.file).name}: {len(rows)} {media.kind}"
    if media.kind == "movies":
        text = head + "\n" + "\n".join(f"  {r['name']:30s} {r['bytes'] / 1e6:7.1f} MB" for r in rows)
    else:
        text = head + "\n" + "\n".join(f"  {r['name']:40s} {r['seconds']:6.2f} s {r['frequency']:>6} Hz"
                                       + ("  alias" if r["alias_of"] else "") for r in rows)
    emit(args, {"kind": media.kind, "entries": rows}, text)


def _pick(media, names):
    if not names:
        return list(media.entries)
    by = {}
    for e in media.entries:
        by.setdefault(e.name.lower(), e)
        by.setdefault(f"0x{e.hash:08x}", e)
        by.setdefault(f"{e.hash:08x}", e)
    missing = [n for n in names if n.lower() not in by]
    if missing:
        raise CliError(f"Not in {media.path.name}: {', '.join(missing)}")
    return [by[n.lower()] for n in names]


def cmd_media_extract(args):
    m, media = _media(args.file)
    files = m.extract(media, args.outdir, _pick(media, args.only))
    emit(args, [str(f) for f in files], f"extracted {len(files)} file(s) to {args.outdir}")


def cmd_media_replace(args):
    m, media = _media(args.file)
    if media.kind != "sounds":
        raise CliError("Only sounds can be replaced - the movies are Bink videos, which these tools can't write.")
    replacements, report = {}, []
    for spec in args.pairs:
        sample, eq, audio = spec.partition("=")
        if not eq:
            raise CliError(f"'{spec}' isn't SAMPLE=AUDIOFILE.")
        entry = _pick(media, [sample])[0]
        if entry.alias_of is not None:
            raise CliError(f"{entry.name} is an alias with no audio of its own - replace the sample it plays.")
        try:
            pcm, rate = m.load_audio(audio)
        except m.MediaError as exc:
            raise CliError(str(exc))
        replacements[entry.hash] = (pcm, rate)
        report.append({"sample": entry.name, "audio": audio, "seconds": round(len(pcm) / 2 / rate, 3), "rate": rate})
    out = Path(args.out)
    if out.exists() and not args.force:
        raise CliError(f"{out} already exists - pass --force to overwrite it.")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(m.rebuild_bank(media.path, replacements))
    emit(args, {"replaced": report, "out": str(out)},
         "\n".join(f"{r['sample']} <- {r['audio']} ({r['seconds']} s, {r['rate']} Hz)" for r in report) + f"\nwrote {out}")


# --- animations -----------------------------------------------------------------------------

def _bank(container, ref):
    """(index path, zaa_ chunk, decoded bank) - REF may leave out the zaa_: tag."""
    if ":" not in ref and not all(p.isdigit() for p in ref.split("/")):
        ref = "zaa_:" + ref
    path, chunk = resolve(container, ref, identical_ok=True)
    if chunk.tag != "zaa_":
        raise CliError(f"{ref} is a '{chunk.tag}' chunk - animations are in 'zaa_' chunks (`anim ls` lists them).")
    core.learn_from_container(container)
    try:
        return path, chunk, core.decode_zaa(chunk.payload)
    except (core.AnimError, struct.error, ValueError, IndexError) as exc:
        raise CliError(f"Can't read the animations in {ref}: {exc}")


def _save_bank(container, ref, bank, args):
    """Writes the bank into every copy of the zaa_ chunk (one per nested level) and saves."""
    if ":" not in ref and not all(p.isdigit() for p in ref.split("/")):
        ref = "zaa_:" + ref
    payload = core.encode_zaa(bank)
    for _, chunk in resolve_all(container, ref):
        chunk.set_payload(payload)
    return save(container, args.out, args)


def _anim_row(a) -> dict:
    return {"name": a.name, "hash": f"0x{a.crc:08x}", "frames": a.frames, "seconds": round(a.seconds, 2),
            "joints": len(a.tracks), "moving_joints": sum(t.moves for t in a.tracks)}


def cmd_anim_ls(args):
    container = load(args.file)
    if not args.ref:
        rows = []
        for path, chunk, where in walk(container):
            if chunk.tag == "zaa_":
                try:
                    count = len(core.decode_zaa(chunk.payload).animations)
                except Exception:
                    count = None
                rows.append({"ref": path, "bank": chunk_name(chunk), "animations": count, "in": where})
        emit(args, rows, "\n".join(f"{r['ref']:>8}  zaa_  {r['bank']:32s} {r['animations']:>4} animations"
                                   + (f"   in {r['in']}" if r["in"] else "") for r in rows)
             or "(no animation banks)")
        return
    path, _, bank = _bank(container, args.ref)
    rows = [_anim_row(a) for a in bank.animations]
    emit(args, {"ref": path, "bank": bank.name, "animations": rows},
         f"{bank.name}: {len(rows)} animations\n" + "\n".join(
             f"  {r['name']:44s} {r['frames']:>4} frames {r['seconds']:6.2f} s  {r['moving_joints']:>3}/{r['joints']} joints move"
             for r in rows))


def _pick_anims(bank, names):
    if not names:
        return list(bank.animations)
    try:
        return [bank.find(n) for n in names]
    except core.AnimError as exc:
        raise CliError(f"{exc} `anim ls` lists them.")


def cmd_anim_export(args):
    container = load(args.file)
    path, _, bank = _bank(container, args.ref)
    picked = _pick_anims(bank, args.only)
    out = Path(args.out)
    data = core.bank_to_dict(core.AnimBank(bank.name, picked, bank.version))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    emit(args, {"ref": path, "animations": [a.name for a in picked], "out": str(out)},
         f"exported {len(picked)} animation(s)\nwrote {out}")


def cmd_anim_import(args):
    container = load(args.file)
    path, _, bank = _bank(container, args.ref)
    try:
        incoming = core.bank_from_dict(json.loads(Path(args.source).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, core.AnimError) as exc:
        raise CliError(f"Can't read {args.source}: {exc}")
    picked = _pick_anims(incoming, args.only)
    if args.as_name:
        if len(picked) != 1:
            raise CliError("--as needs exactly one animation (pick it with --only).")
        picked[0].crc = core.crc_of(args.as_name)
    report = []
    for a in picked:
        hit = next((i for i, b in enumerate(bank.animations) if b.crc == a.crc), None)
        if hit is None:
            bank.animations.append(a)
            report.append(f"added {a.name} ({a.frames} frames)")
        else:
            bank.animations[hit] = a
            report.append(f"replaced {a.name} ({a.frames} frames)")
    out = _save_bank(container, args.ref, bank, args)
    emit(args, {"ref": path, "changes": report, "out": out}, "\n".join(report) + f"\nwrote {out}")


def cmd_anim_edit(args):
    container = load(args.file)
    path, _, bank = _bank(container, args.ref)
    picked = _pick_anims(bank, args.names)
    if args.rename and len(picked) != 1:
        raise CliError("--rename works on one animation at a time.")
    report = []
    for a in picked:
        i = bank.animations.index(a)
        if args.delete:
            bank.animations.remove(a)
            report.append(f"deleted {a.name}")
            continue
        new, did = a, []
        if args.speed:
            if args.speed <= 0:
                raise CliError("--speed must be above 0 (2 = twice as fast, 0.5 = half speed).")
            frames = max(2, round((a.frames - 1) / args.speed) + 1) if a.frames > 1 else 1
            new = core.retime(new, frames)
            did.append(f"frames: {a.frames} -> {frames}")
        if args.motion is not None:
            new = core.scale_motion(new, args.motion)
            did.append(f"motion: x{args.motion}")
        if args.reverse:
            new = core.reverse(new)
            did.append("reversed")
        if args.rename:
            crc = core.crc_of(args.rename)
            if any(b.crc == crc for b in bank.animations if b is not a):
                raise CliError(f"{args.rename} is already in {bank.name} - rename or delete that one first.")
            new = new.copy() if new is a else new
            old_name = a.name
            new.crc = crc
            did.append(f"name: {old_name} -> {new.name}")
        bank.animations[i] = new
        report += [f"{a.name} {d}" if not d.startswith("name") else d for d in did]
    if not report:
        raise CliError("Nothing to do - pass --speed, --motion, --reverse, --rename or --delete.")
    out = _save_bank(container, args.ref, bank, args)
    emit(args, {"ref": path, "changes": report, "out": out}, "\n".join(report) + f"\nwrote {out}")


def cmd_anim_copy(args):
    container = load(args.file)
    path, _, bank = _bank(container, args.ref)
    if args.source_file:
        src_file, _, src_ref = args.source_file.partition("::")
        src_container = load(src_file)
        _, _, src_bank = _bank(src_container, src_ref or args.ref)
    else:
        src_bank = bank
    report = []
    for spec in args.pairs:
        target, eq, source = spec.partition("=")
        if not eq:
            raise CliError(f"'{spec}' isn't TARGET=SOURCE.")
        src = _pick_anims(src_bank, [source])[0].copy()
        src.crc = core.crc_of(target)
        hit = next((i for i, b in enumerate(bank.animations) if b.crc == src.crc), None)
        if hit is None:
            bank.animations.append(src)
            report.append(f"added {src.name} as a copy of {source}")
        else:
            bank.animations[hit] = src
            report.append(f"{src.name} now plays {source}")
    out = _save_bank(container, args.ref, bank, args)
    emit(args, {"ref": path, "changes": report, "out": out}, "\n".join(report) + f"\nwrote {out}")


# --- text (Locl) ----------------------------------------------------------------------------

def _languages(container, lang):
    """[(index path, Locl chunk, decoded strings)] for LANG: a language name, or 'all'."""
    found = [(p, c) for p, c, _ in walk(container) if c.tag == "Locl"]
    if not found:
        raise CliError("No text in this file - the game's text is in core.lvl.")
    if lang.lower() != "all":
        found = [(p, c) for p, c in found if chunk_name(c).lower() == lang.lower()]
        if not found:
            raise CliError(f"No language '{lang}'. `text ls FILE` lists them; 'all' means every one.")
    try:
        return [(p, c, core.decode_locl(c.payload)) for p, c in found]
    except (core.LoclError, struct.error, ValueError) as exc:
        raise CliError(f"Can't read the text: {exc}")


def cmd_text_ls(args):
    container = load(args.file)
    if not args.lang:
        rows = [{"ref": p, "language": s.language, "strings": len(s.entries)} for p, _, s in _languages(container, "all")]
        emit(args, rows, "\n".join(f"{r['ref']:>8}  {r['language']:12s} {r['strings']:>5} strings" for r in rows))
        return
    langs = _languages(container, args.lang)
    out, lines = [], []
    for p, _, s in langs:
        for h, t in s.entries:
            key = core.key_name(h)
            if args.find and args.find.lower() not in t.lower():
                continue
            if args.key and args.key.lower() not in key.lower():
                continue
            out.append({"language": s.language, "key": key, "text": t})
            lines.append(f"  {s.language + '  ' if len(langs) > 1 else ''}{key:44s} {t!r}")
    emit(args, out, "\n".join(lines) or "(nothing matches)")


def cmd_text_set(args):
    container = load(args.file)
    report = []
    for _, chunk, s in _languages(container, args.lang):
        for pair in args.pairs:
            key, eq, text = pair.partition("=")
            if not eq or not key:
                raise CliError(f"'{pair}' isn't KEY=TEXT.")
            old = s.set(key, text.replace("\\n", "\n"))
            report.append(f"{s.language} {core.key_name(core.key_hash(key))}: {old!r} -> {text!r}" if old is not None
                          else f"{s.language} {key}: added {text!r}")
        for key in args.remove or []:
            if not s.remove(key):
                raise CliError(f"{key} isn't in {s.language}.")
            report.append(f"{s.language} {key}: removed")
        chunk.set_payload(core.encode_locl(s))
    if not report:
        raise CliError("Nothing to do - give KEY=TEXT pairs or --remove KEY.")
    out = save(container, args.out, args)
    emit(args, {"changes": report, "out": out}, "\n".join(report) + f"\nwrote {out}")


def cmd_text_export(args):
    container = load(args.file)
    langs = _languages(container, args.lang)
    if len(langs) > 1:
        raise CliError("Export one language at a time (english, french, ...).")
    _, _, s = langs[0]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(f"# {s.language}: key, a tab, the text (\\n is a line break)\n" + core.locl_to_text(s),
                              encoding="utf-8")
    emit(args, {"language": s.language, "strings": len(s.entries), "out": args.out},
         f"exported {len(s.entries)} strings\nwrote {args.out}")


def cmd_text_import(args):
    container = load(args.file)
    try:
        rows = core.locl_from_text(Path(args.source).read_text(encoding="utf-8-sig"))
    except (OSError, core.LoclError) as exc:
        raise CliError(f"Can't read {args.source}: {exc}")
    report = []
    for _, chunk, s in _languages(container, args.lang):
        changed = added = 0
        seen: dict = {}
        for h, text in rows:
            seen[h] = seen.get(h, -1) + 1  # a key listed twice sets its second copy
            old = s.set(f"0x{h:08x}", text, occurrence=seen[h])
            if old is None:
                added += 1
            elif old != text:
                changed += 1
        chunk.set_payload(core.encode_locl(s))
        report.append(f"{s.language}: {changed} changed, {added} added")
    out = save(container, args.out, args)
    emit(args, {"changes": report, "out": out}, "\n".join(report) + f"\nwrote {out}")


# --- mod loader -----------------------------------------------------------------------------

def _loader():
    import Loader
    cfg = Loader.load_config()
    game = cfg.get("game_dir") or (str(Loader.find_game_dir() or ""))
    if not game:
        raise CliError("The game folder isn't set - open the Mod Loader once, or pass --game.")
    return Loader, cfg, game


def _plan_rows(Loader, game, p):
    base = Loader.data_dir(game)
    rel = lambda t: str(Path(t).relative_to(base))
    return {"mod": p.mod,
            "install": [{"file": Path(s).name, "to": rel(t), "guessed": a} for s, t, a in p.copies],
            "restore": [rel(t) for _, t in p.restores],
            "skipped": [str(s) for s in p.unmatched],
            "backed_up_first": [rel(t) for t in p.missing_original]}


def cmd_mods(args):
    Loader, cfg, game = _loader()
    game = args.game or game
    mods_dir, orig = cfg.get("mods_dir"), cfg.get("originals_dir") or None
    if args.action == "status":
        state = Loader.load_state(game)
        data = {"game_dir": game, "mods_dir": mods_dir, "originals_dir": orig,
                "installed": state.get("mod"), "files": list(state.get("files", {}))}
        emit(args, data, f"installed: {data['installed'] or 'original game'}\nfiles: {', '.join(data['files']) or '-'}\n"
                         f"game: {game}\nmods: {mods_dir}\noriginals: {orig}")
    elif args.action == "ls":
        current = Loader.load_state(game).get("mod")
        rows = []
        for name in Loader.list_mods(mods_dir, orig):
            files = [str(p.relative_to(Path(mods_dir) / name)) for p in sorted((Path(mods_dir) / name).rglob("*"))
                     if p.is_file()]
            rows.append({"mod": name, "installed": name == current, "files": files})
        emit(args, rows, "\n".join(f"{'>' if r['installed'] else ' '} {r['mod']}  ({', '.join(r['files'])})"
                                   for r in rows) or "(no mods)")
    elif args.action in ("plan", "install"):
        mod = None if args.mod in (None, "", "original", Loader.ORIGINAL_GAME) else args.mod
        if mod and mod not in Loader.list_mods(mods_dir, orig):
            raise CliError(f"No mod '{mod}' in {mods_dir}. `mods ls` lists them.")
        p = Loader.plan(game, mods_dir, orig, mod)
        rows = _plan_rows(Loader, game, p)
        if args.action == "plan":
            emit(args, rows, _plan_text(rows))
            return
        result = Loader.apply(game, p, orig)
        if args.launch:
            Loader.launch(game)
        rows["applied"] = result
        rows["launched"] = bool(args.launch)
        note = _console_after_launch(args) if args.launch else ""
        emit(args, rows, _plan_text(rows) + f"\ninstalled {p.mod}" + (" and launched the game" if args.launch else "")
             + note)
    elif args.action == "launch":
        Loader.launch(game)
        emit(args, {"launched": True}, "launched the game" + _console_after_launch(args))
    elif args.action == "debugmenu":
        if not mods_dir or not orig:
            raise CliError("Set the Mods folder and Original files in the Mod Loader first.")
        out = Path(mods_dir) / core.DEBUG_MENU_MOD
        if out.exists() and not args.force:
            raise CliError(f"{out} already exists - add --force to build it again.")
        try:
            files = core.make_debug_menu(orig, out)
        except ValueError as exc:
            raise CliError(str(exc))
        emit(args, {"mod": out.name, "files": files},
             f"made {out}: {', '.join(files)}\ninstall it with: mods install \"{out.name}\" --launch --console")


def _console_after_launch(args) -> str:
    if not getattr(args, "console", False):
        return ""
    import Loader
    try:
        return "\nconsole patch: " + Loader.patch_console(wait=120)
    except Loader.PatchError as exc:
        raise CliError(f"The game started, but the console patch failed: {exc}")


def _game():
    import Loader
    try:
        return Loader, Loader.Game.attach()
    except Loader.GameMemoryError as exc:
        raise CliError(str(exc))


def cmd_mem(args):
    M, g = _game()
    try:
        addr = g.resolve(args.address)
        if args.action == "read":
            if args.type:
                v = M.mem_unpack(args.type, g.read(addr, M.MEM_TYPES[args.type][1]))
                emit(args, {"address": g.describe(addr), "type": args.type, "value": v}, f"{g.describe(addr)} = {v}")
                return
            data = g.read(addr, args.count)
            lines = [f"{g.describe(addr + i):>22}  " + " ".join(f"{b:02X}" for b in data[i:i + 16])
                     for i in range(0, len(data), 16)]
            emit(args, {"address": g.describe(addr), "hex": data.hex()}, "\n".join(lines))
        else:
            if not args.type or args.value is None:
                raise CliError("write needs --type and a value, e.g. mem write +4E6D53 1 --type byte")
            old = g.read(addr, M.MEM_TYPES[args.type][1])
            g.write(addr, M.mem_pack(args.type, args.value))
            emit(args, {"address": g.describe(addr), "old": old.hex(), "new": args.value},
                 f"{g.describe(addr)}: {M.mem_unpack(args.type, old)} -> {args.value}")
    except (M.GameMemoryError, ValueError) as exc:
        raise CliError(str(exc))
    finally:
        g.close()


def cmd_cheats(args):
    import time
    import Loader
    cheats = Loader.CheatFile(core.app_dir()).load()
    if args.action == "ls":
        emit(args, [{"name": c.name, "enabled": c.enabled, "note": c.note, "code": c.text()} for c in cheats],
             "\n".join(f"{'[on] ' if c.enabled else '     '}{c.name}" + (f"  - {c.note}" if c.note else "")
                       for c in cheats))
        return
    wanted = {n.lower() for n in args.names}
    chosen = [c for c in cheats if c.name.lower() in wanted] if wanted else [c for c in cheats if c.enabled]
    missing = wanted - {c.name.lower() for c in chosen}
    if missing:
        raise CliError(f"No cheat called {', '.join(sorted(missing))}. `cheats ls` lists them.")
    if not chosen:
        raise CliError("No cheats chosen - name some, or switch them on in the cheats window.")
    for c in chosen:
        c.enabled = True
    runner = Loader.CheatRunner()
    runner.set_cheats(chosen)
    runner.start()
    print(f"applying {', '.join(c.name for c in chosen)} - Ctrl+C to stop (the changes are undone)", flush=True)
    try:
        last = ""
        while True:
            time.sleep(0.5)
            if runner.status != last:
                last = runner.status
                print(last, flush=True)
    except KeyboardInterrupt:
        for c in chosen:
            c.enabled = False
        runner.set_cheats(chosen)
        time.sleep(0.4)
        runner.stop()
        print("stopped")


def cmd_patch(args):
    import Loader
    try:
        result = Loader.patch_console(wait=args.wait)
    except Loader.PatchError as exc:
        raise CliError(str(exc))
    emit(args, {"patch": "console", "result": result}, f"console patch: {result}")


def _plan_text(rows) -> str:
    lines = [f"install {r['file']} -> {r['to']}" + ("  (picked by folder/size)" if r["guessed"] else "")
             for r in rows["install"]]
    lines += [f"restore {t}" for t in rows["restore"]]
    lines += [f"skip {s} (no such game file)" for s in rows["skipped"]]
    return "\n".join(lines) or "nothing to change"


# --- argument parsing -----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bf1", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_text):
        s = sub.add_parser(name, help=help_text, description=help_text)
        s.set_defaults(fn=fn)
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
        return s

    def writes(s, required=True):
        s.add_argument("-o", "--out", required=required, help="where to write the edited file (the input is never changed)")
        s.add_argument("--force", action="store_true", help="overwrite --out if it exists")

    s = add("info", cmd_info, "Summary of a .lvl: size, nested levels, chunk counts by tag")
    s.add_argument("file")
    s = add("ls", cmd_ls, "List chunks with their REF (index path), tag, name and size")
    s.add_argument("file")
    s.add_argument("--tag", help="only this tag (modl, tex_, entc, wpnc, ordc, expc, lvl_...)")
    s.add_argument("--name", help="only names containing this")
    s.add_argument("--top", action="store_true", help="top level only, not inside nested levels")
    s = add("show", cmd_show, "Details of one chunk: class properties, texture formats, model stats and fire points")
    s.add_argument("file"); s.add_argument("ref")
    s = add("classes", cmd_classes, "Every unit/weapon/ordnance/explosion class with its model")
    s.add_argument("file")
    s = add("set", cmd_set, "Set or remove properties of a class chunk")
    s.add_argument("file"); s.add_argument("ref", help="the class, e.g. entc:rep_inf_clone_trooper")
    s.add_argument("assignments", nargs="*", metavar="KEY=VALUE")
    s.add_argument("--remove", nargs="*", metavar="KEY", help="properties to delete")
    s.add_argument("--all", action="store_true", help="set every copy of a repeated key")
    writes(s)
    s = add("export", cmd_export, "Save a chunk as a usable file (texture -> .png/.dds, class -> .odf, script -> .lua, animations -> .json, else raw)")
    s.add_argument("file"); s.add_argument("ref"); s.add_argument("out", help="output file; its extension picks the format")
    s = add("replace", cmd_replace, "Replace a chunk from a file (.png/.dds texture, .odf class, .lua or .luac script, .json animations, raw bytes)")
    s.add_argument("file"); s.add_argument("ref"); s.add_argument("source")
    s.add_argument("--all", action="store_true", help="replace every chunk the name matches (e.g. a texture "
                                                      "copied into several nested levels)")
    writes(s)
    s = add("apply", cmd_apply, "Apply a JSON recipe of property rules to many classes and levels at once "
                                "(see docs/CLI.md)")
    s.add_argument("recipe", help="the recipe .json")
    s.add_argument("files", nargs="+", help=".lvl files to change (they're left as they are)")
    s.add_argument("--out-dir", required=True, help="where the changed levels go, same file names")
    s.add_argument("--force", action="store_true", help="overwrite files already in --out-dir")
    s = add("glbsplit", lambda a: glb_split_main(a.rest), "Split, merge or combine GLB meshes (glbsplit -h for its options)")
    s.add_argument("rest", nargs=argparse.REMAINDER)
    s = add("randomize", lambda a: sys.exit(randomizer_main(a.rest)), "Roll the randomizer mods into the mods folder (randomize -h for its options)")
    s.add_argument("rest", nargs=argparse.REMAINDER)
    s = add("hash", cmd_hash, "The engine hash of names (to identify unknown property hashes)")
    s.add_argument("names", nargs="+")
    s = add("char", cmd_char, "Import a .glb as a character or vehicle model (same as the import window)")
    s.add_argument("file"); s.add_argument("model", help="the modl to replace, e.g. rep_inf_trooper")
    s.add_argument("glb")
    s.add_argument("--list-textures", action="store_true", help="just list the .glb's texture groups")
    s.add_argument("--texture", help="texture chunk to pack the .glb's textures into (default: the model's own)")
    s.add_argument("--keep-texture", action="store_true", help="leave textures alone, use the .glb's UVs as-is")
    s.add_argument("--axis", help="fit along: Height, Length, Width or 'Largest side'")
    s.add_argument("--size", type=float, help="size along --axis in game units (default: the original's)")
    s.add_argument("--rotate", help="turns in degrees X,Y,Z (red, green, blue sliders), e.g. 180,0,0 flips upside down")
    s.add_argument("--arm", type=float, help="arm angle below T-pose in degrees (default: estimated)")
    s.add_argument("--budget", type=int, help="vertex budget (default: the .glb's full size)")
    s.add_argument("--lod-budget", type=int, help="LOD vertex budget (default: same as --budget)")
    s.add_argument("--no-lod", action="store_true", help="don't replace the LOD model")
    s.add_argument("--no-collision", action="store_true", help="don't rebuild collision")
    s.add_argument("--keep-shadow", action="store_true", help="keep the old model's shadow")
    s.add_argument("--hair", type=int, nargs="*", metavar="GROUP", help="texture groups that are hair (default: guessed)")
    s.add_argument("--delete-texture", type=int, nargs="*", metavar="GROUP", help="texture groups to leave out")
    s.add_argument("--preview", help="also save a preview .png of the result")
    writes(s, required=False)
    s = add("points", cmd_points, "List a model's fire points/hardpoints, or move them")
    s.add_argument("file"); s.add_argument("model")
    s.add_argument("--move", nargs="*", metavar="BONE=X,Y,Z", help="new bind-pose positions")
    s.add_argument("--snap", action="store_true", help="snap fire points that float off the model onto it")
    writes(s, required=False)

    media = sub.add_parser("media", help="Movies (.mvs) and sound banks (.bnk, core.lvl)")
    msub = media.add_subparsers(dest="media_command", required=True)
    for name, fn, help_text in (("ls", cmd_media_ls, "List the movies or sounds"),
                                ("extract", cmd_media_extract, "Extract movies (.bik) or sounds (.wav)"),
                                ("replace", cmd_media_replace, "Replace sounds with your own .wav files")):
        s = msub.add_parser(name, help=help_text, description=help_text)
        s.set_defaults(fn=fn)
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        s.add_argument("file")
        if name == "extract":
            s.add_argument("outdir")
            s.add_argument("--only", nargs="*", metavar="NAME", help="just these (names or hashes)")
        if name == "replace":
            s.add_argument("pairs", nargs="+", metavar="SAMPLE=AUDIOFILE")
            writes(s)

    anim = sub.add_parser("anim", help="Animations (zaa_ banks): list, export, import, speed, swap")
    asub = anim.add_subparsers(dest="anim_command", required=True)

    def anim_cmd(name, fn, help_text, ref_optional=False):
        s = asub.add_parser(name, help=help_text, description=help_text)
        s.set_defaults(fn=fn)
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        s.add_argument("file")
        s.add_argument("ref", nargs="?" if ref_optional else None,
                       help="the animation bank (zaa_ chunk), e.g. human_animation_0")
        return s

    anim_cmd("ls", cmd_anim_ls, "List the animation banks, or the animations in one", ref_optional=True)
    s = anim_cmd("export", cmd_anim_export, "Save animations as .json (per joint, per frame: rotation and position)")
    s.add_argument("out")
    s.add_argument("--only", nargs="*", metavar="NAME", help="just these animations")
    s = anim_cmd("import", cmd_anim_import, "Put animations from a .json back in: same names replace, new names are added")
    s.add_argument("source")
    s.add_argument("--only", nargs="*", metavar="NAME", help="just these animations from the .json")
    s.add_argument("--as", dest="as_name", metavar="NAME", help="import the one animation under this name")
    writes(s)
    s = anim_cmd("edit", cmd_anim_edit, "Change animations: speed, motion strength, reverse, rename or delete")
    s.add_argument("names", nargs="+", metavar="NAME", help="animations to change (names or 0x hashes)")
    s.add_argument("--speed", type=float, help="2 = twice as fast, 0.5 = half speed")
    s.add_argument("--motion", type=float, help="1.5 = 50%% bigger movements, 0.5 = half as big")
    s.add_argument("--reverse", action="store_true", help="play backwards")
    s.add_argument("--rename", metavar="NEW", help="give it another name (the game picks animations by name)")
    s.add_argument("--delete", action="store_true", help="remove them")
    writes(s)
    s = anim_cmd("copy", cmd_anim_copy, "Make animations play another animation's motion")
    s.add_argument("pairs", nargs="+", metavar="TARGET=SOURCE",
                   help="TARGET gets SOURCE's motion (a new TARGET name is added)")
    s.add_argument("--from", dest="source_file", metavar="FILE[::BANK]",
                   help="take SOURCE from another level (and bank) - default: the same bank")
    writes(s)

    text = sub.add_parser("text", help="The game's text (Locl, in core.lvl): unit names, menus, messages")
    tsub = text.add_subparsers(dest="text_command", required=True)

    def text_cmd(name, fn, help_text, lang_optional=False):
        s = tsub.add_parser(name, help=help_text, description=help_text)
        s.set_defaults(fn=fn)
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        s.add_argument("file", help="core.lvl")
        s.add_argument("lang", nargs="?" if lang_optional else None,
                       help="english, uk_english, french, german, italian, spanish - or all")
        return s

    s = text_cmd("ls", cmd_text_ls, "List the languages, or one language's strings", lang_optional=True)
    s.add_argument("--find", metavar="TEXT", help="only strings containing this text")
    s.add_argument("--key", metavar="PART", help="only keys containing this")
    s = text_cmd("set", cmd_text_set, "Change, add or remove strings")
    s.add_argument("pairs", nargs="*", metavar="KEY=TEXT", help="a key (or 0x hash) and its new text; \\n is a line break")
    s.add_argument("--remove", nargs="*", metavar="KEY", help="keys to delete")
    writes(s)
    s = text_cmd("export", cmd_text_export, "Save a language as a .txt file: key, tab, text on each line")
    s.add_argument("out")
    s = text_cmd("import", cmd_text_import, "Put an edited .txt back: listed keys are changed or added")
    s.add_argument("source")
    writes(s)

    s = add("mem", cmd_mem, "Read or write the running game's memory (+4E6D53 = Battlefront.exe+4E6D53)")
    s.add_argument("action", choices=("read", "write"))
    s.add_argument("address", help="+OFFSET from Battlefront.exe, 0xABSOLUTE, or [+OFFSET]+N to follow a pointer")
    s.add_argument("value", nargs="?", help="for write: the value")
    s.add_argument("--type", choices=("byte", "2 bytes", "4 bytes", "float", "double"), help="value type")
    s.add_argument("--count", type=int, default=64, help="for read without --type: bytes to show (default 64)")
    s = add("cheats", cmd_cheats, "Gecko-style cheat codes (cheats.txt): list them, or apply some until Ctrl+C")
    s.add_argument("action", choices=("ls", "run"))
    s.add_argument("names", nargs="*", help="for run: cheats by name (default: the ones switched on)")
    s = add("patch", cmd_patch, "Patch the running game: `console` makes the fake console's commands work "
                                "(Battlefront.exe 1.3, single player; nothing on disk changes)")
    s.add_argument("what", choices=("console",))
    s.add_argument("--wait", type=float, default=120, help="seconds to wait for the game to start (default 120)")

    mods = sub.add_parser("mods", help="The Mod Loader: status, list, plan, install, launch, debugmenu")
    mods.set_defaults(fn=cmd_mods)
    mods.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    mods.add_argument("action", choices=("status", "ls", "plan", "install", "launch", "debugmenu"),
                      help="debugmenu: build the Debug Menu mod (Fake Console + Free Camera) from Original files")
    mods.add_argument("--force", action="store_true", help="with debugmenu: build it again if it's already there")
    mods.add_argument("mod", nargs="?", help="mod folder name ('original' for the original game)")
    mods.add_argument("--launch", action="store_true", help="with install: start the game afterwards")
    mods.add_argument("--console", action="store_true",
                      help="with launch: patch the running game so the fake console's commands work")
    mods.add_argument("--game", help="game folder (default: the Mod Loader's setting)")
    return p


def run(argv: list) -> int:
    """One command, as from the command line; returns the exit code."""
    tools = {"glbsplit": glb_split_main, "randomize": randomizer_main}   # their own options
    if argv and argv[0] in tools:
        return tools[argv[0]](argv[1:]) or 0
    args = build_parser().parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    try:
        args.fn(args)
        return 0
    except CliError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(paint("error: ", RED, BOLD) + paint(str(exc), RED), file=sys.stderr)
        return 1


# --- the interactive shell (`bf1` with no arguments) -----------------------------------------

BANNER = r"""
 ██████╗ ███████╗ ██╗      ██╗██████╗ ███████╗
 ██╔══██╗██╔════╝███║      ██║██╔══██╗██╔════╝
 ██████╔╝█████╗  ╚██║█████╗██║██║  ██║█████╗
 ██╔══██╗██╔══╝   ██║╚════╝██║██║  ██║██╔══╝
 ██████╔╝██║      ██║      ██║██████╔╝███████╗
 ╚═════╝ ╚═╝      ╚═╝      ╚═╝╚═════╝ ╚══════╝"""

# commands whose first argument is the level file - `open` fills it in
FILE_FIRST = {"info", "ls", "show", "classes", "set", "export", "replace", "char", "points"}

HELP_GROUPS = [
    ("Look inside a level", [
        ("open FILE", "pick the level the commands below work on"),
        ("info", "size, nested levels, chunk counts"),
        ("ls [--tag modl] [--name rifle]", "chunks with their REF, tag, name and size"),
        ("show REF", "one chunk: properties, texture, model, fire points"),
        ("classes", "every unit, weapon, ordnance and explosion class"),
    ]),
    ("Edit (writes a new file with -o, the original is never changed)", [
        ("set REF KEY=VALUE... -o OUT", "change class properties"),
        ("export REF OUT", "texture -> .png, class -> .odf, script -> .lua"),
        ("replace REF FILE -o OUT", "put an edited file back"),
        ("apply RECIPE FILES... --out-dir DIR", "a JSON recipe of changes over many levels"),
        ("char MODEL GLB -o OUT", "import a .glb character or vehicle"),
        ("points MODEL [--snap] [-o OUT]", "list or move fire points"),
    ]),
    ("Movies and sounds", [
        ("media ls FILE", "the movies in a .mvs, or the sounds in a .bnk / core.lvl"),
        ("media extract FILE DIR", "save them as .bik / .wav"),
        ("media replace FILE SAMPLE=SOUND.wav -o OUT", "put your own sounds in"),
    ]),
    ("Animations", [
        ("anim ls FILE [BANK]", "the animation banks, or the animations in one"),
        ("anim export FILE BANK OUT.json", "save animations as editable .json"),
        ("anim import FILE BANK IN.json -o OUT", "put them back (or add new ones)"),
        ("anim edit FILE BANK NAME --speed 2 -o OUT", "speed, --motion, --reverse, --rename, --delete"),
        ("anim copy FILE BANK TARGET=SOURCE -o OUT", "make one animation play another's motion"),
    ]),
    ("Text (core.lvl)", [
        ("text ls FILE [LANG] [--find TEXT]", "the languages, or the strings in one"),
        ("text set FILE LANG KEY=TEXT -o OUT", "change a string; LANG all for every language"),
        ("text export FILE LANG OUT.txt", "save a language as a .txt to edit"),
        ("text import FILE LANG IN.txt -o OUT", "put it back"),
    ]),
    ("Running game (Battlefront.exe 1.3)", [
        ("patch console", "make the pause menu's fake console work"),
        ("cheats ls | cheats run NAME...", "Gecko-style cheat codes from cheats.txt"),
        ("mem read +4E6D53 [--type byte]", "read the game's memory"),
        ("mem write +4E6D53 1 --type byte", "write it"),
    ]),
    ("Mod Loader", [
        ("mods status | ls", "what's installed, and your mods"),
        ("mods plan MOD", "what installing it would change"),
        ("mods install MOD [--launch]", "install it (and start the game)"),
        ("mods debugmenu", "make the Debug Menu mod: Fake Console + Free Camera"),
        ("randomize [--play wild]", "roll the randomizer mods"),
    ]),
    ("Shell", [
        ("help [COMMAND]", "this list, or every option of one command"),
        ("hash NAME...", "the engine hash of names"),
        ("clear / exit", ""),
    ]),
]


def shell_help(command: str | None = None) -> None:
    if command:
        parser = build_parser()
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        if command not in sub.choices:
            print(paint(f"No command '{command}'. Type help for the list.", RED))
            return
        sub.choices[command].print_help()
        return
    width = max(len(cmd) for _, rows in HELP_GROUPS for cmd, _ in rows)
    for title, rows in HELP_GROUPS:
        print("\n" + paint(title, YELLOW, BOLD))
        for cmd, what in rows:
            print("  " + paint(cmd.ljust(width), CYAN) + "  " + paint(what, GREY))
    print("\n" + paint("REF", BOLD) + paint(" is a chunk: an index path like 9/34 (from ls), tag:name like "
                                            "entc:rep_inf_clone_trooper, or just a name.", GREY))
    print(paint("Example: ", BOLD) + paint("open rep.lvl", CYAN) + paint("  then  ", GREY)
          + paint("set entc:rep_inf_clone_trooper MaxHealth=600 -o mod/rep.lvl", CYAN))


def split_line(line: str) -> list:
    """Shell words; quotes group words, and backslashes stay as they are (Windows paths)."""
    import shlex
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    return list(lexer)


def run_shell() -> int:
    enable_colors()
    current = None
    parser = build_parser()
    known = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
    print(paint(BANNER, BLUE, BOLD))
    print(paint(f"  v{VERSION}", GREY) + paint("  -  Star Wars: Battlefront (2004) modding from the command line", GREY))
    print("\n  " + paint("open rep.lvl", CYAN) + paint(" to start,  ", GREY) + paint("help", CYAN)
          + paint(" for every command,  ", GREY) + paint("exit", CYAN) + paint(" to quit.\n", GREY))
    while True:
        where = paint(f"[{Path(current).name}]", MAGENTA) if current else ""
        try:
            line = input(paint("bf1", BLUE, BOLD) + where + paint("> ", BLUE, BOLD)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        try:
            words = split_line(line)
        except ValueError as exc:  # an unclosed quote
            print(paint(f"error: {exc}", RED))
            continue
        if words and words[0].lower() in ("bf1", "bf1.py", "bf1_cli.py"):
            words = words[1:]  # someone typed the whole command line
        if not words:
            continue
        command = words[0].lower()
        if command in ("exit", "quit", "q"):
            return 0
        if command in ("help", "?", "-h", "--help"):
            shell_help(words[1] if len(words) > 1 else None)
            continue
        if command in ("clear", "cls"):
            print("\033[2J\033[H" if COLOR else "\n" * 40, end="")
            continue
        if command == "open":
            if len(words) < 2:
                print(paint("open which file? e.g. open rep.lvl", RED))
                continue
            path = Path(" ".join(words[1:])).expanduser()
            if not path.is_file():
                print(paint(f"error: no such file: {path}", RED))
                continue
            current = str(path)
            run(["info", current])
            continue
        if command not in known:
            print(paint(f"Unknown command '{command}'.", RED) + paint(" Type ", GREY) + paint("help", CYAN)
                  + paint(" for the list.", GREY))
            continue
        args = [command] + words[1:]
        # with a file open, commands that take one can leave it out
        if current and command in FILE_FIRST and (len(args) < 2 or not Path(args[1]).is_file()
                                                   or args[1].startswith("-")):
            args.insert(1, current)
        elif current and command in ("anim", "text") and len(args) >= 2 and not args[1].startswith("-") and (
                len(args) < 3 or not Path(args[2]).is_file() or args[2].startswith("-")):
            args.insert(2, current)
        try:
            run(args)
        except SystemExit:  # argparse already printed what was wrong (or --help)
            pass
        except KeyboardInterrupt:
            print(paint("\ncancelled", GREY))
        except Exception as exc:  # keep the shell alive whatever happens
            print(paint(f"error: {type(exc).__name__}: {exc}", RED))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):  # never die printing a name the console can't show
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if not argv and sys.stdin.isatty():
        return run_shell()
    if argv in (["--version"], ["-V"]):
        print(f"bf1 {VERSION}")
        return 0
    if "--json" not in argv:
        enable_colors()
    return run(argv)


# ==================================================================================================
# glb_split_primitives - merged in from glb_split_primitives.py
# ==================================================================================================
# Split every mesh primitive in a GLB into N primitives that render identically.
#
# The vertex attributes are left untouched and shared; only the index range is
# partitioned (on triangle boundaries), so the visual result is unchanged.
#
# Usage:
#     python bf1_cli.py glbsplit input.glb -n 4 -o output.glb


GLB_MAGIC = 0x46546C67
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942

COMPONENT_SIZE = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
# vertices per element for each primitive mode
MODE_GROUP = {0: 1, 1: 2, 3: 1, 4: 3, 5: 1, 6: 1}


def read_glb(path):
    with open(path, "rb") as f:
        data = f.read()
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC:
        sys.exit(f"{path}: not a GLB file")
    if version != 2:
        sys.exit(f"{path}: unsupported glTF version {version}")
    gltf, binary, off = None, b"", 12
    while off < min(length, len(data)):
        clen, ctype = struct.unpack_from("<II", data, off)
        chunk = data[off + 8: off + 8 + clen]
        if ctype == CHUNK_JSON:
            gltf = json.loads(chunk.decode("utf-8"))
        elif ctype == CHUNK_BIN:
            binary = chunk
        off += 8 + clen
    if gltf is None:
        sys.exit(f"{path}: no JSON chunk found")
    return gltf, binary


def write_glb(path, gltf, binary):
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    bn = binary + b"\x00" * (-len(binary) % 4)
    total = 12 + 8 + len(js) + (8 + len(bn) if bn else 0)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", GLB_MAGIC, 2, total))
        f.write(struct.pack("<II", len(js), CHUNK_JSON))
        f.write(js)
        if bn:
            f.write(struct.pack("<II", len(bn), CHUNK_BIN))
            f.write(bn)
    return total


def split_counts(total_groups, n):
    """Distribute total_groups over n parts as evenly as possible (no empties)."""
    n = min(n, total_groups)
    base, extra = divmod(total_groups, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def split_primitive(gltf, prim, n):
    """Return a list of n primitives covering the same geometry."""
    group = MODE_GROUP.get(prim.get("mode", 4), 3)

    if "indices" in prim:
        acc = gltf["accessors"][prim["indices"]]
        total = acc["count"]
        stride = COMPONENT_SIZE[acc["componentType"]]
        base = acc.get("byteOffset", 0)
    else:
        # non-indexed: partition the attribute accessors' element range instead
        first_attr = next(iter(prim["attributes"].values()))
        total = gltf["accessors"][first_attr]["count"]

    groups = total // group
    if groups < 2:
        return [prim]

    parts, cursor, out = split_counts(groups, n), 0, []
    for count_groups in parts:
        count = count_groups * group
        new_prim = dict(prim)
        if "indices" in prim:
            new_acc = dict(gltf["accessors"][prim["indices"]])
            new_acc["count"] = count
            new_acc["byteOffset"] = base + cursor * stride
            new_acc.pop("min", None)
            new_acc.pop("max", None)
            gltf["accessors"].append(new_acc)
            new_prim["indices"] = len(gltf["accessors"]) - 1
        else:
            attrs = {}
            for name, ai in prim["attributes"].items():
                src = gltf["accessors"][ai]
                bv = gltf["bufferViews"][src["bufferView"]]
                elem = COMPONENT_SIZE[src["componentType"]] * {
                    "SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
                    "MAT2": 4, "MAT3": 9, "MAT4": 16,
                }[src["type"]]
                step = bv.get("byteStride", elem)
                new_acc = dict(src)
                new_acc["count"] = count
                new_acc["byteOffset"] = src.get("byteOffset", 0) + cursor * step
                new_acc.pop("min", None)
                new_acc.pop("max", None)
                gltf["accessors"].append(new_acc)
                attrs[name] = len(gltf["accessors"]) - 1
            new_prim["attributes"] = attrs
        out.append(new_prim)
        cursor += count
    return out


TYPE_COMPS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
FMT = {5120: "b", 5121: "B", 5122: "h", 5123: "H", 5125: "I", 5126: "f"}


def read_accessor(gltf, binary, idx):
    """Return (list of tuples, componentType, type) for an accessor."""
    acc = gltf["accessors"][idx]
    comps = TYPE_COMPS[acc["type"]]
    csize = COMPONENT_SIZE[acc["componentType"]]
    fmt = FMT[acc["componentType"]]
    elem = comps * csize
    out = []
    if "bufferView" not in acc:
        return [tuple([0] * comps)] * acc["count"], acc["componentType"], acc["type"]
    bv = gltf["bufferViews"][acc["bufferView"]]
    stride = bv.get("byteStride", elem)
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    for i in range(acc["count"]):
        out.append(struct.unpack_from("<" + fmt * comps, binary, base + i * stride))
    return out, acc["componentType"], acc["type"]


def append_accessor(gltf, blob, values, component_type, type_name, target=None, minmax=False):
    comps = TYPE_COMPS[type_name]
    fmt = "<" + FMT[component_type] * comps
    data = b"".join(struct.pack(fmt, *v) for v in values)
    data += b"\x00" * (-len(data) % 4)
    offset = len(blob[0])
    blob[0] += data
    bv = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
    if target:
        bv["target"] = target
    gltf["bufferViews"].append(bv)
    acc = {
        "bufferView": len(gltf["bufferViews"]) - 1,
        "componentType": component_type,
        "count": len(values),
        "type": type_name,
    }
    if minmax and values:
        acc["min"] = [min(v[c] for v in values) for c in range(comps)]
        acc["max"] = [max(v[c] for v in values) for c in range(comps)]
    gltf["accessors"].append(acc)
    return len(gltf["accessors"]) - 1


def merge_mesh(gltf, binary, mesh):
    """Collapse all of a mesh's primitives into a single primitive."""
    prims = mesh["primitives"]
    if len(prims) < 2:
        return binary
    modes = {p.get("mode", 4) for p in prims}
    if len(modes) > 1:
        sys.exit("cannot merge primitives with different modes")
    mats = {p.get("material") for p in prims}
    if len(mats) > 1:
        print(f"  warning: {len(mats)} materials merged into one ({sorted(str(m) for m in mats)})")

    names = [n for n in prims[0]["attributes"] if all(n in p["attributes"] for p in prims)]
    dropped = set().union(*[set(p["attributes"]) for p in prims]) - set(names)
    if dropped:
        print(f"  warning: dropping attributes missing from some primitives: {sorted(dropped)}")

    merged = {n: [] for n in names}
    meta = {}
    indices, base = [], 0
    for p in prims:
        count = None
        for n in names:
            vals, ctype, tname = read_accessor(gltf, binary, p["attributes"][n])
            merged[n].extend(vals)
            meta[n] = (ctype, tname)
            count = len(vals)
        if "indices" in p:
            idx, _, _ = read_accessor(gltf, binary, p["indices"])
            indices.extend(i[0] + base for i in idx)
        else:
            indices.extend(range(base, base + count))
        base += count

    blob = [bytearray(binary)]
    attrs = {}
    for n in names:
        ctype, tname = meta[n]
        attrs[n] = append_accessor(gltf, blob, merged[n], ctype, tname,
                                   target=34962, minmax=(n == "POSITION"))
    ctype = 5125 if base > 65535 else 5123
    iacc = append_accessor(gltf, blob, [(i,) for i in indices], ctype, "SCALAR", target=34963)

    new_prim = {"attributes": attrs, "indices": iacc, "mode": prims[0].get("mode", 4)}
    if prims[0].get("material") is not None:
        new_prim["material"] = prims[0]["material"]
    mesh["primitives"] = [new_prim]
    out = bytes(blob[0])
    gltf["buffers"][0]["byteLength"] = len(out)
    return out


def node_matrix(n):
    if "matrix" in n:
        return list(n["matrix"])
    tx, ty, tz = n.get("translation", [0, 0, 0])
    x, y, z, w = n.get("rotation", [0, 0, 0, 1])
    sx, sy, sz = n.get("scale", [1, 1, 1])
    m = [
        1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w), 0,
        2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w), 0,
        2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y), 0,
        tx, ty, tz, 1,
    ]
    for c, s in enumerate((sx, sy, sz)):
        for r in range(3):
            m[c * 4 + r] *= s
    return m


def mat_mul(a, b):
    """Column-major a*b (b applied first)."""
    out = [0.0] * 16
    for c in range(4):
        for r in range(4):
            out[c * 4 + r] = sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4))
    return out


def xform_point(m, v):
    return (m[0] * v[0] + m[4] * v[1] + m[8] * v[2] + m[12],
            m[1] * v[0] + m[5] * v[1] + m[9] * v[2] + m[13],
            m[2] * v[0] + m[6] * v[1] + m[10] * v[2] + m[14])


def xform_dir(m, v):
    r = (m[0] * v[0] + m[4] * v[1] + m[8] * v[2],
         m[1] * v[0] + m[5] * v[1] + m[9] * v[2],
         m[2] * v[0] + m[6] * v[1] + m[10] * v[2])
    ln = (r[0] ** 2 + r[1] ** 2 + r[2] ** 2) ** 0.5 or 1.0
    return (r[0] / ln, r[1] / ln, r[2] / ln)


def world_matrices(gltf):
    """Map node index -> world matrix."""
    nodes = gltf.get("nodes", [])
    child = {c for n in nodes for c in n.get("children", [])}
    out = {}

    def walk(i, parent):
        m = mat_mul(parent, node_matrix(nodes[i]))
        out[i] = m
        for c in nodes[i].get("children", []):
            walk(c, m)

    ident = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    for i in range(len(nodes)):
        if i not in child:
            walk(i, ident)
    return out


def combine_meshes(gltf, binary, bake_transforms=True):
    """Fold every mesh in the file into a single mesh on a single node.

    Each source primitive is kept (with its own material), but its vertices are
    baked into world space so the assembled model looks identical.
    """
    meshes = gltf.get("meshes", [])
    if len(meshes) < 2:
        return binary
    worlds = world_matrices(gltf)
    blob = [bytearray(binary)]
    new_prims = []

    for ni, node in enumerate(gltf.get("nodes", [])):
        if "mesh" not in node:
            continue
        m = worlds.get(ni) if bake_transforms else None
        for prim in meshes[node["mesh"]]["primitives"]:
            p = dict(prim)
            if m is not None:
                attrs = dict(prim["attributes"])
                pos, ct, tn = read_accessor(gltf, binary, attrs["POSITION"])
                attrs["POSITION"] = append_accessor(
                    gltf, blob, [xform_point(m, v) for v in pos], ct, tn,
                    target=34962, minmax=True)
                for key in ("NORMAL", "TANGENT"):
                    if key in attrs:
                        vals, ct2, tn2 = read_accessor(gltf, binary, attrs[key])
                        if key == "TANGENT":
                            new = [xform_dir(m, v[:3]) + (v[3],) for v in vals]
                        else:
                            new = [xform_dir(m, v) for v in vals]
                        attrs[key] = append_accessor(gltf, blob, new, ct2, tn2, target=34962)
                p["attributes"] = attrs
            new_prims.append(p)

    name = meshes[0].get("name", "combined")
    gltf["meshes"] = [{"name": name, "primitives": new_prims}]
    gltf["nodes"] = [{"name": name, "mesh": 0}]
    gltf["scenes"] = [{"name": (gltf.get("scenes") or [{}])[0].get("name", "Scene"), "nodes": [0]}]
    gltf["scene"] = 0
    for key in ("skins", "animations"):
        gltf.pop(key, None)
    out = bytes(blob[0])
    gltf["buffers"][0]["byteLength"] = len(out)
    print(f"combined {len(meshes)} meshes -> 1 mesh, {len(new_prims)} primitives")
    return out


def glb_split_main(argv=None):
    ap = argparse.ArgumentParser(prog="bf1 glbsplit", description="Split, merge or combine GLB meshes without changing appearance.")
    ap.add_argument("input")
    ap.add_argument("-n", "--count", type=int, default=2, help="primitives per source primitive (default 2)")
    ap.add_argument("--merge", action="store_true", help="collapse each mesh down to ONE primitive")
    ap.add_argument("--combine-meshes", action="store_true",
                    help="fold ALL meshes into a single mesh on a single node (world transforms baked in)")
    ap.add_argument("--no-bake", action="store_true",
                    help="with --combine-meshes: keep raw vertex data instead of baking node transforms")
    ap.add_argument("-o", "--output", help="output path (default: <input>_split.glb)")
    ap.add_argument("-m", "--mesh", type=int, action="append",
                    help="only split this mesh index (repeatable; default all)")
    args = ap.parse_args(argv)

    if args.count < 1:
        sys.exit("--count must be >= 1")
    suffix = "_merged.glb" if (args.merge or args.combine_meshes) else "_split.glb"
    out_path = args.output or args.input.rsplit(".", 1)[0] + suffix

    gltf, binary = read_glb(args.input)
    if args.combine_meshes:
        binary = combine_meshes(gltf, binary, bake_transforms=not args.no_bake)
    targets = set(args.mesh) if args.mesh else None

    for mi, mesh in enumerate(gltf.get("meshes", [])):
        if targets is not None and mi not in targets:
            continue
        before = len(mesh["primitives"])
        if args.merge:
            binary = merge_mesh(gltf, binary, mesh)
        elif args.combine_meshes or args.count == 1:
            pass  # combining only; no splitting requested
        else:
            new_prims = []
            for prim in mesh["primitives"]:
                new_prims.extend(split_primitive(gltf, prim, args.count))
            mesh["primitives"] = new_prims
        print(f"mesh {mi} ({mesh.get('name','')}): "
              f"{before} -> {len(mesh['primitives'])} primitives")

    size = write_glb(out_path, gltf, binary)
    print(f"wrote {out_path} ({size} bytes)")


# ==================================================================================================
# bf1_randomizer - merged in from bf1_randomizer.py
# ==================================================================================================
# bf1_cli.py randomize - rolls the four randomizer mods from your original side files
# and puts them in the Mod Loader's mods folder, ready to pick:
#
#   Randomizer - Safe        61 gameplay properties: health, speed, damage, blast
#                            radii, fire rate, reload, range, ammo, heat, lock-on...
#   Randomizer - Wild        every named property, except camera, physics springs,
#                            collision, and counts/type switches read at map load
#   Randomizer - Chaos       every number, nothing held back, x0.2-x5 (crashes sometimes)
#   Randomizer - Pure Chaos  Chaos's numbers minus map-load counts (x0.2-x5), plus every
#                            ordnance takes on another of its type from its level (a sniper
#                            rifle shooting rifle bolts, missiles swapped) and every shot
#                            that explodes gets a random explosion from its level:
#                            sniper rifles spraying pistol bolts, rockets that pop like grenades
#
# Every number in every unit, weapon, ordnance and explosion class (entc, wpnc,
# ordc, expc) is multiplied by a random factor between --min and --max (0.5x-2x
# by default); levels are rebuilt, so values can grow to any size. Models,
# textures and sounds are never touched.
#
#   python bf1_cli.py randomize                      roll all four with a new seed
#   python bf1_cli.py randomize --seed 1234          replay a roll
#   python bf1_cli.py randomize --play wild          roll, install the Wild one and start the game
#   python bf1_cli.py randomize --modes pure --play pure
#
# The seed is printed and saved as randomizer_last.json in each mod folder.



RANDOMIZER_FOLDERS = {"safe": "Randomizer - Safe", "wild": "Randomizer - Wild", "chaos": "Randomizer - Chaos",
           "pure": "Randomizer - Pure Chaos"}
RANDOM_CLASS_TAGS = ("entc", "wpnc", "ordc", "expc")
RANDOM_NUMBER = re.compile(r"^-?(\d+\.?\d*|\.\d+)$")
RANDOM_H = core.fnv1a32

# "safe": what you feel in a match.
RANDOM_SAFE = {RANDOM_H(n) for n in (
    "MaxHealth", "MaxSpeed", "MaxStrafeSpeed", "MaxTurnSpeed", "Acceleraton", "acceleration", "Deceleration",
    "ForwardSpeed", "ReverseSpeed", "StrafeSpeed", "TurnRate", "Traction", "DropItemProbability",
    "WeaponAmmo1", "WeaponAmmo2", "WeaponAmmo3", "WeaponAmmo4",
    "ShotDelay", "ReloadTime", "RoundsPerClip", "HeatPerShot", "HeatRecoverRate", "HeatThreshold",
    "MaxRange", "MinRange", "OptimalRange", "LockTime", "LockOnRange", "LockOnAngle", "KickStrength",
    "MinSpread", "MaxSpread", "ZoomMin", "ZoomMax", "AutoAimSize",
    "Velocity", "MaxDamage", "Damage", "LifeSpan", "Gravity", "Rebound", "LaserLength", "LaserWidth",
    "GlowLength", "BlurLength", "LightRadius", "LightColor", "LaserGlowColor",
    "DamageRadius", "DamageRadiusInner", "DamageRadiusOuter", "Push", "PushRadius", "PushRadiusInner",
    "PushRadiusOuter", "Shake", "ShakeLength", "ShakeRadius", "ShakeRadiusInner", "ShakeRadiusOuter",
    "LightDuration")}
# never in "wild": camera and physics, and counts/type switches read when a map loads
RANDOM_WILD_DENY = {RANDOM_H(n) for n in (
    "EyePointOffset", "EyePointCenter", "TrackCenter", "TrackOffset", "TiltValue", "MapScale",
    "FirstPersonFOV", "ThirdPersonFOV", "CollisionScale", "CollisionRootScale", "SkeletonRootScale",
    "AddSpringBody", "BodySpringLength", "LiftSpring", "LiftDamp", "LevelSpring", "LevelDamp",
    "VelocitySpring", "VelocityDamp", "OmegaXSpring", "OmegaXDamp", "OmegaZSpring", "OmegaZDamp",
    "BodyOmegaXSpringFactor", "AimerPitchLimits", "AimerYawLimits", "AimerPitchLimts", "AimerYawLimts",
    "PitchLimits", "YawLimits", "SetAltitude", "GravityScale", "HierarchyLevel", "NumWeapons",
    "SalvoCount", "ShotPatternCount", "NumChunks", "ChunkTerrainCollisions", "MaxItems", "LegPairCount",
    "PassengerSlots", "SpawnPointCount", "SpawnPointLocation", "WeaponChannel", "WeaponChannel1",
    "WeaponChannel2", "WeaponChannel3", "WeaponChannel4", "ForceMode", "HealthType", "AISizeType",
    "PilotType", "UnitType", "VehicleType", "FlickerType", "NormalDirection", "IsPilotExposed",
    "NoCombatInterrupt", "NoDeathExplosions", "NoEnterVehicles", "CapturePosts")}
RANDOM_EXPLOSION_NAME = RANDOM_H("ExplosionName")
RANDOM_NOT_SWAPPABLE = {"emitterordnance", "towcable"}  # the arc caster's emitter and the snowspeeder's cable


# The counts and type switches the game reads while a map loads - the one part of
# Chaos that "near-chaos" (Pure Chaos's numbers) leaves alone.
RANDOM_LOAD_TIME = {RANDOM_H(n) for n in (
    "SalvoCount", "ShotPatternCount", "NumChunks", "ChunkTerrainCollisions", "MaxItems", "LegPairCount",
    "PassengerSlots", "SpawnPointCount", "SpawnPointLocation", "HierarchyLevel", "NumWeapons", "WeaponChannel",
    "WeaponChannel1", "WeaponChannel2", "WeaponChannel3", "WeaponChannel4", "ForceMode", "HealthType",
    "AISizeType", "PilotType", "UnitType", "VehicleType", "IsPilotExposed", "NoEnterVehicles", "CapturePosts")}

# default multiplier range per mode (--min/--max override)
RANDOM_RANGES = {"safe": (0.5, 2.0), "wild": (0.5, 2.0), "chaos": (0.2, 5.0), "pure": (0.2, 5.0)}


def random_allowed(mode: str, prop_hash: int) -> bool:
    if mode == "chaos":
        return True
    if mode == "near-chaos":
        return prop_hash not in RANDOM_LOAD_TIME
    if mode == "wild":
        return prop_hash in core.HASH_TO_NAME and prop_hash not in RANDOM_WILD_DENY
    return prop_hash in RANDOM_SAFE


def roll_number(token: str, rng: random.Random, low: float, high: float, chaos: bool) -> str:
    if not RANDOM_NUMBER.match(token):
        return token
    x, is_int = float(token), "." not in token
    if not chaos and (x <= 0 or (is_int and x <= 1)):
        return token  # 0/1 switches and none/infinite markers
    if chaos and x == 0:
        if is_int:
            return str(rng.randint(0, 1))
        x = rng.uniform(0.0, 1.0)
    new = x * math.exp(rng.uniform(math.log(low), math.log(high)))
    if is_int:
        whole = round(abs(new))
        if abs(x) >= 1:
            whole = max(1, whole)  # a count of 3 can drop to 1, never to "none"
        return str(whole if new >= 0 else -whole)
    decimals = max(1, len(token.split(".", 1)[1]))
    text = f"{new:.4f}".rstrip("0")
    text = text + "0" if text.endswith(".") else text
    return text if len(text.split(".")[1]) >= decimals else f"{new:.{decimals}f}"


def roll_value(value: str, rng, low, high, chaos) -> str:
    tokens = value.split(" ")
    rolled = [roll_number(t, rng, low, high, chaos) for t in tokens]
    if 3 <= len(tokens) <= 4 and all(t.isdigit() and int(t) <= 255 for t in tokens):  # a colour
        rolled = [str(max(0, min(255, int(float(r))))) if RANDOM_NUMBER.match(r) else r for r in rolled]
    return " ".join(rolled)


def randomize_container(container, rng, mode: str, low: float, high: float, stats: dict,
                        pure_numbers: str = "near-chaos", add_explosions: bool = False) -> None:
    """One level: the classes directly in it, then each nested level on its own."""
    chunks = container.body.chunks
    blasts = []
    if mode == "pure":
        # Swap what each ordnance IS, not which ordnance a weapon names: pointing a
        # weapon's OrdnanceName at another class crashed the game on the first shot
        # (sniper rifle, pistol). So every weapon keeps firing its own class, and that
        # class takes over another one's whole definition - speed, damage, model,
        # effects, explosion. Only between ordnance of the same base, in this level.
        groups = {}
        for ch in chunks:
            if ch.tag == "ordc":
                d = core.decode_ordnance_payload(ch.payload)
                if d["base"] not in RANDOM_NOT_SWAPPABLE:
                    groups.setdefault(d["base"], []).append((ch, d))
            elif ch.tag == "expc":
                blasts.append(core.decode_ordnance_payload(ch.payload)["type"])
        for base, members in groups.items():
            if len(members) < 2:
                continue
            ring = members[:]
            rng.shuffle(ring)
            # each one takes the next one's definition, so nobody keeps its own
            for (ch, d), (_, donor) in zip(ring, ring[1:] + ring[:1]):
                if donor["type"] != d["type"]:
                    ch.set_payload(core.encode_ordnance_payload(base, d["type"], list(donor["props"])))
                    stats["swaps"] += 1
    for ch in chunks:
        if ch.tag == "lvl_":
            randomize_container(ch.get_nested_container(), rng, mode, low, high, stats, pure_numbers, add_explosions)
            continue
        if ch.tag not in RANDOM_CLASS_TAGS:
            continue
        d = core.decode_ordnance_payload(ch.payload)
        props, changed = [], False
        for key, value in d["props"]:
            h = core.key_to_hash(key)
            new = value
            numbers = pure_numbers if mode == "pure" else mode  # Pure Chaos rolls numbers like Wild by default
            if random_allowed(numbers, h):
                new = roll_value(value, rng, low, high, numbers in ("chaos", "near-chaos"))
                stats["values"] += sum(1 for a, b in zip(value.split(" "), new.split(" ")) if a != b)
            changed |= new != value
            props.append((key, new))
        if mode == "pure" and ch.tag == "ordc" and blasts and d["base"] not in RANDOM_NOT_SWAPPABLE:
            # a random explosion from its level - only for ordnance that already explodes:
            # an infantry bolt given one (it has none in the stock game) crashed when fired
            hits = [i for i, (k, _) in enumerate(props) if core.key_to_hash(k) == RANDOM_EXPLOSION_NAME]
            if hits or add_explosions:
                blast = rng.choice(blasts)
                for i in hits:
                    props[i] = (props[i][0], blast)
                if not hits:
                    props.append(("ExplosionName", blast))
                stats["explosions"] += 1
                changed = True
        if changed:
            ch.set_payload(core.encode_ordnance_payload(d["base"], d["type"], props))


def random_roll(sources: list, out_dir: Path, mode: str, seed: int, low: float, high: float,
         pure_numbers: str = "near-chaos", add_explosions: bool = False) -> dict:
    stats = {"values": 0, "swaps": 0, "explosions": 0}
    for i, src in enumerate(sources):
        container = core.parse_container(src.read_bytes())
        randomize_container(container, random.Random(f"{seed}-{mode}-{src.name}"), mode, low, high, stats,
                            pure_numbers, add_explosions)
        target = out_dir / "SIDE" / src.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(container.raw_bytes())
    report = {"mode": mode, "seed": seed, "min": low, "max": high, "files": [s.name for s in sources], **stats}
    (out_dir / Loader.RANDOMIZER_LAST).write_text(json.dumps(report, indent=2))
    return report


def randomizer_main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bf1 randomize", description="Rolls the randomizer mods (Safe, Wild, Chaos, Pure Chaos) from your original side files into the Mod Loader's mods folder.")
    p.add_argument("--seed", type=int, help="replay a roll (default: a new random seed)")
    p.add_argument("--min", type=float, help="smallest multiplier (default 0.5 for safe/wild, 0.2 for chaos/pure)")
    p.add_argument("--max", type=float, help="largest multiplier (default 2 for safe/wild, 5 for chaos/pure)")
    p.add_argument("--modes", nargs="+", choices=list(RANDOMIZER_FOLDERS), default=list(RANDOMIZER_FOLDERS), help="which to roll (default all)")
    p.add_argument("--play", choices=list(RANDOMIZER_FOLDERS), help="then install this one and start the game")
    p.add_argument("--pure-numbers", choices=("near-chaos", "wild", "chaos"), default="near-chaos",
                   help="how Pure Chaos rolls its numbers (default near-chaos: everything Chaos rolls except the "
                        "counts and type switches read when a map loads)")
    p.add_argument("--add-explosions", action="store_true",
                   help="Pure Chaos also gives explosions to ordnance that has none (infantry bolts) - crashed in testing")
    args = p.parse_args(argv)

    cfg = Loader.load_config()
    mods_dir, originals = cfg.get("mods_dir"), cfg.get("originals_dir")
    if not mods_dir or not originals:
        print("error: set the Mods folder and Original files in the Mod Loader first.", file=sys.stderr)
        return 1
    side = Path(originals) / "Data" / "_LVL_PC" / "SIDE"
    sources = sorted(side.glob("*.lvl"))
    if not sources:
        print(f"error: no side files in {side}", file=sys.stderr)
        return 1
    seed = args.seed if args.seed is not None else random.randrange(1, 1_000_000)
    print(f"seed {seed}, {len(sources)} side files")
    for mode in args.modes:
        low = args.min if args.min is not None else RANDOM_RANGES[mode][0]
        high = args.max if args.max is not None else RANDOM_RANGES[mode][1]
        r = random_roll(sources, Path(mods_dir) / RANDOMIZER_FOLDERS[mode], mode, seed, low, high, args.pure_numbers,
                 args.add_explosions)
        print(f"  {RANDOMIZER_FOLDERS[mode]:26s} x{low}-x{high} {r['values']:>7,} values" + (f", {r['swaps']} ordnance swapped, {r['explosions']} explosions" if mode == "pure" else ""))
    if args.play:
        if args.play not in args.modes:
            print(f"error: --play {args.play} wasn't rolled this time.", file=sys.stderr)
            return 1
        game = cfg.get("game_dir") or str(Loader.find_game_dir() or "")
        plan = Loader.plan(game, mods_dir, originals, RANDOMIZER_FOLDERS[args.play])
        Loader.apply(game, plan, originals)
        Loader.launch(game)
        print(f"installed {RANDOMIZER_FOLDERS[args.play]} and started the game")
    return 0


if __name__ == "__main__":
    sys.exit(main())
