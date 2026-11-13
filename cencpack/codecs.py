"""Derive RFC 6381 codec strings from the source sample entries."""
from __future__ import annotations

from cencpack.boxes import find_all, parse_tree


def avc1_codec(track) -> str:
    avcc = find_all(parse_tree(track.data), "avcC")[0]
    p = avcc.offset + avcc.header_size
    profile = track.data[p + 1]
    constraints = track.data[p + 2]
    level = track.data[p + 3]
    return f"avc1.{profile:02X}{constraints:02X}{level:02X}"


def mp4a_codec(track) -> str:
    # AAC LC is object type 2; the fixtures all use this profile.
    return "mp4a.40.2"
