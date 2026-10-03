"""
bf1_media.py - list and extract the game's movies (.mvs) and sound banks (.bnk).

Both are UCFB files like a .lvl, but every tag and field name is stored as
an FNV-1a hash (bf1_core.fnv1a32) instead of text - cracked by hashing
likely names:

  .mvs  ucfb > [hash] > [hash] > info, data
        info = name <file>, numsegments N, segmentinfo,
               N x (segment, name <movie>, length L, padding P, segmentend)
        data = the movies back to back, each a complete Bink video (BIKi)
               followed by P bytes of padding (L + P is a multiple of 2048)

  .bnk  ucfb > [hash] > samplebank > info, data
        info = name, format 2, numsamples N, size <total>, ..., sampleinfo,
               N x (sample, id <sample>, frequency Hz, size bytes,
                    sizesamples frames, [alias <other id>], padding, sampleend)
        data = 16-bit mono PCM, the samples back to back in info order.
               An 'alias' sample has no audio of its own - it plays the
               sample it names (checked: the alias sizes account exactly for
               the gap between the summed sizes and the bank's total).

Names are only stored as hashes. KNOWN_NAMES holds the ones recovered by
hashing every string in the stock game's .lvl files and the map naming pattern (32 of
shell.mvs's 55 movies, plus tat3fly); everything else is named by its hash.
"""
from __future__ import annotations

import io
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path

from bf1_core import fnv1a32

_H = {name: fnv1a32(name) for name in (
    "info", "data", "name", "numsegments", "segmentinfo", "segment", "length", "padding", "segmentend",
    "format", "numsamples", "size", "sampleinfo", "sample", "id", "frequency", "sizesamples", "alias",
    "sampleend", "samplebank")}
_MARKERS = {_H[k] for k in ("segmentinfo", "segment", "segmentend", "sampleinfo", "sample", "sampleend")}

# hash -> name, recovered from the stock game's own strings (see module docstring)
KNOWN_NAMES = {fnv1a32(n): n for n in (
    "main", "bes1fly", "bes1h01", "bes2fly", "bes2h01", "end1fly", "end1h02", "end1h03", "geo1fly", "geo1h01",
    "hot1fly", "hot1h02", "kam1fly", "kam1h01", "kam1h02", "kas1fly", "kas2fly", "kas2h01", "nab1fly",
    "nab1h01", "nab2fly", "nab2h01", "rhn1fly", "rhn2fly", "tat1fly", "tat1h01", "tat2fly", "tat2h01",
    "tat3fly", "yav1fly", "yav1h01", "yav2fly", "yav2h01")}


class MediaError(Exception):
    pass


@dataclass
class MediaEntry:
    hash: int
    offset: int        # absolute position in the file
    length: int        # bytes
    frequency: int = 0  # sounds only (Hz)
    alias_of: int | None = None

    @property
    def name(self) -> str:
        return KNOWN_NAMES.get(self.hash, f"{self.hash:08x}")

    @property
    def seconds(self) -> float:
        return self.length / 2 / self.frequency if self.frequency else 0.0


@dataclass
class MediaFile:
    path: Path
    kind: str                     # "movies" or "sounds"
    entries: list = field(default_factory=list)

    @property
    def extension(self) -> str:
        return ".bik" if self.kind == "movies" else ".wav"


def _chunks(f, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        tag, size = struct.unpack("<II", f.read(8))
        yield tag, pos + 8, size
        pos += 8 + size + (-size) % 4


def _fields(raw: bytes) -> list:
    """The info block as [(key hash, value or None)] - section markers have no value."""
    words = struct.unpack_from(f"<{len(raw) // 4}I", raw)
    out, i = [], 0
    while i < len(words):
        if words[i] in _MARKERS:
            out.append((words[i], None))
            i += 1
        else:
            out.append((words[i], words[i + 1] if i + 1 < len(words) else None))
            i += 2
    return out


def _records(fields: list, begin: int, end: int) -> list:
    records, current = [], None
    for key, value in fields:
        if key == begin:
            current = {}
        elif key == end:
            if current is not None:
                records.append(current)
            current = None
        elif current is not None:
            current[key] = value
    return records


def read_media(path) -> MediaFile:
    """Reads a .mvs or .bnk's table of contents (not the media itself)."""
    path = Path(path)
    with open(path, "rb") as f:
        if f.read(4) != b"ucfb":
            raise MediaError(f"{path.name} isn't a UCFB file.")
        total = path.stat().st_size
        _, outer, outer_size = next(_chunks(f, 8, total))
        _, inner, inner_size = next(_chunks(f, outer, outer + outer_size))
        parts = {tag: (pos, size) for tag, pos, size in _chunks(f, inner, inner + inner_size)}
        if _H["info"] not in parts or _H["data"] not in parts:
            raise MediaError(f"{path.name} has no info/data block - not a movie or sound bank file.")
        info_pos, info_size = parts[_H["info"]]
        data_pos, data_size = parts[_H["data"]]
        f.seek(info_pos)
        fields = _fields(f.read(info_size))

        segments = _records(fields, _H["segment"], _H["segmentend"])
        samples = _records(fields, _H["sample"], _H["sampleend"])
        if segments:
            media, pos = MediaFile(path, "movies"), data_pos
            for rec in segments:
                media.entries.append(MediaEntry(rec[_H["name"]], pos, rec[_H["length"]]))
                pos += rec[_H["length"]] + rec.get(_H["padding"], 0)
            if pos - data_pos > data_size:
                raise MediaError("The movie table doesn't fit the data - unexpected .mvs layout.")
            return media
        if samples:
            media, pos, by_id = MediaFile(path, "sounds"), data_pos, {}
            for rec in samples:
                entry = MediaEntry(rec[_H["id"]], pos, rec[_H["size"]], rec.get(_H["frequency"], 22050),
                                   rec.get(_H["alias"]))
                if entry.alias_of is None:
                    pos += entry.length + rec.get(_H["padding"], 0)
                media.entries.append(entry)
                by_id[entry.hash] = entry
            for entry in media.entries:  # aliases play another sample's audio
                if entry.alias_of is not None and entry.alias_of in by_id:
                    target = by_id[entry.alias_of]
                    entry.offset, entry.length = target.offset, min(entry.length, target.length)
            if pos - data_pos > data_size:
                raise MediaError("The sample table doesn't fit the data - unexpected .bnk layout.")
            return media
        raise MediaError(f"{path.name} lists no movies or sounds.")


def entry_bytes(media: MediaFile, entry: MediaEntry) -> bytes:
    """The entry as a ready-to-save file: a .bik for movies, a .wav for sounds."""
    with open(media.path, "rb") as f:
        f.seek(entry.offset)
        raw = f.read(entry.length)
    if media.kind == "movies":
        return raw
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(entry.frequency or 22050)
        w.writeframes(raw)
    return out.getvalue()


def extract(media: MediaFile, out_dir, entries=None, progress=None) -> list:
    """Writes `entries` (default: all) into `out_dir`; returns the paths.
    `progress(done, total)` is called after each file."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chosen = list(entries if entries is not None else media.entries)
    written = []
    for i, entry in enumerate(chosen):
        target = out_dir / (entry.name + media.extension)
        target.write_bytes(entry_bytes(media, entry))
        written.append(target)
        if progress:
            progress(i + 1, len(chosen))
    return written
