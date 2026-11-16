#!/usr/bin/env python3
"""Check model-predicted padding against bytes a real program never writes.

For each generated struct: memset the object to 0xAA, assign every leaf field,
dump sizeof bytes.  Bytes still 0xAA are the truly unwritten (padding) bytes.
Compare that set with Layout.holes.
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

SEED = 424242
COUNT = 80


def leaves(unit, sd, prefix, out):
    for f in sd.fields:
        t = unit.resolve(f.type)
        path = f"{prefix}{f.name}"
        if t.ptr:
            out.append((path, "ptr"))
        elif t.base in ("struct", "union"):
            sub = unit.structs[t.ref]
            if sub.kind == "union":
                return False
            if t.dims:
                count = 1
                for d in t.dims:
                    count *= d
                for i in range(count):
                    if not leaves(unit, sub, f"{path}[{i}].", out):
                        return False
            elif not leaves(unit, sub, path + ".", out):
                return False
        elif t.dims:
            count = 1
            for d in t.dims:
                count *= d
            for i in range(count):
                out.append((f"{path}[{i}]", "scalar"))
        else:
            out.append((path, "scalar"))
    return True


def build_program(unit, target) -> str:
    lines = [
        "#include <stdio.h>",
        "#include <string.h>",
        "#include <stdint.h>",
        "static int probe_global;",
        "",
        emit_unit(unit),
        "int main(void) {",
        "    unsigned char *p; size_t i;",
    ]
    for name in unit.order:
        sd = unit.structs[name]
        if sd.kind != "struct":
            continue
        paths = []
        if not leaves(unit, sd, "", paths):
            continue
        lines.append(f"    {{ {sd.kind} {name} s;")
        lines.append(f"      memset(&s, 0xAA, sizeof s);")
        for path, kind in paths:
            if kind == "ptr":
                lines.append(f"      s.{path} = (void*)&probe_global;")
            else:
                lines.append(f"      s.{path} = 1;")
        lines.append(f"      p = (unsigned char*)&s;")
        lines.append(f'      printf("{name} ");')
        lines.append(f'      for (i = 0; i < sizeof s; i++) printf("%02x", p[i]);')
        lines.append(f'      printf("\\n"); }}')
    lines += ["    return 0;", "}"]
    return "\n".join(lines)


def main() -> int:
    unit = generate(SEED, COUNT, allow_union=False)
    target = get_target("lp64")
    model = layout_all(unit, target)
    program = build_program(unit, target)
    with tempfile.TemporaryDirectory(prefix="silentabi-pad-") as tmp:
        src = os.path.join(tmp, "pad.c")
        exe = os.path.join(tmp, "pad")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(program)
        cc = subprocess.run(
            ["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", src, "-o", exe],
            capture_output=True, text=True,
        )
        if cc.returncode != 0:
            print(cc.stderr, file=sys.stderr)
            raise SystemExit("padding oracle failed to compile")
        out = subprocess.run([exe], capture_output=True, text=True, check=True).stdout
        version = subprocess.run(
            ["gcc", "--version"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]

    checked = 0
    mismatches = []
    total_pad_bytes = 0
    for line in out.strip().splitlines():
        name, hexdump = line.split()
        observed = {i for i, byte in enumerate(bytes.fromhex(hexdump)) if byte == 0xAA}
        lay = model[name]
        predicted = lay.holes_as_set()
        if predicted is None:
            continue
        checked += 1
        total_pad_bytes += len(predicted)
        if observed != predicted:
            mismatches.append(
                {
                    "struct": name,
                    "size": lay.size,
                    "predicted_only": sorted(predicted - observed),
                    "observed_only": sorted(observed - predicted),
                }
            )

    report = {
        "seed": SEED,
        "count": COUNT,
        "abi": target.name,
        "gcc": version,
        "structs_checked": checked,
        "padding_bytes_total": total_pad_bytes,
        "mismatches": mismatches,
        "ok": not mismatches,
    }
    path = os.path.join(HERE, "padding_oracle.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps({k: report[k] for k in
                      ("structs_checked", "padding_bytes_total", "ok")}, indent=2))
    print(f"wrote {path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
