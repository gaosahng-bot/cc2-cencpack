"""CENC (AES-128-CTR, scheme 'cenc') sample encryption.

Video (AVC/H.264) uses subsample encryption: NAL length prefixes, NAL
headers and non-VCL NAL units stay in the clear while VCL slice payloads are
encrypted. Audio is encrypted as whole samples. IVs are 8 bytes and unique per
sample within a track.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from cencpack.mp4io import box, fullbox, u16, u32

IV_SIZE = 8

# H.264 NAL unit types that carry slice (VCL) data.
_VCL_NAL_TYPES = {1, 5}


@dataclass
class Subsample:
    clear_bytes: int
    encrypted_bytes: int


@dataclass
class EncryptedSample:
    data: bytes
    iv: bytes
    subsamples: list[Subsample]


class IvCounter:
    """Monotonic per-track IV source; zero-extended to the 16-byte counter."""

    def __init__(self, start: int = 1):
        self._value = start

    def next_iv(self) -> bytes:
        iv = struct.pack(">Q", self._value)
        self._value += 1
        return iv


def _aes_ctr(key: bytes, iv8: bytes, plaintext: bytes) -> bytes:
    nonce = iv8 + b"\x00" * 8
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    return encryptor.update(plaintext) + encryptor.finalize()


def _iter_nalus(sample: bytes):
    """Yield (length_prefix_offset, nal_header_offset, nalu_length) for AVC."""
    pos = 0
    while pos + 4 <= len(sample):
        length = struct.unpack(">I", sample[pos : pos + 4])[0]
        end = pos + 4 + length
        if length == 0 or end > len(sample):
            raise ValueError("malformed AVC length-prefixed sample")
        yield pos, pos + 4, length
        pos = end
    if pos != len(sample):
        raise ValueError("trailing bytes in AVC sample")


def video_subsamples(sample: bytes) -> list[Subsample]:
    """Build the CENC subsample table for one length-prefixed AVC sample."""
    subsamples: list[Subsample] = []
    for prefix_off, header_off, length in _iter_nalus(sample):
        nal_type = sample[header_off] & 0x1F
        if nal_type in _VCL_NAL_TYPES and length > 1:
            clear = 4 + 1  # length prefix + NAL header byte
            encrypted = length - 1
            subsamples.append(Subsample(clear, encrypted))
        else:
            subsamples.append(Subsample(4 + length, 0))
    # Merge adjacent all-clear ranges so the table stays compact and valid.
    merged: list[Subsample] = []
    for sub in subsamples:
        if merged and merged[-1].encrypted_bytes == 0 and sub.encrypted_bytes == 0:
            merged[-1] = Subsample(merged[-1].clear_bytes + sub.clear_bytes, 0)
        else:
            merged.append(sub)
    return merged


def encrypt_video_sample(sample: bytes, key: bytes, iv: bytes) -> EncryptedSample:
    subsamples = video_subsamples(sample)
    out = bytearray(sample)
    pos = 0
    for sub in subsamples:
        pos += sub.clear_bytes
        if sub.encrypted_bytes:
            plain = bytes(out[pos : pos + sub.encrypted_bytes])
            out[pos : pos + sub.encrypted_bytes] = _aes_ctr(key, iv, plain)
            pos += sub.encrypted_bytes
    return EncryptedSample(bytes(out), iv, subsamples)


def encrypt_audio_sample(sample: bytes, key: bytes, iv: bytes) -> EncryptedSample:
    if not sample:
        return EncryptedSample(sample, iv, [Subsample(0, 0)])
    return EncryptedSample(
        _aes_ctr(key, iv, sample), iv, [Subsample(0, len(sample))]
    )


def tenc_box(kid: bytes, is_protected: bool = True) -> bytes:
    # version/flags 0, reserved 0, is_protected=1, per_sample_iv_size=8,
    # 16-byte KID, no constant IV (per-sample IVs are carried in senc).
    payload = (
        b"\x00\x00"
        + bytes([1 if is_protected else 0, IV_SIZE])
        + kid
    )
    return fullbox("tenc", 0, 0, payload)


def sinf_box(original_format: str, kid: bytes) -> bytes:
    frma = box("frma", original_format.encode("latin-1"))
    schm = fullbox("schm", 0, 0, b"cenc" + u16(0x0001))  # scheme + version 1.0
    schi = box("schi", tenc_box(kid))
    return box("sinf", frma + schm + schi)


def senc_box(samples: list[EncryptedSample]) -> bytes:
    # version 0, flags 0x000002: IVs and subsample info follow each entry.
    payload = u32(len(samples))
    for sample in samples:
        if len(sample.iv) != IV_SIZE:
            raise ValueError("only 8-byte IVs are supported")
        payload += sample.iv
        payload += u16(len(sample.subsamples))
        for sub in sample.subsamples:
            payload += u16(sub.clear_bytes) + u32(sub.encrypted_bytes)
    return fullbox("senc", 0, 0x000002, payload)


def parse_senc(data: bytes, box) -> list[dict]:
    """Parse a senc box (verifier side): IV size fixed at 8 bytes."""
    p = box.offset + box.header_size
    count = struct.unpack(">I", data[p + 4 : p + 8])[0]
    o = p + 8
    entries = []
    for _ in range(count):
        iv = data[o : o + IV_SIZE]
        o += IV_SIZE
        nsub = struct.unpack(">H", data[o : o + 2])[0]
        o += 2
        subs = []
        for _ in range(nsub):
            clear, encrypted = struct.unpack(">HI", data[o : o + 6])
            o += 6
            subs.append(Subsample(clear, encrypted))
        entries.append({"iv": iv, "subsamples": subs})
    return entries


def decrypt_sample(encrypted: bytes, iv: bytes, subsamples: list[Subsample], key: bytes) -> bytes:
    out = bytearray(encrypted)
    pos = 0
    for sub in subsamples:
        pos += sub.clear_bytes
        if sub.encrypted_bytes:
            cipher = bytes(out[pos : pos + sub.encrypted_bytes])
            out[pos : pos + sub.encrypted_bytes] = _aes_ctr(key, iv, cipher)
            pos += sub.encrypted_bytes
    if pos != len(encrypted):
        raise ValueError("subsample table does not cover the whole sample")
    return bytes(out)
