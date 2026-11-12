"""M67 单入口编排只投影既有发行事实，不复制构建和签名 owner。"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from scripts.release import (
    ConfirmationRequired, ReleaseBlocked, ReleaseJournal, ReleaseOrchestrator,
    _bundle_set, build_download_request,
    inspect_preflight, main, release_plan, release_run_lock,
)


def git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=root, text=True).strip()


def repository(tmp_path: Path, version: str = "2.0.0") -> tuple[Path, str]:
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    package = root / "satellite_debug_tool"
    package.mkdir()
    (package / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    doc = root / "doc"
    doc.mkdir()
    (doc / f"v{version}_release_notes.md").write_text("# Release\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "release")
    return root, git(root, "rev-parse", "HEAD")


def test_plan_is_read_only_and_binds_exact_clean_commit(tmp_path):
    root, commit = repository(tmp_path)
    state = tmp_path / "state"
    plan = release_plan(root, "2.0.0", state)
    assert plan["source_commit"] == plan["confirmations"]["tag"] == commit
    assert plan["blockers"] == []
    assert plan["mutations"] is False
    assert not state.exists()
    (root / "untracked").write_text("dirty")
    assert inspect_preflight(root, "2.0.0")["blockers"] == ["worktree is dirty"]


def test_cli_dry_run_is_read_only_and_does_not_create_journal(tmp_path, capsys):
    root, commit = repository(tmp_path)
    state = tmp_path / "state"
    assert main(["run", "--version", "2.0.0", "--root", str(root),
                 "--state-root", str(state), "--dry-run"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["source_commit"] == commit
    assert output["mutations"] is False
    assert not state.exists()


def test_existing_tag_must_not_point_to_another_commit(tmp_path):
    root, previous = repository(tmp_path)
    git(root, "tag", "v2.0.0")
    (root / "next").write_text("next")
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "next")
    facts = inspect_preflight(root, "2.0.0")
    assert facts["tag_commit"] == previous
    assert "existing tag points to another commit" in facts["blockers"]


def test_journal_is_exact_commit_bound_and_keeps_stage_evidence(tmp_path):
    path = tmp_path / "run.json"
    first = ReleaseJournal.open(path, version="2.0.0", commit="a" * 40)
    first.mark("preflight", "PASSED", tests="PASS")
    loaded = ReleaseJournal.open(path, version="2.0.0", commit="a" * 40)
    assert loaded.stage("preflight") == {"state": "PASSED", "tests": "PASS"}
    with pytest.raises(Exception, match="differs"):
        ReleaseJournal.open(path, version="2.0.0", commit="b" * 40)


def test_preflight_rejects_source_changes_created_during_validation(tmp_path):
    root, _ = repository(tmp_path)
    calls = []

    def runner(arguments, *, cwd, **_kwargs):
        calls.append(arguments)
        if len(calls) == 1:
            (root / "changed-during-tests").write_text("dirty")
        return ""

    orchestrator = ReleaseOrchestrator(root=root, state_root=tmp_path / "state",
                                       version="2.0.0", runner=runner)
    with pytest.raises(ReleaseBlocked, match="source snapshot changed"):
        orchestrator._preflight()
    assert len(calls) == 4


def test_bundle_confirmation_changes_when_either_publication_changes(tmp_path):
    windows, macos = tmp_path / "windows.json", tmp_path / "macos.json"
    windows.write_text("windows")
    macos.write_text("macos")
    first, hashes = _bundle_set({"windows": str(windows), "macos": str(macos)})
    assert set(hashes) == {"windows", "macos"}
    macos.write_text("changed")
    second, _ = _bundle_set({"windows": str(windows), "macos": str(macos)})
    assert first != second


def test_download_request_binds_both_user_receipts_and_exact_catalogs():
    candidates = {"windows_user": {"sha256": "1" * 64}, "macos_user": {"sha256": "2" * 64}}
    request = build_download_request(version="2.0.0", commit="a" * 40, release_id="a" * 40,
        candidates=candidates, public_base_url="https://example.test/downloads/")
    assert request["users"]["windows_x86_64"]["receipt_sha256"] == "1" * 64
    assert request["users"]["macos_arm64"]["receipt_sha256"] == "2" * 64
    assert request["ops"]["windows_x86_64"]["catalog"].endswith("windows/x86_64.json")
    assert request["ops"]["macos_arm64"]["catalog"].endswith("macos/arm64.json")
    assert len(request["request_id"]) == 64
    assert json.loads(json.dumps(request))["source_commit"] == "a" * 40


def test_confirmation_exception_exposes_content_bound_value():
    error = ConfirmationRequired("stable", "f" * 64, details={"packages": {"windows": "sha"}})
    assert error.kind == "stable"
    assert "--confirm-stable" in str(error)
    assert error.details == {"packages": {"windows": "sha"}}
    assert '"windows": "sha"' in str(error)


def test_same_version_run_lock_rejects_concurrent_owner(tmp_path):
    lock = tmp_path / "run.lock"
    with release_run_lock(lock):
        with pytest.raises(Exception, match="another process"):
            with release_run_lock(lock):
                pass
