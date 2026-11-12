"""CLI."""
from __future__ import annotations

import argparse
import json


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cencpack")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("probe", help="print tracks and sync samples (already works)")
    pr.add_argument("path")
    pk = sub.add_parser("package")
    pk.add_argument("--video", action="append", required=True)
    pk.add_argument("--audio", required=True)
    pk.add_argument("--ad", required=True)
    pk.add_argument("--cues", required=True)
    pk.add_argument("--keys", required=True)
    pk.add_argument("--out", required=True)
    vf = sub.add_parser("verify")
    vf.add_argument("--out", required=True)
    vf.add_argument("--keys", required=True)
    vf.add_argument("--source-video", required=True)
    vf.add_argument("--report", default=None, help="defaults to <out>/verify.json")
    args = p.parse_args(argv)
    if args.cmd == "probe":
        from cencpack.probe import probe
        print(json.dumps(probe(args.path), indent=2))
        return 0
    if args.cmd == "package":
        from cencpack.package import package
        package(args)
        return 0
    if args.cmd == "verify":
        from cencpack.verify import verify
        return verify(args)
    return 2
