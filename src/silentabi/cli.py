"""Command line interface for silentabi."""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import __version__
from .abi import SilentABIError, get_target
from .compiler import compile_source, find_compiler
from .diff import diff_structs
from .emit import emit_unit
from .layout import Layouter
from .parser import parse_file

EXIT_IDENTICAL = 0
EXIT_SOURCE_ONLY = 1
EXIT_ABI_BREAK = 2

_VERDICT_EXIT = {
    "IDENTICAL": EXIT_IDENTICAL,
    "WIRE_ONLY": EXIT_SOURCE_ONLY,
    "SOURCE_ONLY": EXIT_SOURCE_ONLY,
    "ABI_BREAK": EXIT_ABI_BREAK,
}


def _load(path: str):
    return parse_file(path)


def _layout_dict(lay) -> dict:
    return {
        "struct": lay.name,
        "kind": lay.kind,
        "size": lay.size,
        "align": lay.align,
        "fields": [
            {"name": f.name, "type": f.type, "offset": f.offset, "size": f.size,
             "align": f.align}
            for f in lay.fields
        ],
        "padding": lay.holes,
        "padding_bytes": lay.padding_bytes,
        "padding_reason": lay.holes_reason,
    }


def cmd_show(args: argparse.Namespace) -> int:
    unit = _load(args.file)
    target = get_target(args.abi)
    layouter = Layouter(unit, target)
    rows = []
    for name in unit.order:
        sd = unit.structs[name]
        try:
            lay = layouter.layout(name)
            rows.append(
                {
                    "struct": name,
                    "kind": sd.kind,
                    "anonymous": sd.anonymous,
                    "fields": len(sd.fields),
                    "size": lay.size,
                    "align": lay.align,
                    "padding_bytes": lay.padding_bytes,
                }
            )
        except SilentABIError as exc:  # keep going: other structs may be fine
            rows.append({"struct": name, "kind": sd.kind, "error": str(exc)})
    if args.json:
        print(json.dumps({"target": target.name, "structs": rows}, indent=2))
    else:
        print(f"# target={target.name} skipped={unit.skipped_declarations}")
        for r in rows:
            if "error" in r:
                print(f"{r['struct']:<28} ERROR {r['error']}")
            else:
                print(
                    f"{r['struct']:<28} {r['kind']:<6} size={r['size']:<4} "
                    f"align={r['align']:<3} pad={r['padding_bytes']}"
                )
    return 0


def cmd_layout(args: argparse.Namespace) -> int:
    unit = _load(args.file)
    target = get_target(args.abi)
    lay = Layouter(unit, target).layout(args.struct)
    if args.json:
        print(json.dumps(_layout_dict(lay), indent=2))
    else:
        print(f"{lay.kind} {lay.name}  # {target.name}")
        print(f"  sizeof={lay.size} alignof={lay.align} padding={lay.padding_bytes}")
        for f in lay.fields:
            print(f"  +{f.offset:<4} {f.type:<24} {f.name}")
        if lay.holes is not None:
            print(f"  unwritten_bytes={lay.holes}")
        else:
            print(f"  unwritten_bytes=<not modelled: {lay.holes_reason}>")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    old = _load(args.old)
    new = _load(args.new)
    target = get_target(args.abi)
    diff = diff_structs(old, new, target, args.struct)
    if args.cc == "none":
        diff.compiles = None
    else:
        cc = find_compiler(args.cc)
        if cc is None:
            raise SystemExit(f"compiler {args.cc!r} not found")
        result = compile_source(emit_unit(new), cc=cc)
        diff.compiles = result.ok
        diff.compiler_output = result.diagnostics
    if args.json:
        print(json.dumps(diff.to_dict(), indent=2))
    else:
        print(f"{diff.kind} {diff.name}: {diff.verdict}")
        print(
            f"  sizeof {diff.old.size} -> {diff.new.size}, "
            f"align {diff.old.align} -> {diff.new.align}, "
            f"padding {diff.old.padding_bytes} -> {diff.new.padding_bytes}"
        )
        print(
            f"  abi_break={diff.abi_break} source_break={diff.source_break} "
            f"wire_change={diff.wire_change} compiles={diff.compiles} silent={diff.silent}"
        )
        for c in diff.changes:
            print(f"  - {c.kind:<15} {c.detail}")
        if diff.compiler_output:
            print("  compiler:")
            for line in diff.compiler_output.splitlines():
                print(f"    {line}")
    return _VERDICT_EXIT[diff.verdict]


def cmd_audit(args: argparse.Namespace) -> int:
    old = _load(args.old)
    new = _load(args.new)
    target = get_target(args.abi)
    names = [n for n in old.order if n in new.structs and not old.structs[n].anonymous]
    diffs = [diff_structs(old, new, target, n) for n in names]
    cc = None
    if args.cc != "none":
        cc = find_compiler(args.cc)
        if cc is None:
            raise SystemExit(f"compiler {args.cc!r} not found")
    for d in diffs:
        if cc is not None:
            d.compiles = compile_source(emit_unit(new), cc=cc).ok
    summary = {
        "target": target.name,
        "structs": len(diffs),
        "abi_breaking": sum(1 for d in diffs if d.abi_break),
        "source_breaking": sum(1 for d in diffs if d.source_break),
        "wire_changing": sum(1 for d in diffs if d.wire_change),
        "silent_abi_breaking": sum(1 for d in diffs if d.silent),
    }
    payload = {"summary": summary, "diffs": [d.to_dict() for d in diffs]}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(json.dumps(summary, indent=2))
        for d in diffs:
            print(f"{d.name:<28} {d.verdict:<12} silent={d.silent}")
    return EXIT_ABI_BREAK if summary["abi_breaking"] else EXIT_IDENTICAL


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="silentabi",
        description=(
            "Model C struct layout, diff two declarations, and flag changes a "
            "compiler accepts without warnings that still move bytes."
        ),
    )
    p.add_argument("--version", action="version", version=f"silentabi {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="list structs and layouts in a header")
    show.add_argument("file")
    show.add_argument("--abi", default="lp64", help="lp64 | ilp32 | llp64")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    lay = sub.add_parser("layout", help="show one struct layout")
    lay.add_argument("file")
    lay.add_argument("--struct", required=True)
    lay.add_argument("--abi", default="lp64")
    lay.add_argument("--json", action="store_true")
    lay.set_defaults(func=cmd_layout)

    dif = sub.add_parser("diff", help="diff one struct between two headers")
    dif.add_argument("old")
    dif.add_argument("new")
    dif.add_argument("--struct", required=True)
    dif.add_argument("--abi", default="lp64")
    dif.add_argument("--cc", default="gcc", help="gcc | cc | clang | none")
    dif.add_argument("--json", action="store_true")
    dif.set_defaults(func=cmd_diff)

    aud = sub.add_parser("audit", help="diff every common struct between two headers")
    aud.add_argument("old")
    aud.add_argument("new")
    aud.add_argument("--abi", default="lp64")
    aud.add_argument("--cc", default="gcc")
    aud.add_argument("--json", action="store_true")
    aud.set_defaults(func=cmd_audit)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return int(args.func(args))
    except (SilentABIError, KeyError) as exc:
        print(f"silentabi: {exc}", file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        print(f"silentabi: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
