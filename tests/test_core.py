from pathlib import Path

import pytest

from pyvcs.core import Repository, VcsError


def setup_repo(tmp_path: Path) -> Repository:
    repo = Repository.init(tmp_path)
    repo.set_config("user.name", "Test User")
    repo.set_config("user.email", "test@example.com")
    return repo


def write(root: Path, name: str, data: str) -> None:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data, encoding="utf-8")


def commit_file(repo: Repository, root: Path, name: str, data: str, message: str) -> str:
    write(root, name, data)
    repo.add([name])
    return repo.commit(message)


def test_init_commit_log_and_object_integrity(tmp_path):
    repo = setup_repo(tmp_path)
    commit_id = commit_file(repo, tmp_path, "hello.txt", "hello\n", "initial")
    assert repo.head_commit() == commit_id
    assert repo.log()[0][1]["message"] == "initial"
    assert repo.log()[0][1]["author"] == "Test User"
    assert repo.fsck() == []


def test_status_staging_and_diff(tmp_path):
    repo = setup_repo(tmp_path)
    write(tmp_path, "a.txt", "one\n")
    assert repo.status()["untracked"] == ["a.txt"]
    repo.add(["a.txt"])
    assert repo.status()["staged"] == ["a.txt"]
    repo.commit("add a")
    write(tmp_path, "a.txt", "two\n")
    assert repo.status()["unstaged"] == ["a.txt"]
    assert "+two" in repo.diff()


def test_branch_switch(tmp_path):
    repo = setup_repo(tmp_path)
    commit_file(repo, tmp_path, "a.txt", "main\n", "main base")
    repo.branch("feature")
    repo.switch("feature")
    commit_file(repo, tmp_path, "b.txt", "feature\n", "feature file")
    repo.switch("main")
    assert not (tmp_path / "b.txt").exists()
    assert repo.current_branch() == "main"


def test_three_way_merge_non_conflicting(tmp_path):
    repo = setup_repo(tmp_path)
    commit_file(repo, tmp_path, "base.txt", "base\n", "base")
    repo.branch("feature")
    commit_file(repo, tmp_path, "main.txt", "main\n", "main change")
    repo.switch("feature")
    commit_file(repo, tmp_path, "feature.txt", "feature\n", "feature change")
    repo.switch("main")
    result, conflicts = repo.merge("feature")
    assert result == "merged"
    assert conflicts == []
    merge_id = repo.finish_merge()
    commit = repo.show(merge_id)
    assert len(commit["parents"]) == 2
    assert (tmp_path / "feature.txt").read_text() == "feature\n"


def test_three_way_merge_conflict_and_resolution(tmp_path):
    repo = setup_repo(tmp_path)
    commit_file(repo, tmp_path, "a.txt", "base\n", "base")
    repo.branch("feature")
    commit_file(repo, tmp_path, "a.txt", "main edit\n", "main edit")
    repo.switch("feature")
    commit_file(repo, tmp_path, "a.txt", "feature edit\n", "feature edit")
    repo.switch("main")
    result, conflicts = repo.merge("feature")
    assert result == "conflicts"
    assert conflicts == ["a.txt"]
    write(tmp_path, "a.txt", "resolved\n")
    repo.add(["a.txt"])
    merge_id = repo.finish_merge("resolve conflict")
    assert repo.show(merge_id)["parents"]
    assert (tmp_path / "a.txt").read_text() == "resolved\n"


def test_restore_and_revert(tmp_path):
    repo = setup_repo(tmp_path)
    first = commit_file(repo, tmp_path, "a.txt", "old\n", "old")
    second = commit_file(repo, tmp_path, "a.txt", "new\n", "new")
    repo.restore("a.txt", first)
    assert (tmp_path / "a.txt").read_text() == "old\n"
    # Return to current HEAD's state, then revert the second commit.
    repo.restore("a.txt", second)
    repo.revert(second)
    assert (tmp_path / "a.txt").read_text() == "old\n"


def test_empty_commit_rejected(tmp_path):
    repo = setup_repo(tmp_path)
    with pytest.raises(VcsError):
        repo.commit("empty")


def test_ignore_and_reflog(tmp_path):
    repo = setup_repo(tmp_path)
    (tmp_path / ".pyvcsignore").write_text("*.log\n")
    write(tmp_path, "debug.log", "secret\n")
    write(tmp_path, "keep.txt", "keep\n")
    repo.add(["."])
    assert "debug.log" not in repo._read_json(repo.index_file)
    repo.commit("add keep")
    assert repo.reflog()


