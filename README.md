# PyVCS

PyVCS is an educational, local-first version control system written in Python. It uses a content-addressed SHA-256 object store and a CLI. It is designed to run on Ubuntu Linux and Windows.

> **Status:** MVP / learning project. Do not use as the only backup for valuable work. Before relying on it, review limitations and keep independent backups.

## Features

- Repository initialization and developer identity configuration
- Staging, commits, status, unified line diffs, history, and commit inspection
- Diff statistics, changed-path listing, path filters, configurable context, binary notices, and exact-content rename detection
- Compare any two commits with `pyvcs diff --from REV1 --to REV2`
- SHA-256 content-addressed object storage with blob deduplication
- Branches, branch switching, fast-forward updates, and three-way merges
- Conflict markers for overlapping text edits; explicit conflict resolution
- Restore files from a commit and revert a commit by creating a new commit
- Tags, `.pyvcsignore`, local reflog-style activity history, and `fsck`
- No runtime dependencies beyond Python's standard library

## Requirements

- Python 3.10 or newer
- `pip`
- Optional: `pytest` for running tests

## Install on Ubuntu

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
cd pyvcs
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
pytest
```

If your system Python is older than 3.10, install a newer supported Python version first.

## Install on Windows (PowerShell)

```powershell
cd pyvcs
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
pytest
```

If PowerShell blocks activation, you can run the environment's Python directly:
```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest
```

## Quick start

```bash
mkdir sample-project
cd sample-project
pyvcs init
pyvcs config user.name "Your Name"
pyvcs config user.email "you@example.com"

# Create and commit a file
echo "Hello PyVCS" > hello.txt
pyvcs status
pyvcs add hello.txt
pyvcs diff --staged
pyvcs diff --stat
pyvcs commit -m "Add greeting"

# History and branches
pyvcs log
pyvcs branch feature
pyvcs switch feature
echo "Feature work" > feature.txt
pyvcs add feature.txt
pyvcs commit -m "Add feature file"
pyvcs switch main
pyvcs merge feature

# Verify object integrity
pyvcs fsck
pyvcs reflog
```

On Windows PowerShell, `echo` works for simple examples. For exact UTF-8 file contents, use `Set-Content -Encoding utf8`.

## CLI reference

```text
pyvcs init [PATH]
pyvcs config [KEY [VALUE]]
pyvcs add PATH...
pyvcs status
pyvcs commit -m MESSAGE
pyvcs log [-n LIMIT]
pyvcs show REVISION
pyvcs diff [--staged] [--stat | --name-only] [-U CONTEXT] [PATH...]
pyvcs diff --from REV1 --to REV2 [--stat | --name-only] [PATH...]
pyvcs branch [NAME] [-d]
pyvcs switch NAME
pyvcs merge NAME [-m MESSAGE]
pyvcs merge-commit [-m MESSAGE]
pyvcs restore PATH [--source REVISION]
pyvcs revert REVISION
pyvcs tag [NAME [REVISION]] [-d]
pyvcs reflog
pyvcs fsck
```

A revision can be `HEAD`, a local branch or tag name, a full commit ID, or an unambiguous commit-ID prefix.

## Repository layout

```text
project/
├── .pyvcs/
│   ├── HEAD
│   ├── config.json
│   ├── index.json
│   ├── objects/<first-two-hex>/<remaining-hex>
│   ├── refs/heads/
│   ├── refs/tags/
│   ├── logs/HEAD.jsonl
│   └── MERGE_STATE.json   # only during an active merge
├── .pyvcsignore
└── your project files
```

Object files contain `kind + NUL + payload`; the SHA-256 of those exact bytes is the object ID. Commits refer to tree objects and parent commit IDs. Tree objects map normalized relative paths to blob IDs.

## Diff engine

`pyvcs diff` compares the staging area with tracked working-tree files; untracked files remain visible in `pyvcs status` but are not included in the diff until staged. `pyvcs diff --staged` compares HEAD with the index. `pyvcs diff --from REV1 --to REV2` compares two committed snapshots. All modes accept optional file or directory filters.

Examples:

```bash
pyvcs diff                      # unstaged edits to tracked files
pyvcs diff --staged             # staged changes
pyvcs diff --stat               # per-file insertion/deletion summary
pyvcs diff --name-only          # changed paths only
pyvcs diff -U 0 src/            # no context lines, limited to src/
pyvcs diff --from abc123 --to def456
```

The diff engine outputs unified text hunks, notices binary data, calculates line-change statistics, and detects renames when a removed path and added path have identical blob IDs. Rename similarity heuristics are not implemented yet.

## Merge behavior

- If the target branch is already an ancestor, the merge is a no-op.
- If the current branch is an ancestor of the target, PyVCS fast-forwards.
- Otherwise, it finds a common ancestor and performs a three-way path-level merge.
- If one side changed a path and the other did not, the changed version wins.
- If both sides changed the same path differently, PyVCS reports a conflict. For text files where both versions exist, it writes conflict markers. Edit the file, stage it with `pyvcs add FILE`, then finish with `pyvcs merge-commit -m "Resolve merge"`.

## Important limitations in this MVP

1. This is not a drop-in Git replacement and has no remote push/fetch/pull or network authentication.
2. The staging index is a flat path-to-blob map; there are no executable-bit or symlink semantics.
3. Symlinks are skipped to avoid unsafe/cross-platform behavior.
4. The merge engine detects conflicts at file/path level, not by automatically merging independent line edits. Conflict markers are conservative and require manual review.
5. Reflog records are local metadata and are not cryptographic proof of a person's identity.
6. No locking protocol is implemented for concurrent PyVCS processes. Avoid running multiple write commands simultaneously in one repository.
7. Repository metadata is not yet versioned/migrated. Keep backups before testing recovery features.
8. `restore` changes the file and index; it is intended for deliberate recovery and can overwrite local file contents.
9. `revert` is an MVP implementation for ordinary single-parent commits. Complex merge-commit reverts are not implemented.
10. The initial commit cannot be reverted by the current `revert` command.

## Security and reliability notes

- Commit IDs detect accidental changes to stored objects, but do not authenticate authors.
- Do not store passwords, API keys, private keys, or other secrets in a repository.
- Keep a separate backup of important work.
- Use `pyvcs fsck` to check stored object hashes.
- Review `status` and `diff` before committing or restoring.

## Development

```bash
python -m pytest
python -m pyvcs.cli --help
```

## License

MIT. See `LICENSE`.
