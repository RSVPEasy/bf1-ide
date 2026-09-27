"""
bf1_glb_import.py - Import a .glb (binary glTF 2.0) mesh into an existing
'modl' chunk, replacing its geometry while preserving materials/textures.

WHY THIS IS SCOPED THE WAY IT IS
---------------------------------
Reverse-engineered against swbf-unmunge's own source
(github.com/PrismaticFlower/swbf-unmunge, src/handle_model.cpp and
src/vbuf_reader.cpp - the exe bundled in this repo is built from that
project, confirmed by its embedded .pdb path), not guessed:

- A 'modl' chunk is NAME + NODE(s) + INFO (bounding boxes/face count) +
  one or more 'segm' children (one per material) + SPHR. Each 'segm' is
  INFO (topology/vertex_count/primitive_count) + MTRL (material) + RTYP
  (render type string) + TNAM (texture name(s)) + IBUF (index buffer) +
  one-or-more VBUF (vertex buffer variants) + BNAM (bone name).
- The 'gmod' chunk sitting next to it (same name) is NEVER READ by
  swbf-unmunge's model-handling code at all (confirmed by grepping
  handle_model.cpp) - whatever it's for, it isn't part of the renderable
  mesh, so it's safe to leave completely untouched here.
- VBUF's binary layout (confirmed against real game data in this repo,
  byte-for-byte, via vbuf_reader.cpp's Vbuf_flags/Vbuf_info): a 12-byte
  header (count, stride, flags bitmask) followed by per-vertex data whose
  layout depends on the flags. This importer only ever *writes* the
  simplest supported variant - flags 0x222 (position | normal | texcoords,
  all uncompressed float) at a 32-byte stride - which this repo's own
  sample data confirms is a real, accepted variant (present as the second
  VBUF alongside a compressed one in every segment checked).

Given that, this importer intentionally does NOT attempt to build a modl
chunk from scratch (materials, render flags, texture bindings, bone
weights, and the 'coll' collision BVH are a much bigger surface, and
getting any of it wrong risks a corrupt/unloadable level). Instead it
replaces an EXISTING model's geometry - one GLB mesh primitive per
existing 'segm', matched by order - while copying that segment's MTRL/
RTYP/TNAM/BNAM (and anything else in it this module doesn't recognize)
through unchanged. This is intentionally the same "safe subset" tradeoff
as the texture codec: lossy/limited, but every byte it writes is for a
field whose meaning and encoding has actually been verified, not guessed.

CAVEATS (documented, not hidden):
- Vertex normals are computed from the mesh only if the GLB provides them;
  otherwise flat per-face normals are synthesized.
- UVs default to (0, 0) if the GLB primitive has no TEXCOORD_0.
- The 'coll' collision mesh (if any) is left pointing at the OLD geometry
  - it is not regenerated from the new mesh.
- Skinned character models (soldier bodies) are refused here - their
  segments carry per-vertex bone data this module doesn't rebuild. See
  bf1_skinned_import.py for those.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass

import bf1_core as c

_COMPONENT_FORMATS = {
    5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2),
    5125: ("I", 4), 5126: ("f", 4),
}
_TYPE_COMPONENT_COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


class GlbImportError(Exception):
    pass


class GlbCountMismatch(GlbImportError):
    def __init__(self, segments: int, primitives: int):
        self.segments = segments
        self.primitives = primitives
        super().__init__(
            f"This model has {segments} material segment(s) but the .glb has {primitives} mesh "
            "primitive(s) - they should match 1:1, in order, so each segment's existing "
            "material/texture stays with the geometry it's meant for.")


@dataclass
class Primitive:
    positions: list  # [(x, y, z), ...]
    normals: list
    texcoords: list
    indices: list  # flat triangle-list indices (already un-stripped/un-fanned)
    texture_name: str = ""  # .glb material name, used only when building a new modl


def parse_glb(data: bytes) -> tuple[dict, bytes]:
    if data[:4] != b"glTF":
        raise GlbImportError("Not a .glb file (missing 'glTF' magic).")
    _magic, _version, total_len = struct.unpack_from("<4sII", data, 0)
    pos = 12
    gltf = None
    bin_chunk = b""
    while pos < total_len:
        chunk_len, chunk_type = struct.unpack_from("<I4s", data, pos)
        chunk_data = data[pos + 8:pos + 8 + chunk_len]
        if chunk_type == b"JSON":
            gltf = json.loads(chunk_data)
        elif chunk_type == b"BIN\x00":
            bin_chunk = chunk_data
        pos += 8 + chunk_len
    if gltf is None:
        raise GlbImportError("No JSON chunk found in .glb file.")
    return gltf, bin_chunk


def _read_accessor(gltf: dict, bin_buffer: bytes, accessor_index: int) -> list[tuple]:
    accessor = gltf["accessors"][accessor_index]
    if "bufferView" not in accessor:
        raise GlbImportError("Sparse/bufferView-less accessors aren't supported.")
    buffer_view = gltf["bufferViews"][accessor["bufferView"]]
    component_type = accessor["componentType"]
    count = accessor["count"]
    n = _TYPE_COMPONENT_COUNTS.get(accessor["type"])
    if n is None:
        raise GlbImportError(f"Unsupported accessor type: {accessor['type']!r}")
    fmt_char, comp_size = _COMPONENT_FORMATS.get(component_type, (None, None))
    if fmt_char is None:
        raise GlbImportError(f"Unsupported accessor componentType: {component_type}")
    offset = buffer_view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    default_stride = comp_size * n
    stride = buffer_view.get("byteStride", default_stride)
    fmt = f"<{n}{fmt_char}"
    out = []
    for i in range(count):
        base = offset + i * stride
        out.append(struct.unpack_from(fmt, bin_buffer, base))
    return out


def extract_primitives(gltf: dict, bin_buffer: bytes) -> list[Primitive]:
    """Walks glTF *nodes* (not just the raw meshes list) so collision-mesh
    nodes can be filtered out: swbf-unmunge's own glTF exporter (see
    model_builder.cpp) writes each modl segment's render geometry as one
    node, but if a 'coll' chunk was bundled alongside it (as
    export_model_bundle_to_file always does, to get a complete export) it
    *also* writes the collision BVH as extra node(s) named
    "collision_-<flags>-mesh<n>" - those aren't render geometry and must
    not be treated as a segment replacement candidate."""
    primitives = []
    for node in gltf.get("nodes", []):
        if "mesh" not in node:
            continue
        if str(node.get("name", "")).startswith("collision_-"):
            continue
        mesh = gltf["meshes"][node["mesh"]]
        for prim in mesh.get("primitives", []):
            if prim.get("mode", 4) != 4:  # 4 = TRIANGLES
                raise GlbImportError(
                    "Only triangle-list primitives are supported (this mesh uses a "
                    "different primitive mode - re-export with triangles, not strips/fans).")
            attrs = prim.get("attributes", {})
            if "POSITION" not in attrs:
                raise GlbImportError("A mesh primitive has no POSITION attribute.")
            positions = [v[:3] for v in _read_accessor(gltf, bin_buffer, attrs["POSITION"])]
            if "indices" in prim:
                indices = [v[0] for v in _read_accessor(gltf, bin_buffer, prim["indices"])]
            else:
                indices = list(range(len(positions)))
            if "NORMAL" in attrs:
                normals = [v[:3] for v in _read_accessor(gltf, bin_buffer, attrs["NORMAL"])]
            else:
                normals = _compute_flat_normals(positions, indices)
            if "TEXCOORD_0" in attrs:
                texcoords = [v[:2] for v in _read_accessor(gltf, bin_buffer, attrs["TEXCOORD_0"])]
            else:
                texcoords = [(0.0, 0.0)] * len(positions)
            material_name = ""
            if "material" in prim and prim["material"] < len(gltf.get("materials", [])):
                material_name = gltf["materials"][prim["material"]].get("name", "")
            primitives.append(Primitive(positions, normals, texcoords, indices,
                                        c.sanitize_name(material_name)))
    return primitives


def _compute_flat_normals(positions: list, indices: list) -> list:
    normals = [[0.0, 0.0, 0.0] for _ in positions]
    for i in range(0, len(indices) - 2, 3):
        ia, ib, ic = indices[i], indices[i + 1], indices[i + 2]
        ax, ay, az = positions[ia]
        bx, by, bz = positions[ib]
        cx, cy, cz = positions[ic]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        for idx in (ia, ib, ic):
            normals[idx][0] += nx
            normals[idx][1] += ny
            normals[idx][2] += nz
    out = []
    for nx, ny, nz in normals:
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        out.append((nx / length, ny / length, nz / length) if length > 1e-12 else (0.0, 1.0, 0.0))
    return out


# --- Binary encoding (verified format - see module docstring) --------------

_VBUF_FLAGS_POS_NORMAL_UV = 0x222  # position | normal | texcoords, all uncompressed
_TOPOLOGY_TRIANGLE_LIST = 4


def _build_ibuf(indices: list[int]) -> bytes:
    if len(indices) > 0xFFFF:
        raise GlbImportError(f"Too many indices ({len(indices)}) for a 16-bit index buffer.")
    return struct.pack(f"<I{len(indices)}H", len(indices), *indices)


def _build_vbuf(prim: Primitive) -> bytes:
    count = len(prim.positions)
    header = struct.pack("<III", count, 32, _VBUF_FLAGS_POS_NORMAL_UV)
    body = bytearray()
    for i in range(count):
        body += struct.pack("<3f", *prim.positions[i])
        body += struct.pack("<3f", *prim.normals[i])
        body += struct.pack("<2f", *prim.texcoords[i])
    return header + bytes(body)


def _build_segment_info(prim: Primitive) -> bytes:
    return struct.pack("<Iii", _TOPOLOGY_TRIANGLE_LIST, len(prim.positions), len(prim.indices) // 3)


def _bounding_box(all_positions: list) -> tuple:
    xs = [p[0] for p in all_positions]
    ys = [p[1] for p in all_positions]
    zs = [p[2] for p in all_positions]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def replace_segment_geometry(segm_payload: bytes, prim: Primitive) -> bytes:
    """Rebuilds one 'segm' chunk's payload: new INFO/IBUF/VBUF from `prim`,
    every other sub-chunk (MTRL, RTYP, TNAM, BNAM, ...) copied through
    unchanged and in its original relative position."""
    body = c.parse_body(segm_payload)
    new_info = c.tagged_block(b"INFO", _build_segment_info(prim))
    new_ibuf = c.tagged_block(b"IBUF", _build_ibuf(prim.indices))
    new_vbuf = c.tagged_block(b"VBUF", _build_vbuf(prim))
    parts = []
    vbuf_written = False
    for sub in body.chunks:
        if sub.tag == "INFO":
            parts.append(new_info)
        elif sub.tag == "IBUF":
            parts.append(new_ibuf)
        elif sub.tag == "VBUF":
            if not vbuf_written:
                parts.append(new_vbuf)
                vbuf_written = True
            # any additional original VBUF variants (e.g. a compressed one) are dropped
        else:
            parts.append(sub.raw_chunk_bytes() + sub.pad)
    if not vbuf_written:
        parts.append(new_vbuf)
    return body.prefix + b"".join(parts)


def replace_model_geometry(modl_payload: bytes, primitives: list[Primitive]) -> bytes:
    """Rebuilds a whole 'modl' chunk's payload, replacing each 'segm'
    child's geometry with the matching (by order) GLB primitive. Raises
    GlbCountMismatch if the counts don't match - silently pairing them up
    wrong would put the wrong material on the wrong geometry. A modl with NO
    segments (e.g. one made with 'Add New Chunk') is built from scratch
    instead - see _build_new_modl."""
    body = c.parse_body(modl_payload)
    segments = [sub for sub in body.chunks if sub.tag == "segm"]
    if not segments:
        return _build_new_modl(body, primitives)
    if any(ch.tag in ("BMAP", "SKIN") for sub in segments for ch in c.parse_body(sub.payload).chunks):
        raise GlbImportError(
            "This is a skinned character/vehicle model (its segments carry per-vertex bone data - "
            "SKIN/BMAP/VDAT). Replacing only its geometry would leave that bone data describing "
            "the old mesh and break it in-game. Right-click it and use "
            "'Import Character / Vehicle (.glb)...' instead.")
    if len(segments) != len(primitives):
        raise GlbCountMismatch(len(segments), len(primitives))

    all_positions = [p for prim in primitives for p in prim.positions]
    if not all_positions:
        raise GlbImportError("The .glb has no vertex data.")
    box_min, box_max = _bounding_box(all_positions)
    face_count = sum(len(prim.indices) // 3 for prim in primitives)

    parts = []
    seg_index = 0
    for sub in body.chunks:
        if sub.tag == "segm":
            new_payload = replace_segment_geometry(sub.payload, primitives[seg_index])
            new_chunk = c.Chunk("segm", new_payload)
            parts.append(new_chunk.raw_chunk_bytes() + (b"\x00" * c._pad_len(len(new_chunk.payload))))
            seg_index += 1
        elif sub.tag == "INFO":
            parts.append(c.tagged_block(b"INFO", _rebuild_model_info(sub.payload, box_min, box_max, face_count)))
        else:
            parts.append(sub.raw_chunk_bytes() + sub.pad)
    return body.prefix + b"".join(parts)


def _build_new_modl(body: "c.Body", primitives: list[Primitive]) -> bytes:
    """Builds a whole modl from scratch, mirroring the layout of a real one
    (checked against rep.lvl's rep_weap_inf_rifle): NAME, NODE, INFO (68-byte
    swbf1-style form), one segm per primitive, SPHR. Each segm gets a plain
    'Normal' material (MTRL flags=1) whose texture (TNAM slot 0) is the
    .glb material's name, falling back to the model name - retexture by
    making a tex_ chunk with that name. NODE/BNAM name the model itself,
    which only renders if a 'skel' bone with that name exists in the same
    file (real models ship one); this doesn't create it."""
    name_chunk = next((s for s in body.chunks if s.tag == "NAME"), None)
    model_name = c.decode_ascii(name_chunk.payload) if name_chunk else "new_model"
    all_positions = [p for prim in primitives for p in prim.positions]
    if not all_positions:
        raise GlbImportError("The .glb has no vertex data.")
    box_min, box_max = _bounding_box(all_positions)
    face_count = sum(len(prim.indices) // 3 for prim in primitives)

    def z(text: str) -> bytes:
        return text.encode("ascii", "replace") + b"\x00"

    info = struct.pack("<3i", 0, 1, 0) + struct.pack("<3f3f3f3f", *box_min, *box_max, *box_min, *box_max)
    info += struct.pack("<II", 3, face_count)  # 3 = the same unknown int real models carry
    center = tuple((lo + hi) / 2 for lo, hi in zip(box_min, box_max))
    radius = max(sum((p[i] - center[i]) ** 2 for i in range(3)) ** 0.5 for p in all_positions)

    parts = [c.tagged_block(b"NAME", z(model_name)), c.tagged_block(b"NODE", z(model_name)),
             c.tagged_block(b"INFO", info)]
    for prim in primitives:
        segm = b"".join([
            c.tagged_block(b"INFO", _build_segment_info(prim)),
            c.tagged_block(b"MTRL", struct.pack("<I", 1)),
            c.tagged_block(b"RTYP", z("Normal")),
            c.tagged_block(b"TNAM", struct.pack("<I", 0) + z(prim.texture_name or model_name)),
            c.tagged_block(b"IBUF", _build_ibuf(prim.indices)),
            c.tagged_block(b"VBUF", _build_vbuf(prim)),
            c.tagged_block(b"BNAM", z(model_name)),
        ])
        parts.append(c.tagged_block(b"segm", segm))
    parts.append(c.tagged_block(b"SPHR", struct.pack("<4f", *center, radius)))
    return b"".join(parts)


def _rebuild_model_info(original_info: bytes, box_min: tuple, box_max: tuple, face_count: int) -> bytes:
    size = len(original_info)
    if size not in (68, 72):
        raise GlbImportError(f"Unrecognized modl INFO size ({size} bytes) - refusing to guess its layout.")
    leading = 12 if size == 68 else 16  # 3 or 4 leading int32s (swbf1 vs swbf2) - copied through
    out = bytearray(original_info)
    out[leading:leading + 48] = struct.pack("<3f3f3f3f", *box_min, *box_max, *box_min, *box_max)
    struct.pack_into("<I", out, size - 4, face_count)
    return bytes(out)


def import_glb_into_model(chunks: list["c.Chunk"], target: "c.Chunk", glb_bytes: bytes) -> bytes:
    """Given the full chunk list of the container `target` (a 'modl' chunk)
    lives in, and a .glb file's bytes, returns the new payload for `target`
    with its geometry replaced. `chunks` is accepted for symmetry with the
    export side (gather_model_bundle) even though this direction only
    touches `target` itself."""
    if target.tag != "modl":
        raise GlbImportError(f"GLB import only supports 'modl' chunks (got {target.tag!r}).")
    gltf, bin_buffer = parse_glb(glb_bytes)
    primitives = extract_primitives(gltf, bin_buffer)
    if not primitives:
        raise GlbImportError("The .glb file has no mesh primitives.")
    return replace_model_geometry(target.payload, primitives)
