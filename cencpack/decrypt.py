"""Decrypt CENC-encrypted CMAF fragments and rebuild a plaintext MP4.

The verifier uses this instead of ffmpeg's SAMPLE-AES support: we parse each
moof/traf/senc/trun/mdat ourselves, apply AES-128-CTR with the test keys, and
then hand the reconstructed plaintext to ffmpeg for pixel-level checks.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from cencpack.boxes import Box, find_all, parse_tree, u32
from cencpack.cenc import decrypt_sample, parse_senc
from cencpack.mp4io import fullbox


@dataclass
class PlainSample:
    data: bytes
    duration: int
    cto: int
    sync: bool
    decode_time: int
    role: str = "program"


def _trun_samples(data: bytes, trun: Box) -> tuple[int, list[dict]]:
    p = trun.offset + trun.header_size
    version = data[p]
    flags = int.from_bytes(data[p + 1 : p + 4], "big")
    count = u32(data, p + 4)
    o = p + 8
    data_offset = u32(data, o) if flags & 0x000001 else 0
    o += 4 if flags & 0x000001 else 0
    first_flags = u32(data, o) if flags & 0x000004 else 0
    o += 4 if flags & 0x000004 else 0
    samples = []
    for index in range(count):
        entry: dict = {}
        if flags & 0x000100:
            entry["duration"] = u32(data, o)
            o += 4
        if flags & 0x000200:
            entry["size"] = u32(data, o)
            o += 4
        if flags & 0x000008:
            entry["flags"] = u32(data, o)
            o += 4
        if flags & 0x000400:
            entry["cto"] = int.from_bytes(data[o : o + 4], "big", signed=(version == 1))
            o += 4
        entry.setdefault("duration", 0)
        entry.setdefault("size", 0)
        entry.setdefault("cto", 0)
        if flags & 0x000004 and index == 0:
            entry["flags"] = first_flags
        samples.append(entry)
    return data_offset, samples


def decrypt_fragment(
    path: str | Path,
    keys_by_kid: dict[bytes, bytes],
    expected_kid: bytes,
    sync_numbers: set[int] | None = None,
    global_offset: int = 0,
    time_offset: int = 0,
    role: str = "program",
) -> list[PlainSample]:
    data = Path(path).read_bytes()
    tree = parse_tree(data)
    moof = next(b for b in tree if b.typ == "moof")
    mdat = next(b for b in tree if b.typ == "mdat")
    tfdt = find_all([moof], "tfdt")[0]
    base_time = struct.unpack(
        ">Q", data[tfdt.offset + tfdt.header_size + 4 : tfdt.offset + tfdt.header_size + 12]
    )[0]
    trun = find_all([moof], "trun")[0]
    data_offset, entries = _trun_samples(data, trun)
    senc_boxes = find_all([moof], "senc")
    if not senc_boxes:
        raise ValueError(f"{path}: missing senc box (not encrypted)")
    senc_entries = parse_senc(data, senc_boxes[0])
    if len(senc_entries) != len(entries):
        raise ValueError(f"{path}: senc/trun sample count mismatch")

    payload_origin = moof.offset + data_offset
    cursor = payload_origin
    out: list[PlainSample] = []
    running_time = time_offset
    for index, (entry, senc) in enumerate(zip(entries, senc_entries)):
        encrypted = data[cursor : cursor + entry["size"]]
        if len(encrypted) != entry["size"]:
            raise ValueError(f"{path}: truncated sample {index}")
        plain = decrypt_sample(encrypted, senc["iv"], senc["subsamples"], keys_by_kid[expected_kid])
        if plain == encrypted and any(s.encrypted_bytes for s in senc["subsamples"]):
            raise ValueError(f"{path}: sample {index} ciphertext equals plaintext")
        sample_number = global_offset + index + 1
        sync = (sample_number in sync_numbers) if sync_numbers is not None else True
        out.append(
            PlainSample(
                data=plain,
                duration=entry["duration"],
                cto=entry["cto"],
                sync=sync,
                decode_time=running_time,
                role=role,
            )
        )
        running_time += entry["duration"]
        cursor += entry["size"]
    if cursor != mdat.offset + 8 + (mdat.size - 8):
        raise ValueError(f"{path}: samples do not exactly fill mdat")
    return out


def parse_tenc_kid(init_path: str | Path) -> bytes:
    data = Path(init_path).read_bytes()
    tree = parse_tree(data)
    tenc = find_all(tree, "tenc")[0]
    # payload: ver/flags(4) + reserved(2) + is_protected(1) + iv_size(1) + KID(16)
    p = tenc.offset + tenc.header_size
    return data[p + 8 : p + 24]


def collect_plain_samples(
    out_dir: Path,
    playlist,
    keys_by_kid: dict[bytes, bytes],
    init_kid: bytes,
    sync_numbers: set[int] | None = None,
) -> list[PlainSample]:
    samples: list[PlainSample] = []
    current_kid = init_kid
    time_offset = 0
    for entry in playlist.entries:
        if entry.key is not None:
            current_kid = _kid_from_key_uri(entry.key.uri, keys_by_kid)
        fragment_path = out_dir / entry.uri
        global_offset = len(samples)
        samples.extend(
            decrypt_fragment(
                fragment_path,
                keys_by_kid,
                current_kid,
                sync_numbers=sync_numbers,
                global_offset=global_offset,
                time_offset=time_offset,
                role=getattr(entry, "role", "program"),
            )
        )
        time_offset += sum(sample.duration for sample in samples[global_offset:])
    return samples


def avcc_parameter_sets(source_track) -> bytes:
    """Extract SPS/PPS NAL units (length-prefixed) from a track's avcC box."""
    avcc = find_all(parse_tree(source_track.data), "avcC")[0]
    payload = source_track.data[avcc.offset + avcc.header_size : avcc.offset + avcc.size]
    # configurationVersion(1) profile/compat/level(3) u8(1) u8(1) u8(1)
    # numSPS(u5) then SPS entries, then numPPS(u8) then PPS entries.
    pos = 5
    num_sps = payload[pos] & 0x1F
    pos += 1
    nalus = bytearray()
    for _ in range(num_sps):
        length = struct.unpack(">H", payload[pos : pos + 2])[0]
        pos += 2
        nalus += struct.pack(">I", length) + payload[pos : pos + length]
        pos += length
    num_pps = payload[pos]
    pos += 1
    for _ in range(num_pps):
        length = struct.unpack(">H", payload[pos : pos + 2])[0]
        pos += 2
        nalus += struct.pack(">I", length) + payload[pos : pos + length]
        pos += length
    return bytes(nalus)


