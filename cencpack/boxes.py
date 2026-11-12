"""ISO-BMFF box tree + sample-table reader. Complete enough to probe fixtures."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Box:
    typ: str
    offset: int
    size: int
    header_size: int
    children: list["Box"] = field(default_factory=list)
    # payload start = offset + header_size, payload end = offset + size


def _read_box_header(data: bytes, i: int) -> tuple[str, int, int]:
    if i + 8 > len(data):
        raise ValueError("truncated box header")
    size = struct.unpack(">I", data[i : i + 4])[0]
    typ = data[i + 4 : i + 8].decode("latin-1")
    header = 8
    if size == 1:
        if i + 16 > len(data):
            raise ValueError("truncated largesize")
        size = struct.unpack(">Q", data[i + 8 : i + 16])[0]
        header = 16
    elif size == 0:
        size = len(data) - i
    if typ == "uuid":
        header += 16
    if size < header:
        raise ValueError(f"bad size {size} for {typ}")
    return typ, size, header


def _looks_like_type(typ: str) -> bool:
    return len(typ) == 4 and all((32 <= ord(c) < 127) for c in typ)


CONTAINER = {
    "moov", "trak", "mdia", "minf", "stbl", "edts", "dinf", "mvex", "moof", "traf",
    "mfra", "udta", "sinf", "schi", "dref", "mp4a", "avc1", "encv", "enca", "stsd",
}


def parse_tree(data: bytes, start: int = 0, end: int | None = None, parent_typ: str | None = None) -> list[Box]:
    if end is None:
        end = len(data)
    boxes: list[Box] = []
    i = start
    while i + 8 <= end:
        typ, size, header = _read_box_header(data, i)
        payload_end = i + size
        if (not _looks_like_type(typ)) or size < header or payload_end > end:
            # residual payload that is not a child box (e.g. raw avcC header misalignment)
            break
        box = Box(typ=typ, offset=i, size=size, header_size=header)
        payload_start = i + header
        if typ in CONTAINER:
            ps, pe = payload_start, payload_end
            if typ == "stsd":
                ps = payload_start + 8 if payload_start + 8 <= payload_end else payload_end
            elif typ in {"mp4a", "avc1", "encv", "enca"}:
                ps = _sample_entry_child_start(typ, data, payload_start, payload_end)
            box.children = parse_tree(data, ps, pe, typ)
        boxes.append(box)
        i += size
    return boxes


def _sample_entry_child_start(typ: str, data: bytes, start: int, end: int) -> int:
    # Visual sample entry: 78 bytes after the 8-byte box header? After payload start:
    # 6 reserved + 2 dataReferenceIndex + 16 predefined/reserved + width/height etc. = 78
    # Audio: 6+2 + 8 + 2(channels) + 2(size) + 2 + 2 + 4(sample rate) = 28
    if typ in {"avc1", "encv"}:
        return min(start + 78, end)
    if typ in {"mp4a", "enca"}:
        return min(start + 28, end)
    return start


def flatten(boxes: list[Box]) -> list[Box]:
    out = []
    for b in boxes:
        out.append(b)
        out.extend(flatten(b.children))
    return out


def find_all(boxes: list[Box], typ: str) -> list[Box]:
    return [b for b in flatten(boxes) if b.typ == typ]


def load(path: str | Path) -> tuple[bytes, list[Box]]:
    data = Path(path).read_bytes()
    return data, parse_tree(data)


def u32(data: bytes, off: int) -> int:
    return struct.unpack(">I", data[off : off + 4])[0]


def parse_stss(data: bytes, box: Box) -> list[int]:
    """Return 1-based sample numbers of sync samples."""
    p = box.offset + box.header_size
    count = u32(data, p + 4)
    out = []
    o = p + 8
    for _ in range(count):
        out.append(u32(data, o))
        o += 4
    return out


def parse_stts(data: bytes, box: Box) -> list[tuple[int, int]]:
    p = box.offset + box.header_size
    count = u32(data, p + 4)
    out = []
    o = p + 8
    for _ in range(count):
        out.append((u32(data, o), u32(data, o + 4)))
        o += 8
    return out


def sample_deltas(stts: list[tuple[int, int]]) -> list[int]:
    out = []
    for n, delta in stts:
        out.extend([delta] * n)
    return out


def parse_mdhd_timescale(data: bytes, box: Box) -> int:
    p = box.offset + box.header_size
    version = data[p]
    if version == 1:
        return u32(data, p + 4 + 8 + 8)
    return u32(data, p + 4 + 4 + 4)


def parse_hdlr(data: bytes, box: Box) -> str:
    p = box.offset + box.header_size
    return data[p + 8 : p + 12].decode("latin-1")
