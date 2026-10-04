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
import sys
from pathlib import Path

import bf1_core as core

CLASS_TAGS = ("entc", "wpnc", "ordc", "expc")


class CliError(Exception):
    pass


# --- output -------------------------------------------------------------------------------

def emit(args, data, text: str | None = None) -> None:
    """JSON with --json, otherwise `text` (or a readable dump of `data`)."""
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    elif text is not None:
        print(text)
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
        import bf1_skinned_import as sk
        data.update({"vertices": sk.model_vertex_count(chunk.payload),
                     "segments": sk.model_segment_count(chunk.payload),
                     "skinned": sk.is_skinned_model(chunk.payload),
                     "texture": sk.model_texture(chunk.payload)})
        bones = sk.find_skeleton(chunk.parent_body.chunks, chunk.display_name())
        if bones:
            data["character"] = sk.is_character(bones)
            data["fire_points"] = [{"bone": n, "position": p, "is_fire_point": f}
                                   for n, p, f in sk.hard_points(bones, chunk.parent_body.chunks,
                                                                 chunk.display_name())]
            data["used_by"] = sk.classes_using(chunk.parent_body.chunks, chunk.display_name(),
                                               sk.MODEL_PROPERTIES)
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
    try:
        payload = core.import_file_to_chunk_payload(tag, args.source)
    except Exception as exc:
        raise CliError(f"Can't use {args.source} for a '{tag}' chunk: {exc}")
    rows = []
    for path, chunk in targets:
        rows.append({"ref": path, "old_size": len(chunk.payload), "new_size": len(payload)})
        chunk.set_payload(payload)
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
        lines.append(f"{Path(f['file']).name}: {total} values in {f['classes_changed']} classes"
                     + (f" -> {f['out']}" if f["out"] else " (nothing to change, not written)"))
        lines += [f"    {r['rule']}: {r['values_changed']}" for r in f["rules"] if r["values_changed"]]
    emit(args, report, "\n".join(lines))