def _kid_from_key_uri(uri: str, keys_by_kid: dict[bytes, bytes]) -> bytes:
    stem = Path(uri).name
    if not stem.startswith("key-") or not stem.endswith(".bin"):
        raise ValueError(f"unsupported key URI: {uri}")
    kid = bytes.fromhex(stem[len("key-") : -len(".bin")])
    if kid not in keys_by_kid:
        raise ValueError(f"no key available for KID {kid.hex()}")
    return kid


def rebuild_mp4(
    samples: list[PlainSample],
    source_track,
    init_path: Path,
    dest_path: Path,
    ad_track=None,
) -> None:
    """Write an unfragmented MP4 with decrypted samples and source timing.

    All samples must belong to a single continuous timeline (one role). The
    verifier calls this separately for the pre-ad programme, the ad and the
    post-ad programme so H.264 reference state never crosses a discontinuity.
    """
    if source_track.handler == "vide":
        params = avcc_parameter_sets(source_track)
        for sample in samples:
            if sample.sync:
                sample.data = params + sample.data
    mdat_payload = b"".join(sample.data for sample in samples)
    mdat = struct.pack(">I4s", 8 + len(mdat_payload), b"mdat") + mdat_payload
    container_track = source_track
    source_data = container_track.data
    source_tree = parse_tree(source_data)
    ftyp = next(b for b in source_tree if b.typ == "ftyp")
    source_moov = next(b for b in source_tree if b.typ == "moov")
    runs: list[tuple[int, int]] = []
    for sample in samples:
        if runs and runs[-1][1] == sample.duration:
            runs[-1] = (runs[-1][0] + 1, sample.duration)
        else:
            runs.append((1, sample.duration))
    replacements = {
        "stts": _stts_box(runs),
        "stsz": _stsz_box([len(s.data) for s in samples]),
        "stsc": _stsc_box([(1, len(samples), 1)]),
        "stco": _stco_box([ftyp.size + 8]),
    }
    if source_track.handler == "vide":
        ctts_runs: list[tuple[int, int]] = []
        for sample in samples:
            if ctts_runs and ctts_runs[-1][1] == sample.cto:
                ctts_runs[-1] = (ctts_runs[-1][0] + 1, sample.cto)
            else:
                ctts_runs.append((1, sample.cto))
        replacements["ctts"] = _ctts_box(ctts_runs)
        sync_numbers = [index + 1 for index, sample in enumerate(samples) if sample.sync]
        replacements["stss"] = _stss_box(sync_numbers)
    # The spliced sample sequence starts at decode time zero and is contiguous,
    # so the rebuilt file needs no edit list at all.
    replacements["edts"] = b""
    moov_bytes = _rebuild_container(source_data, source_moov, replacements)
    dest_path.write_bytes(
        source_data[ftyp.offset : ftyp.offset + ftyp.size] + mdat + moov_bytes
    )


