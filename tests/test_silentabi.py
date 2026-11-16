"""Behavioural tests for silentabi (stdlib unittest, no third-party deps)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from silentabi import (  # noqa: E402
    ILP32,
    LLP64,
    LP64,
    ParseError,
    UnsupportedFeature,
    diff_structs,
    get_target,
    parse,
)
from silentabi.cli import main as cli_main  # noqa: E402
from silentabi.emit import emit_unit  # noqa: E402
from silentabi.layout import Layouter, layout_all  # noqa: E402

HAS_GCC = shutil.which("gcc") is not None


def lay(source: str, name: str, target=LP64):
    unit = parse(source)
    return unit, Layouter(unit, target).layout(name)


class ParserTests(unittest.TestCase):
    def test_basic_fields_and_typedef(self):
        unit = parse(
            "typedef unsigned int u32;\n"
            "struct A { char c; u32 n; struct A *next; };\n"
        )
        sd = unit.structs["A"]
        self.assertEqual([f.name for f in sd.fields], ["c", "n", "next"])
        self.assertEqual(sd.fields[1].type.base, "u32")
        self.assertEqual(sd.fields[2].type.ptr, 1)

    def test_multiple_declarators(self):
        unit = parse("struct A { int a, b, *c; };\n")
        self.assertEqual([f.name for f in unit.structs["A"].fields], ["a", "b", "c"])
        self.assertEqual(unit.structs["A"].fields[2].type.ptr, 1)

    def test_array_dimensions(self):
        unit = parse("struct A { char buf[16]; int grid[2][3]; };\n")
        self.assertEqual(unit.structs["A"].fields[0].type.dims, (16,))
        self.assertEqual(unit.structs["A"].fields[1].type.dims, (2, 3))

    def test_comments_are_ignored(self):
        unit = parse("struct A { int a; /* c */ int b; // line\n };\n")
        self.assertEqual(len(unit.structs["A"].fields), 2)

    def test_forward_declaration_is_not_a_definition(self):
        unit = parse("struct Fwd;\nstruct A { int a; };\n")
        self.assertNotIn("Fwd", unit.structs)
        self.assertIn("A", unit.structs)

    def test_anonymous_nested_struct(self):
        unit = parse("struct A { struct { int x; int y; } p; };\n")
        self.assertEqual(len(unit.structs["A"].fields), 1)
        inner = unit.structs[unit.structs["A"].fields[0].type.ref]
        self.assertTrue(inner.anonymous)
        self.assertEqual(len(inner.fields), 2)

    def test_non_struct_toplevel_is_skipped(self):
        unit = parse("int f(int);\nstruct A { int a; };\n")
        self.assertIn("A", unit.structs)
        self.assertEqual(unit.skipped_declarations, 1)

    def test_bitfield_rejected(self):
        with self.assertRaises(UnsupportedFeature):
            parse("struct A { unsigned flags : 3; };\n")

    def test_flexible_array_rejected(self):
        with self.assertRaises(UnsupportedFeature):
            parse("struct A { int n; char data[]; };\n")

    def test_function_pointer_rejected(self):
        with self.assertRaises(UnsupportedFeature):
            parse("struct A { void (*cb)(int); };\n")

    def test_non_literal_array_bound_rejected(self):
        with self.assertRaises(UnsupportedFeature):
            parse("struct A { char buf[SIZE]; };\n")

    def test_unterminated_body(self):
        with self.assertRaises(ParseError):
            parse("struct A { int a;\n")


class LayoutTests(unittest.TestCase):
    def test_char_int_char(self):
        _, l = lay("struct A { char a; int b; char c; };", "A")
        self.assertEqual((l.size, l.align), (12, 4))
        self.assertEqual(l.offset_map, {"a": 0, "b": 4, "c": 8})
        self.assertEqual(l.holes, [(1, 3), (9, 3)])
        self.assertEqual(l.padding_bytes, 6)

    def test_char_long_tail_padding(self):
        _, l = lay("struct A { char a; long b; };", "A")
        self.assertEqual((l.size, l.align), (16, 8))
        self.assertEqual(l.holes, [(1, 7)])

    def test_packed_by_hand_scalars(self):
        _, l = lay("struct A { short a; char b; int c; };", "A")
        self.assertEqual(l.offset_map, {"a": 0, "b": 2, "c": 4})
        self.assertEqual(l.size, 8)
        # byte 3 sits between char b and int c
        self.assertEqual(l.holes, [(3, 1)])
        self.assertEqual(l.padding_bytes, 1)

    def test_double_then_char(self):
        _, l = lay("struct A { double d; char c; };", "A")
        self.assertEqual(l.size, 16)
        self.assertEqual(l.holes, [(9, 7)])

    def test_array_of_int(self):
        _, l = lay("struct A { int a[3]; char b; };", "A")
        self.assertEqual(l.offset_map, {"a": 0, "b": 12})
        self.assertEqual(l.size, 16)

    def test_nested_struct_includes_inner_padding(self):
        _, l = lay(
            "struct I { char a; int b; };\nstruct A { char c; struct I in; };",
            "A",
        )
        self.assertEqual(l.offset_map, {"c": 0, "in": 4})
        self.assertEqual(l.size, 12)
        # c's tail padding (1..4) and the inner struct's padding (5..8)
        self.assertEqual(l.holes, [(1, 3), (5, 3)])

    def test_union_size_and_alignment(self):
        _, l = lay("union U { int i; double d; };", "U")
        self.assertEqual((l.size, l.align), (8, 8))
        self.assertIsNone(l.holes)
        self.assertIn("union", l.holes_reason)

    def test_ilp32_long_is_four_bytes(self):
        _, l = lay("struct A { long a; char b; };", "A", ILP32)
        self.assertEqual(l.size, 8)
        self.assertEqual(l.offset_map, {"a": 0, "b": 4})

    def test_llp64_pointer_is_eight_long_is_four(self):
        _, l = lay("struct A { void *p; long l; };", "A", LLP64)
        self.assertEqual(l.offset_map, {"p": 0, "l": 8})
        self.assertEqual(l.size, 16)

    def test_lp64_pointer_and_long(self):
        _, l = lay("struct A { void *p; long l; };", "A", LP64)
        self.assertEqual(l.size, 16)
        self.assertEqual(l.offset_map, {"p": 0, "l": 8})

    def test_unknown_type_is_an_error(self):
        from silentabi import LayoutError

        with self.assertRaises(LayoutError):
            lay("struct A { mystery_t m; };", "A")

    def test_recursive_by_value_rejected(self):
        from silentabi import LayoutError

        with self.assertRaises(LayoutError):
            lay("struct A { struct A inner; };", "A")


class DiffTests(unittest.TestCase):
    def test_identical(self):
        src = "struct A { int a; char b; };"
        d = diff_structs(parse(src), parse(src), LP64, "A")
        self.assertEqual(d.verdict, "IDENTICAL")
        self.assertFalse(d.abi_break)

    def test_tail_field_in_padding_is_not_abi_break(self):
        old = "struct A { int a; char b; };"
        new = "struct A { int a; char b; char c; };"
        d = diff_structs(parse(old), parse(new), LP64, "A")
        self.assertEqual(d.old.size, d.new.size)
        self.assertFalse(d.abi_break)
        self.assertTrue(d.source_break)
        self.assertTrue(d.wire_change)
        self.assertEqual(d.verdict, "SOURCE_ONLY")

    def test_widen_moves_following_offsets(self):
        old = "struct A { char a; int b; };"
        new = "struct A { char a; long b; };"
        d = diff_structs(parse(old), parse(new), LP64, "A")
        self.assertTrue(d.abi_break)
        self.assertEqual(d.verdict, "ABI_BREAK")
        self.assertIn("offset_moved", [c.kind for c in d.changes])

    def test_append_wider_field_changes_size(self):
        old = "struct A { int a; };"
        new = "struct A { int a; long b; };"
        d = diff_structs(parse(old), parse(new), LP64, "A")
        self.assertTrue(d.abi_break)
        self.assertEqual(d.old.size, 4)
        self.assertEqual(d.new.size, 16)

    def test_removed_field_is_source_only_when_size_unchanged(self):
        old = "struct A { int a; char b; char c; char d; char e; };"
        new = "struct A { int a; char b; char c; char d; };"
        d = diff_structs(parse(old), parse(new), LP64, "A")
        self.assertFalse(d.abi_break)
        self.assertTrue(d.source_break)
        self.assertTrue(d.wire_change)
        self.assertEqual(d.verdict, "SOURCE_ONLY")

    def test_field_reorder_moves_offsets(self):
        old = "struct A { char a; int b; };"
        new = "struct A { int b; char a; };"
        d = diff_structs(parse(old), parse(new), LP64, "A")
        self.assertTrue(d.abi_break)
        self.assertEqual(d.old.size, d.new.size)

    def test_resize_array_breaks_size(self):
        old = "struct A { char buf[8]; };"
        new = "struct A { char buf[16]; };"
        d = diff_structs(parse(old), parse(new), LP64, "A")
        self.assertTrue(d.abi_break)

    def test_json_shape(self):
        d = diff_structs(
            parse("struct A { int a; };"), parse("struct A { int a; long b; };"),
            LP64, "A",
        )
        payload = json.loads(json.dumps(d.to_dict()))
        self.assertEqual(payload["verdict"], "ABI_BREAK")
        self.assertEqual(payload["old"]["size"], 4)
        self.assertIsNone(payload["compiles"])
        self.assertFalse(payload["silent"])


class EmitTests(unittest.TestCase):
    def test_emit_parse_roundtrip_preserves_layout(self):
        src = (
            "struct I { char a; int b; };\n"
            "struct A { char c; struct I in; unsigned short arr[3]; };\n"
        )
        unit = parse(src)
        emitted = emit_unit(unit)
        unit2 = parse(emitted)
        a = layout_all(unit, LP64)
        b = layout_all(unit2, LP64)
        self.assertEqual(a["A"].size, b["A"].size)
        self.assertEqual(a["A"].offset_map, b["A"].offset_map)
        self.assertEqual(a["A"].holes, b["A"].holes)

    def test_arrays_are_emitted_as_declarators(self):
        unit = parse("struct A { int grid[2][3]; };")
        self.assertIn("int grid[2][3];", emit_unit(unit))


@unittest.skipUnless(HAS_GCC, "gcc not available")
class CompilerOracleTests(unittest.TestCase):
    def test_model_matches_gcc_on_generated_structs(self):
        from silentabi.layout import layout_all as all_layouts

        src = (
            "struct P { char a; double d; int b; };\n"
            "struct Q { short s; struct P p[2]; char tail[3]; };\n"
            "union R { char c; long l; double d; };\n"
        )
        unit = parse(src)
        model = all_layouts(unit, LP64)
        lines = [
            "#include <stdio.h>",
            "#include <stddef.h>",
            emit_unit(unit),
            "int main(void) {",
        ]
        for name in unit.order:
            sd = unit.structs[name]
            lines.append(
                f'printf("{name} %zu %zu %zu\\n", sizeof({sd.kind} {name}), '
                f'_Alignof({sd.kind} {name}), offsetof({sd.kind} {name}, '
                f"{sd.fields[0].name}));"
            )
        lines.append("return 0; }")
        with tempfile.TemporaryDirectory() as tmp:
            c = os.path.join(tmp, "p.c")
            exe = os.path.join(tmp, "p")
            with open(c, "w") as fh:
                fh.write("\n".join(lines))
            subprocess.run(
                ["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", c, "-o", exe],
                check=True, capture_output=True,
            )
            out = subprocess.run([exe], capture_output=True, text=True, check=True).stdout
        for line in out.strip().splitlines():
            name, size, align, first_offset = line.split()
            lay = model[name]
            self.assertEqual(lay.size, int(size), name)
            self.assertEqual(lay.align, int(align), name)
            self.assertEqual(lay.fields[0].offset, int(first_offset), name)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.path.join(self.tmp.name, "old.h")
        self.new = os.path.join(self.tmp.name, "new.h")
        with open(self.old, "w") as fh:
            fh.write("struct A { char a; int b; };\n")
        with open(self.new, "w") as fh:
            fh.write("struct A { char a; long b; };\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_layout_json(self):
        code = cli_main(["layout", self.old, "--struct", "A", "--json"])
        self.assertEqual(code, 0)

    def test_show_lists_struct(self):
        self.assertEqual(cli_main(["show", self.old, "--abi", "ilp32"]), 0)

    def test_diff_exit_code_abi_break(self):
        code = cli_main(["diff", self.old, self.new, "--struct", "A", "--cc", "none", "--json"])
        self.assertEqual(code, 2)

    def test_diff_exit_code_identical(self):
        code = cli_main(["diff", self.old, self.old, "--struct", "A", "--cc", "none"])
        self.assertEqual(code, 0)

    def test_missing_struct_returns_error_code(self):
        code = cli_main(["layout", self.old, "--struct", "Nope"])
        self.assertEqual(code, 3)

    def test_audit_json(self):
        code = cli_main(["audit", self.old, self.new, "--cc", "none", "--json"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
