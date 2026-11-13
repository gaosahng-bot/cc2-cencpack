"""CMAF fragmented-MP4 writer: init segments and encrypted media fragments."""
from __future__ import annotations

import struct
from dataclasses import dataclass

from cencpack import mp4io as m
from cencpack.cenc import EncryptedSample, senc_box, sinf_box
from cencpack.source import Track


@dataclass
class FragSample:
    data: bytes
    duration: int
    cto: int
    sync: bool
    encrypted: EncryptedSample | None = None


@dataclass
class Fragment:
    track_id: int
    sequence: int
    base_media_decode_time: int
    samples: list[FragSample]
    timescale: int
    is_video: bool


def _protected_entry(track: Track, kid: bytes) -> bytes:
    entry = track.entry_box
    raw = bytearray(track.data[entry.offset : entry.offset + entry.size])
    protected_type = "encv" if track.handler == "vide" else "enca"
    raw[4:8] = protected_type.encode("latin-1")
    sinf = sinf_box(track.entry_type, kid)
    # sinf is the last child box of the protected sample entry; update size.
    struct.pack_into(">I", raw, 0, len(raw) + len(sinf))
    return bytes(raw) + sinf


def _mvhd(timescale: int, duration: int) -> bytes:
    payload = (
        b"\x00" * 4  # creation/modification time (v0)
        + m.u32(timescale)
        + m.u32(duration)
        + struct.pack(">I", 0x00010000)  # rate 1.0
        + struct.pack(">H", 0x0100)  # volume 1.0
        + b"\x00" * 10  # reserved
        + struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
        + b"\x00" * 28  # 6 pre-defined integers + 32-bit reserved
        + struct.pack(">I", 2)  # next track id
    )
    return m.fullbox("mvhd", 0, 0, payload)


def _tkhd(track: Track, duration: int) -> bytes:
    flags = 0x000007  # track enabled / in movie / in preview
    payload = (
        m.u32(0) + m.u32(0)  # creation / modification
        + m.u32(track.track_id) + m.u32(0)  # reserved
        + m.u32(duration)
        + b"\x00" * 8  # reserved
        + struct.pack(">Hh", 0, 0)  # layer / alternate group
        + struct.pack(">H", 0x0100 if track.handler == "soun" else 0)
        + struct.pack(">H", 0)  # reserved
        + struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
        + struct.pack(">II", track.width << 16, track.height << 16)
    )
    return m.fullbox("tkhd", 0, flags, payload)


def _mdhd(track: Track, duration: int) -> bytes:
    payload = (
        m.u32(0) + m.u32(0)
        + m.u32(track.timescale)
        + m.u32(duration)
        + struct.pack(">H", 0x55C4)  # language 'und' (undetermined)
        + struct.pack(">H", 0)  # pre-defined
    )
    return m.fullbox("mdhd", 0, 0, payload)


def _hdlr(track: Track) -> bytes:
    name = ("VideoHandler" if track.handler == "vide" else "SoundHandler") + "\x00"
    payload = m.u32(0) + track.handler.encode("latin-1") + m.u32(0) * 3 + name.encode()
    return m.fullbox("hdlr", 0, 0, payload)


def _media_header(track: Track) -> bytes:
    if track.handler == "vide":
        # vmhd: graphicsmode + 3 opcolor
        return m.fullbox("vmhd", 0, 1, struct.pack(">H3H", 0, 0, 0, 0))
    return m.fullbox("smhd", 0, 0, struct.pack(">Hh", 0, 0))


def _dinf() -> bytes:
    url = m.fullbox("url ", 0, 1, b"")  # self-contained flag
    dref = m.fullbox("dref", 0, 0, m.u32(1) + url)
    return m.box("dinf", dref)


def _stbl(track: Track, kid: bytes) -> bytes:
    stsd = m.fullbox("stsd", 0, 0, m.u32(1) + _protected_entry(track, kid))
    empty_stts = m.fullbox("stts", 0, 0, m.u32(0))
    empty_stsc = m.fullbox("stsc", 0, 0, m.u32(0))
    empty_stsz = m.fullbox("stsz", 0, 0, m.u32(0) + m.u32(0))
    empty_stco = m.fullbox("stco", 0, 0, m.u32(0))
    children = [stsd, empty_stts, empty_stsc, empty_stsz, empty_stco]
    if track.handler == "vide":
        children.insert(2, m.fullbox("stss", 0, 0, m.u32(0)))
    return m.container("stbl", children)


def _edts(track: Track) -> bytes:
    if not track.edits:
        return b""
    entries = b""
    for edit in track.edits:
        entries += struct.pack(">III", edit.segment_duration, max(edit.media_time, 0), 0x00010000)
    return m.box("edts", m.fullbox("elst", 0, 0, m.u32(len(track.edits)) + entries))


def build_init(track: Track, kid: bytes, duration_units: int) -> bytes:
    ftyp = m.box("ftyp", b"cmf2" + m.u32(1) + b"cmf2" + b"iso5" + b"dash" + b"hls4")
    minf = m.container("minf", [_media_header(track), _dinf(), _stbl(track, kid)])
    mdia = m.container("mdia", [_mdhd(track, duration_units), _hdlr(track), minf])
    trex = m.fullbox(
        "trex", 0, 0,
        m.u32(track.track_id) + m.u32(1) + m.u32(0) + m.u32(0) + m.u32(0),
    )
    trak_children = [_tkhd(track, duration_units), _edts(track), mdia]
    trak = m.container("trak", [child for child in trak_children if child])
    mvex = m.box("mvex", trex)
    moov = m.container("moov", [_mvhd(track.timescale, duration_units), trak, mvex])
    return ftyp + moov


def _trun(fragment: Fragment, data_offset: int) -> bytes:
    video = fragment.is_video
    # data-offset present, sample duration/size present; video also carries cto.
    flags = 0x000001 | 0x000100 | 0x000200 | (0x000400 if video else 0)
    payload = m.u32(len(fragment.samples)) + m.i32(data_offset)
    for sample in fragment.samples:
        payload += m.u32(sample.duration) + m.u32(len(sample.data))
        if video:
            payload += m.i32(sample.cto)
    return m.fullbox("trun", (1 if video else 0), flags, payload)


def build_fragment(fragment: Fragment) -> bytes:
    tfhd_flags = 0x020000  # default-base-is-moof
    tfhd = m.fullbox(
        "tfhd", 0, tfhd_flags, m.u32(fragment.track_id)
    )
    tfdt = m.fullbox("tfdt", 1, 0, m.u64(fragment.base_media_decode_time))
    senc_blob = senc_box([s.encrypted for s in fragment.samples if s.encrypted is not None])

    def build_moof(data_offset: int) -> bytes:
        mfhd = m.fullbox("mfhd", 0, 0, m.u32(fragment.sequence))
        traf = m.container("traf", [tfhd, tfdt, _trun(fragment, data_offset), senc_blob])
        return m.container("moof", [mfhd, traf])

    # data_offset in trun is relative to the moof box origin; two passes let
    # us measure the final moof size before filling the value in.
    moof = build_moof(0)
    data_offset = len(moof) + 8  # skip moof itself and the mdat header
    moof = build_moof(data_offset)
    mdat_payload = b"".join(sample.data for sample in fragment.samples)
    return moof + m.box("mdat", mdat_payload)
