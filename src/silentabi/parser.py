"""A deliberately small C declaration parser.

It understands the subset that carries layout information: struct/union
definitions, typedefs, base types, pointers and constant-size arrays.  Anything
that can change layout in a way this parser cannot model (bit-fields, packing
attributes, macros, flexible array members, function pointers) raises
``UnsupportedFeature`` instead of being silently mis-modelled.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .abi import Field, ParseError, StructDef, TypeRef, Unit, UnsupportedFeature

_TOKEN_RE = re.compile(
    r"""
      (?P<comment>/\*.*?\*/|//[^\n]*)
    | (?P<ws>[ \t\r\n]+)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<num>[0-9]+)
    | (?P<punct>[{}()\[\];,:*])
    """,
    re.S | re.X,
)

_BASE_WORDS = {
    "void", "char", "short", "int", "long", "float", "double",
    "signed", "unsigned", "_Bool", "bool", "const", "volatile",
    "restrict", "__restrict", "_Complex",
}
_QUALIFIERS = {
    "const", "volatile", "restrict", "register", "static", "extern",
    "inline", "auto", "__restrict", "_Nonnull", "_Nullable",
}
_IGNORED_TOPLEVEL = {"enum"}


class Token:
    __slots__ = ("kind", "value", "line")

    def __init__(self, kind: str, value: str, line: int) -> None:
        self.kind = kind
        self.value = value
        self.line = line

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.kind!r}, {self.value!r}, line={self.line})"


def tokenize(source: str) -> List[Token]:
    tokens: List[Token] = []
    pos = 0
    line = 1
    while pos < len(source):
        m = _TOKEN_RE.match(source, pos)
        if not m:
            snippet = source[pos:pos + 20].replace("\n", "\\n")
            raise ParseError(f"unexpected character at line {line}: {snippet!r}")
        text = m.group(0)
        kind = m.lastgroup or "punct"
        if kind == "comment":
            line += text.count("\n")
        elif kind == "ws":
            line += text.count("\n")
        elif kind == "punct":
            tokens.append(Token("punct", text, line))
        else:
            tokens.append(Token(kind, text, line))
        pos = m.end()
    tokens.append(Token("eof", "", line))
    return tokens


def _canonical(words: List[str], line: int) -> str:
    words = [w for w in words if w not in _QUALIFIERS]
    if not words:
        raise ParseError(f"line {line}: empty type specifier")
    if "double" in words:
        return "long_double" if "long" in words else "double"
    if "float" in words:
        return "float"
    if "char" in words:
        return "char"
    if "short" in words:
        return "short"
    if words.count("long") >= 2:
        return "long_long"
    if "long" in words:
        return "long"
    if "int" in words or "unsigned" in words or "signed" in words:
        return "int"
    if words[0] in ("_Bool", "bool"):
        return "bool"
    if len(words) == 1:
        # A typedef name, or a fixed-width type such as uint32_t.
        return words[0]
    raise UnsupportedFeature(
        f"line {line}: cannot model type specifier {' '.join(words)!r}"
    )


class Parser:
    def __init__(self, source: str, filename: str = "<source>") -> None:
        self.source = source
        self.filename = filename
        self.tokens = tokenize(source)
        self.pos = 0
        self.unit = Unit()
        self._anon = 0

    # -- token helpers -------------------------------------------------
    def peek(self, offset: int = 0) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def next(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.kind != "eof":
            self.pos += 1
        return tok

    def at(self, value: str) -> bool:
        tok = self.peek()
        return tok.kind in ("punct", "ident") and tok.value == value

    def accept(self, value: str) -> bool:
        if self.at(value):
            self.next()
            return True
        return False

    def expect(self, value: str) -> Token:
        tok = self.peek()
        if tok.value != value:
            raise ParseError(
                f"{self.filename}:{tok.line}: expected {value!r}, got {tok.value!r}"
            )
        return self.next()

    def expect_ident(self) -> str:
        tok = self.peek()
        if tok.kind != "ident":
            raise ParseError(
                f"{self.filename}:{tok.line}: expected identifier, got {tok.value!r}"
            )
        self.next()
        return tok.value

    def eof(self) -> bool:
        return self.peek().kind == "eof"

    # -- top level -----------------------------------------------------
    def parse(self) -> Unit:
        while not self.eof():
            tok = self.peek()
            if tok.value == ";":
                self.next()
            elif tok.value == "typedef":
                self.parse_typedef()
            elif tok.value in ("struct", "union"):
                self.parse_struct_decl(top_level=True)
            elif tok.value in _IGNORED_TOPLEVEL:
                self.skip_statement()
                self.unit.skipped_declarations += 1
            else:
                self.skip_statement()
                self.unit.skipped_declarations += 1
        return self.unit

    def skip_statement(self) -> None:
        depth = 0
        while not self.eof():
            tok = self.next()
            if tok.value == "{":
                depth += 1
            elif tok.value == "}":
                depth -= 1
                if depth <= 0:
                    return
            elif tok.value == ";" and depth == 0:
                return

    def _fresh_anon(self) -> str:
        self._anon += 1
        return f"__anon_{self._anon}"

    def parse_struct_decl(self, top_level: bool) -> Optional[StructDef]:
        kind = self.next().value  # struct | union
        line = self.peek().line
        name = None
        if self.peek().kind == "ident" and not self.at("{"):
            name = self.next().value
        if not self.at("{"):
            # forward declaration or a variable of that type: not a definition
            self.skip_statement()
            return None
        self.expect("{")
        anon = name is None
        if name is None:
            name = self._fresh_anon()
        fields = self.parse_members(kind)
        self.expect("}")
        sd = StructDef(name=name, kind=kind, fields=fields, line=line, anonymous=anon)
        self.unit.add(sd)
        if top_level:
            # consume optional declarators: `} name;` / `} *ptr;`
            while not self.at(";") and not self.eof():
                self.next()
            self.expect(";")
        return sd

    def parse_members(self, kind: str) -> List[Field]:
        fields: List[Field] = []
        while not self.at("}"):
            if self.eof():
                raise ParseError("unterminated struct/union body")
            if self.at(";"):
                self.next()
                continue
            if self.at("struct") or self.at("union"):
                inline_kind = self.peek().value
                if self.peek(1).value == "{":
                    line = self.peek().line
                    self.next()
                    self.expect("{")
                    name = self._fresh_anon()
                    sub_fields = self.parse_members(inline_kind)
                    self.expect("}")
                    self.unit.add(
                        StructDef(name, inline_kind, sub_fields, line, anonymous=True)
                    )
                    tref = TypeRef(inline_kind, name)
                    self.parse_declarators_into(fields, tref)
                    continue
                if self.peek(1).kind == "ident" and self.peek(2).value != "{":
                    self.next()
                    ref = self.next().value
                    tref = TypeRef(inline_kind, ref)
                    self.parse_declarators_into(fields, tref)
                    continue
            base = self.parse_base_spec()
            self.parse_declarators_into(fields, TypeRef(base))
        return fields

    def parse_base_spec(self) -> str:
        line = self.peek().line
        words: List[str] = []
        while self.peek().kind == "ident" and self.peek().value in _BASE_WORDS:
            words.append(self.next().value)
        if not words:
            if self.peek().kind == "ident":
                words.append(self.next().value)
            else:
                raise UnsupportedFeature(
                    f"{self.filename}:{self.peek().line}: unsupported type syntax "
                    f"near {self.peek().value!r}"
                )
        return _canonical(words, line)

    def parse_declarators_into(self, fields: List[Field], base: TypeRef) -> None:
        while True:
            ptr = 0
            while self.accept("*"):
                ptr += 1
            if self.at("("):
                raise UnsupportedFeature(
                    f"{self.filename}:{self.peek().line}: function pointers are not modelled"
                )
            name = self.expect_ident()
            if self.at(":"):
                raise UnsupportedFeature(
                    f"{self.filename}:{self.peek().line}: bit-fields are not modelled"
                )
            dims: List[int] = []
            while self.accept("["):
                if self.at("]"):
                    raise UnsupportedFeature(
                        f"{self.filename}:{self.peek().line}: flexible array member is not modelled"
                    )
                tok = self.peek()
                if tok.kind != "num":
                    raise UnsupportedFeature(
                        f"{self.filename}:{tok.line}: non-literal array bound {tok.value!r}"
                    )
                self.next()
                dims.append(int(tok.value))
                self.expect("]")
            fields.append(
                Field(name=name, type=TypeRef(base.base, base.ref, ptr, tuple(dims)),
                      line=base_line(self))
            )
            if self.accept(","):
                continue
            self.expect(";")
            return

    # -- typedef -------------------------------------------------------
    def parse_typedef(self) -> None:
        self.expect("typedef")
        if self.at("struct") or self.at("union"):
            kind = self.peek().value
            if self.peek(1).value == "{" or (self.peek(1).kind == "ident" and self.peek(2).value == "{"):
                sd = self.parse_struct_decl(top_level=False)
                assert sd is not None
                base = TypeRef(kind, sd.name)
            else:
                self.next()
                ref = self.expect_ident()
                base = TypeRef(kind, ref)
        elif self.at("enum"):
            self.skip_statement()
            self.unit.skipped_declarations += 1
            return
        else:
            base = TypeRef(self.parse_base_spec())
        while True:
            ptr = 0
            while self.accept("*"):
                ptr += 1
            if self.at("("):
                raise UnsupportedFeature(
                    f"{self.filename}:{self.peek().line}: function pointer typedef is not modelled"
                )
            alias = self.expect_ident()
            dims: List[int] = []
            while self.accept("["):
                tok = self.next()
                if tok.kind != "num":
                    raise UnsupportedFeature(
                        f"{self.filename}:{tok.line}: non-literal array bound"
                    )
                dims.append(int(tok.value))
                self.expect("]")
            self.unit.typedefs[alias] = TypeRef(base.base, base.ref, ptr, tuple(dims))
            if self.accept(","):
                continue
            self.expect(";")
            return


def base_line(parser: Parser) -> int:
    return parser.peek().line


def parse(source: str, filename: str = "<source>") -> Unit:
    return Parser(source, filename).parse()


def parse_file(path: str) -> Unit:
    with open(path, "r", encoding="utf-8") as fh:
        return parse(fh.read(), filename=str(path))
