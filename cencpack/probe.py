"""Probe helper built on boxes. Complete."""
from __future__ import annotations

from pathlib import Path

from cencpack.boxes import (
    find_all,
    load,
    parse_hdlr,
    parse_mdhd_timescale,
    parse_stss,
    parse_stts,
    sample_deltas,
)


def probe(path: str | Path) -> dict:
    data, tree = load(path)
    tracks = []
    for trak in find_all(tree, "trak"):
        mdhd = find_all([trak], "mdhd")
        hdlr = find_all([trak], "hdlr")
        stts = find_all([trak], "stts")
        stss = find_all([trak], "stss")
        ts = parse_mdhd_timescale(data, mdhd[0]) if mdhd else None
        handler = parse_hdlr(data, hdlr[0]) if hdlr else "?"
        deltas = sample_deltas(parse_stts(data, stts[0])) if stts else []
        sync = parse_stss(data, stss[0]) if stss else []
        duration = sum(deltas)
        tracks.append({
            "handler": handler,
            "timescale": ts,
            "samples": len(deltas),
            "duration_s": (duration / ts) if ts else None,
            "sync_samples": sync,
            "gop": (sync[1] - sync[0]) if len(sync) >= 2 else None,
        })
    return {"path": str(path), "size": len(data), "tracks": tracks}