def cmd_randomize(args):
    """Randomizes the classes of .lvl files (bf1_randomizer's rules, any one mode)."""
    import random
    import bf1_randomizer as rz
    mode = "chaos" if args.chaos else args.mode
    seed = args.seed if args.seed is not None else random.randrange(1, 1_000_000)
    rows = []
    for file in args.files:
        container = load(file)
        stats = {"values": 0, "swaps": 0, "explosions": 0}
        rz.randomize_container(container, random.Random(f"{seed}-{mode}-{Path(file).name}"), mode,
                               args.min, args.max, stats)
        out = save(container, Path(args.out_dir) / Path(file).name, args)
        rows.append({"file": str(file), "out": out, **stats})
    emit(args, {"seed": seed, "mode": mode, "files": rows},
         f"seed {seed}, mode {mode}\n" + "\n".join(
             f"{Path(r['file']).name}: {r['values']:,} values" + (f", {r['swaps']} ordnance swaps" if mode == "pure" else "")
             + f" -> {r['out']}" for r in rows))


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
    import bf1_skinned_import as sk
    container = load(args.file)
    path, model = resolve(container, args.model)
    if model.tag != "modl" or not sk.is_skinned_model(model.payload):
        raise CliError(f"{args.model} isn't a character or vehicle model (a skinned 'modl').")
    name, siblings = model.display_name(), model.parent_body.chunks
    bones = sk.find_skeleton(siblings, name)
    if bones is None:
        raise CliError(f"No skeleton for {name} next to it.")
    lod = None if args.no_lod else sk.find_lod(siblings, name)
    lod_bones = (sk.find_skeleton(siblings, lod.display_name()) or bones) if lod is not None else None
    glb = Path(args.glb).read_bytes()
    source = sk.load_glb_mesh(glb)
    usage = sk.texture_usage(source)
    if args.list_textures:
        emit(args, [{"group": g, "vertices": n} for g, n in sorted(usage.items())],
             "\n".join(f"texture {g}: {n:,} vertices" for g, n in sorted(usage.items())))
        return
    if not args.out:
        raise CliError("Importing needs -o/--out (or --list-textures to just look).")
    if args.delete_texture:
        source = sk.drop_groups(source, set(args.delete_texture))
        usage = sk.texture_usage(source)
        if not usage:
            raise CliError("Deleting those textures leaves nothing of the model.")
    images = sk.fill_missing_images(sk.load_glb_images(glb), usage)
    character = sk.is_character(bones)
    bounds = sk.template_bounds(model.payload, bones)
    axis = args.axis or ("Height" if character else "Largest side")
    if axis not in sk.FIT_AXES:
        raise CliError(f"--axis must be one of: {', '.join(sk.FIT_AXES)}")
    textures = {ch.display_name(): ch for ch in siblings if ch.tag == "tex_"}
    tex_name = None
    if not args.keep_texture:
        if args.texture:
            tex_name = args.texture
        else:
            own = sk.find_texture(siblings, sk.model_texture(model.payload))
            tex_name = own.display_name() if own is not None else None
        if tex_name is not None and tex_name not in textures:
            raise CliError(f"No texture '{tex_name}' next to the model. Options: {', '.join(sorted(textures))}")
    mesh, atlas, size = source, None, 0
    if tex_name:
        size = _texture_size(textures[tex_name])
        mesh, atlas = sk.build_atlas(source, images, sk.auto_atlas_layout(usage, images, size), size)
    fit_size = args.size
    rotate = tuple(float(v) for v in args.rotate.split(",")) if args.rotate else (0.0, 0.0, 0.0)
    if len(rotate) != 3:
        raise CliError("--rotate takes three angles: X,Y,Z")
    mesh = sk.fit_to_bounds(mesh, *bounds, axis, fit_size, rotate)
    arm = args.arm if args.arm is not None else (
        sk.estimate_arm_drop(mesh, bones, sk.bind_world_matrices(bones)) if character else 0.0)
    hair = set(args.hair) if args.hair is not None else (sk.guess_loose_groups(mesh) if character else set())
    limits = {g: sk.TORSO_BONES for g in hair}
    budget = args.budget or len(mesh.positions)
    main, stats = sk.build_character_model(model.payload, bones, mesh, budget, arm, limits, tex_name)
    changes, report = [(model, main)], {"model": name, "ref": path, "vertices": stats["vertices"],
                                        "triangles": stats["tris"], "segments": stats["segments"],
                                        "arm_angle": stats.get("arm_drop"), "hair_textures": sorted(hair)}
    if lod is not None:
        lod_payload, lod_stats = sk.build_character_model(lod.payload, lod_bones, mesh, args.lod_budget or budget,
                                                          arm, limits, tex_name)
        changes.append((lod, lod_payload))
        report["lod"] = {"model": lod.display_name(), "vertices": lod_stats["vertices"],
                         "segments": lod_stats["segments"]}
    if not args.no_collision:
        coll, prim = sk.find_collision(siblings, name)
        if coll is not None:
            new_coll, cstats = sk.rebuild_collision_mesh(coll.payload, bones, sk.model_triangles(main, bones),
                                                         sk.auto_collision_budget(coll.payload))
            changes.append((coll, new_coll))
            report["collision_triangles"] = cstats["triangles"]
        if prim is not None:
            new_prim, count = sk.refit_primitives(prim.payload, bones, bounds, sk.model_bounds(main, bones))
            changes.append((prim, new_prim))
            report["collision_shapes"] = count
    if not args.keep_shadow:
        changes = [(c, sk.strip_shadows(p)) if c.tag == "modl" else (c, p) for c, p in changes]
    if tex_name:
        changes.append((textures[tex_name], core.encode_texture_chunk(tex_name, size, size, atlas)))
        report["texture"] = {"name": tex_name, "size": size}
    fire = sk.weapon_point_report(sk.weapon_points(bones, siblings, name), main, bones)
    report["fire_points_off_model"] = [n for n, _, off in fire if off]
    if args.preview:
        from PIL import Image
        tex_img = Image.frombytes("RGBA", (size, size), atlas) if atlas is not None else None
        sk.render_preview(main, bones, tex_img).save(args.preview)
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
    import bf1_skinned_import as sk
    container = load(args.file)
    path, model = resolve(container, args.model)
    name, siblings = model.display_name(), model.parent_body.chunks
    bones = sk.find_skeleton(siblings, name) if model.tag == "modl" else None
    if not bones:
        raise CliError(f"{args.model} has no skeleton, so no fire points.")
    points = {n: (p, f) for n, p, f in sk.hard_points(bones, siblings, name)}
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
        for bone, _, off in sk.weapon_point_report([(n, p) for n, (p, f) in points.items() if f], model.payload, bones):
            if off and bone not in moves:
                moves[bone] = sk.nearest_surface_point(points[bone][0], model.payload, bones)
    rows = [{"bone": n, "position": moves.get(n, p), "moved": n in moves, "is_fire_point": f}
            for n, (p, f) in points.items()]
    if not moves:
        emit(args, rows, "\n".join(f"{r['bone']:24s} {tuple(round(v, 3) for v in r['position'])}"
                                   + ("  (fire point)" if r["is_fire_point"] else "") for r in rows))
        return
    if not args.out:
        raise CliError("Moving points needs -o/--out.")
    lod = sk.find_lod(siblings, name)
    done = set()
    for model_name in [name] + ([lod.display_name()] if lod is not None else []):
        skel = sk.find_skeleton_chunk(siblings, model_name)
        if skel is None or id(skel) in done:
            continue
        done.add(id(skel))
        _, skel_bones = sk.decode_skeleton_chunk(skel.payload)
        wanted = {n: p for n, p in moves.items() if any(b.name == n for b in skel_bones)}
        if wanted:
            skel.set_payload(sk.encode_skeleton_transforms(skel.payload, sk.move_bones(skel_bones, wanted)))
    out = save(container, args.out, args)
    emit(args, {"moved": {n: p for n, p in moves.items()}, "out": out},
         "\n".join(f"moved {n} -> {tuple(round(v, 3) for v in p)}" for n, p in moves.items()) + f"\nwrote {out}")


