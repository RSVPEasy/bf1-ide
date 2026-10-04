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

Names are only stored as hashes. With Phantom's dictionary.txt next to the tools
(see sound_names) every stock sound and movie gets its real name; without it,
KNOWN_NAMES holds the ones recovered by
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


DICTIONARY_FILE = "dictionary.txt"
DICTIONARY_URL = "https://github.com/phantom567459/SoundFMVextractor"
_sound_names = None


def sound_names() -> dict:
    """hash -> name from a dictionary.txt next to the tools: the list of known
    SWBF sound and movie file names from Phantom's SoundFMVextractor (GPL-3.0,
    so it isn't bundled - see DICTIONARY_URL). It names every sample in the
    stock common.bnk and every stock movie. Empty when the file isn't there."""
    global _sound_names
    if _sound_names is None:
        from bf1_core import app_dir
        _sound_names = {}
        path = app_dir() / DICTIONARY_FILE
        if path.is_file():
            for line in path.read_text(encoding="ascii", errors="replace").splitlines():
                line = line.strip()
                if line:
                    _sound_names.setdefault(fnv1a32(line), line)
    return _sound_names


def reload_sound_names() -> int:
    """Re-reads dictionary.txt (e.g. after it was added); returns how many names it has."""
    global _sound_names
    _sound_names = None
    return len(sound_names())


PCM_FORMAT = 2      # a sample bank's 'format' for 16-bit mono PCM (all of common.bnk)
MAX_RATE = 44100    # highest sample rate the stock game uses


@dataclass
class MediaEntry:
    hash: int
    offset: int        # absolute position in the file
    length: int        # bytes
    frequency: int = 0  # sounds only (Hz)
    alias_of: int | None = None

    @property
    def name(self) -> str:
        return KNOWN_NAMES.get(self.hash) or sound_names().get(self.hash) or f"{self.hash:08x}"

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
            fmt = next((v for k, v in fields if k == _H["format"]), None)
            if fmt != PCM_FORMAT:
                # the per-map Sound\*.lvl banks are format 5: compressed 4-bit streams (music,
                # ambience) - reading those as PCM would just give noise
                raise MediaError(f"{path.name} holds compressed audio streams (format {fmt}), which aren't "
                                 f"supported yet - only plain sample banks like common.bnk are.")
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
    return pcm_to_wav(raw, entry.frequency or 22050)


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    """16-bit mono PCM as a .wav file."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def find_ffmpeg() -> Path | None:
    """ffmpeg.exe next to the tools (or the release .exe), else on PATH. It
    isn't bundled: it's a separate GPL program and far too big for the repo."""
    import shutil
    from bf1_core import app_dir
    for candidate in (app_dir() / "ffmpeg.exe", app_dir() / "ffmpeg"):
        if candidate.is_file():
            return candidate
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def bik_to_mp4(bik_path, mp4_path, ffmpeg) -> None:
    """Converts a Bink video to an H.264 .mp4 that any player opens (ffmpeg
    can read Bink but not write it, so this is one-way)."""
    import subprocess
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-v", "error", "-y", "-i", str(bik_path),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(mp4_path)],
        capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # no console window flashing up
    if result.returncode != 0:
        raise MediaError(f"ffmpeg couldn't convert {Path(bik_path).name}: "
                         f"{(result.stderr or '').strip().splitlines()[-1:] or 'unknown error'}")


def extract(media: MediaFile, out_dir, entries=None, progress=None, ffmpeg=None) -> list:
    """Writes `entries` (default: all) into `out_dir`; returns the paths.
    With `ffmpeg` (a path), movies are saved as .mp4 instead of .bik.
    `progress(done, total)` is called after each file."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chosen = list(entries if entries is not None else media.entries)
    written = []
    for i, entry in enumerate(chosen):
        target = out_dir / (entry.name + media.extension)
        target.write_bytes(entry_bytes(media, entry))
        if ffmpeg and media.kind == "movies":
            mp4 = target.with_suffix(".mp4")
            try:
                bik_to_mp4(target, mp4, ffmpeg)
            finally:
                target.unlink(missing_ok=True)
            target = mp4
        written.append(target)
        if progress:
            progress(i + 1, len(chosen))
    return written


# --- putting sounds back ----------------------------------------------------------------

