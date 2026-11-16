# silentabi

A pure-Python model of C struct/union layout that answers one question: **which
struct edits compile with zero diagnostics yet silently move bytes or change
`sizeof`?** It parses a header, computes offsets/padding for three ABI models,
diffs two versions, and optionally hands the mutated declaration to a real
compiler so the "silent" part is verified rather than asserted.

Upstream: none

This is an original library. It is **not** an optimization claim: there is no
speedup table here, and no benchmark was run for marketing purposes.

## What it does

- Parses the layout-carrying subset of C: `struct`, `union`, `typedef`, base
  types, pointers, constant-size arrays (including arrays of structs), nested
  and anonymous structs. Bit-fields, packed attributes, macros, flexible array
  members and function pointers are **rejected** with `UnsupportedFeature`
  instead of being silently mis-modelled.
- Computes `sizeof`, `_Alignof`, field offsets and **unwritten padding bytes**
  for `lp64` (x86-64 SysV), `ilp32` (i386 SysV) and `llp64` (MSVC).
- Diffs two declarations and classifies the edit:
  `IDENTICAL` / `WIRE_ONLY` / `SOURCE_ONLY` / `ABI_BREAK`, with a `silent` flag
  for "compiles cleanly *and* is an ABI break".
- Exposes the same engine over a CLI and a JSON certificate.

## Evidence (all produced by the scripts in `evidence/`, all run on this machine)

| Check | Oracle | Result |
| --- | --- | --- |
| Layout model vs real compiler | gcc 13.3.0, `-std=c11 -Wall -Wextra -Werror`, 300 generated structs/unions, 1,731 `offsetof` probes | **300/300** types match on `sizeof`, `_Alignof` and every field offset; **0** mismatches |
| Padding model vs real writes | 80 generated structs: `memset(0xAA)`, assign every leaf field, dump bytes | **80/80** structs; the **1,218** predicted padding bytes are exactly the bytes the program never wrote; **0** mismatches |
| Silent ABI breaks | 3 realistic headers (`net.h`, `proto.h`, `db.h`), 13 structs, 127 deterministic mutations, each compiled with `gcc -Wall -Wextra -Werror` | **76** mutations compile with zero diagnostics; **71 of those 76 (93.42%)** change `sizeof` or an existing field offset |

Reproduce each number:

```bash
python3 evidence/gen_layout_oracle.py     # writes evidence/layout_oracle.json
python3 evidence/gen_padding_oracle.py    # writes evidence/padding_oracle.json
python3 evidence/gen_mutation_audit.py    # writes evidence/mutation_audit.json
```

The mutation corpus is synthetic-but-realistic (packet, message and page
headers written for this repo). The mutations are deterministic edits, **not**
replayed history from a real project; the claim is about compiler silence, not
about how often real projects make these edits. Raw per-mutation records are in
`evidence/mutation_audit.json`.

Example witness (`eth_header`, append one `uint8_t`): old `sizeof=14`, new
`sizeof=16`, gcc exits 0 with no warnings, and the model reports
`ABI_BREAK` (`added`, `size_changed`, `padding_changed`).

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
python3 -m unittest discover -s tests -v
```

Python 3.9+ and no runtime dependencies. `gcc` (or `cc`) is optional and only
needed for the `silent` flag and the evidence scripts.

## CLI

```bash
silentabi layout evidence/corpus/net.h --struct eth_header
silentabi show evidence/corpus/db.h --abi ilp32
silentabi diff old.h new.h --struct message_header --json
silentabi audit old.h new.h --cc gcc --json
```

Exit codes for `diff`/`audit`: `0` identical, `1` source-only/wire-only change,
`2` ABI break, `3` parse/model/compiler error.

`diff` without `--cc none` compiles the new header with
`gcc -std=c11 -Wall -Wextra -Werror -c`; `compiles: true` plus
`abi_break: true` yields `silent: true`.

## Verdict semantics

- `ABI_BREAK` — `sizeof`, alignment or an existing field offset changed, so
  binaries built against the old declaration may read the wrong bytes.
- `SOURCE_ONLY` — fields were added/removed/retyped but the byte geometry of
  surviving fields is unchanged (e.g. a new field lands in existing tail
  padding). Recompile is required; already-built binaries still agree.
- `WIRE_ONLY` — only the set of never-written bytes changed.
- `IDENTICAL` — same size, alignment, offsets, types and padding set.

Padding is reported as byte ranges that no leaf field covers. Structs
containing unions report `padding: null` with a reason, because only one union
member is written at a time and the unwritten set depends on the tag.

## Limits (explicit, not TODO)

- No bit-fields, `#pragma pack`, `__attribute__((packed))`, macros or
  flexible array members. These change layout and would need a real
  preprocessor; the parser refuses rather than guesses.
- `ilp32` and `llp64` are model values. This machine has no 32-bit libc and no
  MSVC, so **only `lp64` was checked against a compiler**; the other two tables
  encode the published i386 SysV / MSVC rules and their tests are hand-computed.
- Unions cannot have their padding certified.
- Only CPython 3.12.3 is available here; the package declares 3.9+ but 3.9–3.11
  were not executed.
- A clean compile does not prove linkage compatibility; `silent` means "no
  diagnostic from this compiler for this declaration", nothing more.

## Tests

`python3 -m unittest discover -s tests` → **41 tests, OK** (parser errors,
layout on hand-computed cases, diff classes, emit→parse round-trip, CLI exit
codes, and a live gcc cross-check that skips when gcc is absent).

## 本轮创新

First executable bridge between "the compiler accepted it" and "the bytes
moved": the same header is compiled by gcc (silence is observed, not assumed)
and modelled independently (byte geometry is computed, not guessed), and the
two are cross-checked against each other — 1,731 `offsetof` probes and 1,218
observed padding bytes with zero disagreement, plus a measured 71/76
silent-ABI-break rate on a fixed mutation corpus. Prior lab work here measured
speed; this measures the ABI delta that no compiler diagnostic reports.

## License

MIT. No upstream code was copied; the ABI tables encode published calling
convention rules.