def test_diff_engine_add_delete_and_stats(tmp_path):
    repo = setup_repo(tmp_path)
    write(tmp_path, "a.txt", "one\ntwo\n")
    repo.add(["a.txt"])
    repo.commit("add a")

    write(tmp_path, "a.txt", "one\nthree\n")
    text_diff = repo.diff()
    assert "diff --git a/a.txt b/a.txt" in text_diff
    assert "-two" in text_diff
    assert "+three" in text_diff
    assert "@@" in text_diff
    stats = repo.diff(output_format="stat")
    assert "a.txt | 2 +-" in stats
    assert "1 insertion(+)" in stats
    assert "1 deletion(-)" in stats

    (tmp_path / "a.txt").unlink()
    repo.add(["."])
    staged = repo.diff(staged=True)
    assert "deleted file" in staged
    assert "--- a/a.txt" in staged
    assert "+++ /dev/null" in staged


def test_diff_engine_detects_exact_rename_after_commit(tmp_path):
    repo = setup_repo(tmp_path)
    commit_file(repo, tmp_path, "old.txt", "same content\n", "old file")
    old = repo.head_commit()
    (tmp_path / "old.txt").rename(tmp_path / "new.txt")
    repo.add(["."])
    repo.commit("rename file")
    diff = repo.diff(from_revision=old, to_revision=repo.head_commit())
    assert "rename from old.txt" in diff
    assert "rename to new.txt" in diff
    names = repo.diff(from_revision=old, to_revision=repo.head_commit(), output_format="name-only")
    assert names == "old.txt => new.txt\n"


def test_diff_engine_binary_notice_and_path_filter(tmp_path):
    repo = setup_repo(tmp_path)
    (tmp_path / "image.bin").write_bytes(b"\x00\x01\x02")
    write(tmp_path, "a.txt", "one\n")
    repo.add(["."])
    first = repo.commit("initial files")
    (tmp_path / "image.bin").write_bytes(b"\x00\x01\x03")
    write(tmp_path, "a.txt", "two\n")
    repo.add(["."])
    second = repo.commit("modify both")

    output = repo.diff(from_revision=first, to_revision=second, paths=["image.bin"])
    assert "Binary files a/image.bin and b/image.bin differ" in output
    assert "a.txt" not in output
    stats = repo.diff(from_revision=first, to_revision=second, output_format="stat", paths=["image.bin"])
    assert "image.bin | Bin" in stats


def test_diff_does_not_include_untracked_files_by_default(tmp_path):
    repo = setup_repo(tmp_path)
    write(tmp_path, "untracked.txt", "new file\n")
    assert repo.diff() == "No differences found.\n"
    assert repo.diff(paths=["untracked.txt"]) == "No differences found.\n"


def test_diff_requires_both_revisions(tmp_path):
    repo = setup_repo(tmp_path)
    commit_file(repo, tmp_path, "a.txt", "one\n", "first")
    with pytest.raises(VcsError, match="both --from and --to"):
        repo.diff(from_revision=repo.head_commit())


def test_diff_can_resolve_head_branch_and_tag_names(tmp_path):
    repo = setup_repo(tmp_path)
    first = commit_file(repo, tmp_path, "a.txt", "old\n", "first")
    repo.tag("v1")
    commit_file(repo, tmp_path, "a.txt", "new\n", "second")

    assert repo.resolve_commit("HEAD") == repo.head_commit()
    assert repo.resolve_commit("main") == repo.head_commit()
    assert repo.resolve_commit("v1") == first
    assert "-old" in repo.diff(from_revision="v1", to_revision="HEAD")
    assert "+new" in repo.diff(from_revision="v1", to_revision="main")


def test_diff_marks_missing_final_newline(tmp_path):
    repo = setup_repo(tmp_path)
    write(tmp_path, "a.txt", "before")
    repo.add(["a.txt"])
    repo.commit("before without newline")
    write(tmp_path, "a.txt", "after")
    output = repo.diff()
    assert "-before\n\\ No newline at end of file\n" in output
    assert "+after\n\\ No newline at end of file\n" in output


def test_unified_diff_emits_each_file_header_once(tmp_path):
    repo = setup_repo(tmp_path)
    write(tmp_path, "a.txt", "old\n")
    repo.add(["a.txt"])
    repo.commit("old")
    write(tmp_path, "a.txt", "new\n")
    output = repo.diff()
    assert output.count("--- a/a.txt") == 1
    assert output.count("+++ b/a.txt") == 1


def test_diff_empty_file_addition_has_file_headers(tmp_path):
    repo = setup_repo(tmp_path)
    (tmp_path / "empty.txt").write_bytes(b"")
    repo.add(["empty.txt"])
    output = repo.diff(staged=True)
    assert "new file" in output
    assert "--- /dev/null\n" in output
    assert "+++ b/empty.txt\n" in output
