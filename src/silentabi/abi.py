"""Data model, ABI size tables and parse/AST containers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


class SilentABIError(Exception):
    """Base error for every failure this package reports."""


class ParseError(SilentABIError):
    pass


class UnsupportedFeature(SilentABIError):
    pass


class LayoutError(SilentABIError):
    pass


@dataclass(frozen=True)
class TypeRef:
    """A parsed C type expression.

    ``base`` is canonical (``int``, ``long``, ``struct``, ``size_t`` ...).
    ``ref`` names the struct/union when ``base`` is ``struct``/``union``.
    ``ptr`` is the pointer depth, ``dims`` the array dimensions.
    """

    base: str
    ref: Optional[str] = None
    ptr: int = 0
    dims: Tuple[int, ...] = ()

    def describe(self) -> str:
        if self.ptr:
            core = f"{self.base} {self.ref or ''}".strip()
            return f"{core}{'*' * self.ptr}"
        if self.base in ("struct", "union"):
            core = f"{self.base} {self.ref}"
        else:
            core = self.base
        return core + "".join(f"[{d}]" for d in self.dims)

    def with_dims(self, dims: Tuple[int, ...]) -> "TypeRef":
        return TypeRef(self.base, self.ref, self.ptr, dims)

    def without_dims(self) -> "TypeRef":
        return TypeRef(self.base, self.ref, self.ptr, ())


@dataclass
class Field:
    name: str
    type: TypeRef
    line: int = 0


@dataclass
class StructDef:
    name: str
    kind: str  # "struct" | "union"
    fields: List[Field] = field(default_factory=list)
    line: int = 0
    anonymous: bool = False

    def field(self, name: str) -> Optional[Field]:
        for f in self.fields:
            if f.name == name:
                return f
        return None


@dataclass
class Unit:
    structs: Dict[str, StructDef] = field(default_factory=dict)
    typedefs: Dict[str, TypeRef] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)
    skipped_declarations: int = 0

    def add(self, sd: StructDef) -> StructDef:
        self.structs[sd.name] = sd
        if sd.name not in self.order:
            self.order.append(sd.name)
        return sd

    def resolve(self, t: TypeRef, _seen: Optional[set] = None) -> TypeRef:
        """Expand typedefs until a concrete type is reached."""
        seen = _seen or set()
        if t.ptr:
            return t
        if t.base in ("struct", "union"):
            return t
        if t.base in self.structs:
            return TypeRef("struct", t.base, 0, t.dims)
        alias = self.typedefs.get(t.base)
        if alias is None:
            return t
        if t.base in seen:
            raise LayoutError(f"typedef cycle at {t.base!r}")
        seen.add(t.base)
        resolved = self.resolve(alias, seen)
        return TypeRef(resolved.base, resolved.ref, resolved.ptr, t.dims)


@dataclass(frozen=True)
class Target:
    """Size/alignment of the scalar types for one ABI."""

    name: str
    sizes: Dict[str, Tuple[int, int]]
    pointer: Tuple[int, int]

    def lookup(self, base: str) -> Optional[Tuple[int, int]]:
        return self.sizes.get(base)


def _t(name: str, **kw: Tuple[int, int]) -> Target:
    return Target(name=name, sizes=dict(kw), pointer=kw["ptr"])


# Data model for x86-64 System V.  Long double: 16 bytes, 16-byte aligned.
LP64 = _t(
    "lp64",
    char=(1, 1),
    short=(2, 2),
    int=(4, 4),
    long=(8, 8),
    long_long=(8, 8),
    float=(4, 4),
    double=(8, 8),
    long_double=(16, 16),
    bool=(1, 1),
    ptr=(8, 8),
    size_t=(8, 8),
    ssize_t=(8, 8),
    ptrdiff_t=(8, 8),
    intptr_t=(8, 8),
    uintptr_t=(8, 8),
    int8_t=(1, 1),
    uint8_t=(1, 1),
    int16_t=(2, 2),
    uint16_t=(2, 2),
    int32_t=(4, 4),
    uint32_t=(4, 4),
    int64_t=(8, 8),
    uint64_t=(8, 8),
)

# i386 System V: long long/double are 4-byte aligned, long double is 12/4.
ILP32 = _t(
    "ilp32",
    char=(1, 1),
    short=(2, 2),
    int=(4, 4),
    long=(4, 4),
    long_long=(8, 4),
    float=(4, 4),
    double=(8, 4),
    long_double=(12, 4),
    bool=(1, 1),
    ptr=(4, 4),
    size_t=(4, 4),
    ssize_t=(4, 4),
    ptrdiff_t=(4, 4),
    intptr_t=(4, 4),
    uintptr_t=(4, 4),
    int8_t=(1, 1),
    uint8_t=(1, 1),
    int16_t=(2, 2),
    uint16_t=(2, 2),
    int32_t=(4, 4),
    uint32_t=(4, 4),
    int64_t=(8, 4),
    uint64_t=(8, 4),
)

# Windows LLP64 as implemented by MSVC: long stays 32-bit, pointers are 8.
LLP64 = _t(
    "llp64",
    char=(1, 1),
    short=(2, 2),
    int=(4, 4),
    long=(4, 4),
    long_long=(8, 8),
    float=(4, 4),
    double=(8, 8),
    long_double=(8, 8),
    bool=(1, 1),
    ptr=(8, 8),
    size_t=(8, 8),
    ssize_t=(8, 8),
    ptrdiff_t=(8, 8),
    intptr_t=(8, 8),
    uintptr_t=(8, 8),
    int8_t=(1, 1),
    uint8_t=(1, 1),
    int16_t=(2, 2),
    uint16_t=(2, 2),
    int32_t=(4, 4),
    uint32_t=(4, 4),
    int64_t=(8, 8),
    uint64_t=(8, 8),
)

TARGETS = {"lp64": LP64, "ilp32": ILP32, "llp64": LLP64}


def get_target(name: str) -> Target:
    try:
        return TARGETS[name]
    except KeyError:
        raise LayoutError(
            f"unknown ABI {name!r}; choose one of {', '.join(sorted(TARGETS))}"
        ) from None