# --- movies and sounds --------------------------------------------------------------------

def _media(path):
    import bf1_media
    try:
        return bf1_media, bf1_media.read_media(path)
    except bf1_media.MediaError as exc:
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
    ffmpeg = None
    if args.mp4:
        ffmpeg = m.find_ffmpeg()
        if ffmpeg is None:
            raise CliError("--mp4 needs ffmpeg.exe next to the tools or on PATH.")
    files = m.extract(media, args.outdir, _pick(media, args.only), ffmpeg=ffmpeg)
    emit(args, [str(f) for f in files], f"extracted {len(files)} file(s) to {args.outdir}")


def cmd_media_replace(args):
    m, media = _media(args.file)
    if media.kind != "sounds":
        raise CliError("Only sounds can be replaced (the game's movies are Bink, which ffmpeg can't write).")
    replacements, report = {}, []
    for spec in args.pairs:
        sample, eq, audio = spec.partition("=")
        if not eq:
            raise CliError(f"'{spec}' isn't SAMPLE=AUDIOFILE.")
        entry = _pick(media, [sample])[0]
        if entry.alias_of is not None:
            raise CliError(f"{entry.name} is an alias with no audio of its own - replace the sample it plays.")
        try:
            pcm, rate = m.load_audio(audio, target_rate=entry.frequency, ffmpeg=m.find_ffmpeg())
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
        emit(args, rows, _plan_text(rows) + f"\ninstalled {p.mod}" + (" and launched the game" if args.launch else ""))
    elif args.action == "launch":
        Loader.launch(game)
        emit(args, {"launched": True}, "launched the game")


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
    s = add("export", cmd_export, "Save a chunk as a usable file (texture -> .png/.dds, class -> .odf, script -> .luac, else raw)")
    s.add_argument("file"); s.add_argument("ref"); s.add_argument("out", help="output file; its extension picks the format")
    s = add("replace", cmd_replace, "Replace a chunk from a file (.png/.dds texture, .odf class, .luac script, raw bytes)")
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
    s = add("randomize", cmd_randomize, "Randomize every number of every unit/weapon/ordnance/explosion class")
    s.add_argument("files", nargs="+", help=".lvl files (left as they are)")
    s.add_argument("--out-dir", required=True)
    s.add_argument("--seed", type=int, help="replay a roll (default: a new random seed, printed)")
    s.add_argument("--min", type=float, default=0.5, help="smallest multiplier (default 0.5)")
    s.add_argument("--max", type=float, default=2.0, help="largest multiplier (default 2.0)")
    s.add_argument("--mode", choices=("safe", "wild", "chaos", "pure"), default="safe",
                   help="safe: gameplay values only (default); wild: every named value except camera/physics/counts; "
                        "chaos: everything; pure: chaos plus random ordnance swaps")
    s.add_argument("--chaos", action="store_true", help="same as --mode chaos")
    s.add_argument("--force", action="store_true")
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
                                ("extract", cmd_media_extract, "Extract movies (.bik/.mp4) or sounds (.wav)"),
                                ("replace", cmd_media_replace, "Replace sounds with your own audio files")):
        s = msub.add_parser(name, help=help_text, description=help_text)
        s.set_defaults(fn=fn)
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        s.add_argument("file")
        if name == "extract":
            s.add_argument("outdir")
            s.add_argument("--only", nargs="*", metavar="NAME", help="just these (names or hashes)")
            s.add_argument("--mp4", action="store_true", help="movies as .mp4 (needs ffmpeg)")
        if name == "replace":
            s.add_argument("pairs", nargs="+", metavar="SAMPLE=AUDIOFILE")
            writes(s)

    mods = sub.add_parser("mods", help="The Mod Loader: status, list, plan, install, launch")
    mods.set_defaults(fn=cmd_mods)
    mods.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    mods.add_argument("action", choices=("status", "ls", "plan", "install", "launch"))
    mods.add_argument("mod", nargs="?", help="mod folder name ('original' for the original game)")
    mods.add_argument("--launch", action="store_true", help="with install: start the game afterwards")
    mods.add_argument("--game", help="game folder (default: the Mod Loader's setting)")
    return p


def main(argv=None) -> int:
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
            print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
