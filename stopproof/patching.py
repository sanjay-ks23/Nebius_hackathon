"""Security boundary for model-proposed artifacts: generated tests and repair patches.

Nothing here executes model output. Tests are validated statically and locked by
content hash; patches are parsed and applied as pure text transforms only after
they pass the path and content policy.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import re
from dataclasses import dataclass

LOCKED_TEST_PATH = "locked/race_test.py"
MAX_TEST_BYTES = 6_000
MAX_PATCH_CHANGED_LINES = 120

ALLOWED_TEST_IMPORT_PREFIXES = ("stopproof.runtime", "pathlib", "typing", "__future__")
FORBIDDEN_TEST_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "input", "globals", "locals", "setattr", "delattr",
    "os", "sys", "subprocess", "socket", "shutil", "importlib", "builtins", "ctypes",
}
FORBIDDEN_PATCH_TOKENS = re.compile(
    r"\b(subprocess|socket|ctypes|eval|exec|__import__|importlib|urllib|requests|httpx|shutil|stopproof|provider_send|sys\.modules|os\.(system|popen|environ|remove|unlink))\b"
)


class Rejected(ValueError):
    """A model-proposed artifact violated policy."""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class LockedTest:
    path: str
    source: str
    sha256: str


def lock_test(source: str, repo_modules: set[str], must_reference: frozenset[str] = frozenset()) -> LockedTest:
    """Statically validates a generated race test and records its canonical hash.

    `must_reference` are symbols from the (validated) analysis that a non-vacuous test has to
    exercise: the agent entry, the repository's STOP entry point and the worker.
    """
    if len(source.encode()) > MAX_TEST_BYTES:
        raise Rejected("generated test is too large")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise Rejected(f"generated test does not parse: {exc}") from exc

    has_main_call = has_build = has_scenario = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                if not (name.startswith(ALLOWED_TEST_IMPORT_PREFIXES) or name.split(".")[0] in repo_modules):
                    raise Rejected(f"generated test imports {name!r}, which is neither the repo nor the StopProof runtime")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_TEST_NAMES:
            raise Rejected(f"generated test uses forbidden name {node.id!r}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise Rejected("generated test accesses a dunder attribute")
        if isinstance(node, ast.FunctionDef) and node.name == "build":
            has_build = True
        if isinstance(node, ast.Name) and node.id == "Scenario":
            has_scenario = True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "main":
            has_main_call = True
    if not (has_build and has_scenario and has_main_call):
        raise Rejected("generated test must define build(workdir) returning a Scenario and call main(build)")
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    if missing := sorted(must_reference - used):
        raise Rejected(f"generated test never uses {missing}; it must run the agent, deliver the repository's own STOP entry point and drain the real worker")
    return LockedTest(LOCKED_TEST_PATH, source, sha256(source))


def edits_to_diff(files: dict[str, str], edits: list[dict[str, str]]) -> str:
    """Renders model edits (exact old -> new replacements) as a unified diff."""
    updated = dict(files)
    for edit in edits:
        path, old, new = edit["path"], edit["old"], edit["new"]
        if path not in updated:
            raise Rejected(f"edit targets unknown file {path!r}")
        text = updated[path]
        if old and text.count(old) != 1 and text.count(old.strip("\n")) == 1 and old.strip("\n"):
            old, new = old.strip("\n"), new.strip("\n")  # tolerate stray leading/trailing newlines around an otherwise exact match
        if not old or text.count(old) != 1:
            raise Rejected(_no_unique_match(path, text, old))
        updated[path] = text.replace(old, new)
    chunks = [
        "".join(
            difflib.unified_diff(
                files[p].splitlines(keepends=True), updated[p].splitlines(keepends=True), f"a/{p}", f"b/{p}"
            )
        )
        for p in sorted(updated)
        if updated[p] != files[p]
    ]
    if not chunks:
        raise Rejected("patch changes nothing")
    return "".join(chunks)


def _no_unique_match(path: str, text: str, old: str) -> str:
    """Rejection message that shows the model what the file actually contains."""
    first = next((line for line in old.splitlines() if line.strip()), "")
    close = difflib.get_close_matches(first, text.splitlines(), n=1, cutoff=0.6)
    found = text.count(old) if old else 0
    hint = f"; the closest line in the file is {close[0]!r}" if close else ""
    return f"edit for {path!r}: `old` must match the file exactly once (it matched {found} times). Copy the lines verbatim, including indentation{hint}"


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def apply_diff(diff: str, files: dict[str, str], allowed: set[str], protected: set[str]) -> dict[str, str]:
    """Validates a unified diff against policy and returns the patched file map."""
    out = dict(files)
    lines = diff.splitlines()
    i, changed = 0, 0
    touched: list[str] = []
    while i < len(lines):
        if not lines[i].startswith("--- "):
            if lines[i].strip():
                raise Rejected(f"unexpected line outside a file section: {lines[i][:60]!r}")
            i += 1
            continue
        if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
            raise Rejected("malformed diff header")
        old_path, new_path = lines[i][4:].split("\t")[0], lines[i + 1][4:].split("\t")[0]
        path = new_path.removeprefix("b/")
        if old_path.removeprefix("a/") != path or "/dev/null" in (old_path, new_path):
            raise Rejected("creating, deleting or renaming files is not allowed")
        if path.startswith("/") or ".." in path.split("/") or path not in files:
            raise Rejected(f"patch targets a path outside the repository: {path!r}")
        if path in protected:
            raise Rejected(f"patch touches protected file {path!r}")
        if path not in allowed:
            raise Rejected(f"patch touches {path!r}, which is not on the allowed list")
        touched.append(path)
        i += 2
        src = out[path].splitlines(keepends=True)
        result, cursor = [], 0
        while i < len(lines) and lines[i].startswith("@@"):
            m = _HUNK.match(lines[i])
            if not m:
                raise Rejected("malformed hunk header")
            start, old_count = int(m[1]), int(m[2] or 1)
            start = start - 1 if old_count else start
            if start < cursor:
                raise Rejected("overlapping hunks")
            result += src[cursor:start]
            i += 1
            consumed, pos = 0, start
            while i < len(lines) and (consumed < old_count or lines[i][:1] in ("+", "\\")):
                tag, text = lines[i][:1], lines[i][1:]
                if tag == "\\":
                    i += 1
                    continue
                if tag in (" ", "-"):
                    if pos >= len(src) or src[pos].rstrip("\n") != text:
                        raise Rejected(f"hunk does not match {path} near line {pos + 1}")
                    pos, consumed = pos + 1, consumed + 1
                    if tag == "-":
                        changed += 1
                    else:
                        result.append(src[pos - 1])
                elif tag == "+":
                    if FORBIDDEN_PATCH_TOKENS.search(text):
                        raise Rejected(f"patch adds a forbidden construct: {text.strip()[:80]!r}")
                    result.append(text + "\n")
                    changed += 1
                else:
                    raise Rejected(f"unexpected diff line {lines[i][:40]!r}")
                i += 1
            cursor = pos
        result += src[cursor:]
        out[path] = "".join(result)
        if path.endswith(".py"):
            try:
                ast.parse(out[path])
            except SyntaxError as exc:
                rows = out[path].splitlines()
                bad = rows[exc.lineno - 1].strip() if exc.lineno and exc.lineno <= len(rows) else ""
                raise Rejected(f"patched {path} does not parse: {exc.msg} at line {exc.lineno}: {bad!r}. Check `new` for stray JSON punctuation or unbalanced brackets") from exc
    if not touched:
        raise Rejected("empty patch")
    if changed > MAX_PATCH_CHANGED_LINES:
        raise Rejected(f"patch is too large ({changed} changed lines)")
    return out
