"""Line-oriented diff engine for PyVCS.

The engine compares two snapshots (path -> blob object ID), classifies changes,
recognizes exact-content renames, renders unified text diffs, reports binary
changes, and calculates simple change statistics. It intentionally keeps
repository/object access outside this module through a blob-reader callback.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Callable, Iterable


BlobReader = Callable[[str], bytes]


@dataclass(frozen=True)
class FileChange:
    status: str  # added, deleted, modified, renamed
    path: str   # new path for renames
    old_oid: str | None = None
    new_oid: str | None = None
    old_path: str | None = None

    @property
    def display_path(self) -> str:
        if self.status == "renamed" and self.old_path:
            return f"{self.old_path} => {self.path}"
        return self.path


@dataclass(frozen=True)
class FileStats:
    change: FileChange
    insertions: int
    deletions: int
    binary: bool = False


@dataclass(frozen=True)
class DiffResult:
    changes: tuple[FileChange, ...]
    stats: tuple[FileStats, ...]

    @property
    def insertions(self) -> int:
        return sum(item.insertions for item in self.stats)

    @property
    def deletions(self) -> int:
        return sum(item.deletions for item in self.stats)


def _path_matches(path: str, old_path: str | None, filters: Iterable[str] | None) -> bool:
    if not filters:
        return True
    for raw in filters:
        wanted = raw.replace("\\", "/").strip("/")
        if not wanted or wanted == ".":
            return True
        for candidate in (path, old_path):
            if candidate and (candidate == wanted or candidate.startswith(wanted + "/")):
                return True
    return False


def classify_changes(
    old_tree: dict[str, str],
    new_tree: dict[str, str],
    paths: Iterable[str] | None = None,
) -> list[FileChange]:
    """Classify snapshot changes and identify 100%-identical content renames.

    Rename detection is deliberately exact-content based. Similarity-based
    rename detection can be added later without changing the rendering API.
    """
    removed = set(old_tree) - set(new_tree)
    added = set(new_tree) - set(old_tree)
    additions_by_oid: dict[str, list[str]] = {}
    for path in sorted(added):
        additions_by_oid.setdefault(new_tree[path], []).append(path)

    consumed_removed: set[str] = set()
    consumed_added: set[str] = set()
    changes: list[FileChange] = []
    for old_path in sorted(removed):
        candidates = additions_by_oid.get(old_tree[old_path], [])
        new_path = next((p for p in candidates if p not in consumed_added), None)
        if new_path is not None:
            consumed_removed.add(old_path)
            consumed_added.add(new_path)
            changes.append(FileChange("renamed", new_path, old_tree[old_path], new_tree[new_path], old_path))

    for path in sorted(set(old_tree) | set(new_tree)):
        if path in consumed_removed or path in consumed_added:
            continue
        old_oid, new_oid = old_tree.get(path), new_tree.get(path)
        if old_oid == new_oid:
            continue
        if old_oid is None:
            status = "added"
        elif new_oid is None:
            status = "deleted"
        else:
            status = "modified"
        changes.append(FileChange(status, path, old_oid, new_oid))

    changes = [change for change in changes if _path_matches(change.path, change.old_path, paths)]
    return sorted(changes, key=lambda c: (c.path, c.status, c.old_path or ""))


def _as_text(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _line_counts(old_data: bytes, new_data: bytes) -> tuple[int, int, bool]:
    old_text, new_text = _as_text(old_data), _as_text(new_data)
    if old_text is None or new_text is None:
        return 0, 0, True
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    insertions = deletions = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, old_lines, new_lines, autojunk=False
    ).get_opcodes():
        if tag in ("delete", "replace"):
            deletions += i2 - i1
        if tag in ("insert", "replace"):
            insertions += j2 - j1
    return insertions, deletions, False


def build_result(
    old_tree: dict[str, str],
    new_tree: dict[str, str],
    read_blob: BlobReader,
    paths: Iterable[str] | None = None,
) -> DiffResult:
    changes = classify_changes(old_tree, new_tree, paths)
    stats: list[FileStats] = []
    for change in changes:
        old_data = read_blob(change.old_oid) if change.old_oid else b""
        new_data = read_blob(change.new_oid) if change.new_oid else b""
        if change.status == "renamed":
            stats.append(FileStats(change, 0, 0, False))
            continue
        insertions, deletions, binary = _line_counts(old_data, new_data)
        stats.append(FileStats(change, insertions, deletions, binary))
    return DiffResult(tuple(changes), tuple(stats))


def _diff_line_output(old_data: bytes, new_data: bytes, old_label: str, new_label: str, context: int) -> str:
    old_text, new_text = _as_text(old_data), _as_text(new_data)
    if old_text is None or new_text is None:
        return f"--- {old_label}\n+++ {new_label}\nBinary files {old_label} and {new_label} differ\n"

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    # difflib keeps line endings from input. Add the conventional marker where
    # a changed source line did not end in a newline.
    raw = list(difflib.unified_diff(
        old_lines, new_lines, fromfile=old_label, tofile=new_label,
        n=context, lineterm="\n"
    ))
    if not raw:
        # Empty added/deleted files still need file headers even though there
        # are no line hunks to render.
        return f"--- {old_label}\n+++ {new_label}\n"
    output: list[str] = []
    for line in raw:
        if line.endswith("\n"):
            output.append(line)
        else:
            output.append(line + "\n")
            output.append("\\ No newline at end of file\n")
    return "".join(output)


def render_unified(result: DiffResult, read_blob: BlobReader, context: int = 3) -> str:
    if context < 0:
        raise ValueError("context must be zero or greater")
    output: list[str] = []
    for change in result.changes:
        old_path = change.old_path or change.path
        output.append(f"diff --git a/{old_path} b/{change.path}\n")
        if change.status == "renamed":
            output.append("similarity index 100%\n")
            output.append(f"rename from {change.old_path}\n")
            output.append(f"rename to {change.path}\n")
            continue

        old_label = "/dev/null" if change.status == "added" else f"a/{change.path}"
        new_label = "/dev/null" if change.status == "deleted" else f"b/{change.path}"
        if change.status == "added":
            output.append("new file\n")
        elif change.status == "deleted":
            output.append("deleted file\n")
        old_data = read_blob(change.old_oid) if change.old_oid else b""
        new_data = read_blob(change.new_oid) if change.new_oid else b""
        output.append(_diff_line_output(old_data, new_data, old_label, new_label, context))
    return "".join(output) if output else "No differences found.\n"


def render_name_only(result: DiffResult) -> str:
    if not result.changes:
        return ""
    return "".join(change.display_path + "\n" for change in result.changes)


def render_stat(result: DiffResult) -> str:
    if not result.stats:
        return "No differences found.\n"
    rows: list[str] = []
    for item in result.stats:
        total = item.insertions + item.deletions
        graph = "+" * min(item.insertions, 40) + "-" * min(item.deletions, 40)
        if item.binary:
            detail = "Bin"
        elif item.change.status == "renamed":
            detail = "Renamed"
        else:
            detail = f"{total} {graph}".rstrip()
        rows.append(f" {item.change.display_path} | {detail}\n")
    count = len(result.changes)
    files_label = "file changed" if count == 1 else "files changed"
    summary = f" {count} {files_label}"
    if result.insertions:
        summary += f", {result.insertions} insertion" + ("" if result.insertions == 1 else "s") + "(+)"
    if result.deletions:
        summary += f", {result.deletions} deletion" + ("" if result.deletions == 1 else "s") + "(-)"
    return "".join(rows) + summary + "\n"
