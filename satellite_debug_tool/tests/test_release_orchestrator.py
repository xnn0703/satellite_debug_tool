"""M67 单入口编排只投影既有发行事实，不复制构建和签名 owner。"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.release import (
    ConfirmationRequired, ReleaseBlocked, ReleaseJournal, ReleaseOrchestrator,
    ReleaseCommandError, _run, build_download_request,
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


def test_both_platforms_prepare_and_resume_one_publication(tmp_path):
    root, commit = repository(tmp_path)
    calls = []

    def runner(arguments, *, cwd, **kwargs):
        calls.append(arguments)
        if "prepare" in arguments:
            output = Path(arguments[arguments.index("--output") + 1])
            output.mkdir(parents=True)
            (output / "publication.json").write_text("combined signed publication")
        return json.dumps({"state": "published", "bundle_id": "b" * 64, "reused": True})

    state = tmp_path / "state"
    first = ReleaseOrchestrator(root=root, state_root=state, version="2.0.0", runner=runner)
    first._prepare_publication()
    assert len(calls) == 1
    assert calls[0].count("--candidate") == 2
    assert calls[0].count("--min-os") == 2
    second = ReleaseOrchestrator(root=root, state_root=state, version="2.0.0", runner=runner)
    assert second.commit == commit
    second.journal.data["evidence"]["publication_result"] = {
        "bundle_id": "b" * 64, "verification_request_id": "verified-request"}
    second._commit_publication()
    assert second.journal.data["evidence"]["publication_result"]["verification_request_id"] == "verified-request"
    assert len(calls) == 2
    assert calls[1].count("--bundle") == 1
    assert calls[1].count("--candidate") == 2
    assert calls[1].count("--remote-candidate") == 2
    assert "--recover-lock" not in calls[1]
    manifest = Path(second.journal.data["evidence"]["publication"])
    manifest.write_text("changed")
    with pytest.raises(ReleaseBlocked, match="approved digest"):
        second._commit_publication()
    assert len(calls) == 2

    second.journal.mark("commit_publication", "FAILED", error="connection dropped")
    resumed = ReleaseOrchestrator(root=root, state_root=state, version="2.0.0", runner=runner)
    manifest.write_text("combined signed publication")
    resumed._commit_publication()
    assert "--recover-lock" in calls[-1]


def test_captured_failure_preserves_stderr_return_code_and_journal(tmp_path):
    root, _ = repository(tmp_path)
    orchestrator = ReleaseOrchestrator(root=root, state_root=tmp_path / "state", version="2.0.0")
    def command():
        _run([sys.executable, "-c", "import sys; sys.stderr.write('TLS issuer missing'); sys.exit(7)"],
             cwd=root, capture=True)
        return {}
    with pytest.raises(ReleaseCommandError, match="TLS issuer missing") as error:
        orchestrator._exec_stage("commit_publication", command)
    assert error.value.returncode == 7
    loaded = ReleaseJournal.load(orchestrator.journal.path, version="2.0.0")
    assert "TLS issuer missing" in loaded.stage("commit_publication")["error"]


def test_stage_result_contract_error_is_recorded_as_failed(tmp_path):
    root, _ = repository(tmp_path)
    orchestrator = ReleaseOrchestrator(root=root, state_root=tmp_path / "state", version="2.0.0")
    with pytest.raises(TypeError, match="state"):
        orchestrator._exec_stage("commit_publication", lambda: {"state": "published"})
    assert orchestrator.journal.stage("commit_publication")["state"] == "FAILED"


def test_release_http_probes_use_shared_trusted_opener(monkeypatch):
    from io import BytesIO
    from scripts import release
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            stream = BytesIO(b"x")
            stream.status = 206
            stream.headers = {"Content-Range": "bytes 0-0/1"}
            return stream
    monkeypatch.setattr(release, "build_https_opener", Opener)
    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *a, **k: pytest.fail("default CA path"))
    assert release._range_probe("https://example.test/package", 0, 0, 1) == b"x"
    assert release._small_download("https://example.test/index") == b"x"
    assert len(calls) == 2


def test_missing_publisher_dependency_stops_before_journal_creation(tmp_path, monkeypatch):
    from scripts import release
    root, _ = repository(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setattr(release.importlib.util, "find_spec", lambda name: None if name == "paramiko" else object())
    with pytest.raises(ReleaseBlocked, match="release runtime requires paramiko"):
        main(["run", "--version", "2.0.0", "--root", str(root), "--state-root", str(state)])
    assert not state.exists()


def test_download_resume_consumes_bound_receipt_without_resubmitting(tmp_path, monkeypatch):
    from scripts import release
    from contextlib import nullcontext
    from types import SimpleNamespace
    request = {"request_id": "a" * 64}
    result = {"schema": release.DOWNLOAD_RESULT_SCHEMA, "request_id": request["request_id"],
              "request_sha256": release.hashlib.sha256(release.canonical_bytes(request)).hexdigest(),
              "state": "published"}
    sftp = SimpleNamespace(get_channel=lambda: SimpleNamespace(settimeout=lambda _: None))
    client = SimpleNamespace(open_sftp=lambda: nullcontext(sftp), close=lambda: None)
    monkeypatch.setattr(release, "_connect", lambda _: client)
    monkeypatch.setattr(release, "_read", lambda *a: release.canonical_bytes(result))
    monkeypatch.setattr(release, "upload_file", lambda *a, **k: pytest.fail("completed request resubmitted"))
    assert release.submit_download_request(request, config=tmp_path, known_hosts=tmp_path, identity_file=tmp_path) == result
    result["request_sha256"] = "b" * 64
    with pytest.raises(ReleaseBlocked, match="download publication failed"):
        release.submit_download_request(request, config=tmp_path, known_hosts=tmp_path, identity_file=tmp_path)


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


def test_run_finishes_publication_and_download_stages_with_domain_states(tmp_path, monkeypatch):
    """完整阶段包装必须将发布结果 state 与 journal 阶段 state 分开。"""
    from scripts import release
    root, _ = repository(tmp_path)
    def runner(arguments, **kwargs):
        assert "commit" in arguments
        return json.dumps({"state": "published", "bundle_id": "b" * 64})
    orchestrator = ReleaseOrchestrator(root=root, state_root=tmp_path / "state", version="2.0.0", runner=runner)
    manifest = tmp_path / "publication.json"
    manifest.write_text("signed publication")
    orchestrator.journal.data["evidence"].update({
        "publication": str(manifest), "publication_sha256": release.sha256_file(manifest),
        "candidates": {"windows_user": {"sha256": "1" * 64}, "macos_user": {"sha256": "2" * 64}},
    })
    for name in ("preflight", "tag", "windows_candidate", "macos_candidate", "candidate_audit",
                 "prepare_publication", "stable_confirmation"):
        orchestrator.journal.mark(name, "PASSED")
    monkeypatch.setattr(release, "submit_download_request", lambda *a, **k: {
        "state": "published", "release_index": "https://example.test/index.json"})
    monkeypatch.setattr(release, "post_verify", lambda *a, **k: {"verified": True})
    result = orchestrator.run()
    assert all(stage["state"] == "PASSED" for stage in result["stages"].values())
    assert result["stages"]["commit_publication"]["publication_result"]["state"] == "published"
    assert result["stages"]["publish_downloads"]["publication_state"] == "published"
    assert Path(result["stages"]["evidence"]["validation"]).is_file()
