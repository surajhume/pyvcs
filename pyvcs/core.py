from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any

META = ".pyvcs"


class VcsError(Exception):
    """Expected user-facing repository error."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with temp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


class Repository:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.meta = self.root / META
        if not self.meta.is_dir():
            raise VcsError(f"No PyVCS repository found in {self.root}. Run 'pyvcs init' first.")
        self.objects = self.meta / "objects"
        self.refs_heads = self.meta / "refs" / "heads"
        self.refs_tags = self.meta / "refs" / "tags"
        self.head_file = self.meta / "HEAD"
        self.index_file = self.meta / "index.json"
        self.config_file = self.meta / "config.json"
        self.reflog_file = self.meta / "logs" / "HEAD.jsonl"
        self.merge_state_file = self.meta / "MERGE_STATE.json"

    @classmethod
    def init(cls, path: Path) -> "Repository":
        root = path.resolve()
        root.mkdir(parents=True, exist_ok=True)
        meta = root / META
        if meta.exists():
            raise VcsError(f"{meta} already exists.")
        for directory in (
            meta / "objects", meta / "refs" / "heads", meta / "refs" / "tags",
            meta / "logs"
        ):
            directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(meta / "HEAD", b"ref: refs/heads/main\n")
        _atomic_write(meta / "index.json", _json_bytes({}))
        _atomic_write(meta / "config.json", _json_bytes({
            "user.name": os.environ.get("PYVCS_NAME", os.environ.get("USER", os.environ.get("USERNAME", "Unknown User"))),
            "user.email": os.environ.get("PYVCS_EMAIL", "unknown@example.com")
        }))
        _atomic_write(meta / "refs" / "heads" / "main", b"\n")
        _atomic_write(meta / "logs" / "HEAD.jsonl", b"")
        return cls(root)

    def _read_json(self, path: Path, default: Any = None) -> Any:
        if not path.exists():
            return {} if default is None else default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise VcsError(f"Could not read repository metadata {path}: {exc}") from exc

    def _write_json(self, path: Path, value: Any) -> None:
        _atomic_write(path, _json_bytes(value) + b"\n")

    def config(self) -> dict[str, str]:
        return self._read_json(self.config_file, {})

    def set_config(self, key: str, value: str) -> None:
        cfg = self.config()
        cfg[key] = value
        self._write_json(self.config_file, cfg)

    def _head_text(self) -> str:
        return self.head_file.read_text(encoding="utf-8").strip()

    def current_branch(self) -> str | None:
        head = self._head_text()
        if head.startswith("ref: refs/heads/"):
            return head[len("ref: refs/heads/"):]
        return None

    def head_commit(self) -> str | None:
        head = self._head_text()
        if head.startswith("ref: "):
            ref = self.meta / head[5:]
            if not ref.exists():
                return None
            value = ref.read_text(encoding="utf-8").strip()
            return value or None
        return head or None

    def _set_head_commit(self, commit_id: str | None) -> None:
        branch = self.current_branch()
        if branch is None:
            _atomic_write(self.head_file, ((commit_id or "") + "\n").encode())
        else:
            ref = self.refs_heads / branch
            _atomic_write(ref, ((commit_id or "") + "\n").encode())

    def _log(self, action: str, old: str | None, new: str | None, detail: str = "") -> None:
        cfg = self.config()
        row = {
            "time": int(time.time()), "user": cfg.get("user.name", "Unknown User"),
            "email": cfg.get("user.email", "unknown@example.com"),
            "action": action, "old": old, "new": new, "detail": detail
        }
        previous = self.reflog_file.read_bytes() if self.reflog_file.exists() else b""
        _atomic_write(self.reflog_file, previous + _json_bytes(row) + b"\n")

    def _store_object(self, kind: str, payload: bytes) -> str:
        raw = kind.encode() + b"\0" + payload
        oid = hashlib.sha256(raw).hexdigest()
        target = self.objects / oid[:2] / oid[2:]
        if not target.exists():
            _atomic_write(target, raw)
        return oid

    def _read_object(self, oid: str) -> tuple[str, bytes]:
        if len(oid) != 64 or any(c not in "0123456789abcdef" for c in oid):
            raise VcsError(f"Invalid object ID: {oid}")
        path = self.objects / oid[:2] / oid[2:]
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise VcsError(f"Object {oid} is missing or unreadable.") from exc
        kind, sep, payload = raw.partition(b"\0")
        if not sep or hashlib.sha256(raw).hexdigest() != oid:
            raise VcsError(f"Object {oid} failed its integrity check.")
        return kind.decode(), payload

    def _store_json_object(self, kind: str, value: Any) -> str:
        return self._store_object(kind, _json_bytes(value))

    def _read_json_object(self, oid: str, expected_kind: str) -> dict[str, Any]:
        kind, payload = self._read_object(oid)
        if kind != expected_kind:
            raise VcsError(f"Object {oid} is {kind}, expected {expected_kind}.")
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VcsError(f"Invalid {kind} object {oid}.") from exc

    def _validate_relpath(self, value: str) -> str:
        p = PurePosixPath(value.replace("\\", "/"))
        if p.is_absolute() or not p.parts or any(part in ("..", "") for part in p.parts):
            raise VcsError(f"Unsafe repository path: {value}")
        if p.parts[0] == META:
            raise VcsError(f"Cannot operate on internal {META} directory.")
        return p.as_posix()

    def _ignore_patterns(self) -> list[str]:
        path = self.root / ".pyvcsignore"
        if not path.exists():
            return []
        return [
            line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def _is_ignored(self, rel: str) -> bool:
        patterns = self._ignore_patterns()
        parts = rel.split("/")
        return any(
            fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(parts[-1], pat)
            or any(fnmatch.fnmatch("/".join(parts[:i]), pat) for i in range(1, len(parts) + 1))
            for pat in patterns
        )

    def _working_tree(self) -> dict[str, str]:
        tree: dict[str, str] = {}
        for base, dirs, names in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d != META and not d.startswith(".pyvcs-tmp"))
            for name in sorted(names):
                full = Path(base) / name
                rel = full.relative_to(self.root).as_posix()
                if rel == META or rel.startswith(META + "/") or self._is_ignored(rel):
                    continue
                if full.is_symlink():
                    continue  # Symlinks are deliberately excluded in MVP for cross-platform safety.
                try:
                    data = full.read_bytes()
                except OSError:
                    continue
                tree[rel] = self._store_object("blob", data)
        return tree

    def _tree_for_commit(self, commit_id: str | None) -> dict[str, str]:
        if not commit_id:
            return {}
        commit = self._read_json_object(commit_id, "commit")
        return self._read_json_object(commit["tree"], "tree")

    def status(self) -> dict[str, list[str]]:
        head_tree = self._tree_for_commit(self.head_commit())
        index = self._read_json(self.index_file, {})
        working = self._working_tree()
        staged, unstaged, untracked = [], [], []
        for path in sorted(set(head_tree) | set(index)):
            if head_tree.get(path) != index.get(path):
                staged.append(path)
        for path in sorted(set(index) | set(working)):
            if index.get(path) != working.get(path):
                if path not in index and path not in head_tree:
                    untracked.append(path)
                else:
                    unstaged.append(path)
        # A deleted file staged from the index appears as staged; unstaged deletion is listed above.
        return {"staged": staged, "unstaged": unstaged, "untracked": untracked}

    def add(self, paths: list[str]) -> list[str]:
        index = self._read_json(self.index_file, {})
        head_tree = self._tree_for_commit(self.head_commit())
        added: list[str] = []
        for raw in paths:
            if raw == ".":
                candidates: set[str] = set()
                for base, dirs, names in os.walk(self.root):
                    dirs[:] = [d for d in dirs if d != META and not d.startswith(".pyvcs-tmp")]
                    for name in names:
                        candidate = (Path(base) / name).relative_to(self.root).as_posix()
                        if not candidate.startswith(META + "/") and not self._is_ignored(candidate):
                            candidates.add(candidate)
                # `add .` also stages deletions of previously tracked files.
                candidates.update(p for p in set(index) | set(head_tree) if not self._is_ignored(p))
            else:
                rel = self._validate_relpath(raw)
                full = self.root / Path(*PurePosixPath(rel).parts)
                if full.is_dir():
                    candidates = {
                        p.relative_to(self.root).as_posix()
                        for p in full.rglob("*") if p.is_file() and not p.is_symlink()
                    }
                    prefix = rel.rstrip("/") + "/"
                    candidates.update(
                        p for p in set(index) | set(head_tree)
                        if p == rel or p.startswith(prefix)
                    )
                else:
                    candidates = {rel}
            for candidate in sorted(candidates):
                rel = self._validate_relpath(candidate)
                if self._is_ignored(rel):
                    continue
                full = self.root / Path(*PurePosixPath(rel).parts)
                if full.is_file() and not full.is_symlink():
                    index[rel] = self._store_object("blob", full.read_bytes())
                    added.append(rel)
                elif rel in index or rel in head_tree:
                    # Missing tracked paths are staged as deletions.
                    if rel in index:
                        index.pop(rel, None)
                    added.append(rel)
        self._write_json(self.index_file, index)
        return sorted(set(added))

    def diff(
        self,
        staged: bool = False,
        paths: list[str] | None = None,
        from_revision: str | None = None,
        to_revision: str | None = None,
        output_format: str = "unified",
        context: int = 3,
    ) -> str:
        from .diff_engine import build_result, render_name_only, render_stat, render_unified

        if context < 0:
            raise VcsError("Diff context must be zero or greater.")
        if bool(from_revision) != bool(to_revision):
            raise VcsError("Compare revisions by providing both --from and --to.")
        if staged and from_revision:
            raise VcsError("--staged cannot be combined with --from/--to.")

        if from_revision and to_revision:
            old = self._tree_for_commit(self.resolve_commit(from_revision))
            new = self._tree_for_commit(self.resolve_commit(to_revision))
        elif staged:
            old = self._tree_for_commit(self.head_commit())
            new = self._read_json(self.index_file, {})
        else:
            old = self._read_json(self.index_file, {})
            # Like Git, normal diff compares the index to tracked working files;
            # untracked files are shown by status but are not included by default.
            working = self._working_tree()
            new = {path: oid for path, oid in working.items() if path in old}

        result = build_result(old, new, lambda oid: self._read_object(oid)[1], paths)
        if output_format == "stat":
            return render_stat(result)
        if output_format == "name-only":
            return render_name_only(result)
        return render_unified(result, lambda oid: self._read_object(oid)[1], context)

    def commit(self, message: str) -> str:
        if not message.strip():
            raise VcsError("Commit message cannot be empty.")
        state = self.status()
        if not state["staged"]:
            raise VcsError("Nothing staged to commit. Use 'pyvcs add' first.")
        if self.merge_state_file.exists():
            raise VcsError("A merge is in progress. Resolve conflicts and use 'pyvcs add' before committing.")
        index = self._read_json(self.index_file, {})
        tree_id = self._store_json_object("tree", index)
        parent = self.head_commit()
        cfg = self.config()
        commit_data = {
            "tree": tree_id, "parents": [parent] if parent else [],
            "author": cfg.get("user.name", "Unknown User"),
            "email": cfg.get("user.email", "unknown@example.com"),
            "timestamp": int(time.time()), "message": message
        }
        oid = self._store_json_object("commit", commit_data)
        self._set_head_commit(oid)
        self._log("commit", parent, oid, message)
        return oid

    def log(self, limit: int | None = None) -> list[tuple[str, dict[str, Any]]]:
        result, oid, seen = [], self.head_commit(), set()
        while oid and oid not in seen:
            seen.add(oid)
            obj = self._read_json_object(oid, "commit")
            result.append((oid, obj))
            if limit and len(result) >= limit:
                break
            parents = obj.get("parents", [])
            oid = parents[0] if parents else None
        return result

    def show(self, oid: str) -> dict[str, Any]:
        resolved = self.resolve_commit(oid)
        data = self._read_json_object(resolved, "commit")
        data["id"] = resolved
        return data

    def resolve_commit(self, prefix: str) -> str:
        """Resolve HEAD, a local branch/tag name, a full ID, or an ID prefix."""
        revision = prefix.strip()
        if revision in ("HEAD", "@"):
            oid = self.head_commit()
            if not oid:
                raise VcsError("HEAD does not point to a commit yet.")
            return oid
        if revision.startswith("refs/heads/"):
            ref_path = self.meta / revision
            if ref_path.is_file():
                oid = ref_path.read_text(encoding="utf-8").strip()
                if oid:
                    return oid
        if revision.startswith("refs/tags/"):
            ref_path = self.meta / revision
            if ref_path.is_file():
                oid = ref_path.read_text(encoding="utf-8").strip()
                if oid:
                    return oid
        if "/" not in revision:
            for ref_path in (self.refs_heads / revision, self.refs_tags / revision):
                if ref_path.is_file():
                    oid = ref_path.read_text(encoding="utf-8").strip()
                    if oid:
                        return oid
        if len(revision) == 64:
            kind, _ = self._read_object(revision)
            if kind != "commit":
                raise VcsError(f"Object {revision} is not a commit.")
            return revision
        matches = []
        if self.objects.exists():
            for folder in self.objects.iterdir():
                if folder.is_dir():
                    for file in folder.iterdir():
                        oid = folder.name + file.name
                        if oid.startswith(revision):
                            try:
                                kind, _ = self._read_object(oid)
                                if kind == "commit":
                                    matches.append(oid)
                            except VcsError:
                                pass
        if len(matches) != 1:
            raise VcsError(f"Revision '{revision}' is {'ambiguous' if matches else 'unknown'}.")
        return matches[0]

    def branch(self, name: str | None = None, delete: bool = False) -> list[str] | None:
        if name is None:
            current = self.current_branch()
            return [("* " if n == current else "  ") + n for n in sorted(p.name for p in self.refs_heads.iterdir() if p.is_file())]
        if "/" in name or "\\" in name or name in ("", ".", "..") or name.startswith("-"):
            raise VcsError("Invalid branch name.")
        target = self.refs_heads / name
        if delete:
            if name == self.current_branch():
                raise VcsError("Cannot delete the current branch.")
            if not target.exists():
                raise VcsError(f"Branch '{name}' does not exist.")
            target.unlink()
            return None
        if target.exists():
            raise VcsError(f"Branch '{name}' already exists.")
        head = self.head_commit()
        _atomic_write(target, ((head or "") + "\n").encode())
        return None

    def switch(self, name: str) -> None:
        target = self.refs_heads / name
        if not target.exists():
            raise VcsError(f"Branch '{name}' does not exist.")
        if any(self.status().values()):
            raise VcsError("Working tree or index has changes. Commit or stash them before switching.")
        old = self.head_commit()
        new = target.read_text(encoding="utf-8").strip() or None
        self._checkout_tree(self._tree_for_commit(new))
        _atomic_write(self.head_file, f"ref: refs/heads/{name}\n".encode())
        self._write_json(self.index_file, self._tree_for_commit(new))
        self._log("switch", old, new, name)

    def _checkout_tree(self, tree: dict[str, str]) -> None:
        current = self._tree_for_commit(self.head_commit())
        # Remove only files known to current commit, leaving untracked files untouched.
        for rel in set(current) - set(tree):
            path = self.root / Path(*PurePosixPath(rel).parts)
            if path.is_file() and not path.is_symlink():
                path.unlink()
        for rel, oid in tree.items():
            path = self.root / Path(*PurePosixPath(rel).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, self._read_object(oid)[1])

    def _ancestors(self, start: str | None) -> dict[str, int]:
        distances: dict[str, int] = {}
        queue: list[tuple[str, int]] = [(start, 0)] if start else []
        while queue:
            oid, dist = queue.pop(0)
            if oid in distances and distances[oid] <= dist:
                continue
            distances[oid] = dist
            commit = self._read_json_object(oid, "commit")
            queue.extend((p, dist + 1) for p in commit.get("parents", []))
        return distances

    def _merge_base(self, ours: str, theirs: str) -> str | None:
        a, b = self._ancestors(ours), self._ancestors(theirs)
        common = set(a) & set(b)
        return min(common, key=lambda x: a[x] + b[x]) if common else None

    def merge(self, name: str) -> tuple[str, list[str]]:
        if any(self.status().values()):
            raise VcsError("Commit or otherwise clean the working tree before merging.")
        target_ref = self.refs_heads / name
        if not target_ref.exists():
            raise VcsError(f"Branch '{name}' does not exist.")
        ours_id, theirs_id = self.head_commit(), target_ref.read_text(encoding="utf-8").strip() or None
        if not ours_id or not theirs_id:
            raise VcsError("Both branches must have at least one commit before merging.")
        if ours_id == theirs_id:
            return "up-to-date", []
        ours_anc, theirs_anc = self._ancestors(ours_id), self._ancestors(theirs_id)
        if ours_id in theirs_anc:
            self._checkout_tree(self._tree_for_commit(theirs_id))
            self._set_head_commit(theirs_id)
            self._write_json(self.index_file, self._tree_for_commit(theirs_id))
            self._log("fast-forward", ours_id, theirs_id, name)
            return "fast-forward", []
        if theirs_id in ours_anc:
            return "up-to-date", []
        base_id = self._merge_base(ours_id, theirs_id)
        base = self._tree_for_commit(base_id)
        ours = self._tree_for_commit(ours_id)
        theirs = self._tree_for_commit(theirs_id)
        merged: dict[str, str] = {}
        conflicts: list[str] = []
        for path in sorted(set(base) | set(ours) | set(theirs)):
            b, o, t = base.get(path), ours.get(path), theirs.get(path)
            if o == t:
                if o is not None:
                    merged[path] = o
            elif o == b:
                if t is not None:
                    merged[path] = t
            elif t == b:
                if o is not None:
                    merged[path] = o
            else:
                conflicts.append(path)
                # Keep ours in the index while writing explicit markers for text conflicts below.
                if o is not None:
                    merged[path] = o
        self._checkout_tree(ours)
        # Apply automatically merged paths to the working tree; leave conflicting
        # paths at ours until explicit conflict markers are written below.
        for path in sorted((set(ours) | set(merged)) - set(conflicts)):
            target = self.root / Path(*PurePosixPath(path).parts)
            if path in merged:
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(target, self._read_object(merged[path])[1])
            elif target.is_file() and not target.is_symlink():
                target.unlink()
        for path in sorted(set(base) | set(ours) | set(theirs)):
            b, o, t = base.get(path), ours.get(path), theirs.get(path)
            if path not in conflicts:
                continue
            target = self.root / Path(*PurePosixPath(path).parts)
            if o is None or t is None:
                # Delete/modify conflicts are left as ours, and require manual resolution.
                continue
            try:
                ob = self._read_object(o)[1].decode("utf-8").splitlines()
                tb = self._read_object(t)[1].decode("utf-8").splitlines()
                bb = self._read_object(b)[1].decode("utf-8").splitlines() if b else []
            except UnicodeDecodeError:
                continue
            # A conservative line-level marker for overlapping file edits.
            marker = ["<<<<<<< HEAD"] + ob + ["======="] + tb + [">>>>>>> " + name]
            _atomic_write(target, ("\n".join(marker) + "\n").encode("utf-8"))
        self._write_json(self.index_file, merged)
        self._write_json(self.merge_state_file, {
            "ours": ours_id, "theirs": theirs_id, "branch": name,
            "conflicts": conflicts, "base": base_id
        })
        self._log("merge-start", ours_id, theirs_id, name)
        if conflicts:
            return "conflicts", conflicts
        return "merged", []

    def finish_merge(self, message: str = "") -> str:
        if not self.merge_state_file.exists():
            raise VcsError("No merge is in progress.")
        state = self._read_json(self.merge_state_file, {})
        conflicts = state.get("conflicts", [])
        if conflicts:
            for path in conflicts:
                if path not in self._read_json(self.index_file, {}):
                    raise VcsError(f"Conflict path {path} is not staged.")
            # Conflict markers are intentionally not auto-accepted: users should edit and add paths.
            for path in conflicts:
                full = self.root / Path(*PurePosixPath(path).parts)
                if full.exists() and b"<<<<<<< HEAD" in full.read_bytes():
                    raise VcsError(f"Conflict markers remain in {path}; resolve it and run 'pyvcs add {path}'.")
        index = self._read_json(self.index_file, {})
        tree_id = self._store_json_object("tree", index)
        cfg = self.config()
        ours, theirs = state["ours"], state["theirs"]
        data = {
            "tree": tree_id, "parents": [ours, theirs],
            "author": cfg.get("user.name", "Unknown User"),
            "email": cfg.get("user.email", "unknown@example.com"),
            "timestamp": int(time.time()),
            "message": message or f"Merge branch '{state['branch']}'"
        }
        oid = self._store_json_object("commit", data)
        self._set_head_commit(oid)
        self.merge_state_file.unlink(missing_ok=True)
        self._log("merge-commit", ours, oid, data["message"])
        return oid

    def restore(self, path: str, revision: str | None = None) -> None:
        rel = self._validate_relpath(path)
        tree = self._tree_for_commit(self.resolve_commit(revision) if revision else self.head_commit())
        full = self.root / Path(*PurePosixPath(rel).parts)
        if rel not in tree:
            if full.exists() and full.is_file():
                full.unlink()
            index = self._read_json(self.index_file, {})
            index.pop(rel, None)
            self._write_json(self.index_file, index)
            return
        data = self._read_object(tree[rel])[1]
        _atomic_write(full, data)
        index = self._read_json(self.index_file, {})
        index[rel] = tree[rel]
        self._write_json(self.index_file, index)

    def revert(self, revision: str) -> str:
        target_id = self.resolve_commit(revision)
        target = self._read_json_object(target_id, "commit")
        parents = target.get("parents", [])
        if not parents:
            raise VcsError("Reverting the initial commit is not supported in this MVP.")
        if any(self.status().values()):
            raise VcsError("Working tree or index must be clean before revert.")
        current_tree = self._tree_for_commit(self.head_commit())
        target_tree = self._tree_for_commit(target_id)
        parent_tree = self._tree_for_commit(parents[0])
        result = dict(current_tree)
        for path in set(parent_tree) | set(target_tree):
            if parent_tree.get(path) != target_tree.get(path):
                if path in parent_tree:
                    result[path] = parent_tree[path]
                else:
                    result.pop(path, None)
        self._checkout_tree(result)
        self._write_json(self.index_file, result)
        return self.commit(f"Revert \"{target.get('message', target_id[:8])}\"")

    def tag(self, name: str, revision: str | None = None, delete: bool = False) -> list[str] | None:
        if not name or "/" in name or "\\" in name:
            raise VcsError("Invalid tag name.")
        path = self.refs_tags / name
        if delete:
            if not path.exists():
                raise VcsError(f"Tag '{name}' does not exist.")
            path.unlink()
            return None
        if path.exists():
            raise VcsError(f"Tag '{name}' already exists.")
        oid = self.resolve_commit(revision) if revision else self.head_commit()
        if not oid:
            raise VcsError("Cannot tag an empty repository.")
        _atomic_write(path, (oid + "\n").encode())
        return None

    def tags(self) -> list[str]:
        return sorted(p.name for p in self.refs_tags.iterdir() if p.is_file())

    def reflog(self) -> list[dict[str, Any]]:
        rows = []
        if self.reflog_file.exists():
            for line in self.reflog_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return rows

    def fsck(self) -> list[str]:
        errors = []
        for folder in self.objects.iterdir():
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                oid = folder.name + path.name
                try:
                    self._read_object(oid)
                except VcsError as exc:
                    errors.append(str(exc))
        return errors
