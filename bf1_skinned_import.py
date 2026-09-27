"""
bf1_skinned_import.py - Replace a skinned character model (soldier bodies,
anything whose 'modl' segments carry per-vertex bone indices) with new
geometry, rebuilding every per-segment chunk in the same layout the game's
own files use.

FORMAT (verified against rep.lvl's rep_inf_trooper by decoding the original
and re-encoding it byte-for-byte - see verify_roundtrip):

  skel   INFO = owner model name \\0 + u16 bone count; NAME / PRNT = that many
         back-to-back \\0-terminated bone / parent names; XFRM = that many
         12-float parent-local transforms (3 basis columns, then translation).
  modl   NAME, NODE (root bone), INFO (3 ints, vertex_box, visibility_box,
         bone count, face count), [shdw...], segm..., SPHR.
         vertex_box = bounds of the BONE-LOCAL vertex positions (it's the
         range the compressed VBUF positions are quantized into);
         visibility_box = bounds of the model in bind pose (world space).
  segm   INFO (topology, vertex count, primitive count), MTRL, [MNAM], RTYP,
         TNAM, IBUF, VBUF flags 0xf226 (compressed, stride 20), VBUF flags
         0x226 (uncompressed, stride 36), VDAT (a 0x222 vertex buffer: no
         bone field), SKIN (u32 count, u32 1, one byte per vertex), BMAP
         (u32 count, one byte per entry).
  vertex positions/normals are BONE-LOCAL: world = bind_world[bone] * local.
  The per-vertex bone byte is an index into the segment's BMAP (a palette of
  skeleton bone indices), and in both VBUFs it sits in bits 16-23 of a u32.

Only hard skinning (one bone per vertex) is written - that's what the stock
soldier models use (no bone_weights flag), so joints are rigid like the
originals'.
"""
from __future__ import annotations

import heapq
import math
import struct
from dataclasses import dataclass, field
from typing import Optional

import bf1_core as c

VBUF_COMPRESSED = 0xF226
VBUF_SKINNED = 0x226
VBUF_PLAIN = 0x222
VBUF_TANGENTS = 0x40
TOPOLOGY_TRIANGLE_LIST = 4
TOPOLOGY_TRIANGLE_STRIP = 5


class SkinnedImportError(Exception):
    pass


# --- skeleton ---------------------------------------------------------------

@dataclass
class Bone:
    name: str
    parent: str
    transform: tuple  # 12 floats, parent-local


def _read_cstr(data: bytes, pos: int) -> tuple:
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("ascii", "replace"), end + 1


def decode_skeleton_chunk(payload: bytes) -> tuple:
    body = c.parse_body(payload)
    kids = {s.tag: s for s in body.chunks}
    if not all(t in kids for t in ("INFO", "NAME", "PRNT", "XFRM")):
        raise SkinnedImportError("Malformed 'skel' chunk (missing INFO/NAME/PRNT/XFRM).")
    owner, pos = _read_cstr(kids["INFO"].payload, 0)
    count = struct.unpack_from("<H", kids["INFO"].payload, pos)[0]
    names, parents = [], []
    pos = 0
    for _ in range(count):
        n, pos = _read_cstr(kids["NAME"].payload, pos)
        names.append(n)
    pos = 0
    for _ in range(count):
        n, pos = _read_cstr(kids["PRNT"].payload, pos)
        parents.append(n)
    xfrm = kids["XFRM"].payload
    return owner, [Bone(names[i], parents[i], struct.unpack_from("<12f", xfrm, i * 48)) for i in range(count)]


def find_skeleton(sibling_chunks: list, model_name: str) -> Optional[list]:
    """A model's skeleton is a 'skel' chunk next to it whose INFO names it."""
    for ch in sibling_chunks:
        if ch.tag != "skel":
            continue
        try:
            owner, bones = decode_skeleton_chunk(ch.payload)
        except (SkinnedImportError, ValueError, struct.error):
            continue
        if owner == model_name:
            return bones
    return None


def _mat_from_transform(t) -> list:
    return [[t[0], t[3], t[6], t[9]], [t[1], t[4], t[7], t[10]],
            [t[2], t[5], t[8], t[11]], [0.0, 0.0, 0.0, 1.0]]


def mat_mul(a, b) -> list:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def mat_point(m, p) -> tuple:
    x, y, z = p
    return (m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3],
            m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3],
            m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3])


def mat_dir(m, v) -> tuple:
    x, y, z = v
    r = (m[0][0] * x + m[0][1] * y + m[0][2] * z,
         m[1][0] * x + m[1][1] * y + m[1][2] * z,
         m[2][0] * x + m[2][1] * y + m[2][2] * z)
    n = math.sqrt(r[0] ** 2 + r[1] ** 2 + r[2] ** 2)
    return (r[0] / n, r[1] / n, r[2] / n) if n > 1e-12 else (0.0, 1.0, 0.0)


def mat_inverse(m) -> list:
    a, b, cc = m[0][:3]
    d, e, f = m[1][:3]
    g, h, i = m[2][:3]
    det = a * (e * i - f * h) - b * (d * i - f * g) + cc * (d * h - e * g)
    if abs(det) < 1e-12:
        raise SkinnedImportError("Degenerate bone transform.")
    k = 1.0 / det
    r = [[(e * i - f * h) * k, (cc * h - b * i) * k, (b * f - cc * e) * k],
         [(f * g - d * i) * k, (a * i - cc * g) * k, (cc * d - a * f) * k],
         [(d * h - e * g) * k, (b * g - a * h) * k, (a * e - b * d) * k]]
    t = (m[0][3], m[1][3], m[2][3])
    ti = [-(r[j][0] * t[0] + r[j][1] * t[1] + r[j][2] * t[2]) for j in range(3)]
    return [r[0] + [ti[0]], r[1] + [ti[1]], r[2] + [ti[2]], [0.0, 0.0, 0.0, 1.0]]


_IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def bind_world_matrices(bones: list, local_overrides: Optional[dict] = None) -> list:
    """World matrix per bone. `local_overrides` maps bone index -> a 4x4
    matrix to pre-multiply onto that bone's local transform (used to pose the
    skeleton to match a source mesh - see pose_arms_down)."""
    index = {b.name: i for i, b in enumerate(bones)}
    out = [None] * len(bones)

    def world(i):
        if out[i] is None:
            local = _mat_from_transform(bones[i].transform)
            if local_overrides and i in local_overrides:
                local = mat_mul(local, local_overrides[i])
            p = index.get(bones[i].parent)
            out[i] = mat_mul(world(p) if p is not None else _IDENTITY, local)
        return out[i]

    for i in range(len(bones)):
        world(i)
    return out


# --- segment decoding --------------------------------------------------------

@dataclass
class Vertex:
    pos: tuple      # bone-local
    normal: tuple   # bone-local
    uv: tuple
    bone: int       # index into the segment's BMAP


@dataclass
class Segment:
    children: list                  # [(tag, payload)] in file order
    topology: int
    indices: list
    vertices: list                  # [Vertex], from the uncompressed skinned VBUF
    bmap: list = field(default_factory=list)
    skinned: bool = True


def _strip_to_list(idx: list) -> list:
    tris = []
    for i in range(len(idx) - 2):
        a, b, cc = idx[i], idx[i + 1], idx[i + 2]
        if a == b or b == cc or a == cc:
            continue
        tris.extend((a, b, cc) if i % 2 == 0 else (b, a, cc))
    return tris


def decode_segment(payload: bytes) -> Segment:
    body = c.parse_body(payload)
    children = [(s.tag, s.payload) for s in body.chunks]
    info = next(p for t, p in children if t == "INFO")
    topology = struct.unpack_from("<I", info, 0)[0]
    ibuf = next(p for t, p in children if t == "IBUF")
    n = struct.unpack_from("<I", ibuf, 0)[0]
    indices = list(struct.unpack_from(f"<{n}H", ibuf, 4))
    bmap = []
    bm = next((p for t, p in children if t == "BMAP"), None)
    if bm is not None:
        bmap = list(bm[4:4 + struct.unpack_from("<I", bm, 0)[0]])
    vbufs = {struct.unpack_from("<I", p, 8)[0]: p for t, p in children if t == "VBUF"}
    verts = []
    if VBUF_SKINNED in vbufs:
        vb = vbufs[VBUF_SKINNED]
        count, stride, _ = struct.unpack_from("<III", vb, 0)
        for i in range(count):
            o = 12 + i * stride
            p = struct.unpack_from("<3f", vb, o)
            bone = (struct.unpack_from("<I", vb, o + 12)[0] >> 16) & 0xFF
            nn = struct.unpack_from("<3f", vb, o + 16)
            uv = struct.unpack_from("<2f", vb, o + 28)
            verts.append(Vertex(p, nn, uv, bone))
        return Segment(children, topology, indices, verts, bmap, True)
    if VBUF_PLAIN in vbufs:
        vb = vbufs[VBUF_PLAIN]
        count, stride, _ = struct.unpack_from("<III", vb, 0)
        for i in range(count):
            o = 12 + i * stride
            verts.append(Vertex(struct.unpack_from("<3f", vb, o), struct.unpack_from("<3f", vb, o + 12),
                                struct.unpack_from("<2f", vb, o + 24), 0))
    return Segment(children, topology, indices, verts, bmap, False)


