#!/usr/bin/env python3
"""Split every mesh primitive in a GLB into N primitives that render identically.

The vertex attributes are left untouched and shared; only the index range is
partitioned (on triangle boundaries), so the visual result is unchanged.

Usage:
    python glb_split_primitives.py input.glb -n 4 -o output.glb
"""

import argparse
import json
import struct
import sys

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


def main():
    ap = argparse.ArgumentParser(description="Split, merge or combine GLB meshes without changing appearance.")
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
    args = ap.parse_args()

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


if __name__ == "__main__":
    main()