def load_audio(path, target_rate: int | None = None, ffmpeg=None) -> tuple:
    """(16-bit mono PCM bytes, sample rate) from an audio file. A .wav is
    read directly (8/16/24/32-bit, mono or stereo - stereo is mixed down);
    anything else (mp3, ogg, flac, float .wav...) goes through ffmpeg, at
    `target_rate` (usually the rate of the sample being replaced). Rates
    above 44.1 kHz come down to 44.1 kHz, the highest the game uses."""
    import numpy as np
    path = Path(path)
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
                raw = w.readframes(w.getnframes())
        except (wave.Error, EOFError):
            raw = None  # e.g. 32-bit float - ffmpeg can still read it
        if raw is not None:
            if width == 1:
                samples = (np.frombuffer(raw, np.uint8).astype(np.float64) - 128) * 256
            elif width == 3:
                b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
                samples = ((b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)) << 8 >> 8).astype(np.float64) / 256
            elif width in (2, 4):
                samples = np.frombuffer(raw, np.int16 if width == 2 else np.int32).astype(np.float64)
                if width == 4:
                    samples /= 65536
            else:
                raise MediaError(f"{path.name}: {width * 8}-bit audio isn't supported.")
            samples = samples.reshape(-1, channels).mean(axis=1)  # mix down to mono
            if rate > MAX_RATE:
                n = int(len(samples) * MAX_RATE / rate)
                samples = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples)
                rate = MAX_RATE
            return np.clip(np.round(samples), -32768, 32767).astype("<i2").tobytes(), rate
    if ffmpeg is None:
        raise MediaError(f"{path.name}: only .wav files can be read without ffmpeg. Put ffmpeg.exe next to the "
                         f"editor to use mp3, ogg, flac and other formats.")
    import subprocess
    rate = min(target_rate or 22050, MAX_RATE)
    result = subprocess.run([str(ffmpeg), "-hide_banner", "-v", "error", "-i", str(path), "-vn",
                             "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(rate), "pipe:1"],
                            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0 or not result.stdout:
        err = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise MediaError(f"ffmpeg couldn't read {path.name}: {err[-1] if err else 'no audio found'}")
    return result.stdout[: len(result.stdout) // 2 * 2], rate


def rebuild_bank(path, replacements: dict) -> bytes:
    """A sound bank's bytes with samples replaced: `replacements` maps a
    sample's hash to (16-bit mono PCM bytes, sample rate). Everything else
    is kept byte for byte; the sample table, the bank's total size and the
    chunk sizes are updated, and the audio block keeps the stock 2048-byte
    alignment. Aliases of a replaced sample follow it. With no replacements
    the result is identical to the original file."""
    data = Path(path).read_bytes()
    try:
        if data[:4] != b"ucfb":
            raise ValueError
        outer_tag, outer_size = struct.unpack_from("<II", data, 8)
        inner_tag, inner_size = struct.unpack_from("<II", data, 16)
        info_tag, info_size = struct.unpack_from("<II", data, 24)
        data_at = 32 + info_size
        data_tag, data_size = struct.unpack_from("<II", data, data_at)
    except (ValueError, struct.error):
        raise MediaError(f"{Path(path).name} isn't a sound bank.")
    if not (info_tag == _H["info"] and data_tag == _H["data"] and inner_size == 16 + info_size + data_size
            and outer_size == inner_size + 8 and len(data) == outer_size + 16 and info_size % 4 == 0):
        raise MediaError(f"{Path(path).name} has an unexpected layout - can't safely rewrite it.")
    words = list(struct.unpack_from(f"<{info_size // 4}I", data, 32))

    # walk the table, remembering where each value lives so it can be patched in place
    records, current, total_at, in_samples, i = [], None, None, False, 0
    while i < len(words):
        key = words[i]
        if key in _MARKERS:
            if key == _H["sampleinfo"]:
                in_samples = True
            elif key == _H["sample"]:
                current = {}
            elif key == _H["sampleend"] and current is not None:
                records.append(current)
                current = None
            i += 1
            continue
        if current is not None:
            current[key] = i + 1
        elif not in_samples and key == _H["size"] and total_at is None:
            total_at = i + 1
        i += 2
    if total_at is None or not records:
        raise MediaError(f"{Path(path).name} has no sample table.")

    value = lambda rec, key: words[rec[key]] if key in rec else 0
    audio, pos, new_by_id = [], 40 + info_size, {}
    for rec in records:
        if _H["alias"] in rec:
            continue
        sample_id, size, padding = value(rec, _H["id"]), value(rec, _H["size"]), value(rec, _H["padding"])
        pcm, rate = replacements.get(sample_id, (data[pos:pos + size], value(rec, _H["frequency"])))
        pos += size + padding
        audio.append(pcm + b"\0" * padding)
        new_by_id[sample_id] = (len(pcm), rate)
        words[rec[_H["size"]]] = len(pcm)
        words[rec[_H["sizesamples"]]] = len(pcm) // 2
        if _H["frequency"] in rec:
            words[rec[_H["frequency"]]] = rate
    for rec in records:  # an alias plays its target, so it takes the target's new length and rate
        if _H["alias"] in rec and value(rec, _H["alias"]) in new_by_id:
            size, rate = new_by_id[value(rec, _H["alias"])]
            words[rec[_H["size"]]] = size
            words[rec[_H["sizesamples"]]] = size // 2
            if _H["frequency"] in rec:
                words[rec[_H["frequency"]]] = rate
    blob = b"".join(audio)
    words[total_at] = sum(size for size, _ in new_by_id.values())
    # stock banks pad the audio to whole 2048-byte blocks, and the padding opens with a
    # record of its own: 'padding' + (padding size - 16) - same in the stock and a modded bank
    pad = -len(blob) % 2048
    if pad < 16:
        pad += 2048
    blob += struct.pack("<II", _H["padding"], pad - 16) + b"\0" * (pad - 8)

    info = struct.pack(f"<{len(words)}I", *words)
    inner = struct.pack("<II", info_tag, len(info)) + info + struct.pack("<II", data_tag, len(blob)) + blob
    outer = struct.pack("<II", inner_tag, len(inner)) + inner
    return b"ucfb" + struct.pack("<I", len(outer) + 8) + struct.pack("<II", outer_tag, len(outer)) + outer