def triangles(seg: Segment) -> list:
    if seg.topology == TOPOLOGY_TRIANGLE_STRIP:
        return _strip_to_list(seg.indices)
    return list(seg.indices[: len(seg.indices) // 3 * 3])


# --- encoders ----------------------------------------------------------------

def _q16(value: float, lo: float, hi: float) -> int:
    if hi - lo < 1e-12:
        return -32768
    q = round((value - lo) * 65535.0 / (hi - lo)) - 32768
    return max(-32768, min(32767, q))


def _wrap16(v: int) -> int:
    """int16 wraparound - matches the game's own quantizer, where UVs past +-16 wrap."""
    return (v + 32768) % 65536 - 32768


def _unorm8(v: float) -> int:
    return max(0, min(255, round((v + 1.0) * 0.5 * 255.0)))


def encode_vbuf_compressed(verts: list, vertex_box: tuple, normal_w: int = 0) -> bytes:
    lo, hi = vertex_box
    out = bytearray(struct.pack("<III", len(verts), 20, VBUF_COMPRESSED))
    for v in verts:
        out += struct.pack("<4h", *(_q16(v.pos[k], lo[k], hi[k]) for k in range(3)), 0)
        out += struct.pack("<I", (v.bone & 0xFF) << 16)
        nx, ny, nz = v.normal
        out += bytes((_unorm8(nz), _unorm8(ny), _unorm8(nx), normal_w))
        out += struct.pack("<2h", *(_wrap16(round(t * 2048.0)) for t in v.uv))
    return bytes(out)


VBUF_PLAIN_COMPRESSED = 0xD222


def encode_vbuf_plain_compressed(verts: list, vertex_box: tuple, normal_w: int = 0) -> bytes:
    lo, hi = vertex_box
    out = bytearray(struct.pack("<III", len(verts), 16, VBUF_PLAIN_COMPRESSED))
    for v in verts:
        out += struct.pack("<4h", *(_q16(v.pos[k], lo[k], hi[k]) for k in range(3)), 0)
        nx, ny, nz = v.normal
        out += bytes((_unorm8(nz), _unorm8(ny), _unorm8(nx), normal_w))
        out += struct.pack("<2h", *(_wrap16(round(t * 2048.0)) for t in v.uv))
    return bytes(out)


def encode_vbuf_skinned(verts: list) -> bytes:
    out = bytearray(struct.pack("<III", len(verts), 36, VBUF_SKINNED))
    for v in verts:
        out += struct.pack("<3fI3f2f", *v.pos, (v.bone & 0xFF) << 16, *v.normal, *v.uv)
    return bytes(out)


def encode_vbuf_plain(verts: list) -> bytes:
    out = bytearray(struct.pack("<III", len(verts), 32, VBUF_PLAIN))
    for v in verts:
        out += struct.pack("<3f3f2f", *v.pos, *v.normal, *v.uv)
    return bytes(out)


def encode_skin(verts: list) -> bytes:
    return struct.pack("<II", len(verts), 1) + bytes(v.bone & 0xFF for v in verts)


def encode_bmap(bmap: list) -> bytes:
    return struct.pack("<I", len(bmap)) + bytes(bmap)


def encode_ibuf(indices: list) -> bytes:
    if indices and max(indices) > 0xFFFF:
        raise SkinnedImportError("Segment has more than 65535 vertices.")
    return struct.pack(f"<I{len(indices)}H", len(indices), *indices)


def build_segment(template: Segment, verts: list, tri_indices: list, vertex_box: tuple) -> bytes:
    """Re-emit `template`'s children in their original order, regenerating
    every geometry-bearing chunk from `verts`/`tri_indices` (triangle list)
    and copying material chunks (MTRL/MNAM/RTYP/TNAM/BNAM/...) through."""
    normal_w = 0
    for t, p in template.children:
        if t == "VBUF" and len(p) >= 12 + 16:
            flags, stride = struct.unpack_from("<I", p, 8)[0], struct.unpack_from("<I", p, 4)[0]
            if flags == VBUF_COMPRESSED:
                normal_w = p[12 + 15]
            elif flags == VBUF_PLAIN_COMPRESSED:
                normal_w = p[12 + 11]
    parts = []
    written = set()
    for tag, payload in template.children:
        if tag == "INFO":
            new = struct.pack("<III", TOPOLOGY_TRIANGLE_LIST, len(verts), len(tri_indices) // 3)
        elif tag == "IBUF":
            new = encode_ibuf(tri_indices)
        elif tag == "VBUF":
            # tangent variants (bump-mapped segments: 0x266 / 0xf266) are
            # written as their plain equivalents - see rebuild_model
            flags = struct.unpack_from("<I", payload, 8)[0] & ~VBUF_TANGENTS
            if flags in written:
                continue
            written.add(flags)
            if flags == VBUF_COMPRESSED:
                new = encode_vbuf_compressed(verts, vertex_box, normal_w)
            elif flags == VBUF_SKINNED:
                new = encode_vbuf_skinned(verts)
            elif flags == VBUF_PLAIN:
                new = encode_vbuf_plain(verts)
            elif flags == VBUF_PLAIN_COMPRESSED:
                new = encode_vbuf_plain_compressed(verts, vertex_box, normal_w)
            else:
                raise SkinnedImportError(f"Template segment has an unsupported VBUF variant {flags:#x}.")
        elif tag == "VDAT":
            new = encode_vbuf_plain(verts)
        elif tag == "SKIN":
            new = encode_skin(verts)
        elif tag == "BMAP":
            new = encode_bmap(template.bmap)
        else:
            new = payload
        parts.append(c.tagged_block(tag.encode("ascii"), new))
    return b"".join(parts)


# --- source mesh ---------------------------------------------------------------

@dataclass
class Mesh:
    """World-space triangle mesh (triangle list). `groups` tags each vertex
    with the source texture (glTF image index, -1 = untextured) so the atlas
    builder and bone limits can tell hair from body etc."""
    positions: list
    normals: list
    uvs: list
    tris: list
    groups: list = field(default_factory=list)


def _node_matrix(node: dict) -> list:
    if "matrix" in node:
        m = node["matrix"]  # column-major
        return [[m[0], m[4], m[8], m[12]], [m[1], m[5], m[9], m[13]],
                [m[2], m[6], m[10], m[14]], [0.0, 0.0, 0.0, 1.0]]
    tx, ty, tz = node.get("translation", (0.0, 0.0, 0.0))
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    sx, sy, sz = node.get("scale", (1.0, 1.0, 1.0))
    r = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
         [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
         [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    return [[r[0][0] * sx, r[0][1] * sy, r[0][2] * sz, tx],
            [r[1][0] * sx, r[1][1] * sy, r[1][2] * sz, ty],
            [r[2][0] * sx, r[2][1] * sy, r[2][2] * sz, tz],
            [0.0, 0.0, 0.0, 1.0]]


def load_glb_mesh(glb_bytes: bytes) -> Mesh:
    """Every triangle primitive in the file's scene, node transforms baked
    in, merged into one mesh (collision_- nodes skipped, like the static
    importer)."""
    import bf1_glb_import as gi
    gltf, binbuf = gi.parse_glb(glb_bytes)
    nodes = gltf.get("nodes", [])
    children = {ci for n in nodes for ci in n.get("children", [])}
    mesh = Mesh([], [], [], [], [])

    def image_of(prim):
        mats = gltf.get("materials", [])
        if "material" not in prim or prim["material"] >= len(mats):
            return -1
        mat = mats[prim["material"]]
        tex = mat.get("pbrMetallicRoughness", {}).get("baseColorTexture", {}).get("index")
        if tex is None:  # older exporters: the colour map lives in the spec/gloss extension
            tex = (mat.get("extensions", {}).get("KHR_materials_pbrSpecularGlossiness", {})
                   .get("diffuseTexture", {}).get("index"))
        if tex is None or tex >= len(gltf.get("textures", [])):
            return -1
        return gltf["textures"][tex].get("source", -1)

    scene = gltf.get("scenes", [{}])[gltf.get("scene", 0)] if gltf.get("scenes") else {}
    roots = scene.get("nodes") or [i for i in range(len(nodes)) if i not in children]
    worlds, order = {}, []

    def walk(ni, parent):
        worlds[ni] = mat_mul(parent, _node_matrix(nodes[ni]))
        order.append(ni)
        for ci in nodes[ni].get("children", []):
            walk(ci, worlds[ni])

    for ri in roots:
        walk(ri, _IDENTITY)

    def skin_matrices(skin_index):
        """Per joint: joint world matrix * inverse bind matrix - what a glTF
        viewer uses to place a rigged mesh (the mesh node's own transform
        is ignored for those, per the spec)."""
        skin = gltf["skins"][skin_index]
        joints = skin.get("joints", [])
        if "inverseBindMatrices" in skin:
            ibms = gi._read_accessor(gltf, binbuf, skin["inverseBindMatrices"])
        else:
            ibms = [(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)] * len(joints)
        out = []
        for j, m in zip(joints, ibms):  # accessor data is column-major
            ibm = [[m[0], m[4], m[8], m[12]], [m[1], m[5], m[9], m[13]],
                   [m[2], m[6], m[10], m[14]], [0.0, 0.0, 0.0, 1.0]]
            out.append(mat_mul(worlds.get(j, _IDENTITY), ibm))
        return out

    for ni in order:
        node = nodes[ni]
        if "mesh" not in node or str(node.get("name", "")).startswith("collision_-"):
            continue
        world = worlds[ni]
        joint_mats = skin_matrices(node["skin"]) if "skin" in node and gltf.get("skins") else None
        for prim in gltf["meshes"][node["mesh"]].get("primitives", []):
            if prim.get("mode", 4) != 4:
                raise SkinnedImportError("Only triangle-list primitives are supported.")
            attrs = prim["attributes"]
            pos = [v[:3] for v in gi._read_accessor(gltf, binbuf, attrs["POSITION"])]
            idx = ([v[0] for v in gi._read_accessor(gltf, binbuf, prim["indices"])]
                   if "indices" in prim else list(range(len(pos))))
            nrm = ([v[:3] for v in gi._read_accessor(gltf, binbuf, attrs["NORMAL"])]
                   if "NORMAL" in attrs else gi._compute_flat_normals(pos, idx))
            uv = ([v[:2] for v in gi._read_accessor(gltf, binbuf, attrs["TEXCOORD_0"])]
                  if "TEXCOORD_0" in attrs else [(0.0, 0.0)] * len(pos))
            base = len(mesh.positions)
            if joint_mats is not None and "JOINTS_0" in attrs and "WEIGHTS_0" in attrs:
                joints = gi._read_accessor(gltf, binbuf, attrs["JOINTS_0"])
                weights = gi._read_accessor(gltf, binbuf, attrs["WEIGHTS_0"])
                wscale = {5121: 255.0, 5123: 65535.0}.get(gltf["accessors"][attrs["WEIGHTS_0"]]["componentType"], 1.0)
                for p, n, js, ws in zip(pos, nrm, joints, weights):
                    acc_p, acc_n, total = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], 0.0
                    for j, w in zip(js, ws):
                        w = w / wscale
                        if w <= 0 or j >= len(joint_mats):
                            continue
                        m = joint_mats[j]
                        q = mat_point(m, p)
                        r = (m[0][0] * n[0] + m[0][1] * n[1] + m[0][2] * n[2],
                             m[1][0] * n[0] + m[1][1] * n[1] + m[1][2] * n[2],
                             m[2][0] * n[0] + m[2][1] * n[1] + m[2][2] * n[2])
                        for k in range(3):
                            acc_p[k] += w * q[k]
                            acc_n[k] += w * r[k]
                        total += w
                    if total > 0:
                        mesh.positions.append(tuple(v / total for v in acc_p))
                        ln = math.sqrt(sum(v * v for v in acc_n)) or 1.0
                        mesh.normals.append(tuple(v / ln for v in acc_n))
                    else:
                        mesh.positions.append(mat_point(world, p))
                        mesh.normals.append(mat_dir(world, n))
            else:
                mesh.positions += [mat_point(world, p) for p in pos]
                mesh.normals += [mat_dir(world, n) for n in nrm]
            mesh.uvs += [tuple(t) for t in uv]
            mesh.groups += [image_of(prim)] * len(pos)
            mesh.tris += [base + i for i in idx[: len(idx) // 3 * 3]]
    if not mesh.tris:
        raise SkinnedImportError("The .glb has no triangles.")
    return mesh


def fit_to_height(mesh: Mesh, height: float) -> Mesh:
    """Uniform scale so the mesh is `height` tall, feet on y=0, centered on x/z."""
    ys = [p[1] for p in mesh.positions]
    xs = [p[0] for p in mesh.positions]
    zs = [p[2] for p in mesh.positions]
    s = height / (max(ys) - min(ys))
    cx, cz, y0 = (min(xs) + max(xs)) / 2, (min(zs) + max(zs)) / 2, min(ys)
    pos = [((x - cx) * s, (y - y0) * s, (z - cz) * s) for x, y, z in mesh.positions]
    return Mesh(pos, list(mesh.normals), list(mesh.uvs), list(mesh.tris), list(mesh.groups))


FIT_AXES = {"Height": 1, "Length (front-back)": 2, "Width": 0, "Largest side": None}


def rotation_matrix(turn_x: float = 0.0, turn_y: float = 0.0, turn_z: float = 0.0) -> list:
    """3x3 rotation: turn about X (tips it forward/back - flips an
    upside-down model with 180), then Y (spins it to face another way), then
    Z (rolls it onto its side), in degrees."""
    def rx(a):
        c_, s = math.cos(a), math.sin(a)
        return [[1, 0, 0], [0, c_, -s], [0, s, c_]]

    def ry(a):
        c_, s = math.cos(a), math.sin(a)
        return [[c_, 0, s], [0, 1, 0], [-s, 0, c_]]

    def rz(a):
        c_, s = math.cos(a), math.sin(a)
        return [[c_, -s, 0], [s, c_, 0], [0, 0, 1]]

    def mul(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    return mul(rz(math.radians(turn_z)), mul(ry(math.radians(turn_y)), rx(math.radians(turn_x))))


def fit_to_bounds(mesh: Mesh, lo: tuple, hi: tuple, axis: str = "Height", size: Optional[float] = None,
                  rotate_deg=0.0) -> Mesh:
    """Rotate the mesh (`rotate_deg` = degrees about the vertical axis, or an
    (x, y, z) tuple of turns - see rotation_matrix), scale it uniformly so
    its extent along `axis` (a FIT_AXES key) is `size` (default: the target
    box's own extent on that axis), then place it in the target box
    `lo`/`hi`: centred on x/z, bottom on the box's floor. For a character
    that's "trooper height, feet on the ground"; for a vehicle, "same
    length, same place" as the model it replaces."""
    turns = tuple(rotate_deg) if isinstance(rotate_deg, (tuple, list)) else (0.0, float(rotate_deg), 0.0)
    r = rotation_matrix(*turns)

    def rot(v):
        return (r[0][0] * v[0] + r[0][1] * v[1] + r[0][2] * v[2],
                r[1][0] * v[0] + r[1][1] * v[1] + r[1][2] * v[2],
                r[2][0] * v[0] + r[2][1] * v[1] + r[2][2] * v[2])

    pos = [rot(p) for p in mesh.positions]
    nrm = [rot(n) for n in mesh.normals]
    mlo = [min(p[k] for p in pos) for k in range(3)]
    mhi = [max(p[k] for p in pos) for k in range(3)]
    ext = [mhi[k] - mlo[k] for k in range(3)]
    k_axis = FIT_AXES[axis]
    if k_axis is None:
        k_axis = max(range(3), key=lambda k: ext[k])
        target_k = max(range(3), key=lambda k: hi[k] - lo[k])
    else:
        target_k = k_axis
    if size is None:
        size = hi[target_k] - lo[target_k]
    s = size / (ext[k_axis] or 1.0)
    cx, cz = (mlo[0] + mhi[0]) / 2, (mlo[2] + mhi[2]) / 2
    tx, tz = (lo[0] + hi[0]) / 2, (lo[2] + hi[2]) / 2
    out = [((x - cx) * s + tx, (y - mlo[1]) * s + lo[1], (z - cz) * s + tz) for x, y, z in pos]
    return Mesh(out, nrm, list(mesh.uvs), list(mesh.tris), list(mesh.groups))


def template_bounds(template_payload: bytes, bones: list) -> tuple:
    """Actual bind-pose extents (lo, hi) of a model's own geometry - tighter
    than its INFO visibility box, which for some vehicles is a loose cube."""
    body = c.parse_body(template_payload)
    mats = bind_world_matrices(bones)
    pts = []
    for s in body.chunks:
        if s.tag == "segm":
            seg = decode_segment(s.payload)
            if seg.skinned and seg.bmap:
                pts += [mat_point(mats[seg.bmap[v.bone]], v.pos) for v in seg.vertices if v.bone < len(seg.bmap)]
    if not pts:
        raise SkinnedImportError("The model being replaced has no skinned geometry to measure.")
    return (tuple(min(p[k] for p in pts) for k in range(3)), tuple(max(p[k] for p in pts) for k in range(3)))


def is_character(bones: list) -> bool:
    """Humanoid skeleton (has arms) vs vehicle/other."""
    return _find_bone(bones, "bone_l_upperarm") is not None and _find_bone(bones, "bone_r_upperarm") is not None


def load_glb_images(glb_bytes: bytes) -> dict:
    """{glTF image index: PIL RGBA image} for every image embedded in the .glb."""
    import io
    from PIL import Image
    import bf1_glb_import as gi
    gltf, binbuf = gi.parse_glb(glb_bytes)
    out = {}
    for i, im in enumerate(gltf.get("images", [])):
        if "bufferView" not in im:
            continue
        bv = gltf["bufferViews"][im["bufferView"]]
        start = bv.get("byteOffset", 0)
        out[i] = Image.open(io.BytesIO(binbuf[start:start + bv["byteLength"]])).convert("RGBA")
    return out


def build_atlas(mesh: Mesh, images: dict, layout: dict, size: int = 512, gutter: float = 2.0) -> tuple:
    """Packs several source textures into one `size`x`size` image (the game
    gives a character ONE texture - e.g. a class's OverrideTexture - so a
    multi-texture model has to share an atlas). `layout` = {image index:
    (x, y, w, h) in atlas pixels}. UVs are remapped into each image's
    region; tiled UVs (outside 0..1) are wrapped per triangle, splitting
    vertices where neighbouring triangles wrap differently. Transparent
    pixels are filled with the image's average colour (the stock soldier
    material is opaque). Returns (new mesh, atlas RGBA bytes)."""
    from PIL import Image
    atlas = Image.new("RGBA", (size, size), (128, 128, 128, 255))
    for img_i, (x, y, w, h) in layout.items():
        src = images[img_i]
        opaque = [p for p in src.resize((64, 64)).getdata() if p[3] > 128] or [(128, 128, 128, 255)]
        avg = tuple(sum(p[k] for p in opaque) // len(opaque) for k in range(3)) + (255,)
        flat = Image.new("RGBA", src.size, avg)
        flat.alpha_composite(src)
        atlas.paste(flat.resize((w, h), Image.LANCZOS), (x, y))
    positions, normals, uvs, groups, tris, remap = [], [], [], [], [], {}
    for t in range(0, len(mesh.tris), 3):
        tri = mesh.tris[t:t + 3]
        g = mesh.groups[tri[0]] if mesh.groups else -1
        su = math.floor(sum(mesh.uvs[v][0] for v in tri) / 3)
        sv = math.floor(sum(mesh.uvs[v][1] for v in tri) / 3)
        for v in tri:
            key = (v, su, sv)
            if key not in remap:
                remap[key] = len(positions)
                u, vv = mesh.uvs[v]
                u = min(1.0, max(0.0, u - su))
                vv = min(1.0, max(0.0, vv - sv))
                if g in layout:
                    x, y, w, h = layout[g]
                    u = (x + gutter + u * (w - 2 * gutter)) / size
                    vv = (y + gutter + vv * (h - 2 * gutter)) / size
                positions.append(mesh.positions[v])
                normals.append(mesh.normals[v])
                uvs.append((u, vv))
                groups.append(g)
            tris.append(remap[key])
    return Mesh(positions, normals, uvs, tris, groups), atlas.tobytes()


# --- decimation (quadric error metric, half-edge collapse) --------------------

def _plane_quadric(a, b, cc, d, w):
    return [w * a * a, w * a * b, w * a * cc, w * a * d, w * b * b, w * b * cc, w * b * d,
            w * cc * cc, w * cc * d, w * d * d]


def _q_err(q, p):
    x, y, z = p
    return (q[0] * x * x + 2 * q[1] * x * y + 2 * q[2] * x * z + 2 * q[3] * x + q[4] * y * y
            + 2 * q[5] * y * z + 2 * q[6] * y + q[7] * z * z + 2 * q[8] * z + q[9])


def _face_normal(a, b, cc):
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = cc[0] - a[0], cc[1] - a[1], cc[2] - a[2]
    return (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)


PRE_REDUCE_LIMIT = 40000


def pre_reduce(mesh: Mesh, max_vertices: int, locked: set = frozenset()) -> tuple:
    """Fast first pass for very heavy meshes (a 380k-vertex tank would take
    the quadric simplifier minutes): snaps vertices to a grid fine enough to
    land under `max_vertices`, merging ones in the same cell with the same
    texture and roughly the same UV (so texture seams survive). Locked
    vertices are never merged. Returns (mesh, locked indices in the new mesh)."""
    P = mesh.positions
    lo = [min(p[k] for p in P) for k in range(3)]
    hi = [max(p[k] for p in P) for k in range(3)]
    size = max(hi[k] - lo[k] for k in range(3)) or 1.0
    groups = mesh.groups or [0] * len(P)
    for cells in (1024, 768, 512, 384, 256, 192, 128, 96, 64, 48, 32):
        cell = size / cells
        keys = {}
        remap = []
        for i, p in enumerate(P):
            if i in locked:
                key = ("locked", i)
            else:
                uv = mesh.uvs[i]
                key = (int((p[0] - lo[0]) / cell), int((p[1] - lo[1]) / cell), int((p[2] - lo[2]) / cell),
                       groups[i], round(uv[0] * 16), round(uv[1] * 16))
            j = keys.get(key)
            if j is None:
                j = keys[key] = len(keys)
            remap.append(j)
        if len(keys) <= max_vertices:
            break
    count = len(keys)
    first = [-1] * count
    for i, j in enumerate(remap):
        if first[j] < 0:
            first[j] = i
    positions = [P[i] for i in first]
    normals = [mesh.normals[i] for i in first]
    uvs = [mesh.uvs[i] for i in first]
    new_groups = [groups[i] for i in first]
    tris, seen = [], set()
    for t in range(0, len(mesh.tris), 3):
        a, b, cc = (remap[v] for v in mesh.tris[t:t + 3])
        if a == b or b == cc or a == cc:
            continue
        key = tuple(sorted((a, b, cc)))
        if key in seen:
            continue
        seen.add(key)
        tris += (a, b, cc)
    new_locked = {remap[i] for i in locked}
    return Mesh(positions, normals, uvs, tris, new_groups), new_locked


def decimate(mesh: Mesh, target_vertices: int, boundary_weight: float = 50.0,
             locked: Optional[set] = None) -> Mesh:
    """Garland-Heckbert quadric simplification with half-edge collapses (a
    vertex merges into a neighbour and takes its position, UV and normal -
    so UVs never get interpolated across texture seams). Open edges (mesh
    borders and UV seams) get heavy perpendicular-plane quadrics so they
    hold their shape, and collapses that would flip a triangle are
    rejected. `locked` vertices never move or merge away, so any triangle
    made only of locked vertices survives untouched (protected areas) - if
    they alone exceed the budget, the result simply ends up over it."""
    if len(mesh.positions) <= target_vertices:
        return mesh
    locked = locked or set()
    if len(mesh.positions) > PRE_REDUCE_LIMIT and len(locked) < PRE_REDUCE_LIMIT // 2:
        mesh, locked = pre_reduce(mesh, PRE_REDUCE_LIMIT, locked)
        if len(mesh.positions) <= target_vertices:
            return mesh
    pos = mesh.positions
    n = len(pos)
    faces = [list(mesh.tris[i:i + 3]) for i in range(0, len(mesh.tris), 3)]
    vfaces = [set() for _ in range(n)]
    Q = [[0.0] * 10 for _ in range(n)]
    edge_count = {}
    for fi, (a, b, cc) in enumerate(faces):
        for v in (a, b, cc):
            vfaces[v].add(fi)
        nx, ny, nz = _face_normal(pos[a], pos[b], pos[cc])
        ln = math.sqrt(nx * nx + ny * ny + nz * nz)
        if ln < 1e-15:
            continue
        nx, ny, nz = nx / ln, ny / ln, nz / ln
        d = -(nx * pos[a][0] + ny * pos[a][1] + nz * pos[a][2])
        q = _plane_quadric(nx, ny, nz, d, ln * 0.5)
        for v in (a, b, cc):
            Qv = Q[v]
            for k in range(10):
                Qv[k] += q[k]
        for e in ((a, b), (b, cc), (cc, a)):
            key = (min(e), max(e))
            edge_count[key] = edge_count.get(key, 0) + 1
    for (a, b), cnt in edge_count.items():
        if cnt != 1:
            continue
        fi = next(iter(vfaces[a] & vfaces[b]))
        fa, fb, fc = faces[fi]
        fn = _face_normal(pos[fa], pos[fb], pos[fc])
        ex, ey, ez = (pos[b][k] - pos[a][k] for k in range(3))
        px, py, pz = ey * fn[2] - ez * fn[1], ez * fn[0] - ex * fn[2], ex * fn[1] - ey * fn[0]
        ln = math.sqrt(px * px + py * py + pz * pz)
        if ln < 1e-15:
            continue
        px, py, pz = px / ln, py / ln, pz / ln
        d = -(px * pos[a][0] + py * pos[a][1] + pz * pos[a][2])
        q = _plane_quadric(px, py, pz, d, boundary_weight * (ex * ex + ey * ey + ez * ez))
        for v in (a, b):
            for k in range(10):
                Q[v][k] += q[k]

    neighbors = [set() for _ in range(n)]
    for a, b in edge_count:
        neighbors[a].add(b)
        neighbors[b].add(a)
    version = [0] * n
    alive = [bool(vf) for vf in vfaces]
    heap = []

    def push(u, v):
        if u in locked:
            return
        qs = [Q[u][k] + Q[v][k] for k in range(10)]
        heapq.heappush(heap, (_q_err(qs, pos[v]), u, v, version[u], version[v]))

    for a, b in edge_count:
        push(a, b)
        push(b, a)

    live = sum(alive)
    live_faces = [True] * len(faces)
    while live > target_vertices and heap:
        cost, u, v, vu, vv = heapq.heappop(heap)
        if not (alive[u] and alive[v]) or version[u] != vu or version[v] != vv or v not in neighbors[u]:
            continue
        # reject if moving u onto v flips any surviving face of u
        flips = False
        for fi in vfaces[u]:
            f = faces[fi]
            if v in f:
                continue
            old = _face_normal(*(pos[x] for x in f))
            new = _face_normal(*(pos[v] if x == u else pos[x] for x in f))
            if old[0] * new[0] + old[1] * new[1] + old[2] * new[2] <= 0.0:
                flips = True
                break
        if flips:
            continue
        touched = {v}
        for fi in list(vfaces[u]):
            f = faces[fi]
            if v in f:
                live_faces[fi] = False
                for x in f:
                    if x != u:
                        vfaces[x].discard(fi)
                        touched.add(x)
            else:
                f[f.index(u)] = v
                vfaces[v].add(fi)
        vfaces[u] = set()
        for w in neighbors[u]:
            if w != v:
                neighbors[w].discard(u)
                neighbors[w].add(v)
                neighbors[v].add(w)
        neighbors[v].discard(u)
        neighbors[u] = set()
        alive[u] = False
        for k in range(10):
            Q[v][k] += Q[u][k]
        version[v] += 1
        live -= 1
        for x in touched:  # vertices left with no triangles no longer count
            if alive[x] and not vfaces[x]:
                alive[x] = False
                live -= 1
                for w in neighbors[x]:
                    neighbors[w].discard(x)
                neighbors[x] = set()
        for w in neighbors[v]:
            push(v, w)
            push(w, v)

    remap, positions, normals, uvs, groups = {}, [], [], [], []
    tris = []
    for fi, f in enumerate(faces):
        if not live_faces[fi] or len(set(f)) < 3:
            continue
        for x in f:
            if x not in remap:
                remap[x] = len(positions)
                positions.append(pos[x])
                normals.append(mesh.normals[x])
                uvs.append(mesh.uvs[x])
                groups.append(mesh.groups[x] if mesh.groups else -1)
            tris.append(remap[x])
    return Mesh(positions, normals, uvs, tris, groups)


# --- skeleton fitting and bone assignment ---------------------------------------

def _subtree(bones: list, root: int) -> list:
    out, stack = [], [root]
    while stack:
        i = stack.pop()
        out.append(i)
        stack.extend(j for j, b in enumerate(bones) if b.parent == bones[i].name)
    return out


def _rot_about(pivot, axis, deg) -> list:
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    x, y, z = axis
    r = [[ca + x * x * (1 - ca), x * y * (1 - ca) - z * sa, x * z * (1 - ca) + y * sa],
         [y * x * (1 - ca) + z * sa, ca + y * y * (1 - ca), y * z * (1 - ca) - x * sa],
         [z * x * (1 - ca) - y * sa, z * y * (1 - ca) + x * sa, ca + z * z * (1 - ca)]]
    px, py, pz = pivot
    t = [px - (r[0][0] * px + r[0][1] * py + r[0][2] * pz),
         py - (r[1][0] * px + r[1][1] * py + r[1][2] * pz),
         pz - (r[2][0] * px + r[2][1] * py + r[2][2] * pz)]
    return [r[0] + [t[0]], r[1] + [t[1]], r[2] + [t[2]], [0.0, 0.0, 0.0, 1.0]]


def _find_bone(bones: list, *suffixes) -> Optional[int]:
    for i, b in enumerate(bones):
        if b.name.lower() in suffixes:
            return i
    return None


def estimate_arm_drop(mesh: Mesh, bones: list, mats: list) -> float:
    """Degrees the source mesh's arms hang below the skeleton's arms (0 for a
    T-pose source, ~45 for an A-pose one). Measured from the shoulder joint to
    the source's outermost points, per side, averaged."""
    drops = []
    for side in ("l", "r"):
        ua = _find_bone(bones, f"bone_{side}_upperarm")
        tip = _find_bone(bones, f"bone_{side}_hand") or _find_bone(bones, f"bone_{side}_forearm")
        if ua is None or tip is None:
            continue
        sx, sy = mats[ua][0][3], mats[ua][1][3]
        tx, ty = mats[tip][0][3], mats[tip][1][3]
        sign = 1.0 if tx > sx else -1.0
        pts = [p for p in mesh.positions if (p[0] - sx) * sign > 0.1 and p[1] > sy - 0.9]
        if len(pts) < 10:
            continue
        pts.sort(key=lambda p: -(p[0] - sx) * sign)
        tipset = pts[: max(5, len(pts) // 50)]
        mx = sum(p[0] for p in tipset) / len(tipset)
        my = sum(p[1] for p in tipset) / len(tipset)
        src = math.degrees(math.atan2(my - sy, abs(mx - sx)))
        skel = math.degrees(math.atan2(ty - sy, abs(tx - sx)))
        drops.append(skel - src)
    return sum(drops) / len(drops) if drops else 0.0


def pose_arms(bones: list, mats: list, drop_deg: float) -> list:
    """Copy of `mats` with each arm (upperarm subtree) rotated down by
    `drop_deg` around the shoulder, in the body's front plane."""
    out = [list(map(list, m)) for m in mats]
    if abs(drop_deg) < 1e-6:
        return out
    for side in ("l", "r"):
        ua = _find_bone(bones, f"bone_{side}_upperarm")
        if ua is None:
            continue
        pivot = (mats[ua][0][3], mats[ua][1][3], mats[ua][2][3])
        child = next((j for j, b in enumerate(bones) if b.parent == bones[ua].name), None)
        outward = 1.0 if child is None or mats[child][0][3] >= pivot[0] else -1.0
        r = _rot_about(pivot, (0.0, 0.0, 1.0), -drop_deg * outward)
        for i in _subtree(bones, ua):
            out[i] = mat_mul(r, mats[i])
    return out


def template_skin_points(template_payload: bytes, mats: list) -> list:
    """[(world position, skeleton bone index)] for every skinned vertex of a
    model, placed with `mats` (bind or posed world matrices)."""
    body = c.parse_body(template_payload)
    out = []
    for s in body.chunks:
        if s.tag != "segm":
            continue
        seg = decode_segment(s.payload)
        if seg.skinned and seg.bmap:
            out += [(mat_point(mats[seg.bmap[v.bone]], v.pos), seg.bmap[v.bone]) for v in seg.vertices]
    return out


def _shell(r: int):
    """Grid offsets at exactly Chebyshev distance r (the surface of a cube)."""
    if r == 0:
        yield (0, 0, 0)
        return
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            if abs(dx) == r or abs(dy) == r:
                for dz in range(-r, r + 1):
                    yield (dx, dy, dz)
            else:
                yield (dx, dy, -r)
                yield (dx, dy, r)


def transfer_bones(points: list, skin_points: list) -> list:
    """Skin transfer: each point takes the bone of the nearest template vertex
    (the original model's own artist-made skinning). Uses scipy's KD-tree or
    numpy when installed (milliseconds), otherwise a pure-Python grid search."""
    if not skin_points:
        raise SkinnedImportError("The model being replaced has no skinned vertices to copy bones from.")
    if not points:
        return []
    try:
        import numpy as np
    except ImportError:
        return _transfer_bones_grid(points, skin_points)
    ref = np.asarray([p for p, _ in skin_points], dtype=np.float64)
    bones = np.asarray([b for _, b in skin_points])
    query = np.asarray(points, dtype=np.float64)
    try:
        from scipy.spatial import cKDTree
        nearest = cKDTree(ref).query(query)[1]
    except ImportError:
        nearest = np.empty(len(query), dtype=np.int64)
        for start in range(0, len(query), 512):  # chunked so memory stays small
            chunk = query[start:start + 512]
            d = ((chunk[:, None, :] - ref[None, :, :]) ** 2).sum(axis=2)
            nearest[start:start + 512] = d.argmin(axis=1)
    return [int(b) for b in bones[nearest]]


def _transfer_bones_grid(points: list, skin_points: list) -> list:
    """Pure-Python fallback for transfer_bones: a grid sized to the template
    (~40 cells across, so it works the same for a 2m soldier and a 15m
    walker), searched in growing shells that stop as soon as no nearer point
    can exist."""
    lo = [min(p[k] for p, _ in skin_points) for k in range(3)]
    hi = [max(p[k] for p, _ in skin_points) for k in range(3)]
    cell = max(max(hi[k] - lo[k] for k in range(3)) / 40.0, 1e-4)
    grid = {}
    for p, b in skin_points:
        grid.setdefault((int(p[0] // cell), int(p[1] // cell), int(p[2] // cell)), []).append((p, b))
    keys = list(grid)
    kmin = [min(k[i] for k in keys) for i in range(3)]
    kmax = [max(k[i] for k in keys) for i in range(3)]
    out = []
    for p in points:
        g = (int(p[0] // cell), int(p[1] // cell), int(p[2] // cell))
        # no point in searching further out than the far corner of the occupied grid
        r_max = max(max(abs(g[i] - kmin[i]), abs(g[i] - kmax[i])) for i in range(3))
        best, bd, r = None, None, 0
        while r <= r_max:
            if bd is not None and ((r - 1) * cell) ** 2 > bd:
                break  # everything in this shell and beyond is farther than the best so far
            for dx, dy, dz in _shell(r):
                for q, b in grid.get((g[0] + dx, g[1] + dy, g[2] + dz), ()):
                    d = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2
                    if bd is None or d < bd:
                        best, bd = b, d
            r += 1
        out.append(best)
    return out


# --- model rebuild ----------------------------------------------------------------

# Per-SEGMENT ceilings, found by testing in-game: a soldier LOD segment with
# 7,500 vertices / 29,868 indices animates fine; 7,900 / 31,917 animates
# glitchily and the soldier's weapon disappears (while the same model split
# into two smaller segments looked fine). So instead of capping how detailed
# a model can be, big segments are split into several copies of the
# template segment (same material, texture and bone palette), each under
# these limits.
SEGMENT_VERTEX_LIMIT = 7000
SEGMENT_INDEX_LIMIT = 28000
# The model header's triangle count drives the game's detail level: in-game,
# 9,956 there is fine and 10,639 puts the unit in its far-away mode (weapon
# hidden, minimal animation). Capped independently of the model being
# replaced, which may itself be an earlier import with a too-high count.
SAFE_HEADER_FACES = 9000


def _template_key(seg: Segment) -> tuple:
    """Material + bone palette of a segment - two segments with the same key
    are copies made by an earlier import's split (see _split_triangles)."""
    return (tuple(seg.bmap), tuple((t, p) for t, p in seg.children
                                   if t in ("MTRL", "MNAM", "RTYP", "TNAM", "BNAM")))


def _split_triangles(mesh: Mesh, tris: list) -> list:
    """Splits a segment's triangles into spatially compact pieces that each
    stay under SEGMENT_VERTEX_LIMIT vertices and SEGMENT_INDEX_LIMIT indices
    (triangles sorted along the segment's longest axis, then filled greedily)."""
    if not tris:
        return []
    unique = {v for tri in tris for v in tri}
    if len(unique) <= SEGMENT_VERTEX_LIMIT and len(tris) * 3 <= SEGMENT_INDEX_LIMIT:
        return [tris]
    P = mesh.positions
    cents = [tuple(sum(P[v][k] for v in tri) / 3 for k in range(3)) for tri in tris]
    axis = max(range(3), key=lambda k: max(ct[k] for ct in cents) - min(ct[k] for ct in cents))
    order = sorted(range(len(tris)), key=lambda i: cents[i][axis])
    pieces, current, seen = [], [], set()
    for i in order:
        new = [v for v in tris[i] if v not in seen]
        if current and (len(seen) + len(new) > SEGMENT_VERTEX_LIMIT or (len(current) + 1) * 3 > SEGMENT_INDEX_LIMIT):
            pieces.append(current)
            current, seen = [], set()
            new = list(tris[i])
        current.append(tris[i])
        seen.update(new)
    if current:
        pieces.append(current)
    return pieces


_SPECIAL_MATERIAL_FLAGS = 0x4 | 0x80 | 0x100  # swbf1 transparent | additive | glow


def _is_opaque(seg: Segment) -> bool:
    mtrl = next((p for t, p in seg.children if t == "MTRL"), None)
    return mtrl is None or not (struct.unpack_from("<I", mtrl, 0)[0] & _SPECIAL_MATERIAL_FLAGS)


def rebuild_model(template_payload: bytes, bones: list, mesh: Mesh, posed: list,
                  bone_limits: Optional[dict] = None, texture_name: Optional[str] = None) -> bytes:
    """New 'modl' payload with `mesh` (world space, already fitted to the
    skeleton in pose `posed`) replacing the template's geometry.

    Each vertex takes the bone of the nearest template vertex (skin transfer
    from the original model's own skinning); `bone_limits` = {mesh group:
    set of allowed bone names} restricts that for e.g. hair, which should
    ride the head/torso rather than whatever arm it hangs next to.
    Triangles are split across the template's skinned segments by bone
    palette (BMAP), so materials, palettes and chunk layout stay exactly the
    template's. Unskinned segments are kept but emptied to a single
    zero-area triangle. `texture_name` retargets every segment's diffuse
    texture (TNAM slot 0), e.g. at a new atlas."""
    body = c.parse_body(template_payload)
    segs = [(i, decode_segment(s.payload)) for i, s in enumerate(body.chunks) if s.tag == "segm"]
    # New geometry has no tangents, so bump-mapped segments (vehicles) become
    # plain "Normal" ones with just the diffuse texture - otherwise they'd keep
    # the OLD model's normal map (TNAM slot 1). Same layout as the game's own
    # non-bump segments. `texture_name` also retargets the diffuse (slot 0).
    tnam = struct.pack("<I", 0) + texture_name.encode("ascii") + b"\x00" if texture_name else None
    for _, seg in segs:
        kids = []
        for t, p in seg.children:
            if t == "TNAM":
                if struct.unpack_from("<I", p, 0)[0] != 0:
                    continue
                p = tnam or p
            elif t == "RTYP" and c.decode_ascii(p) == "Bump":
                p = b"Normal\x00"
            kids.append((t, p))
        seg.children = kids
    skinned = [(i, s) for i, s in segs if s.skinned and s.bmap]
    if not skinned:
        raise SkinnedImportError("Template model has no skinned segments.")
    # importing over an earlier import: its split-off copies of a segment get
    # merged back into one (and re-split below only if still needed), rather
    # than piling up as extra, empty segments
    firsts, duplicates = {}, set()
    for i, s in skinned:
        key = _template_key(s)
        if key in firsts:
            duplicates.add(i)
        else:
            firsts[key] = i
    skinned = [(i, s) for i, s in skinned if i not in duplicates]
    skin_pts = template_skin_points(template_payload, posed)
    vbone = transfer_bones(mesh.positions, skin_pts)
    for group, allowed in (bone_limits or {}).items():
        pts = [sp for sp in skin_pts if bones[sp[1]].name in allowed]
        idx = [i for i, g in enumerate(mesh.groups) if g == group]
        if pts and idx:
            for i, b in zip(idx, transfer_bones([mesh.positions[i] for i in idx], pts)):
                vbone[i] = b
    inv = {}

    def inv_posed(b):
        if b not in inv:
            inv[b] = mat_inverse(posed[b])
        return inv[b]

    palettes = [set(s.bmap) for _, s in skinned]
    opaque = [_is_opaque(s) for _, s in skinned]
    seg_tris = [[] for _ in skinned]
    for t in range(0, len(mesh.tris), 3):
        tri = mesh.tris[t:t + 3]
        tb = [vbone[v] for v in tri]
        # most of the triangle's bones in the palette; ties go to opaque
        # segments (not a vehicle's glass canopy or see-through grate)
        best = max(range(len(skinned)), key=lambda k: (sum(b in palettes[k] for b in tb), opaque[k]))
        seg_tris[best].append(tri)

    built = {}  # template chunk index -> [(segment, verts, indices)] - more than one piece if split
    all_local, world_bind = [], []
    bind = bind_world_matrices(bones)
    for k, (ci, seg) in enumerate(skinned):
        pal = seg.bmap
        pal_pts = [sp for sp in skin_pts if sp[1] in palettes[k]]
        slot = {b: j for j, b in enumerate(pal)}
        pieces = []
        for chunk in _split_triangles(mesh, seg_tris[k]):
            remap, verts, indices = {}, [], []
            for tri in chunk:
                for v in tri:
                    if v not in remap:
                        b = vbone[v]
                        if b not in slot:
                            b = transfer_bones([mesh.positions[v]], pal_pts)[0]
                        m = inv_posed(b)
                        local = mat_point(m, mesh.positions[v])
                        remap[v] = len(verts)
                        verts.append(Vertex(local, mat_dir(m, mesh.normals[v]), mesh.uvs[v], slot[b]))
                        all_local.append(local)
                        world_bind.append(mat_point(bind[b], local))
                    indices.append(remap[v])
            pieces.append((seg, verts, indices))
        built[ci] = pieces or [(seg, [Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0), 0)] * 3, [0, 1, 2])]

    for ci, seg in segs:
        if ci in duplicates:
            built[ci] = []  # merged into its first copy above - dropped
        elif ci not in built:
            built[ci] = [(seg, [Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0), 0)] * 3, [0, 1, 2])]

    lo = tuple(min(p[k] for p in all_local) for k in range(3))
    hi = tuple(max(p[k] for p in all_local) for k in range(3))
    vlo = tuple(min(p[k] for p in world_bind) for k in range(3))
    vhi = tuple(max(p[k] for p in world_bind) for k in range(3))
    face_count = sum(len(ix) // 3 for pieces in built.values() for _, _, ix in pieces)
    segment_count = sum(len(pieces) for pieces in built.values())

    parts = []
    for i, s in enumerate(body.chunks):
        if s.tag == "segm":
            for seg, verts, ix in built[i]:
                parts.append(c.tagged_block(b"segm", build_segment(seg, verts, ix, (lo, hi))))
            continue
        if s.tag == "INFO":
            info = bytearray(s.payload)
            lead = 12 if len(info) == 68 else 16
            if len(info) == 68:  # (1, segment count, 0) - verified against all 1,677 stock models
                struct.pack_into("<i", info, 4, segment_count)
            struct.pack_into("<6f6f", info, lead, *lo, *hi, *vlo, *vhi)
            # The game picks a unit's detail level from THIS number, not the real
            # geometry: in-game tests showed ~10,000+ here puts a soldier into its
            # far-away mode even up close (weapon hidden, minimal animation - what
            # you see through a sniper scope), while a 52,693-triangle model with
            # the stock count here renders and animates normally. So never report
            # more than the original model did.
            original_faces = struct.unpack_from("<I", s.payload, len(s.payload) - 4)[0]
            struct.pack_into("<I", info, len(info) - 4, min(face_count, original_faces, SAFE_HEADER_FACES))
            payload = bytes(info)
        elif s.tag == "SPHR":
            center = tuple((vlo[k] + vhi[k]) / 2 for k in range(3))
            radius = max(math.sqrt(sum((p[k] - center[k]) ** 2 for k in range(3))) for p in world_bind)
            payload = struct.pack("<4f", *center, radius)
        else:
            payload = s.payload
        parts.append(c.tagged_block(s.tag.encode("ascii"), payload))
    return body.prefix + b"".join(parts)


def _compressed_diff(a: bytes, b: bytes, stride: int, has_bone: bool) -> list:
    """[max pos diff, bone mismatch (0/1), max normal byte diff, max uv diff]
    between two compressed VBUFs of the same layout."""
    worst = [0, 0, 0, 0]
    for o in range(12, len(a), stride):
        pa, pb = struct.unpack_from("<4h", a, o), struct.unpack_from("<4h", b, o)
        worst[0] = max(worst[0], max(abs(x - y) for x, y in zip(pa, pb)))
        k = o + 8
        if has_bone:
            worst[1] = max(worst[1], int(a[k:k + 4] != b[k:k + 4]))
            k += 4
        worst[2] = max(worst[2], max(abs(x - y) for x, y in zip(a[k:k + 4], b[k:k + 4])))
        ua, ub = struct.unpack_from("<2h", a, k + 4), struct.unpack_from("<2h", b, k + 4)
        worst[3] = max(worst[3], max(abs(x - y) for x, y in zip(ua, ub)))
    return worst


def verify_roundtrip(modl_payload: bytes) -> list:
    """Decode every segment of an original model and re-encode it with the
    encoders above; return a list of (segment, chunk, problem) mismatches.
    Empty list = the encoders reproduce the game's own bytes (compressed
    VBUFs allowed +-1 quantization step)."""
    body = c.parse_body(modl_payload)
    info = next((s.payload for s in body.chunks if s.tag == "INFO"), None)
    if info is None:
        return []  # empty placeholder model (e.g. fresh from "Add New Chunk") - nothing to check
    lead = 12 if len(info) == 68 else 16
    box = struct.unpack_from("<6f", info, lead)
    vertex_box = (box[:3], box[3:])
    problems = []
    for si, s in enumerate(x for x in body.chunks if x.tag == "segm"):
        seg = decode_segment(s.payload)
        orig = {}
        for t, p in seg.children:
            orig[(t, struct.unpack_from("<I", p, 8)[0]) if t == "VBUF" else (t, None)] = p
        exact = {("VBUF", VBUF_PLAIN): encode_vbuf_plain(seg.vertices)}
        if seg.skinned:
            exact.update({("VBUF", VBUF_SKINNED): encode_vbuf_skinned(seg.vertices),
                          ("VDAT", None): encode_vbuf_plain(seg.vertices),
                          ("SKIN", None): encode_skin(seg.vertices),
                          ("BMAP", None): encode_bmap(seg.bmap)})
        for key, data in exact.items():
            if key in orig and orig[key] != data:
                problems.append((si, key, f"differs ({len(orig[key])} vs {len(data)} bytes)"))
        for flags, stride, has_bone, enc in ((VBUF_COMPRESSED, 20, True, encode_vbuf_compressed),
                                             (VBUF_PLAIN_COMPRESSED, 16, False, encode_vbuf_plain_compressed)):
            a = orig.get(("VBUF", flags))
            if a is None or len(a) < 12 + stride:
                continue
            b = enc(seg.vertices, vertex_box, a[12 + stride - 5])
            if len(a) != len(b):
                problems.append((si, f"VBUF {flags:#x}", "length differs"))
                continue
            worst = _compressed_diff(a, b, stride, has_bone)
            if worst[1] or max(worst[0], worst[2], worst[3]) > 1:
                problems.append((si, f"VBUF {flags:#x}", f"max diff pos/bone/normal/uv = {worst}"))
    return problems


# --- building blocks for the IDE's "Import Character" dialog -----------------------

LOD_SUFFIXES = ("_low1", "_low2", "LOWD", "_lowrez", "_lowres")
TORSO_BONES = {"bone_head", "bone_neck", "bone_ribcage", "bone_b_spine"}


def is_skinned_model(payload: bytes) -> bool:
    return any(s.tag == "segm" and any(ch.tag in ("BMAP", "SKIN") for ch in c.parse_body(s.payload).chunks)
               for s in c.parse_body(payload).chunks)


def model_vertex_count(payload: bytes) -> int:
    return sum(len(decode_segment(s.payload).vertices)
               for s in c.parse_body(payload).chunks if s.tag == "segm")


def model_height(payload: bytes) -> float:
    """Height of the model's bind-pose visibility box (e.g. ~2.1 for a trooper)."""
    info = next(s.payload for s in c.parse_body(payload).chunks if s.tag == "INFO")
    lead = 12 if len(info) == 68 else 16
    vis = struct.unpack_from("<6f", info, lead + 24)
    return vis[4] - vis[1]


def model_texture(payload: bytes) -> str:
    """Diffuse texture name (TNAM slot 0) of the model's biggest segment -
    its main skin, not e.g. a gunship's small see-through turret balls."""
    best, best_size = "", -1
    for s in c.parse_body(payload).chunks:
        if s.tag != "segm":
            continue
        for ch in c.parse_body(s.payload).chunks:
            if ch.tag == "TNAM" and struct.unpack_from("<I", ch.payload, 0)[0] == 0 and len(s.payload) > best_size:
                best, best_size = c.decode_ascii(ch.payload[4:]), len(s.payload)
    return best


def find_texture(sibling_chunks: list, name: str):
    """The 'tex_' chunk called `name`, ignoring case (models and classes
    don't always spell texture names with the same case as the texture)."""
    low = name.lower()
    return next((ch for ch in sibling_chunks if ch.tag == "tex_" and ch.display_name().lower() == low), None)


def find_lod(sibling_chunks: list, model_name: str):
    """The model's low-detail version next to it (e.g. rep_inf_trooper_low1)."""
    for suffix in LOD_SUFFIXES:
        for ch in sibling_chunks:
            if ch.tag == "modl" and ch.display_name() == model_name + suffix:
                return ch
    return None


def drop_groups(mesh: Mesh, groups) -> Mesh:
    """`mesh` without every triangle that uses one of `groups` (source
    textures), and without the vertices only those triangles used."""
    groups = set(groups)
    if not groups:
        return mesh
    keep = [t for t in range(0, len(mesh.tris), 3) if mesh.groups[mesh.tris[t]] not in groups]
    remap = {}
    for t in keep:
        for v in mesh.tris[t:t + 3]:
            remap.setdefault(v, len(remap))
    order = sorted(remap, key=remap.get)
    pick = lambda values: [values[v] for v in order] if values else []
    return Mesh(pick(mesh.positions), pick(mesh.normals), pick(mesh.uvs),
                [remap[v] for t in keep for v in mesh.tris[t:t + 3]], pick(mesh.groups))


def texture_usage(mesh: Mesh) -> dict:
    """{group: vertex count} - how much of the mesh each source texture covers."""
    out = {}
    for g in mesh.groups:
        out[g] = out.get(g, 0) + 1
    return out


def guess_loose_groups(mesh: Mesh) -> set:
    """Groups that look like hair: nearly all of it above mid-body, reaching
    the top of the head, and hanging a long way down. Those should follow the
    head/torso (TORSO_BONES) instead of whatever limb they hang next to. Only
    a default - the dialog lets you change it. Expects a fitted mesh (feet at 0)."""
    top = max(p[1] for p in mesh.positions) or 1.0
    cx = (min(p[0] for p in mesh.positions) + max(p[0] for p in mesh.positions)) / 2
    by_group = {}
    for p, g in zip(mesh.positions, mesh.groups):
        by_group.setdefault(g, []).append((p[1] / top, abs(p[0] - cx) / top))
    out = set()
    for g, pts in by_group.items():
        ys = sorted(y for y, _ in pts)
        xs = sorted(x for _, x in pts)
        n = len(ys)
        high = sum(1 for y in ys if y > 0.45) / n
        extent = ys[int(n * 0.95)] - ys[int(n * 0.05)]
        # hair hangs near the body's centre line; a texture that also reaches
        # out to the hands (e.g. one material shared by helmet and gloves) isn't hair
        spread = xs[int(n * 0.95)]
        if n >= 20 and high >= 0.95 and extent >= 0.25 and ys[int(n * 0.95)] >= 0.9 and spread <= 0.2:
            out.add(g)
    return out


MODEL_PROPERTIES = {"geometryname", "geometrylowres"}
TEXTURE_PROPERTIES = {"overridetexture"}


def classes_using(sibling_chunks: list, name: str, properties: set) -> list:
    """Class (entc) names in the same container with one of `properties`
    (lower-case .odf property names, e.g. MODEL_PROPERTIES) set to `name`."""
    out = []
    for ch in sibling_chunks:
        if ch.tag != "entc":
            continue
        props = c.decode_ordnance_payload(ch.payload)["props"]
        if any(k.lower() in properties and v == name for k, v in props):
            out.append(ch.display_name())
    return out


def models_textured_with(sibling_chunks: list, texture: str) -> list:
    """Models in the same container whose diffuse texture (TNAM) is `texture`."""
    return [ch.display_name() for ch in sibling_chunks
            if ch.tag == "modl" and ch.payload and model_texture(ch.payload).lower() == texture.lower()]


def auto_vertex_budget(template_vertices: int, lod: bool = False) -> int:
    """Automatic downscale target. Stock soldiers have ~600-2000 vertices per
    model, and a level only has so much memory for everything in it - a
    model several times bigger has been seen to push other classes out of
    the spawn list. Budgets stay near the model being replaced."""
    return max(900 if lod else 1500, round(template_vertices * (2.2 if lod else 1.4)))


def auto_atlas_layout(usage: dict, images: dict, size: int) -> dict:
    """Packs one region per used texture into a `size`x`size` atlas, area
    roughly proportional to how much of the model uses it (damped so small
    parts still get readable space), each keeping its source aspect ratio.
    Returns {group: (x, y, w, h)}."""
    groups = [g for g in usage if usage[g] > 0]
    weights = {g: usage[g] ** 0.75 for g in groups}
    total = sum(weights.values()) or 1.0
    aspect = {g: (images[g].width / images[g].height) if g in images else 1.0 for g in groups}
    scale = 0.95
    while scale > 0.05:
        rects = {}
        for g in groups:
            area = weights[g] / total * size * size * scale
            w = max(8, min(size, int(math.sqrt(area * aspect[g])) // 4 * 4))
            h = max(8, min(size, int(math.sqrt(area / aspect[g])) // 4 * 4))
            rects[g] = (w, h)
        placed, x, y, shelf = {}, 0, 0, 0
        for g in sorted(groups, key=lambda g: -rects[g][1]):
            w, h = rects[g]
            if x + w > size:
                x, y, shelf = 0, y + shelf, 0
            if y + h > size:
                break
            placed[g] = (x, y, w, h)
            x, shelf = x + w, max(shelf, h)
        if len(placed) == len(groups):
            return placed
        scale *= 0.92
    raise SkinnedImportError("Too many textures to pack into one atlas.")


def fill_missing_images(images: dict, usage: dict) -> dict:
    """Untextured parts (group -1) and textures that aren't embedded in the
    .glb get a plain grey swatch so they still have a place in the atlas."""
    from PIL import Image
    out = dict(images)
    for g in usage:
        if g not in out:
            out[g] = Image.new("RGBA", (8, 8), (170, 170, 170, 255))
    return out


@dataclass
class Zone:
    """A protected area drawn on a view of the source .glb: `rect` is
    (left, bottom, right, top) as fractions of the source's bounding box in
    that view - Front: x across, y up; Side: z across, y up (matching
    render_views). It extends all the way through the model in depth."""
    view: str
    rect: tuple


_ZONE_AXES = {"Front": 0, "Side": 2}


def protected_vertices(mesh: Mesh, zones: list = (), groups=()) -> set:
    """Vertices of every triangle that's inside a Zone (by its centre) or
    uses one of the protected texture `groups` - decimate(locked=...) never
    touches those, so those parts keep their full detail."""
    groups = set(groups)
    if not zones and not groups:
        return set()
    lo = [min(p[k] for p in mesh.positions) for k in range(3)]
    hi = [max(p[k] for p in mesh.positions) for k in range(3)]
    span = [(hi[k] - lo[k]) or 1.0 for k in range(3)]
    boxes = [(_ZONE_AXES[z.view], z.rect) for z in zones]
    out = set()
    for t in range(0, len(mesh.tris), 3):
        tri = mesh.tris[t:t + 3]
        keep = bool(mesh.groups) and mesh.groups[tri[0]] in groups
        if not keep and boxes:
            cx = [sum(mesh.positions[v][k] for v in tri) / 3 for k in range(3)]
            y = (cx[1] - lo[1]) / span[1]
            for axis, (a0, b0, a1, b1) in boxes:
                a = (cx[axis] - lo[axis]) / span[axis]
                if min(a0, a1) <= a <= max(a0, a1) and min(b0, b1) <= y <= max(b0, b1):
                    keep = True
                    break
        if keep:
            out.update(tri)
    return out


def model_index_count(payload: bytes) -> int:
    return sum(len(decode_segment(s.payload).indices) for s in c.parse_body(payload).chunks if s.tag == "segm")


def model_segment_count(payload: bytes) -> int:
    return sum(1 for s in c.parse_body(payload).chunks if s.tag == "segm")


# Largest whole models proven in-game so far (the 32,221-vertex / 52,693-
# triangle CE Master Chief on the clone pilot, split into 7 segments, with
# the header triangle count kept at stock - see rebuild_model). Past these
# the budget still works, it's just untested.
TESTED_SOLDIER_VERTICES = 32221
TESTED_VEHICLE_VERTICES = 7969


def tested_vertices(bones: list) -> int:
    return TESTED_SOLDIER_VERTICES if is_character(bones) else TESTED_VEHICLE_VERTICES


def build_character_model(template_payload: bytes, bones: list, mesh: Mesh, target_vertices: int,
                          arm_drop: Optional[float] = None, bone_limits: Optional[dict] = None,
                          texture_name: Optional[str] = None, locked: Optional[set] = None) -> tuple:
    """Decimate (only if bigger than the budget, never touching `locked`
    vertices) + fit the skeleton's arms to the mesh's pose + rebuild. There
    is no cap on detail: segments too big for the game are split (see
    _split_triangles). Returns (payload, stats dict)."""
    src_v, src_t = len(mesh.positions), len(mesh.tris) // 3
    n_locked = len(locked or ())
    # protected vertices come on top of the unprotected part's share - which
    # never drops below half the budget, or a big protected area would leave
    # the rest of the model simplified away to nothing
    target = n_locked + max(target_vertices - n_locked, target_vertices // 2) if n_locked else target_vertices
    dm = decimate(mesh, target, locked=locked) if src_v > target else mesh
    mats = bind_world_matrices(bones)
    drop = estimate_arm_drop(dm, bones, mats) if arm_drop is None else arm_drop
    payload = rebuild_model(template_payload, bones, dm, pose_arms(bones, mats, drop), bone_limits, texture_name)
    return payload, {"source_vertices": src_v, "source_tris": src_t, "vertices": model_vertex_count(payload),
                     "tris": len(dm.tris) // 3, "arm_drop": drop, "bytes": len(payload),
                     "protected": n_locked, "segments": model_segment_count(payload),
                     "indices": model_index_count(payload), "tested": tested_vertices(bones)}


# --- preview renderer ------------------------------------------------------------------

_PREVIEW_POSE = {"bone_r_upperarm": ("z", -55), "bone_l_forearm": ("y", 60),
                 "bone_r_thigh": ("x", -35), "bone_r_calf": ("x", 55), "bone_head": ("y", 35)}


def _axis_rot(axis: str, deg: float) -> list:
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    r = {"x": [[1, 0, 0], [0, ca, -sa], [0, sa, ca]],
         "y": [[ca, 0, sa], [0, 1, 0], [-sa, 0, ca]],
         "z": [[ca, -sa, 0], [sa, ca, 0], [0, 0, 1]]}[axis]
    return [r[0] + [0.0], r[1] + [0.0], r[2] + [0.0], [0.0, 0.0, 0.0, 1.0]]


def model_triangles(payload: bytes, bones: list, pose: Optional[dict] = None) -> list:
    """[(a, b, c, uv centroid)] world-space triangles decoded from a modl's
    own bytes, optionally with bones rotated ({bone name: (axis, degrees)})."""
    names = [b.name for b in bones]
    overrides = {names.index(n): _axis_rot(ax, d) for n, (ax, d) in (pose or {}).items() if n in names}
    mats = bind_world_matrices(bones, overrides)
    body = c.parse_body(payload)
    node = c.decode_ascii(next(s.payload for s in body.chunks if s.tag == "NODE"))
    out = []
    for s in body.chunks:
        if s.tag != "segm":
            continue
        seg = decode_segment(s.payload)
        if seg.skinned:
            world = [mat_point(mats[seg.bmap[v.bone]], v.pos) for v in seg.vertices]
        else:
            bnam = next((p for t, p in seg.children if t == "BNAM"), None)
            bname = c.decode_ascii(bnam) if bnam else node
            m = mats[names.index(bname)] if bname in names else _IDENTITY
            world = [mat_point(m, v.pos) for v in seg.vertices]
        tris = triangles(seg)
        for i in range(0, len(tris) - 2, 3):
            ids = tris[i:i + 3]
            uv = (sum(seg.vertices[j].uv[0] for j in ids) / 3, sum(seg.vertices[j].uv[1] for j in ids) / 3)
            out.append((world[ids[0]], world[ids[1]], world[ids[2]], uv))
    return out


_VIEWS = {  # (screen x, screen y, depth towards the viewer) from world (x, y, z)
    "Front": lambda p: (p[0], p[1], p[2]),
    "Side": lambda p: (p[2], p[1], -p[0]),
    "Back": lambda p: (-p[0], p[1], -p[2]),
    "Top": lambda p: (p[0], p[2], p[1]),
}


def mesh_triangles(mesh: Mesh) -> list:
    """[(a, b, c, uv centroid)] for a Mesh, in the same shape model_triangles gives."""
    out = []
    P, U = mesh.positions, mesh.uvs
    for t in range(0, len(mesh.tris), 3):
        i, j, k = mesh.tris[t:t + 3]
        out.append((P[i], P[j], P[k], ((U[i][0] + U[j][0] + U[k][0]) / 3, (U[i][1] + U[j][1] + U[k][1]) / 3)))
    return out


def render_preview(payload: bytes, bones: list, texture=None, panel: tuple = (230, 300), markers=()):
    """Flat-shaded, textured front / side / back views in bind pose, plus a
    4th view: for characters a front view with a few bones rotated (shows the
    skinning follows the bones), otherwise a top view. `markers` =
    [(world point, label, (r, g, b))] drawn on the bind-pose views (e.g.
    weapon fire points). `texture` is a PIL image (the atlas) or None."""
    names = {b.name for b in bones}
    posable = all(n in names for n in _PREVIEW_POSE)
    bind = model_triangles(payload, bones)
    views = [("Front", "Front", bind, True), ("Side", "Side", bind, True), ("Back", "Back", bind, True)]
    if posable:
        views.append(("Test pose", "Front", model_triangles(payload, bones, _PREVIEW_POSE), False))
    else:
        views.append(("Top", "Top", bind, True))
    return render_views(views, texture, panel, markers)[0]


def render_views(views: list, texture=None, panel: tuple = (230, 300), markers=(), draw_markers: bool = True) -> tuple:
    """Draws `views` = [(label, view name (a _VIEWS key), triangles, show
    markers?)] side by side, each scaled to fit its panel. Returns (image,
    frames) where frames[i] = (view name, scale, ox, oy): a world point p
    lands at pixel (ox + a * scale, oy - b * scale), (a, b, _) = _VIEWS[view](p)."""
    from PIL import Image, ImageDraw
    W, H = panel
    img = Image.new("RGB", (W * len(views), H + 18), (28, 28, 34))
    d = ImageDraw.Draw(img)
    tex = texture.convert("RGB") if texture is not None else None
    light = (0.4, 0.5, 0.75)
    frames = []
    for vi, (label, view, tris, show_markers) in enumerate(views):
        project = _VIEWS[view]
        proj = []
        scale, ox, oy = 1.0, W * vi + W / 2, 18 + H / 2
        for a, b, cc, uv in tris:
            P = [project(p) for p in (a, b, cc)]
            ux, uy, uz = (P[1][k] - P[0][k] for k in range(3))
            vx, vy, vz = (P[2][k] - P[0][k] for k in range(3))
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            shade = 0.35 + 0.65 * abs(nx * light[0] + ny * light[1] + nz * light[2]) / ln
            proj.append((sum(p[2] for p in P), P, shade, uv))
        if proj:
            xs = [p[0] for _, P, _, _ in proj for p in P]
            ys = [p[1] for _, P, _, _ in proj for p in P]
            if show_markers:  # keep far-off fire points in frame too
                xs += [project(m[0])[0] for m in markers]
                ys += [project(m[0])[1] for m in markers]
            w, h = (max(xs) - min(xs)) or 1.0, (max(ys) - min(ys)) or 1.0
            scale = min((W - 12) / w, (H - 12) / h)
            ox = W * vi + W / 2 - (min(xs) + max(xs)) / 2 * scale
            oy = 18 + H / 2 + (min(ys) + max(ys)) / 2 * scale
        proj.sort(key=lambda t: t[0])
        for _, P, shade, uv in proj:
            if tex is not None:
                r, g, b = tex.getpixel((min(tex.width - 1, max(0, int(uv[0] * tex.width))),
                                        min(tex.height - 1, max(0, int(uv[1] * tex.height)))))
            else:
                r = g = b = 225
            d.polygon([(ox + p[0] * scale, oy - p[1] * scale) for p in P],
                      fill=(int(r * shade), int(g * shade), int(b * shade)))
        if show_markers and draw_markers:
            for point, text, color in markers:
                a, b, _ = project(point)
                x, y = ox + a * scale, oy - b * scale
                d.ellipse((x - 4, y - 4, x + 4, y + 4), outline=color, width=2)
                d.line((x - 7, y, x + 7, y), fill=color)
                d.line((x, y - 7, x, y + 7), fill=color)
                if text:
                    d.text((x + 6, y - 12), text, fill=color)
        d.text((W * vi + 6, 3), label, fill=(220, 220, 220))
        frames.append((view, scale, ox, oy))
    return img, frames


# --- collision -------------------------------------------------------------------------
# 'coll' (verified by decoding every stock one back exactly, see
# swbf-unmunge's handle_collision.cpp for the reader): NAME, [MASK], NODE,
# INFO (u32 vertex count, node count, leaf count, index count, then the
# float min/max box), POSI (vertex_count float3), TREE. TREE is a binary
# bounding-volume tree stored parent-first: a NODE is 6 bytes (u8 min
# xyz, u8 max xyz), a LEAF is u8 point count + those 6 box bytes + u16
# polygon indices; every box is quantized to 0..255 of its PARENT's box
# (the root's parent is INFO's box). Leaf polygons wind the opposite way
# to glTF (the game is clockwise-front).
# 'prim' (handle_primitives.cpp): INFO (owner model \0, i32 count), then per
# shape NAME, [MASK], PRNT (bone), XFRM (12 floats, bone-local), DATA (u32
# type: 0 sphere / 2 cylinder / 4 box, then 3 floats of size). Classes
# refer to shapes by name (vehiclecollision = p_vehiclesphere, ...).

def find_collision(sibling_chunks: list, model_name: str) -> tuple:
    """(the model's 'coll' chunk or None, its 'prim' chunk or None)."""
    coll = next((ch for ch in sibling_chunks if ch.tag == "coll" and ch.display_name() == model_name), None)
    prim = None
    for ch in sibling_chunks:
        if ch.tag == "prim":
            info = next((s.payload for s in c.parse_body(ch.payload).chunks if s.tag == "INFO"), b"")
            if info.split(b"\x00", 1)[0].decode("ascii", "replace") == model_name:
                prim = ch
                break
    return coll, prim


def model_has_shadow(payload: bytes) -> bool:
    return any(s.tag == "shdw" for s in c.parse_body(payload).chunks)


def strip_shadows(payload: bytes) -> bytes:
    """The model without its 'shdw' shadow-volume pieces - those still have
    the OLD model's shape. Plenty of stock models ship without any (e.g. the
    low-detail soldiers)."""
    body = c.parse_body(payload)
    return body.prefix + b"".join(c.tagged_block(s.tag.encode("ascii"), s.payload)
                                  for s in body.chunks if s.tag != "shdw")


def _weld(tris: list, tolerance: float) -> Mesh:
    """World-space triangle soup -> welded Mesh (shared vertices), so the
    collision mesh is one connected surface the simplifier can work on."""
    index, positions, flat = {}, [], []
    for tri in tris:
        for p in tri[:3]:
            key = tuple(round(v / tolerance) for v in p)
            if key not in index:
                index[key] = len(positions)
                positions.append(tuple(p))
            flat.append(index[key])
    out = []
    for t in range(0, len(flat), 3):
        if len({flat[t], flat[t + 1], flat[t + 2]}) == 3:
            out += flat[t:t + 3]
    return Mesh(positions, [(0.0, 1.0, 0.0)] * len(positions), [(0.0, 0.0)] * len(positions), out,
                [0] * len(positions))


def _quantize_box(box: tuple, parent: tuple) -> tuple:
    """(6 quantized bytes, the dequantized box they stand for) - rounded
    outward so the stored box always contains the real one."""
    (lo, hi), (plo, phi) = box, parent
    q_lo, q_hi = [], []
    for k in range(3):
        span = phi[k] - plo[k]
        if span <= 0:
            q_lo.append(0)
            q_hi.append(255)
            continue
        q_lo.append(max(0, min(255, math.floor((lo[k] - plo[k]) / span * 255.0))))
        q_hi.append(max(0, min(255, math.ceil((hi[k] - plo[k]) / span * 255.0))))
    deq = (tuple(plo[k] + q_lo[k] / 255.0 * (phi[k] - plo[k]) for k in range(3)),
           tuple(plo[k] + q_hi[k] / 255.0 * (phi[k] - plo[k]) for k in range(3)))
    return bytes(q_lo + q_hi), deq


def build_collision_tree(positions: list, tris: list) -> tuple:
    """(TREE payload, node count, leaf count, index count, root box) for a
    triangle list: one triangle per leaf, split at the median along the
    longest axis."""
    faces = [tuple(tris[i:i + 3]) for i in range(0, len(tris), 3)]
    boxes = []
    for f in faces:
        pts = [positions[v] for v in f]
        boxes.append((tuple(min(p[k] for p in pts) for k in range(3)), tuple(max(p[k] for p in pts) for k in range(3))))
    cents = [tuple((b[0][k] + b[1][k]) / 2 for k in range(3)) for b in boxes]
    root = (tuple(min(p[k] for p in positions) for k in range(3)), tuple(max(p[k] for p in positions) for k in range(3)))
    out, counts = [], [0, 0]

    def union(ids):
        return (tuple(min(boxes[i][0][k] for i in ids) for k in range(3)),
                tuple(max(boxes[i][1][k] for i in ids) for k in range(3)))

    def build(ids, parent):
        q, deq = _quantize_box(union(ids), parent)
        if len(ids) == 1:
            a, b, cc = faces[ids[0]]
            out.append(c.tagged_block(b"LEAF", bytes([3]) + q + struct.pack("<3H", a, cc, b)))
            counts[1] += 1
            return
        out.append(c.tagged_block(b"NODE", q))
        counts[0] += 1
        axis = max(range(3), key=lambda k: max(cents[i][k] for i in ids) - min(cents[i][k] for i in ids))
        ids = sorted(ids, key=lambda i: cents[i][axis])
        mid = len(ids) // 2
        build(ids[:mid], deq)
        build(ids[mid:], deq)

    build(list(range(len(faces))), root)
    return b"".join(out), counts[0], counts[1], 3 * counts[1], root


def rebuild_collision_mesh(template_payload: bytes, bones: list, world_tris: list, target_vertices: int) -> tuple:
    """New 'coll' payload from the new model's bind-pose triangles: welded,
    simplified to `target_vertices`, placed relative to the chunk's NODE bone,
    written in the template's layout. Returns (payload, stats)."""
    body = c.parse_body(template_payload)
    kids = [(s.tag, s.payload) for s in body.chunks]
    node = c.decode_ascii(next(p for t, p in kids if t == "NODE"))
    names = [b.name for b in bones]
    to_local = mat_inverse(bind_world_matrices(bones)[names.index(node)]) if node in names else _IDENTITY
    size = max(max(p[k] for t in world_tris for p in t[:3]) - min(p[k] for t in world_tris for p in t[:3])
               for k in range(3)) or 1.0
    mesh = _weld(world_tris, size * 1e-4)
    mesh = decimate(mesh, target_vertices, boundary_weight=10.0) if len(mesh.positions) > target_vertices else mesh
    if len(mesh.tris) < 3:
        raise SkinnedImportError("The new model left nothing to build a collision mesh from.")
    local = [mat_point(to_local, p) for p in mesh.positions]
    tree, node_count, leaf_count, index_count, root = build_collision_tree(local, mesh.tris)
    info = struct.pack("<4I6f", len(local), node_count, leaf_count, index_count, *root[0], *root[1])
    posi = b"".join(struct.pack("<3f", *p) for p in local)
    parts = []
    for t, p in kids:
        if t == "INFO":
            p = info
        elif t == "POSI":
            p = posi
        elif t == "TREE":
            p = tree
        parts.append(c.tagged_block(t.encode("ascii"), p))
    return body.prefix + b"".join(parts), {"vertices": len(local), "triangles": leaf_count}


def decode_collision_mesh(payload: bytes, strict: bool = False) -> tuple:
    """(positions, triangles) of a 'coll' chunk. `strict` also checks every
    box on the way down contains its polygons - true of everything this
    module writes and of 68 of the 71 stock meshes; the other 3 (e.g.
    all_fly_ywing) have boxes the original tools got wrong, which the game
    evidently tolerates."""
    kids = {s.tag: s.payload for s in c.parse_body(payload).chunks}
    vc, nc, lc, ic = struct.unpack_from("<4I", kids["INFO"])
    box = struct.unpack_from("<6f", kids["INFO"], 16)
    pos = [struct.unpack_from("<3f", kids["POSI"], i * 12) for i in range(vc)]
    items = [(s.tag, s.payload) for s in c.parse_body(kids["TREE"]).chunks]
    tris, i = [], [0]

    def dq(q, parent):
        lo, hi = parent
        return (tuple(lo[k] + q[k] / 255.0 * (hi[k] - lo[k]) for k in range(3)),
                tuple(lo[k] + q[3 + k] / 255.0 * (hi[k] - lo[k]) for k in range(3)))

    def walk(parent):
        tag, p = items[i[0]]
        i[0] += 1
        if tag == "NODE":
            b = dq(p[:6], parent)
            walk(b)
            walk(b)
            return
        b = dq(p[1:7], parent)
        ids = struct.unpack_from(f"<{p[0]}H", p, 7)
        for v in ids if strict else ():
            for k in range(3):
                span = (parent[1][k] - parent[0][k]) or 1.0
                if not (b[0][k] - span / 254 <= pos[v][k] <= b[1][k] + span / 254):
                    raise SkinnedImportError("Collision tree box doesn't contain its polygon.")
        for j in range(2, len(ids)):
            tris.append((ids[0], ids[j], ids[j - 1]))

    walk((box[:3], box[3:]))
    nodes = sum(1 for t, _ in items if t == "NODE")
    leaves = len(items) - nodes
    points = sum(p[0] for t, p in items if t == "LEAF")
    if i[0] != len(items) or (nc, lc, ic) != (nodes, leaves, points):
        raise SkinnedImportError("Collision tree counts don't match its INFO.")
    return pos, tris


def refit_primitives(prim_payload: bytes, bones: list, old_bounds: tuple, new_bounds: tuple) -> tuple:
    """Moves and stretches each collision shape (sphere / cylinder / box) so
    it sits in the same relative spot of the new model's box as it did in
    the old one's - same names, bones and types, so every class reference
    still works. Returns (payload, number of shapes refitted)."""
    (olo, ohi), (nlo, nhi) = old_bounds, new_bounds
    ratio = [((nhi[k] - nlo[k]) / (ohi[k] - olo[k])) if ohi[k] - olo[k] > 1e-6 else 1.0 for k in range(3)]
    uniform = (ratio[0] * ratio[1] * ratio[2]) ** (1.0 / 3.0)
    oc = [(olo[k] + ohi[k]) / 2 for k in range(3)]
    nc = [(nlo[k] + nhi[k]) / 2 for k in range(3)]
    names = [b.name for b in bones]
    mats = bind_world_matrices(bones)
    body = c.parse_body(prim_payload)
    kids = [(s.tag, s.payload) for s in body.chunks]
    parts, bone, xfrm_world, count = [], None, None, 0
    for t, p in kids:
        if t == "PRNT":
            bone = c.decode_ascii(p)
        elif t == "XFRM" and bone in names:
            parent = mats[names.index(bone)]
            local = _mat_from_transform(struct.unpack_from("<12f", p))
            xfrm_world = mat_mul(parent, local)
            center = (xfrm_world[0][3], xfrm_world[1][3], xfrm_world[2][3])
            new_center = tuple(nc[k] + (center[k] - oc[k]) * ratio[k] for k in range(3))
            nl = mat_point(mat_inverse(parent), new_center)
            vals = list(struct.unpack_from("<12f", p))
            vals[9:12] = nl
            p = struct.pack("<12f", *vals)
        elif t == "DATA" and xfrm_world is not None:
            kind, sx, sy, sz = struct.unpack_from("<I3f", p)
            if kind == 4:  # box: stretch each of its own axes by how much the model stretched along it
                axes = [[xfrm_world[r][k] for r in range(3)] for k in range(3)]
                f = []
                for ax in axes:
                    n = math.sqrt(sum(v * v for v in ax)) or 1.0
                    f.append(math.sqrt(sum((ax[j] / n * ratio[j]) ** 2 for j in range(3))))
                sx, sy, sz = sx * f[0], sy * f[1], sz * f[2]
            else:  # sphere / cylinder
                sx, sy, sz = sx * uniform, sy * uniform, sz * uniform
            p = struct.pack("<I3f", kind, sx, sy, sz) + p[16:]
            xfrm_world = None
            count += 1
        parts.append(c.tagged_block(t.encode("ascii"), p))
    return body.prefix + b"".join(parts), count


def model_bounds(payload: bytes, bones: list) -> tuple:
    tris = model_triangles(payload, bones)
    pts = [p for t in tris for p in t[:3]]
    return (tuple(min(p[k] for p in pts) for k in range(3)), tuple(max(p[k] for p in pts) for k in range(3)))


def auto_collision_budget(template_coll_payload: bytes) -> int:
    kids = {s.tag: s.payload for s in c.parse_body(template_coll_payload).chunks}
    vc = struct.unpack_from("<I", kids["INFO"])[0] if "INFO" in kids else 60
    return max(40, min(600, vc * 2))


# --- live orientation view -----------------------------------------------------------

def orientation_triangles(mesh: Mesh, images: dict, limit: int = 3000) -> tuple:
    """Lightweight stand-in for the live 3D orientation view: (up to 5000
    evenly sampled positions - enough to fit it like the real import does -,
    the `limit` biggest triangles, each coloured with its texture's average
    colour)."""
    colors = {}
    for g, im in images.items():
        r, gg, b, *_ = im.convert("RGBA").resize((1, 1)).getpixel((0, 0))
        colors[g] = (r, gg, b)
    P = mesh.positions
    tris = []
    for t in range(0, len(mesh.tris), 3):
        a, b, cc = (P[v] for v in mesh.tris[t:t + 3])
        n = _face_normal(a, b, cc)
        area = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
        g = mesh.groups[mesh.tris[t]] if mesh.groups else -1
        tris.append((area, a, b, cc, colors.get(g, (170, 170, 170))))
    tris.sort(key=lambda x: -x[0])
    step = max(1, len(P) // 5000)
    return P[::step], [t[1:] for t in tris[:limit]]


def render_orientation(fit_points: list, tris: list, turns: tuple, lo: tuple, hi: tuple, axis: str,
                       size: Optional[float], yaw: float, pitch: float, panel: tuple):
    """One frame of the live orientation view: the model turned by `turns`
    (x, y, z degrees) and fitted into the original's box exactly like
    fit_to_bounds, seen from a camera at `yaw`/`pitch`. Shows the ground, the
    original model's box, a yellow arrow for the way the game treats as
    forward, and a red/green/blue X/Y/Z axis marker. Returns a PIL image."""
    from PIL import Image, ImageDraw
    W, H = panel
    r = rotation_matrix(*turns)

    def rot(v):
        return (r[0][0] * v[0] + r[0][1] * v[1] + r[0][2] * v[2],
                r[1][0] * v[0] + r[1][1] * v[1] + r[1][2] * v[2],
                r[2][0] * v[0] + r[2][1] * v[1] + r[2][2] * v[2])

    pts = [rot(p) for p in fit_points]
    mlo = [min(p[k] for p in pts) for k in range(3)]
    mhi = [max(p[k] for p in pts) for k in range(3)]
    ext = [mhi[k] - mlo[k] for k in range(3)]
    k_axis = FIT_AXES[axis]
    target_k = k_axis
    if k_axis is None:
        k_axis = max(range(3), key=lambda k: ext[k])
        target_k = max(range(3), key=lambda k: hi[k] - lo[k])
    s = (size if size else hi[target_k] - lo[target_k]) / (ext[k_axis] or 1.0)
    cx, cz = (mlo[0] + mhi[0]) / 2, (mlo[2] + mhi[2]) / 2
    tx, tz = (lo[0] + hi[0]) / 2, (lo[2] + hi[2]) / 2

    def place(p):
        x, y, z = rot(p)
        return ((x - cx) * s + tx, (y - mlo[1]) * s + lo[1], (z - cz) * s + tz)

    center = [(lo[k] + hi[k]) / 2 for k in range(3)]
    cy, sy_ = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))

    def view(p):  # camera: yaw about Y, then pitch about X; +z out of the screen
        x, y, z = (p[k] - center[k] for k in range(3))
        x, z = x * cy - z * sy_, x * sy_ + z * cy
        y, z = y * cp - z * sp, y * sp + z * cp
        return x, y, z

    radius = max(math.dist(lo, hi) / 2, max(ext) * s * 0.75) or 1.0
    scale = min(W, H) * 0.42 / radius

    def screen(v):
        return (W / 2 + v[0] * scale, H * 0.52 - v[1] * scale)

    img = Image.new("RGB", (W, H), (28, 28, 34))
    d = ImageDraw.Draw(img)
    # ground grid
    g = max(hi[0] - lo[0], hi[2] - lo[2]) * 0.75 or 1.0
    for i in range(-4, 5):
        f = i / 4 * g
        for a, b in (((center[0] + f, lo[1], center[2] - g), (center[0] + f, lo[1], center[2] + g)),
                     ((center[0] - g, lo[1], center[2] + f), (center[0] + g, lo[1], center[2] + f))):
            d.line([screen(view(a)), screen(view(b))], fill=(55, 55, 64))
    # the original model's box
    corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    for i, a in enumerate(corners):
        for j, b in enumerate(corners):
            if i < j and sum(a[k] != b[k] for k in range(3)) == 1:
                d.line([screen(view(a)), screen(view(b))], fill=(90, 90, 110))
    # the model
    light = (0.35, 0.6, 0.72)
    drawn = []
    for a, b, cc, col in tris:
        va, vb, vc = view(place(a)), view(place(b)), view(place(cc))
        n = _face_normal(va, vb, vc)
        ln = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2) or 1.0
        shade = 0.35 + 0.65 * abs(n[0] * light[0] + n[1] * light[1] + n[2] * light[2]) / ln
        drawn.append((va[2] + vb[2] + vc[2], (screen(va), screen(vb), screen(vc)),
                      tuple(int(ch * shade) for ch in col)))
    drawn.sort(key=lambda t: t[0])
    for _, poly, col in drawn:
        d.polygon(poly, fill=col)
    # forward arrow along +z at ground level
    length = max(hi[2] - lo[2], hi[0] - lo[0]) * 0.75
    base = (center[0], lo[1], center[2])
    tip = (center[0], lo[1], center[2] + length)
    wing = length * 0.18
    b2, t2 = screen(view(base)), screen(view(tip))
    d.line([b2, t2], fill=(255, 210, 60), width=3)
    for side in (-1, 1):
        d.line([t2, screen(view((center[0] + side * wing, lo[1], center[2] + length - wing)))],
               fill=(255, 210, 60), width=3)
    d.text((t2[0] + 4, t2[1] - 12), "Front", fill=(255, 210, 60))
    # axis marker
    ox, oy, ln = 26, H - 26, 18
    for vec, col, label in (((1, 0, 0), (235, 80, 80), "X"), ((0, 1, 0), (90, 210, 100), "Y"),
                            ((0, 0, 1), (90, 140, 255), "Z")):
        v = view(tuple(center[k] + vec[k] for k in range(3)))
        ex, ey = ox + v[0] * ln, oy - v[1] * ln
        d.line([(ox, oy), (ex, ey)], fill=col, width=2)
        d.text((ex + 2, ey - 6), label, fill=col)
    return img


# --- weapon / fire points --------------------------------------------------------------

_FIRE_WORDS = ("fire", "gun", "cannon", "missile", "weapon", "barrel", "turret", "launcher")


def weapon_points(bones: list, sibling_chunks: list, model_name: str) -> list:
    """[(bone name, bind-pose world position)] for the points a model's
    weapons fire from / attach to: every bone the classes using this model
    name as FirePointName, plus 'hp_*' bones that look weapon-related
    (hp_cannon_1, hp_fire, hp_weapons...). These stay exactly where the
    original model had them - the new geometry doesn't move them."""
    wanted = set()
    for ch in sibling_chunks:
        if ch.tag != "entc":
            continue
        props = c.decode_ordnance_payload(ch.payload)["props"]
        if not any(k.lower() in MODEL_PROPERTIES and v == model_name for k, v in props):
            continue
        wanted.update(v for k, v in props if k.lower() == "firepointname")
    mats = bind_world_matrices(bones)
    out = []
    for i, b in enumerate(bones):
        low = b.name.lower()
        if b.name in wanted or (low.startswith("hp_") and any(w in low for w in _FIRE_WORDS)):
            out.append((b.name, (mats[i][0][3], mats[i][1][3], mats[i][2][3])))
    return out


def hard_points(bones: list, sibling_chunks: list, model_name: str) -> list:
    """[(bone name, bind-pose world position, is a fire point?)] for every
    'hp_*' bone (fire points, gunner/passenger seats, damage smoke...) plus
    any other bone a class names as its FirePointName. None of these are
    ever skinned to (checked on all 1,039 in the stock game), so moving one
    moves only that point, never any geometry."""
    fire = {name for name, _ in weapon_points(bones, sibling_chunks, model_name)}
    mats = bind_world_matrices(bones)
    return [(b.name, (mats[i][0][3], mats[i][1][3], mats[i][2][3]), b.name in fire)
            for i, b in enumerate(bones) if b.name in fire or b.name.lower().startswith("hp_")]


def find_skeleton_chunk(sibling_chunks: list, model_name: str):
    """The 'skel' chunk whose INFO names `model_name`, or None."""
    for ch in sibling_chunks:
        if ch.tag != "skel":
            continue
        try:
            owner, _ = decode_skeleton_chunk(ch.payload)
        except (SkinnedImportError, ValueError, struct.error):
            continue
        if owner == model_name:
            return ch
    return None


def move_bones(bones: list, targets: dict) -> list:
    """A copy of `bones` with each bone named in `targets` (name -> bind-pose
    world position) moved there. Only the translation changes (the point
    keeps its aim direction), and any child bones are compensated so they
    stay exactly where they were."""
    mats = bind_world_matrices(bones)
    index = {b.name: i for i, b in enumerate(bones)}
    # rotations never change, so every bone's world matrix is its old one
    # with (for the moved ones) a new translation
    world = [[row[:] for row in m] for m in mats]
    for name, pos in targets.items():
        if name in index:
            for k in range(3):
                world[index[name]][k][3] = pos[k]
    out = list(bones)
    for i, b in enumerate(bones):
        p = index.get(b.parent)
        if b.name not in targets and (p is None or bones[p].name not in targets):
            continue  # neither it nor its parent moved
        pos = tuple(world[i][k][3] for k in range(3))
        t = list(b.transform)
        t[9:12] = mat_point(mat_inverse(world[p]), pos) if p is not None else pos
        out[i] = Bone(b.name, b.parent, tuple(t))
    return out


def encode_skeleton_transforms(skel_payload: bytes, bones: list) -> bytes:
    """`skel_payload` with its XFRM replaced by `bones`' transforms (same
    bones, same order - only positions/rotations change)."""
    body = c.parse_body(skel_payload)
    xfrm = b"".join(struct.pack("<12f", *b.transform) for b in bones)
    parts = []
    for s in body.chunks:
        if s.tag == "XFRM":
            if len(s.payload) != len(xfrm):
                raise SkinnedImportError("Skeleton bone count changed - can't rewrite its transforms.")
            parts.append(c.tagged_block(b"XFRM", xfrm))
        else:
            parts.append(c.tagged_block(s.tag.encode("ascii"), s.payload))
    return body.prefix + b"".join(parts)


def nearest_surface_point(point: tuple, payload: bytes, bones: list) -> tuple:
    """The model vertex (bind pose, world space) closest to `point`."""
    verts = [p for t in model_triangles(payload, bones) for p in t[:3]]
    if not verts:
        return tuple(point)
    return tuple(min(verts, key=lambda q: (q[0] - point[0]) ** 2 + (q[1] - point[1]) ** 2 + (q[2] - point[2]) ** 2))


def weapon_point_report(points: list, payload: bytes, bones: list) -> list:
    """[(bone name, distance to the new model's surface vertices, off_model?)]
    - a fire point well away from the new model means shots will appear to
    come out of thin air."""
    tris = model_triangles(payload, bones)
    verts = [p for t in tris for p in t[:3]]
    if not verts:
        return []
    size = max(max(p[k] for p in verts) - min(p[k] for p in verts) for k in range(3))
    limit = max(0.15, size * 0.05)
    try:
        import numpy as np
        V = np.asarray(verts)
        dist = lambda p: float(np.sqrt(((V - np.asarray(p)) ** 2).sum(axis=1)).min())
    except ImportError:
        dist = lambda p: min(math.dist(p, q) for q in verts)
    return [(name, d, d > limit) for name, p in points for d in [dist(p)]]


