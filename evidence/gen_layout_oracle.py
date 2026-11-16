#!/usr/bin/env python3
"""Compare the pure-Python layout model against gcc, field by field.

Runs gcc on a generated translation unit and checks sizeof/alignof/offsetof for
every struct and union. Writes evidence/layout_oracle.json.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from generator import generate  # noqa: E402
from silentabi import get_target  # noqa: E402
from silentabi.emit import emit_unit  # noqa: E402
from silentabi.layout import layout_all  # noqa: E402

SEED = 20261116
COUNT = 300


def build_program(unit) -> str:
    lines = [
        "#include <stdio.h>",
        "#include <stddef.h>",
        "#include <stdint.h>",
        "",
        emit_unit(unit),
        "int main(void) {",
    ]
    for name in unit.order:
        sd = unit.structs[name]
        offsets = " ".join(f"{f.name}:%zu" for f in sd.fields)
        fmt = f"{name} %zu %zu" + (f" {offsets}" if offsets else "")
        args = ["sizeof(" + f"{sd.kind} {name})", "_Alignof(" + f"{sd.kind} {name})"]
        args += [f"offsetof({sd.kind} {name}, {f.name})" for f in sd.fields]
        lines.append(f'    printf("{fmt}\\n", {", ".join(args)});')
    lines.append("    return 0;")
    lines.append("}")
    return "\n".join(lines)


def main() -> int:
    unit = generate(SEED, COUNT, allow_union=True)
    target = get_target("lp64")
    model = layout_all(unit, target)
    program = build_program(unit)
    with tempfile.TemporaryDirectory(prefix="silentabi-oracle-") as tmp:
        src = os.path.join(tmp, "oracle.c")
        exe = os.path.join(tmp, "oracle")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(program)
        cc = subprocess.run(
            ["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", src, "-o", exe],
            capture_output=True, text=True,
        )
        if cc.returncode != 0:
            print(cc.stderr, file=sys.stderr)
            raise SystemExit("oracle program failed to compile")
        out = subprocess.run([exe], capture_output=True, text=True, check=True).stdout
        version = subprocess.run(
            ["gcc", "--version"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]

    checked = 0
    mismatches = []
    for line in out.strip().splitlines():
        parts = line.split()
        name, size, align = parts[0], int(parts[1]), int(parts[2])
        gcc_offsets = {}
        for item in parts[3:]:
            fname, off = item.rsplit(":", 1)
            gcc_offsets[fname] = int(off)
        lay = model[name]
        checked += 1
        if lay.size != size:
            mismatches.append({"struct": name, "what": "size", "model": lay.size, "gcc": size})
        if lay.align != align:
            mismatches.append({"struct": name, "what": "align", "model": lay.align, "gcc": align})
        if lay.offset_map != gcc_offsets:
            mismatches.append(
                {"struct": name, "what": "offsets", "model": lay.offset_map, "gcc": gcc_offsets}
            )

    padding_checked = 0
    padding_identity_failures = []
    for name, lay in model.items():
        if lay.holes is None:
            continue
        padding_checked += 1
        leaf_bytes = 0
        for f in lay.fields:
            leaf_bytes += f.size
        if leaf_bytes + lay.padding_bytes != lay.size:
            # Nested structs contain their own padding, so the identity is only
            # exact when every field is a scalar or an array of scalars.
            flat = True
            for f in unit.structs[name].fields:
                t = unit.resolve(f.type)
                if not t.ptr and t.base in ("struct", "union"):
                    flat = False
            if flat:
                padding_identity_failures.append(
                    {"struct": name, "leaf": leaf_bytes, "padding": lay.padding_bytes,
                     "size": lay.size}
                )

    report = {
        "seed": SEED,
        "count": COUNT,
        "abi": target.name,
        "gcc": version,
        "structures_checked": checked,
        "padding_structs_checked": padding_checked,
        "mismatches": mismatches,
        "padding_identity_failures": padding_identity_failures,
        "ok": not mismatches and not padding_identity_failures,
    }
    path = os.path.join(HERE, "layout_oracle.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps({k: report[k] for k in
                      ("structures_checked", "padding_structs_checked", "ok")}, indent=2))
    print(f"wrote {path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
