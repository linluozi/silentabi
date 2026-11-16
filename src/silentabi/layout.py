"""Struct/union layout computation on top of the parsed declarations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .abi import LayoutError, StructDef, Target, TypeRef, Unit


def _round_up(value: int, align: int) -> int:
    if align <= 1:
        return value
    return ((value + align - 1) // align) * align


@dataclass
class FieldLayout:
    name: str
    type: str
    offset: int
    size: int
    align: int


@dataclass
class Layout:
    name: str
    kind: str
    size: int
    align: int
    fields: List[FieldLayout] = field(default_factory=list)
    # Byte ranges never covered by a leaf field, as (start, length) pairs.
    holes: Optional[List[Tuple[int, int]]] = None
    holes_reason: Optional[str] = None

    def field(self, name: str) -> Optional[FieldLayout]:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    @property
    def offset_map(self) -> Dict[str, int]:
        return {f.name: f.offset for f in self.fields}

    @property
    def padding_bytes(self) -> Optional[int]:
        if self.holes is None:
            return None
        return sum(length for _, length in self.holes)

    def holes_as_set(self) -> Optional[set]:
        if self.holes is None:
            return None
        out = set()
        for start, length in self.holes:
            out.update(range(start, start + length))
        return out


class Layouter:
    def __init__(self, unit: Unit, target: Target) -> None:
        self.unit = unit
        self.target = target
        self._cache: Dict[str, Layout] = {}

    def _type_geometry(self, t: TypeRef, stack: Tuple[str, ...]) -> Tuple[int, int]:
        if t.ptr:
            return self.target.pointer
        resolved = self.unit.resolve(t)
        if resolved.base in ("struct", "union"):
            sub = self.layout(resolved.ref or "", stack)
            size, align = sub.size, sub.align
        else:
            pair = self.target.lookup(resolved.base)
            if pair is None:
                raise LayoutError(
                    f"unknown type {resolved.base!r}; this ABI model has no size for it"
                )
            size, align = pair
        if resolved.dims:
            count = 1
            for d in resolved.dims:
                count *= d
            size *= count
        return size, align

    def _leaf_ranges(
        self,
        sd: StructDef,
        fields: List[FieldLayout],
        base: int,
        stack: Tuple[str, ...],
    ) -> Tuple[List[Tuple[int, int]], Optional[str]]:
        """Absolute byte ranges written by assigning every leaf field."""
        offsets = {f.name: f.offset for f in fields}
        ranges: List[Tuple[int, int]] = []
        for f in sd.fields:
            t = self.unit.resolve(f.type)
            off = base + offsets[f.name]
            if t.ptr:
                ranges.append((off, self.target.pointer[0]))
                continue
            count = 1
            for d in t.dims:
                count *= d
            elem = t.without_dims()
            if elem.base in ("struct", "union"):
                sub = self.unit.structs.get(elem.ref or "")
                if sub is None:
                    raise LayoutError(f"unknown {elem.base} {elem.ref!r}")
                if sub.kind == "union":
                    return [], f"contains union {sub.name}"
                sub_lay = self.layout(sub.name, stack)
                for i in range(count):
                    sub_ranges, reason = self._leaf_ranges(
                        sub, sub_lay.fields, off + i * sub_lay.size, stack
                    )
                    if reason:
                        return [], reason
                    ranges.extend(sub_ranges)
            else:
                esize, _ = self._type_geometry(elem, stack)
                ranges.append((off, esize * count))
        return ranges, None

    def layout(self, name: str, stack: Tuple[str, ...] = ()) -> Layout:
        if name in self._cache:
            return self._cache[name]
        sd = self.unit.structs.get(name)
        if sd is None:
            raise LayoutError(f"unknown struct/union {name!r}")
        if name in stack:
            raise LayoutError("recursive type: " + " -> ".join(stack + (name,)))
        stack = stack + (name,)
        fields: List[FieldLayout] = []
        if sd.kind == "struct":
            offset = 0
            max_align = 1
            for f in sd.fields:
                size, align = self._type_geometry(f.type, stack)
                offset = _round_up(offset, align)
                fields.append(
                    FieldLayout(f.name, f.type.describe(), offset, size, align)
                )
                offset += size
                max_align = max(max_align, align)
            size = _round_up(offset, max_align)
        else:
            size = 0
            max_align = 1
            for f in sd.fields:
                fsize, falign = self._type_geometry(f.type, stack)
                fields.append(FieldLayout(f.name, f.type.describe(), 0, fsize, falign))
                size = max(size, fsize)
                max_align = max(max_align, falign)
            size = _round_up(size, max_align)
        lay = Layout(sd.name, sd.kind, size, max_align, fields)
        if sd.kind == "struct":
            ranges, reason = self._leaf_ranges(sd, fields, 0, stack)
            if reason:
                lay.holes_reason = reason
            else:
                covered = set()
                for start, length in ranges:
                    covered.update(range(start, start + length))
                holes: List[Tuple[int, int]] = []
                i = 0
                while i < lay.size:
                    if i in covered:
                        i += 1
                        continue
                    start = i
                    while i < lay.size and i not in covered:
                        i += 1
                    holes.append((start, i - start))
                lay.holes = holes
        else:
            lay.holes_reason = "unions: only one member is written at a time"
        self._cache[name] = lay
        return lay


def layout_of(unit: Unit, name: str, target: Target) -> Layout:
    return Layouter(unit, target).layout(name)


def layout_all(unit: Unit, target: Target) -> Dict[str, Layout]:
    layouter = Layouter(unit, target)
    return {name: layouter.layout(name) for name in unit.order}
