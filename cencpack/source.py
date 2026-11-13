"""Read unfragmented ISO-BMFF fixtures into per-sample objects.

Only what the packager/verifier needs is modelled: sample sizes/offsets,
decode/presentation timing, sync samples, the sample entry (avc1/mp4a with
avcC/esds children) and edit-list priming.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from cencpack.boxes import (
    Box,
    find_all,
    load,
    parse_hdlr,
    parse_mdhd_timescale,
    parse_stss,
    parse_stts,
    sample_deltas,
)


@dataclass
class Sample:
    index: int  # 0-based
    offset: int
    size: int
    dts_delta: int
    cto: int  # composition time offset (pts = dts + cto), media timescale
    sync: bool


@dataclass
class Edit:
    segment_duration: int  # movie timescale units
    media_time: int  # track timescale; -1 == empty edit


@dataclass
class Track:
    path: str
    handler: str  # "vide" / "soun"
    track_id: int
    timescale: int
    samples: list[Sample]
    sync_samples: list[int]  # 1-based sample numbers
    edits: list[Edit]
    entry_type: str  # avc1 / mp4a
    entry_box: Box  # sample entry box in the source file
    width: int
    height: int
    channels: int
    sample_rate: int
    data: bytes = field(repr=False, default=b"")

    def sample_data(self, sample: Sample) -> bytes:
        return self.data[sample.offset : sample.offset + sample.size]

    def dts(self, index: int) -> int:
        return sum(s.dts_delta for s in self.samples[:index])


def _u32(data: bytes, off: int) -> int:
    return struct.unpack(">I", data[off : off + 4])[0]


def _parse_sizes(data: bytes, stsz: Box) -> list[int]:
    p = stsz.offset + stsz.header_size
    uniform = _u32(data, p + 4)
    count = _u32(data, p + 8)
    if uniform:
        return [uniform] * count
    o = p + 12
    return [_u32(data, o + 4 * i) for i in range(count)]


def _parse_chunk_offsets(data: bytes, trak: Box) -> list[int]:
    co = find_all([trak], "stco")
    if co:
        box = co[0]
        p = box.offset + box.header_size
        n = _u32(data, p + 4)
        return [_u32(data, p + 8 + 4 * i) for i in range(n)]
    box = find_all([trak], "co64")[0]
    p = box.offset + box.header_size
    n = _u32(data, p + 4)
    return [struct.unpack(">Q", data[p + 8 + 8 * i : p + 16 + 8 * i])[0] for i in range(n)]


def _sample_offsets(data: bytes, trak: Box, sizes: list[int]) -> list[int]:
    chunks = _parse_chunk_offsets(data, trak)
    stsc = find_all([trak], "stsc")[0]
    p = stsc.offset + stsc.header_size
    n = _u32(data, p + 4)
    entries = [struct.unpack(">III", data[p + 8 + 12 * i : p + 20 + 12 * i]) for i in range(n)]
    # (first_chunk 1-based, samples_per_chunk, sample_desc_index)
    offsets: list[int] = []
    cursor = 0
    entries.append((len(chunks) + 1, entries[-1][1], entries[-1][2]))
    for (first, per_chunk, _), (next_first, _, _) in zip(entries, entries[1:]):
        for chunk in range(first - 1, next_first - 1):
            pos = chunks[chunk]
            for _ in range(per_chunk):
                offsets.append(pos)
                pos += sizes[cursor]
                cursor += 1
    return offsets


def _parse_cto(data: bytes, trak: Box, count: int) -> list[int]:
    boxes = find_all([trak], "ctts")
    if not boxes:
        return [0] * count
    box = boxes[0]
    p = box.offset + box.header_size
    version = data[p]
    n = _u32(data, p + 4)
    out: list[int] = []
    o = p + 8
    for _ in range(n):
        cnt = _u32(data, o)
        if version == 1:
            off = struct.unpack(">i", data[o + 4 : o + 8])[0]
        else:
            off = _u32(data, o + 4)
        out.extend([off] * cnt)
        o += 8
    return out


def _parse_edits(data: bytes, trak: Box) -> list[Edit]:
    boxes = find_all([trak], "elst")
    if not boxes:
        return []
    box = boxes[0]
    p = box.offset + box.header_size
    version = data[p]
    n = _u32(data, p + 4)
    out: list[Edit] = []
    o = p + 8
    for _ in range(n):
        if version == 1:
            duration, media_time, _rate = struct.unpack(">QIi", data[o : o + 20])
            o += 20
        else:
            duration, mtime, _rate = struct.unpack(">III", data[o : o + 12])
            media_time = struct.unpack(">i", struct.pack(">I", mtime))[0]
            o += 12
        out.append(Edit(duration, media_time))
    return out


def _parse_trak(path: str, data: bytes, trak: Box) -> Track:
    hdlr = find_all([trak], "hdlr")[0]
    handler = parse_hdlr(data, hdlr)
    mdhd = find_all([trak], "mdhd")[0]
    timescale = parse_mdhd_timescale(data, mdhd)
    tkhd = find_all([trak], "tkhd")[0]
    tp = tkhd.offset + tkhd.header_size
    version = data[tp]
    track_id = _u32(data, tp + (4 + 16 if version else 12))

    stts = find_all([trak], "stts")[0]
    deltas = sample_deltas(parse_stts(data, stts))
    sizes = _parse_sizes(data, find_all([trak], "stsz")[0])
    offsets = _sample_offsets(data, trak, sizes)
    ctos = _parse_cto(data, trak, len(deltas))
    sync_set = set(parse_stss(data, find_all([trak], "stss")[0])) if find_all([trak], "stss") else set()
    if not sync_set:
        sync_set = set(range(1, len(deltas) + 1))
    samples = [
        Sample(i, offsets[i], sizes[i], deltas[i], ctos[i], (i + 1) in sync_set)
        for i in range(len(deltas))
    ]

    stsd = find_all([trak], "stsd")[0]
    entry = next(iter(stsd.children))
    ep = entry.offset + entry.header_size
    if handler == "vide":
        width, height = struct.unpack(">HH", data[ep + 24 : ep + 28])
        channels, rate = 0, 0
    else:
        width = height = 0
        channels = struct.unpack(">H", data[ep + 16 : ep + 18])[0]
        rate = _u32(data, ep + 24) >> 16

    return Track(
        path=path,
        handler=handler,
        track_id=track_id,
        timescale=timescale,
        samples=samples,
        sync_samples=sorted(sync_set),
        edits=_parse_edits(data, trak),
        entry_type=entry.typ,
        entry_box=entry,
        width=width,
        height=height,
        channels=channels,
        sample_rate=rate,
        data=data,
    )


def read_tracks(path: str | Path) -> list[Track]:
    data, tree = load(path)
    return [_parse_trak(str(path), data, trak) for trak in find_all(tree, "trak")]


def read_track(path: str | Path, handler: str) -> Track:
    tracks = read_tracks(path)
    matches = [t for t in tracks if t.handler == handler]
    if len(matches) != 1:
        raise ValueError(f"{path}: expected one {handler} track, found {len(matches)}")
    return matches[0]
