#!/usr/bin/env python3
"""Measure how many compile-clean struct edits silently move bytes.

For every struct in evidence/corpus/*.h we apply a fixed set of textual-semantic
mutations (append / insert / widen / array resize / reorder).  Each mutated
translation unit is compiled with `gcc -std=c11 -Wall -Wextra -Werror`; edits
that compile cleanly are "silent" as far as the compiler is concerned.  We then
diff the layout model and count how many of those silent edits are ABI breaks.

Writes evidence/mutation_audit.json.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from silentabi import get_target, parse_file  # noqa: E402
from silentabi.abi import Field, StructDef, TypeRef  # noqa: E402
from silentabi.compiler import compile_source  # noqa: E402
from silentabi.diff import diff_structs  # noqa: E402
from silentabi.emit import emit_unit  # noqa: E402

CORPUS = ["net.h", "proto.h", "db.h"]


def _is_scalar_array(unit, t: TypeRef) -> bool:
    if t.ptr:
        return False
    r = unit.resolve(t)
    return r.base not in ("struct", "union")


def mutations(unit, sd: StructDef):
    """Yield (kind, mutated_struct) pairs; deterministic order."""
    fields = sd.fields

    def clone():
        nsd = StructDef(sd.name, sd.kind, copy.deepcopy(sd.fields), sd.line)
        return nsd

    def emit(kind, nsd):
        return kind, nsd

    if len(fields) >= 1:
        nsd = clone()
        nsd.fields.append(Field("tail_u8", TypeRef("uint8_t")))
        yield emit("append_u8", nsd)

        nsd = clone()
        nsd.fields.append(Field("tail_u64", TypeRef("uint64_t")))
        yield emit("append_u64", nsd)

        nsd = clone()
        nsd.fields.insert(1, Field("ins_u8", TypeRef("uint8_t")))
        yield emit("insert_u8_after_first", nsd)

        nsd = clone()
        nsd.fields.insert(1, Field("ins_u64", TypeRef("uint64_t")))
        yield emit("insert_u64_after_first", nsd)

        nsd = clone()
        nsd.fields.insert(len(nsd.fields) - 1, Field("pad4", TypeRef("char", dims=(4,))))
        yield emit("insert_char4_before_last", nsd)

        nsd = clone()
        nsd.fields.append(nsd.fields.pop(0))
        yield emit("move_first_to_end", nsd)

    if len(fields) >= 2:
        nsd = clone()
        nsd.fields.pop()
        yield emit("remove_last", nsd)

    for i, f in enumerate(fields):
        if not f.type.ptr and _is_scalar_array(unit, f.type):
            nsd = clone()
            nsd.fields[i] = Field(f.name, TypeRef("long"))
            yield emit("widen_to_long", nsd)
            break
    for i in range(len(fields) - 1, -1, -1):
        f = fields[i]
        if not f.type.ptr and _is_scalar_array(unit, f.type):
            nsd = clone()
            nsd.fields[i] = Field(f.name, TypeRef("double"))
            yield emit("retype_last_to_double", nsd)
            break
    for i, f in enumerate(fields):
        t = unit.resolve(f.type)
        if t.dims and f.type.dims and f.type.dims[0] > 1:
            nsd = clone()
            nsd.fields[i] = Field(f.name, TypeRef(f.type.base, f.type.ref, 0, (f.type.dims[0] - 1,)))
            yield emit("array_shrink", nsd)
            nsd = clone()
            nsd.fields[i] = Field(f.name, TypeRef(f.type.base, f.type.ref, 0, (f.type.dims[0] + 1,)))
            yield emit("array_grow", nsd)
            break
    for i in range(len(fields) - 1):
        a, b = fields[i], fields[i + 1]
        if _is_scalar_array(unit, a.type) and _is_scalar_array(unit, b.type):
            if a.type.dims != b.type.dims:
                nsd = clone()
                nsd.fields[i], nsd.fields[i + 1] = nsd.fields[i + 1], nsd.fields[i]
                yield emit("swap_adjacent", nsd)
                break


def main() -> int:
    target = get_target("lp64")
    cc_version = subprocess.run(
        ["gcc", "--version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    records = []
    totals = {}
    for filename in CORPUS:
        path = os.path.join(HERE, "corpus", filename)
        unit = parse_file(path)
        for name in unit.order:
            sd = unit.structs[name]
            if sd.anonymous or sd.kind != "struct":
                continue
            for kind, mutated in mutations(unit, sd):
                new_unit = copy.deepcopy(unit)
                new_unit.structs[name] = mutated
                result = compile_source(emit_unit(new_unit))
                diff = diff_structs(unit, new_unit, target, name)
                diff.compiles = result.ok
                bucket = totals.setdefault(
                    kind, {"total": 0, "compiled_clean": 0, "abi_break": 0, "silent": 0}
                )
                bucket["total"] += 1
                if result.ok:
                    bucket["compiled_clean"] += 1
                    if diff.abi_break:
                        bucket["abi_break"] += 1
                        bucket["silent"] += 1
                records.append(
                    {
                        "file": filename,
                        "struct": name,
                        "mutation": kind,
                        "compiled": result.ok,
                        "verdict": diff.verdict,
                        "abi_break": diff.abi_break,
                        "size_old": diff.old.size,
                        "size_new": diff.new.size,
                        "changes": [c.kind for c in diff.changes],
                    }
                )

    total = sum(b["total"] for b in totals.values())
    clean = sum(b["compiled_clean"] for b in totals.values())
    silent = sum(b["silent"] for b in totals.values())
    examples = [
        r for r in records if r["compiled"] and r["abi_break"]
    ][:8]
    report = {
        "gcc": cc_version,
        "abi": target.name,
        "compiler_flags": ["-std=c11", "-Wall", "-Wextra", "-Werror"],
        "corpus": CORPUS,
        "mutations_total": total,
        "mutations_compiled_clean": clean,
        "silent_abi_breaks": silent,
        "silent_share_of_clean": round(silent / clean, 6) if clean else None,
        "by_mutation": totals,
        "examples": examples,
        "records": records,
    }
    out = os.path.join(HERE, "mutation_audit.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(
        json.dumps(
            {
                "mutations_total": total,
                "compiled_clean": clean,
                "silent_abi_breaks": silent,
                "silent_share_of_clean": report["silent_share_of_clean"],
            },
            indent=2,
        )
    )
    for kind, b in sorted(totals.items()):
        print(f"{kind:<26} clean {b['compiled_clean']:>3}/{b['total']:<3} abi {b['abi_break']:>3}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
