from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import Repository, VcsError


def find_repo(start: Path | None = None) -> Repository:
    path = (start or Path.cwd()).resolve()
    for candidate in (path, *path.parents):
        if (candidate / ".pyvcs").is_dir():
            return Repository(candidate)
    raise VcsError("Not inside a PyVCS repository. Run 'pyvcs init' first.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyvcs", description="PyVCS: a local-first version control system")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="initialize a repository")
    p.add_argument("path", nargs="?", default=".")

    p = sub.add_parser("config", help="read or set repository configuration")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")

    p = sub.add_parser("add", help="stage file changes")
    p.add_argument("paths", nargs="+")

    sub.add_parser("status", help="show staged, unstaged, and untracked files")

    p = sub.add_parser("commit", help="create a commit")
    p.add_argument("-m", "--message", required=True)

    p = sub.add_parser("log", help="show commit history")
    p.add_argument("-n", "--limit", type=int)

    p = sub.add_parser("show", help="show commit details")
    p.add_argument("revision")

    p = sub.add_parser("diff", help="show changes between the index, working tree, or commits")
    p.add_argument("--staged", action="store_true", help="compare HEAD with the staging area")
    p.add_argument("--from", dest="from_revision", help="starting commit for a revision comparison")
    p.add_argument("--to", dest="to_revision", help="ending commit for a revision comparison")
    p.add_argument("--stat", action="store_true", help="show per-file change statistics")
    p.add_argument("--name-only", action="store_true", help="list changed paths only")
    p.add_argument("-U", "--unified", type=int, default=3, help="number of context lines (default: 3)")
    p.add_argument("paths", nargs="*", help="optional file or directory path filters")

    p = sub.add_parser("branch", help="list, create, or delete branches")
    p.add_argument("name", nargs="?")
    p.add_argument("-d", "--delete", action="store_true")

    p = sub.add_parser("switch", help="switch branches")
    p.add_argument("name")

    p = sub.add_parser("merge", help="merge a branch into the current branch")
    p.add_argument("name")
    p.add_argument("-m", "--message")

    p = sub.add_parser("merge-commit", help="finish a merge after resolving conflicts")
    p.add_argument("-m", "--message", default="")

    p = sub.add_parser("restore", help="restore a file from HEAD or a revision")
    p.add_argument("path")
    p.add_argument("--source", dest="revision")

    p = sub.add_parser("revert", help="create a new commit undoing a commit")
    p.add_argument("revision")

    p = sub.add_parser("tag", help="list, create, or delete tags")
    p.add_argument("name", nargs="?")
    p.add_argument("revision", nargs="?")
    p.add_argument("-d", "--delete", action="store_true")

    sub.add_parser("reflog", help="show local reference activity")
    sub.add_parser("fsck", help="check object integrity")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            repo = Repository.init(Path(args.path))
            print(f"Initialized empty PyVCS repository in {repo.meta}")
            return 0

        repo = find_repo()
        if args.command == "config":
            if not args.key:
                for k, v in sorted(repo.config().items()):
                    print(f"{k}={v}")
            elif args.value is None:
                print(repo.config().get(args.key, ""))
            else:
                repo.set_config(args.key, args.value)
            return 0
        if args.command == "add":
            added = repo.add(args.paths)
            print("Staged: " + (", ".join(added) if added else "(no file changes)"))
        elif args.command == "status":
            state = repo.status()
            print(f"On branch {repo.current_branch() or '(detached HEAD)'}")
            for label, key in (("Changes staged:", "staged"), ("Changes not staged:", "unstaged"), ("Untracked files:", "untracked")):
                if state[key]:
                    print(label)
                    for item in state[key]:
                        print(f"  {item}")
            if not any(state.values()):
                print("Working tree clean")
        elif args.command == "commit":
            print(f"[{repo.current_branch() or 'detached'} {repo.commit(args.message)}] {args.message}")
        elif args.command == "log":
            for oid, c in repo.log(args.limit):
                print(f"commit {oid}\nAuthor: {c.get('author')} <{c.get('email')}>\nDate:   {c.get('timestamp')}\n\n    {c.get('message')}\n")
        elif args.command == "show":
            c = repo.show(args.revision)
            print(f"commit {c['id']}\nAuthor: {c['author']} <{c['email']}>\nDate: {c['timestamp']}\nParents: {', '.join(c['parents']) or '(root)'}\n\n    {c['message']}")
        elif args.command == "diff":
            if args.stat and args.name_only:
                raise VcsError("Choose only one of --stat and --name-only.")
            output_format = "stat" if args.stat else "name-only" if args.name_only else "unified"
            print(repo.diff(
                staged=args.staged,
                paths=args.paths,
                from_revision=args.from_revision,
                to_revision=args.to_revision,
                output_format=output_format,
                context=args.unified,
            ), end="")
        elif args.command == "branch":
            if args.name is None:
                print("\n".join(repo.branch()))
            else:
                repo.branch(args.name, delete=args.delete)
                print(f"{'Deleted' if args.delete else 'Created'} branch {args.name}")
        elif args.command == "switch":
            repo.switch(args.name)
            print(f"Switched to branch '{args.name}'")
        elif args.command == "merge":
            result, conflicts = repo.merge(args.name)
            if result == "conflicts":
                print("Merge has conflicts. Edit the listed files, run 'pyvcs add <file>', then 'pyvcs merge-commit -m \"message\"'.")
                for path in conflicts:
                    print(f"  CONFLICT: {path}")
                return 1
            if result == "merged":
                oid = repo.finish_merge(args.message or f"Merge branch '{args.name}'")
                print(f"Merge committed as {oid}")
            else:
                print(result)
        elif args.command == "merge-commit":
            print(f"Merge commit {repo.finish_merge(args.message)}")
        elif args.command == "restore":
            repo.restore(args.path, args.revision)
            print(f"Restored {args.path}")
        elif args.command == "revert":
            print(f"Revert commit {repo.revert(args.revision)}")
        elif args.command == "tag":
            if args.name is None:
                print("\n".join(repo.tags()))
            else:
                repo.tag(args.name, args.revision, args.delete)
                print(f"{'Deleted' if args.delete else 'Created'} tag {args.name}")
        elif args.command == "reflog":
            for row in repo.reflog():
                print(f"{row['time']} {row['user']} <{row['email']}> {row['action']}: {row.get('old') or '-'} -> {row.get('new') or '-'} {row.get('detail', '')}")
        elif args.command == "fsck":
            errors = repo.fsck()
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            print("All stored objects passed integrity checks.")
        return 0
    except VcsError as exc:
        print(f"pyvcs: error: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print(f"pyvcs: filesystem error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
