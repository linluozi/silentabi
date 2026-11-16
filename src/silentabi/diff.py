"""Layout diffing: which compile-clean edits move bytes, and which move padding."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .abi import Target, Unit
from .layout import Layout, Layouter


@dataclass
class Change:
    kind: str  # removed | added | type_changed | offset_moved | size_changed | align_changed | padding_changed
    field: Optional[str]
    detail: str
    old: Optional[object] = None
    new: Optional[object] = None


@dataclass
class StructDiff:
    name: str
    kind: str
    old: Layout
    new: Layout
    changes: List[Change] = field(default_factory=list)
    abi_break: bool = False
    source_break: bool = False
    wire_change: bool = False
    verdict: str = "IDENTICAL"
    compiles: Optional[bool] = None
    compiler_output: str = ""

    @property
    def silent(self) -> bool:
        """True when the new declaration compiles cleanly yet ABI changed."""
        return bool(self.compiles) and self.abi_break

    def to_dict(self) -> Dict[str, object]:
        return {
            "struct": self.name,
            "kind": self.kind,
            "verdict": self.verdict,
            "abi_break": self.abi_break,
            "source_break": self.source_break,
            "wire_change": self.wire_change,
            "compiles": self.compiles,
            "silent": self.silent,
            "old": {
                "size": self.old.size,
                "align": self.old.align,
                "offsets": self.old.offset_map,
                "padding_bytes": self.old.padding_bytes,
            },
            "new": {
                "size": self.new.size,
                "align": self.new.align,
                "offsets": self.new.offset_map,
                "padding_bytes": self.new.padding_bytes,
            },
            "changes": [
                {"kind": c.kind, "field": c.field, "detail": c.detail}
                for c in self.changes
            ],
        }


def diff_layouts(name: str, kind: str, old: Layout, new: Layout) -> StructDiff:
    d = StructDiff(name=name, kind=kind, old=old, new=new)
    old_fields = {f.name: f for f in old.fields}
    new_fields = {f.name: f for f in new.fields}

    for fname in old_fields:
        if fname not in new_fields:
            d.changes.append(
                Change("removed", fname, f"field {fname!r} removed")
            )
            d.source_break = True
    for fname in new_fields:
        if fname not in old_fields:
            d.changes.append(Change("added", fname, f"field {fname!r} added"))
            d.source_break = True
    for fname in old_fields:
        if fname not in new_fields:
            continue
        a, b = old_fields[fname], new_fields[fname]
        if a.type != b.type or a.size != b.size:
            d.changes.append(
                Change(
                    "type_changed",
                    fname,
                    f"{fname!r}: {a.type} ({a.size}B) -> {b.type} ({b.size}B)",
                    a.type,
                    b.type,
                )
            )
            d.source_break = True
        if a.offset != b.offset:
            d.changes.append(
                Change(
                    "offset_moved",
                    fname,
                    f"{fname!r} moved {a.offset} -> {b.offset}",
                    a.offset,
                    b.offset,
                )
            )
            d.abi_break = True
    if old.size != new.size:
        d.changes.append(
            Change("size_changed", None, f"sizeof {old.size} -> {new.size}", old.size, new.size)
        )
        d.abi_break = True
    if old.align != new.align:
        d.changes.append(
            Change(
                "align_changed", None, f"alignof {old.align} -> {new.align}", old.align, new.align
            )
        )
        d.abi_break = True
    if old.holes is not None and new.holes is not None and old.holes != new.holes:
        d.changes.append(
            Change(
                "padding_changed",
                None,
                f"unwritten byte ranges {old.holes} -> {new.holes}",
                old.holes,
                new.holes,
            )
        )
        d.wire_change = True
        if old.padding_bytes != new.padding_bytes:
            d.source_break = True
    if d.abi_break:
        d.verdict = "ABI_BREAK"
    elif d.source_break:
        d.verdict = "SOURCE_ONLY"
    elif d.wire_change:
        d.verdict = "WIRE_ONLY"
    else:
        d.verdict = "IDENTICAL"
    return d


def diff_structs(old_unit: Unit, new_unit: Unit, target: Target, name: str) -> StructDiff:
    old_sd = old_unit.structs.get(name)
    new_sd = new_unit.structs.get(name)
    if old_sd is None or new_sd is None:
        raise KeyError(f"struct {name!r} missing from one of the translation units")
    old_lay = Layouter(old_unit, target).layout(name)
    new_lay = Layouter(new_unit, target).layout(name)
    return diff_layouts(name, new_sd.kind, old_lay, new_lay)


def diff_units(old_unit: Unit, new_unit: Unit, target: Target) -> List[StructDiff]:
    out = []
    for name in old_unit.order:
        if name not in new_unit.structs:
            continue
        if old_unit.structs[name].anonymous or new_unit.structs[name].anonymous:
            continue
        out.append(diff_structs(old_unit, new_unit, target, name))
    return out
