"""Small ISO-BMFF box writer used by the fragmenter/packager.

The reader side lives in :mod:`cencpack.boxes`; this module only serializes
boxes that we build ourselves (init segments, moof/traf, senc/tenc, ...).
"""
from __future__ import annotations

import struct


def box(typ: str, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ.encode("latin-1")) + payload


def fullbox(typ: str, version: int, flags: int, payload: bytes = b"") -> bytes:
    return box(typ, bytes([version]) + struct.pack(">I", flags)[1:] + payload)


def container(typ: str, children: list[bytes]) -> bytes:
    return box(typ, b"".join(children))


def u16(v: int) -> bytes:
    return struct.pack(">H", v)


def u32(v: int) -> bytes:
    return struct.pack(">I", v)


def u64(v: int) -> bytes:
    return struct.pack(">Q", v)


def i32(v: int) -> bytes:
    return struct.pack(">i", v)
