"""Thin wrapper around a real C compiler, used as an external oracle."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CompileResult:
    ok: bool
    returncode: int
    command: List[str] = field(default_factory=list)
    diagnostics: str = ""


def find_compiler(preferred: str = "gcc") -> Optional[str]:
    if shutil.which(preferred):
        return preferred
    for candidate in ("cc", "clang"):
        if shutil.which(candidate):
            return candidate
    return None


def compile_source(
    source: str,
    cc: Optional[str] = None,
    cflags: Optional[List[str]] = None,
    timeout: int = 30,
) -> CompileResult:
    """Compile ``source`` with -Wall -Wextra -Werror; return diagnostics."""
    compiler = cc or find_compiler()
    if compiler is None:
        raise FileNotFoundError("no C compiler found (tried gcc, cc, clang)")
    flags = list(cflags or ["-std=c11", "-Wall", "-Wextra", "-Werror"])
    with tempfile.TemporaryDirectory(prefix="silentabi-") as tmp:
        src = os.path.join(tmp, "probe.c")
        obj = os.path.join(tmp, "probe.o")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(source)
        cmd = [compiler, *flags, "-c", src, "-o", obj]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    return CompileResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        command=cmd,
        diagnostics=(proc.stderr or "").strip(),
    )