def _rebuild_container(data: bytes, box: Box, replacements: dict[str, bytes]) -> bytes:
    """Serialize a box again, swapping leaf boxes in `replacements` and fixing sizes."""
    if box.typ in replacements:
        return replacements[box.typ]
    if not box.children:
        return data[box.offset : box.offset + box.size]
    payload = bytearray()
    payload_start = box.offset + box.header_size
    cursor = payload_start
    for child in box.children:
        payload.extend(data[cursor:child.offset])  # fullbox prefix / padding
        payload.extend(_rebuild_container(data, child, replacements))
        cursor = child.offset + child.size
    payload.extend(data[cursor : box.offset + box.size])  # trailing bytes
    header = data[box.offset : box.offset + box.header_size]
    return struct.pack(">I4s", 8 + len(payload), box.typ.encode("latin-1")) + bytes(payload)


def _stsz_box(sizes: list[int]) -> bytes:
    payload = struct.pack(">II", 0, len(sizes)) + b"".join(struct.pack(">I", s) for s in sizes)
    return fullbox("stsz", 0, 0, payload)


def _stco_box(offsets: list[int]) -> bytes:
    payload = struct.pack(">I", len(offsets)) + b"".join(struct.pack(">I", o) for o in offsets)
    return fullbox("stco", 0, 0, payload)


def _stsc_box(entries: list[tuple[int, int, int]]) -> bytes:
    payload = struct.pack(">I", len(entries)) + b"".join(struct.pack(">III", *e) for e in entries)
    return fullbox("stsc", 0, 0, payload)


def _stts_box(runs: list[tuple[int, int]]) -> bytes:
    payload = struct.pack(">I", len(runs)) + b"".join(struct.pack(">II", *r) for r in runs)
    return fullbox("stts", 0, 0, payload)


def _ctts_box(runs: list[tuple[int, int]]) -> bytes:
    payload = struct.pack(">I", len(runs)) + b"".join(struct.pack(">Ii", *r) for r in runs)
    return fullbox("ctts", 1, 0, payload)


def _stss_box(sync_numbers: list[int]) -> bytes:
    payload = struct.pack(">I", len(sync_numbers)) + b"".join(struct.pack(">I", n) for n in sync_numbers)
    return fullbox("stss", 0, 0, payload)
