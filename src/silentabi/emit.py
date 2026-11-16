"""Canonical C emission (used to hand mutated declarations to a real compiler)."""

from __future__ import annotations

from typing import Iterable, List, Optional

from .abi import StructDef, TypeRef, Unit

_C_SPELLING = {
    "long_long": "long long",
    "long_double": "long double",
    "bool": "_Bool",
}


def type_c(t: TypeRef, unit: Unit, expand_typedef: bool = True) -> str:
    if expand_typedef:
        t = unit.resolve(t)
    if t.base in ("struct", "union"):
        core = f"{t.base} {t.ref}"
    else:
        core = _C_SPELLING.get(t.base, t.base)
    core += "*" * t.ptr
    return core


def declarator(t: TypeRef, unit: Unit, name: str, expand_typedef: bool = True) -> str:
    dims = "".join(f"[{d}]" for d in t.dims)
    return f"{type_c(t, unit, expand_typedef)} {name}{dims}"


def emit_struct(sd: StructDef, unit: Unit, name: Optional[str] = None) -> str:
    cname = name or sd.name
    lines: List[str] = [f"{sd.kind} {cname} {{"]
    for f in sd.fields:
        lines.append(f"    {declarator(f.type, unit, f.name)};")
    lines.append("};")
    return "\n".join(lines)


def emit_unit(unit: Unit, names: Optional[Iterable[str]] = None) -> str:
    chosen: List[str] = list(names) if names is not None else list(unit.order)
    parts = [emit_struct(unit.structs[n], unit) for n in chosen if n in unit.structs]
    parts.append("")
    return "\n".join(parts)


def emit_typedef_aliases(unit: Unit) -> str:
    """Emit typedef names as aliases, so original source spellings still parse."""
    lines = []
    for alias, t in unit.typedefs.items():
        lines.append(f"typedef {type_c(t, unit)} {alias};")
    return "\n".join(lines)
