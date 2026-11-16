"""Deterministic random struct generator shared by the evidence scripts."""

from __future__ import annotations

import random
from typing import List

from silentabi.abi import Field, StructDef, TypeRef, Unit

SCALARS = [
    "char", "short", "int", "long", "long_long", "float", "double",
    "bool", "size_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
]


def generate(seed: int, count: int, allow_union: bool = True) -> Unit:
    rng = random.Random(seed)
    unit = Unit()
    names: List[str] = []
    for i in range(count):
        name = f"S{i:03d}"
        kind = "union" if allow_union and i > 0 and rng.random() < 0.12 else "struct"
        fields: List[Field] = []
        for j in range(rng.randint(1, 7)):
            roll = rng.random()
            if roll < 0.20 and names:
                target = rng.choice(names)
                fields.append(
                    Field(f"f{j}", TypeRef(unit.structs[target].kind, target))
                )
            elif roll < 0.30:
                fields.append(Field(f"f{j}", TypeRef("void", ptr=1)))
            else:
                base = rng.choice(SCALARS)
                if rng.random() < 0.45:
                    fields.append(
                        Field(f"f{j}", TypeRef(base, dims=(rng.randint(1, 4),)))
                    )
                else:
                    fields.append(Field(f"f{j}", TypeRef(base)))
        unit.add(StructDef(name=name, kind=kind, fields=fields))
        names.append(name)
    return unit


def struct_names(unit: Unit) -> List[str]:
    return [n for n in unit.order if unit.structs[n].kind == "struct"]
