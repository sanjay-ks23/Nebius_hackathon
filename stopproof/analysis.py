"""Repository snapshot + deterministic candidate scanners.

The scanners are cheap, reproducible and conservative. Nemotron interprets
their output (and the code) but every claim it makes is checked back against
this snapshot before it is used.
"""

from __future__ import annotations

import ast
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 64_000
MAX_FILES = 200
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".stopproof"}
# StopProof's own instrumentation shipped inside the demo repos (the live-demo
# self-check). It is not agent code and it calls request_stop()/the worker
# itself, which would poison the scanner's candidate sets — skip it entirely.
SKIP_FILES = {"selfcheck.py"}
SECRET_NAME = re.compile(r"(^\.env|\.pem$|\.key$|^id_rsa|credentials|secrets?\.)", re.IGNORECASE)
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".cfg", ".ini", ".json", ".yaml", ".yml"}

HANDOFF_CALLS = {"enqueue", "put", "put_nowait", "submit", "delay", "apply_async", "send_task", "schedule", "publish", "create_task"}
STOP_NAME = re.compile(r"(stop|cancel|abort|kill|shutdown|interrupt|revoke)", re.IGNORECASE)
WORKER_NAME = re.compile(r"(worker|consume|dequeue|drain|handle|process_job|run_until)", re.IGNORECASE)
SINK_CALLS = {"send_email", "send_mail", "sendmail", "send", "post"}


@dataclass(frozen=True)
class RepoSnapshot:
    root: str
    files: dict[str, str]

    @property
    def digest(self) -> str:
        h = hashlib.sha256()
        for path in sorted(self.files):
            h.update(path.encode() + b"\0" + self.files[path].encode() + b"\0")
        return h.hexdigest()


def load_snapshot(root: Path) -> RepoSnapshot:
    """Reads text files only; skips symlinks, secrets, vendored dirs and oversized files."""
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"repository not found: {root}")
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or path.is_symlink() or not path.is_file():
            continue
        if path.name in SKIP_FILES or SECRET_NAME.search(path.name) or path.suffix not in TEXT_SUFFIXES or path.stat().st_size > MAX_FILE_BYTES:
            continue
        files[rel.as_posix()] = path.read_text(encoding="utf-8", errors="replace")
        if len(files) > MAX_FILES:
            raise ValueError(f"repository has more than {MAX_FILES} text files; point StopProof at the agent package")
    if not any(p.endswith(".py") for p in files):
        raise ValueError("no Python sources found; StopProof's MVP analyzes Python agent repositories")
    return RepoSnapshot(str(root), files)


def _parse(files: dict[str, str]) -> tuple[dict[str, ast.Module], list[str]]:
    trees, broken = {}, []
    for path, text in files.items():
        if path.endswith(".py"):
            try:
                trees[path] = ast.parse(text)
            except SyntaxError:
                broken.append(path)
    return trees, broken


def _enclosing(tree: ast.Module) -> dict[ast.AST, str]:
    """Maps each node to the qualified name of its enclosing function or class."""
    owner: dict[ast.AST, str] = {}

    def walk(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                child_scope = f"{scope}.{child.name}" if scope else child.name
            owner[child] = child_scope
            walk(child, child_scope)

    walk(tree, "")
    return owner


def _call_name(node: ast.Call) -> str | None:
    fn = node.func
    return fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None


def _line(files: dict[str, str], path: str, lineno: int) -> str:
    return files[path].splitlines()[lineno - 1].strip()[:160]


def scan(snapshot: RepoSnapshot) -> dict[str, Any]:
    files = snapshot.files
    trees, broken = _parse(files)
    if not trees:
        raise ValueError(f"repository cannot be parsed: no valid Python file ({broken})")
    out: dict[str, list] = {"handoffs": [], "stop_controls": [], "stop_entrypoints": [], "workers": [], "sinks": [], "entrypoints": []}
    stdlib = set(sys.stdlib_module_names)
    local = {Path(p).stem for p in trees}
    third_party: set[str] = set()
    imports_runtime: list[str] = []
    called = {name for tree in trees.values() for node in ast.walk(tree) if isinstance(node, ast.Call) and (name := _call_name(node))}

    for path, tree in trees.items():
        scope_of = _enclosing(tree)
        for node in ast.walk(tree):
            here = {"file": path, "line": getattr(node, "lineno", 0), "symbol": scope_of.get(node, "")}
            if isinstance(node, ast.Call) and _call_name(node) in HANDOFF_CALLS:
                out["handoffs"].append({**here, "call": _call_name(node), "code": _line(files, path, node.lineno)})
            if isinstance(node, ast.Call) and _call_name(node) in SINK_CALLS:
                out["sinks"].append({**here, "call": _call_name(node), "code": _line(files, path, node.lineno)})
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in HANDOFF_CALLS:
                out["handoffs"].append({**here, "call": node.name, "code": _line(files, path, node.lineno)})
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{scope_of.get(node, '')}".strip(".")
                target = out["stop_controls"] if STOP_NAME.search(node.name) else out["workers"] if WORKER_NAME.search(node.name) else None
                if target is not None:
                    entry = {"file": path, "line": node.lineno, "symbol": qual, "code": _line(files, path, node.lineno)}
                    target.append(entry)
                    # Outermost STOP entry: a stop-like function nothing in the repo calls (a user/UI action triggers it).
                    if target is out["stop_controls"] and not isinstance(node, ast.ClassDef) and node.name not in called and not node.name.startswith("_"):
                        out["stop_entrypoints"].append(entry)
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and STOP_NAME.search(node.attr):
                out["stop_controls"].append({**here, "code": _line(files, path, node.lineno)})
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for name in names:
                    top = name.split(".")[0]
                    if top == "stopproof":
                        imports_runtime.append(path)
                    elif top and top not in stdlib and top not in local:
                        third_party.add(top)
            if isinstance(node, ast.If) and ast.unparse(node.test).replace("'", '"') == '__name__ == "__main__"':
                out["entrypoints"].append({"file": path, "line": node.lineno})

    requirements = sorted(
        line.strip() for name in ("requirements.txt",) for line in files.get(name, "").splitlines() if line.strip() and not line.startswith("#")
    )
    return {
        **out,
        "protected_by_runtime_import": sorted(set(imports_runtime)),
        "dependencies": {"third_party_imports": sorted(third_party), "requirements": requirements},
        "unparsed_files": broken,
    }


def symbol_line(snapshot: RepoSnapshot, path: str, symbol: str) -> int | None:
    """1-based line where `symbol` (last dotted part) is defined or used, or None if absent."""
    text = snapshot.files.get(path)
    if text is None or not symbol:
        return None
    leaf = symbol.split(".")[-1]
    for i, line in enumerate(text.splitlines(), 1):
        if re.search(rf"\b{re.escape(leaf)}\b", line):
            return i
    return None


def module_index(snapshot: RepoSnapshot) -> dict[str, list[str]]:
    """Importable top-level names per module with constructor/call signatures, e.g. {'worker': ['class Worker(queue)']}."""
    index: dict[str, list[str]] = {}
    for path, text in snapshot.files.items():
        if not path.endswith(".py") or "/" in path:
            continue
        entries = []
        for node in ast.parse(text).body:
            if isinstance(node, ast.ClassDef):
                init = next((n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
                args = [a.arg for a in init.args.args[1:]] if init else []
                methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]
                entries.append(f"class {node.name}({', '.join(args)})  methods: {', '.join(methods)}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                entries.append(f"def {node.name}({', '.join(a.arg for a in node.args.args)})")
        if entries:
            index[Path(path).stem] = entries
    return index
