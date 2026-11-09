"""Guardrails: the hard safety boundary around autonomous agents.

Agents never touch the filesystem or run processes directly. They go through
`Workspace` (sandboxed, write-scoped, scanned) and `run_unittests` (timeout,
scrubbed environment). Violations raise GuardrailViolation, which is never
retried.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

from .core import GuardrailViolation

ALLOWED_EXTENSIONS = {".py", ".json", ".sql", ".md", ".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".sh", ".diff", ""}
ALLOWED_BARE_NAMES = {"Dockerfile", ".dockerignore", ".gitignore", "Makefile"}
MAX_FILE_BYTES = 200_000
MAX_FILES = 200

# Conservative secret patterns: generated code must never embed credentials.
SECRET_PATTERNS = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("generic_secret", re.compile(r"""(?i)(?:password|secret|api[_-]?key|token)\s*=\s*["'][^"'\s]{8,}["']""")),
]


def scan_for_secrets(text: str) -> List[str]:
    return [name for name, pat in SECRET_PATTERNS if pat.search(text)]


def check_test_amendment(old: str, new: str, path: str) -> List[str]:
    """An amended test may correct an assertion; it may not make the suite weaker. Counted, so it is not a judgement call.
    Returns the problems (empty = acceptable). Used by the agent before it writes AND by the validator after."""
    import ast
    try:
        ast.parse(new)
    except (SyntaxError, ValueError) as exc:
        return [f"{path}: amended test has SyntaxError line {getattr(exc, 'lineno', '?')}: {getattr(exc, 'msg', exc)}"]
    problems: List[str] = []
    tests = lambda s: set(re.findall(r"def\s+(test_\w+)\s*\(", s))
    asserts = lambda s: len(re.findall(r"\bassert\w*\b", s))
    disabled = lambda s: bool(re.search(r"@(?:unittest\.)?(?:skip|expectedFailure)|\bskipTest\b", s))
    if not tests(old) <= tests(new):
        problems.append(f"{path}: amendment removes test(s) {sorted(tests(old) - tests(new))}; an amendment may not delete a test")
    if asserts(new) < asserts(old):
        problems.append(f"{path}: amendment lowers the number of assertions {asserts(old)} -> {asserts(new)}; it may not weaken a test")
    if disabled(new) and not disabled(old):
        problems.append(f"{path}: amendment disables a test (skip / expectedFailure)")
    return problems


RUNTIME_DATA_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm")


def is_runtime_data(p: Path) -> bool:
    """A database file a RUNNING service left behind (binary, not source). It is never part of a codebase an agent reads or releases."""
    return p.name.lower().endswith(RUNTIME_DATA_SUFFIXES)


def robust_rmtree(path: Path, attempts: int = 6) -> None:
    """rmtree that survives transient locks (Windows indexers, OneDrive sync, AV scanners).
    Raises instead of silently leaving stale files behind: a half-deleted workspace would
    leak old artifacts into a new run."""
    import stat
    import time as _t

    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    for i in range(attempts):
        if not path.exists():
            return
        shutil.rmtree(path, onerror=onerror)
        if not path.exists():
            return
        _t.sleep(0.3 * (i + 1))
    if path.exists():
        raise OSError(f"could not remove {path} (locked by another process?)")


@dataclass
class ManifestEntry:
    path: str
    kind: str
    bytes: int
    sha256: str
    producer: str


class Workspace:
    """Sandboxed directory that agents write into.

    Enforces: path containment, extension allow-list, size/file-count caps,
    secret scanning, and per-task write scopes (a task may only write paths
    matching the globs it declared in the plan).
    """

    def __init__(self, root: Path, baseline_root: Optional[Path] = None):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Pristine copy of an imported repository. Brownfield agents derive their output from the
        # baseline, never from their own previous output, so a repair re-run is idempotent.
        self.baseline_root = baseline_root.resolve() if baseline_root else None
        self._lock = threading.Lock()
        self.manifest: Dict[str, ManifestEntry] = {}

    def read_baseline(self, rel: str) -> str:
        if self.baseline_root is None:
            return self.read(rel)
        target = (self.baseline_root / rel).resolve()
        if self.baseline_root != target and self.baseline_root not in target.parents:
            raise GuardrailViolation(f"path escapes baseline: {rel!r}")
        if not target.exists():
            raise FileNotFoundError(rel)
        return target.read_text(encoding="utf-8")

    # -- path safety -----------------------------------------------------
    def _resolve(self, rel: str) -> Path:
        if not rel or os.path.isabs(rel) or re.match(r"^[A-Za-z]:", rel):
            raise GuardrailViolation(f"absolute or empty path rejected: {rel!r}")
        target = (self.root / rel).resolve()
        if self.root != target and self.root not in target.parents:
            raise GuardrailViolation(f"path escapes workspace: {rel!r}")
        return target

    @staticmethod
    def _in_scope(rel: str, scopes: Optional[List[str]]) -> bool:
        if scopes is None:
            return True
        posix = PurePosixPath(rel.replace("\\", "/")).as_posix()
        return any(fnmatch.fnmatch(posix, pat) for pat in scopes)

    # -- API ---------------------------------------------------------------
    def write(self, rel: str, content: str, *, kind: str, producer: str,
              scopes: Optional[List[str]] = None) -> None:
        target = self._resolve(rel)
        name = target.name
        if target.suffix not in ALLOWED_EXTENSIONS and name not in ALLOWED_BARE_NAMES:
            raise GuardrailViolation(f"extension not allowed: {rel!r}")
        if not self._in_scope(rel, scopes):
            raise GuardrailViolation(f"task '{producer}' wrote outside its declared scope: {rel!r} (scope={scopes})")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise GuardrailViolation(f"file too large ({len(data)} bytes): {rel!r}")
        leaks = scan_for_secrets(content)
        if leaks:
            raise GuardrailViolation(f"secret-like content ({', '.join(leaks)}) in {rel!r}")
        with self._lock:
            if rel not in self.manifest and len(self.manifest) >= MAX_FILES:
                raise GuardrailViolation("workspace file-count cap exceeded")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            self.manifest[rel.replace("\\", "/")] = ManifestEntry(
                rel.replace("\\", "/"), kind, len(data), hashlib.sha256(data).hexdigest(), producer)

    def write_test_update(self, rel: str, content: str, *, producer: str) -> None:
        """Replace an EXISTING baseline test file. The only way an agent may touch a test it did not write.

        Narrower than `write`: the path must be a test module that exists in the pristine baseline (no new files, no
        paths outside tests/). Whether the update is justified is for the validator and the human gate to decide."""
        if self.baseline_root is None or not re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", rel) or rel.startswith("tests/test_change_"):
            raise GuardrailViolation(f"not an existing baseline test module: {rel!r}")
        if not (self.baseline_root / rel).is_file():
            raise GuardrailViolation(f"{rel!r} does not exist in the baseline; new tests go in tests/test_change_*.py")
        self.write(rel, content, kind="test_update", producer=producer, scopes=[rel])

    def revert_baseline_tests(self) -> List[str]:
        """Put every amended baseline test back. A repair re-run proposes its amendments from scratch, never on top of the last."""
        reverted = self.modified_baseline_tests()
        for rel in reverted:
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.baseline_root / rel, dest)
            self.refresh_manifest_entry(rel, "baseline", "baseline")
        return reverted

    def modified_baseline_tests(self) -> List[str]:
        """Baseline test files whose content now differs from the pristine baseline. Computed from the disk, not from what
        any agent reports, so it cannot be bypassed by writing around the API."""
        if self.baseline_root is None or not (self.baseline_root / "tests").is_dir():
            return []
        changed = []
        for p in sorted((self.baseline_root / "tests").rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts or p.suffix == ".pyc":
                continue
            rel = p.relative_to(self.baseline_root).as_posix()
            now = self.root / rel
            if not now.is_file() or now.read_bytes() != p.read_bytes():
                changed.append(rel)
        return changed

    def read(self, rel: str) -> str:
        target = self._resolve(rel)
        if not target.exists():
            raise FileNotFoundError(rel)
        return target.read_text(encoding="utf-8")

    def exists(self, rel: str) -> bool:
        return self._resolve(rel).exists()

    def list_files(self, suffix: Optional[str] = None) -> List[str]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc") and not is_runtime_data(p):
                rel = p.relative_to(self.root).as_posix()
                if suffix is None or rel.endswith(suffix):
                    out.append(rel)
        return out

    def import_tree(self, source: Path, *, producer: str = "baseline") -> int:
        """Copy an existing repository into the workspace (brownfield). The
        original is never modified - all edits happen on this copy."""
        count = 0
        for p in sorted(source.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts or p.suffix == ".pyc" or is_runtime_data(p):
                continue
            rel = p.relative_to(source).as_posix()
            dest = self._resolve(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dest)
            if self.baseline_root:
                snap = self.baseline_root / rel
                snap.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, snap)
            data = dest.read_bytes()
            self.manifest[rel] = ManifestEntry(rel, "baseline", len(data), hashlib.sha256(data).hexdigest(), producer)
            count += 1
        return count

    def refresh_manifest_entry(self, rel: str, kind: str, producer: str) -> None:
        data = self._resolve(rel).read_bytes()
        self.manifest[rel] = ManifestEntry(rel, kind, len(data), hashlib.sha256(data).hexdigest(), producer)

    def promote(self, destination: Path) -> int:
        """Copy the validated workspace to the release directory."""
        robust_rmtree(destination)
        count = 0
        for rel in self.list_files():
            dest = destination / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / rel, dest)
            count += 1
        return count


@dataclass
class TestRun:
    ok: bool
    tests_run: int
    failures: int
    output: str
    seconds: float


def run_unittests(cwd: Path, timeout: int = 120, extra_args: Optional[List[str]] = None) -> TestRun:
    """Run generated tests in a child process: hard timeout, scrubbed env,
    isolated interpreter (-I ignores PYTHON* vars and user site-packages)."""
    import time as _t
    safe_env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC")}
    safe_env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"] + (extra_args or [])
    start = _t.time()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=safe_env, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return TestRun(False, 0, 1, f"TIMEOUT after {timeout}s", _t.time() - start)
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"Ran (\d+) tests?", out)
    ran = int(m.group(1)) if m else 0
    f = re.search(r"FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?", out)
    failures = sum(int(x) for x in f.groups() if x) if f else 0
    return TestRun(proc.returncode == 0 and ran > 0, ran, failures, out, _t.time() - start)
