"""卫通终端管理工具单入口发版编排；复用既有构建、签名、服务器校验 owner。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.promote_release import STATE_ROOT, _connect, _read
from scripts.upload_release import upload_file
from satellite_debug_tool.edition import APP_ID, Edition
from satellite_debug_tool.release_config import load_release_config
from satellite_debug_tool.updater.checker import ReleaseChecker
from satellite_debug_tool.updater.payload import canonical_bytes, sha256_file
from satellite_debug_tool.updater.network import build_https_opener
from satellite_debug_tool.versioning import parse_version


RUN_SCHEMA = "satellite.release-run/v2"
DOWNLOAD_REQUEST_SCHEMA = "satellite.release-download-request/v1"
DOWNLOAD_RESULT_SCHEMA = "satellite.release-download-result/v1"
STAGES = (
    "preflight", "tag", "windows_candidate", "macos_candidate", "candidate_audit",
    "prepare_publication", "stable_confirmation", "commit_publication",
    "publish_downloads", "post_verify", "evidence",
)
TERMINAL_STAGE_STATES = {"PASSED", "SKIPPED"}
HASH40 = re.compile(r"[0-9a-f]{40}")
HASH64 = re.compile(r"[0-9a-f]{64}")


class ReleaseBlocked(RuntimeError):
    pass


class ReleaseCommandError(subprocess.CalledProcessError):
    """保留原返回码，并把工具诊断投影到终端和发布 journal。"""

    def __str__(self) -> str:
        diagnostics = "\n".join(value.strip() for value in (self.stdout, self.stderr) if value)
        return super().__str__() + ("\n" + diagnostics if diagnostics else "")


class ConfirmationRequired(ReleaseBlocked):
    def __init__(self, kind: str, value: str, *, details: Mapping[str, Any] | None = None):
        message = f"confirmation required: --confirm-{kind} {value}"
        if details:
            message += "\n" + json.dumps(details, ensure_ascii=False, indent=2, sort_keys=True)
        super().__init__(message)
        self.kind, self.value, self.details = kind, value, dict(details or {})


def validate_publish_runtime() -> None:
    """发布入口先确认自身可完成 SFTP 与 HTTPS，应用测试环境独立。"""
    missing = [name for name in ("paramiko", "certifi") if importlib.util.find_spec(name) is None]
    if missing:
        raise ReleaseBlocked("release runtime requires " + ", ".join(missing)
            + "; use ~/.config/satellite-release/venv/bin/python (requirements/publish.txt)")
    build_https_opener()


def _run(arguments: list[str], *, cwd: Path = ROOT, env: Mapping[str, str] | None = None,
         capture: bool = False) -> str:
    try:
        result = subprocess.run(arguments, cwd=cwd, env=dict(env) if env else None, check=True,
                                text=True, capture_output=capture)
    except subprocess.CalledProcessError as error:
        raise ReleaseCommandError(error.returncode, error.cmd, output=error.stdout,
                                  stderr=error.stderr) from error
    return result.stdout.strip() if capture else ""


def _git(root: Path, *arguments: str) -> str:
    return _run(["git", *arguments], cwd=root, capture=True)


def _version_source(root: Path) -> str:
    source = (root / "satellite_debug_tool/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    if match is None:
        raise ReleaseBlocked("version source is missing")
    return match.group(1)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    raw = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".release-run-", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@dataclass
class ReleaseJournal:
    path: Path
    data: dict[str, Any]

    @classmethod
    def open(cls, path: Path, *, version: str, commit: str) -> "ReleaseJournal":
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if (data.get("schema") != RUN_SCHEMA or data.get("version") != version
                    or data.get("source_commit") != commit):
                raise ReleaseBlocked("existing release journal differs from this exact release")
            if set(data.get("stages", {})) != set(STAGES):
                raise ReleaseBlocked("release journal stage schema differs")
            return cls(path, data)
        data = {"schema": RUN_SCHEMA, "version": version, "tag": "v" + version,
                "source_commit": commit, "release_id": commit,
                "stages": {name: {"state": "PENDING"} for name in STAGES}, "evidence": {}}
        journal = cls(path, data)
        journal.save()
        return journal

    @classmethod
    def load(cls, path: Path, *, version: str) -> "ReleaseJournal":
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("schema") != RUN_SCHEMA or data.get("version") != version
                or set(data.get("stages", {})) != set(STAGES)):
            raise ReleaseBlocked("release journal schema differs")
        return cls(path, data)

    def save(self) -> None:
        _atomic_json(self.path, self.data)

    def stage(self, name: str) -> dict[str, Any]:
        return self.data["stages"][name]

    def mark(self, name: str, state: str, **evidence: Any) -> None:
        self.data["stages"][name] = {"state": state, **evidence}
        self.save()

    def passed(self, name: str) -> bool:
        return self.stage(name).get("state") in TERMINAL_STAGE_STATES


@contextmanager
def release_run_lock(path: Path):
    """使用内核 advisory lock 拒绝同版本并发；进程退出自动释放。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ReleaseBlocked("another process owns this release run") from error
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ReleaseBlocked("another process owns this release run") from error
        locked = True
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, json.dumps({"pid": os.getpid()}).encode())
        os.fsync(descriptor)
        yield
    finally:
        if locked:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def inspect_preflight(root: Path, version: str) -> dict[str, Any]:
    parse_version(version, complete=True)
    commit = _git(root, "rev-parse", "HEAD")
    if not HASH40.fullmatch(commit):
        raise ReleaseBlocked("source commit is invalid")
    dirty = _git(root, "status", "--porcelain")
    source_version = _version_source(root)
    notes = root / f"doc/v{version}_release_notes.md"
    tag = "v" + version
    tag_commit = ""
    try:
        tag_commit = _git(root, "rev-parse", f"{tag}^{{commit}}")
    except subprocess.CalledProcessError:
        pass
    blockers = []
    if dirty:
        blockers.append("worktree is dirty")
    if source_version != version:
        blockers.append(f"version source is {source_version}")
    if not notes.is_file():
        blockers.append(f"release notes are missing: {notes.relative_to(root)}")
    if tag_commit and tag_commit != commit:
        blockers.append("existing tag points to another commit")
    return {"version": version, "tag": tag, "source_commit": commit, "source_version": source_version,
            "release_notes": str(notes), "tag_commit": tag_commit or None, "blockers": blockers}


def release_plan(root: Path, version: str, state_root: Path) -> dict[str, Any]:
    facts = inspect_preflight(root, version)
    return {"schema": "satellite.release-plan/v1", **facts,
            "journal": str(state_root / ("v" + version) / "release-run.json"),
            "stages": list(STAGES), "mutations": False,
            "confirmations": {"tag": facts["source_commit"], "stable": "computed after prepare"}}


def _receipt(path: Path, *, version: str, commit: str, edition: str,
             platform: str, architecture: str) -> dict[str, Any]:
    raw = path.read_bytes()
    document = json.loads(raw)
    identity = document.get("identity", {})
    expected = {"app_id": APP_ID, "version": version, "build_id": commit,
                "edition": edition, "platform": platform, "architecture": architecture}
    if (document.get("schema") != "satellite.release-candidate/v1"
            or document.get("source_commit") != commit
            or any(identity.get(key) != value for key, value in expected.items())):
        raise ReleaseBlocked(f"candidate receipt identity differs: {path}")
    for row in document.get("artifacts", []):
        artifact = path.parent / row["name"]
        if not artifact.is_file() or (artifact.stat().st_size, sha256_file(artifact)) != (row["size"], row["sha256"]):
            raise ReleaseBlocked(f"candidate artifact differs from receipt: {artifact}")
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "identity": identity,
            "artifacts": document["artifacts"]}


def audit_candidates(run_root: Path, source_root: Path, *, version: str, commit: str) -> dict[str, Any]:
    paths = {
        "windows_user": (run_root / "candidates/windows-user/receipt.json", "user", "win", "x86_64"),
        "windows_ops": (run_root / "candidates/windows-ops/receipt.json", "ops", "win", "x86_64"),
        "macos_user": (source_root / f"release/candidates/{commit}/macos/user/receipt.json", "user", "mac", "arm64"),
        "macos_ops": (source_root / f"release/candidates/{commit}/macos/ops/receipt.json", "ops", "mac", "arm64"),
    }
    return {name: _receipt(path, version=version, commit=commit, edition=edition,
                           platform=platform, architecture=architecture)
            for name, (path, edition, platform, architecture) in paths.items()}


def build_download_request(*, version: str, commit: str, release_id: str,
                           candidates: Mapping[str, Any], public_base_url: str) -> dict[str, Any]:
    base = {"schema": DOWNLOAD_REQUEST_SCHEMA, "app_id": APP_ID, "version": version,
            "source_commit": commit, "public_base_url": public_base_url,
            "users": {
                "windows_x86_64": {"candidate": f"{release_id}/{APP_ID}/user/win/x86_64/{commit}",
                                    "receipt_sha256": candidates["windows_user"]["sha256"]},
                "macos_arm64": {"candidate": f"{release_id}/{APP_ID}/user/mac/arm64/{commit}",
                                "receipt_sha256": candidates["macos_user"]["sha256"]}},
            "ops": {
                "windows_x86_64": {"catalog": "catalog/ops/stable/windows/x86_64.json"},
                "macos_arm64": {"catalog": "catalog/ops/stable/macos/arm64.json"}}}
    return {**base, "request_id": hashlib.sha256(canonical_bytes(base)).hexdigest()}


def submit_download_request(request: Mapping[str, Any], *, config: Path,
                            known_hosts: Path, identity_file: Path,
                            timeout: float = 180.0) -> dict[str, Any]:
    args = argparse.Namespace(config=config, known_hosts=known_hosts, identity_file=identity_file)
    client = _connect(args)
    raw = canonical_bytes(request)
    def verified_result(result_raw: bytes) -> dict[str, Any]:
        result = json.loads(result_raw)
        if (result.get("schema") != DOWNLOAD_RESULT_SCHEMA
                or result.get("request_id") != request["request_id"]
                or result.get("request_sha256") != hashlib.sha256(raw).hexdigest()
                or result.get("state") not in ("published", "reused")):
            raise ReleaseBlocked(f"download publication failed: {result}")
        return result
    try:
        with client.open_sftp() as sftp, tempfile.TemporaryDirectory(prefix="release-download-request-") as directory:
            sftp.get_channel().settimeout(60)
            result_path = STATE_ROOT + "/downloads/results/" + request["request_id"] + ".json"
            existing_result = _read(sftp, result_path)
            if existing_result is not None:
                return verified_result(existing_result)
            local = Path(directory) / (request["request_id"] + ".json")
            local.write_bytes(raw)
            remote = STATE_ROOT + "/downloads/requests/" + local.name
            upload_file(sftp, local, remote, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                result_raw = _read(sftp, result_path)
                if result_raw is not None:
                    return verified_result(result_raw)
                time.sleep(1.0)
    finally:
        client.close()
    raise ReleaseBlocked("server download publication result timed out")


def _range_probe(url: str, start: int, end: int, total: int) -> bytes:
    request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with build_https_opener().open(request, timeout=20) as response:
        expected = f"bytes {start}-{end}/{total}"
        if response.status != 206 or response.headers.get("Content-Range", "") != expected:
            raise ReleaseBlocked(f"HTTPS Range response is invalid: {url}")
        return response.read()


def _small_download(url: str, maximum: int = 1024 * 1024) -> bytes:
    with build_https_opener().open(url, timeout=20) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ReleaseBlocked(f"release index exceeds its bound: {url}")
    return data


def post_verify(result: Mapping[str, Any], *, root: Path, version: str, commit: str,
                cache_root: Path) -> dict[str, Any]:
    config = load_release_config(root / "release.config.json")
    releases = {}
    for platform, architecture, os_version in (("windows", "x86_64", "10.0.19041"),
                                                ("macos", "arm64", "14.0")):
        latest = ReleaseChecker(config, edition=Edition.OPS, cache_dir=cache_root / platform,
                                platform=platform, architecture=architecture,
                                os_version=os_version).fetch_latest()
        if (latest.tag_name != version or latest.identity.get("build_id") != commit
                or latest.installation_mode != "automatic"):
            raise ReleaseBlocked(f"fresh release checker differs for {platform}")
        releases[platform] = {"version": latest.tag_name, "build_id": latest.identity["build_id"],
                              "installation_mode": latest.installation_mode,
                              "sha256": latest.assets[0].sha256, "size": latest.assets[0].size,
                              "delta_count": len(latest.deltas), "catalog_sha256": latest.catalog_sha256}
    ranges = []
    index = json.loads(_small_download(result["release_index"]))
    if (index.get("schema") != "satellite.release-download-index/v1"
            or index.get("version") != version or index.get("source_commit") != commit
            or index.get("artifacts") != result["artifacts"]):
        raise ReleaseBlocked("public release index differs from the server receipt")
    sums = _small_download(result["sha256sums"]).decode("utf-8")
    for row in result["artifacts"]:
        if f"{row['sha256']}  {row['name']}\n" not in sums:
            raise ReleaseBlocked("public SHA256SUMS differs from the server receipt")
        size = row["size"]
        offsets = sorted({0, max(0, size // 2), max(0, size - 1)})
        for offset in offsets:
            data = _range_probe(row["url"], offset, offset, size)
            if len(data) != 1:
                raise ReleaseBlocked("HTTPS Range returned an unexpected byte count")
        ranges.append({"url": row["url"], "size": size, "probes": offsets})
    return {"release_checker": releases, "https_range": ranges}


class ReleaseOrchestrator:
    def __init__(self, *, root: Path, state_root: Path, version: str,
                 confirm_tag: str = "", confirm_stable: str = "",
                 github_run_id: str = "", runner: Callable[..., str] = _run):
        self.root, self.state_root, self.version = Path(root), Path(state_root), version
        self.confirm_tag, self.confirm_stable = confirm_tag, confirm_stable
        self.github_run_id, self.runner = github_run_id, runner
        self.facts = inspect_preflight(self.root, version)
        self.commit, self.tag = self.facts["source_commit"], self.facts["tag"]
        self.run_root = self.state_root / self.tag
        self.journal = ReleaseJournal.open(self.run_root / "release-run.json", version=version, commit=self.commit)
        self.publish_python = Path.home() / ".config/satellite-release/venv/bin/python"
        self.publish_config = Path.home() / ".config/satellite-release/publish.json"
        self.known_hosts = Path.home() / ".config/satellite-release/known_hosts"
        self.publish_key = Path.home() / ".config/satellite-release/publish_ed25519"

    def _exec_stage(self, name: str, action: Callable[[], Mapping[str, Any]]) -> None:
        if self.journal.passed(name):
            return
        self.journal.mark(name, "RUNNING")
        try:
            evidence = dict(action())
            self.journal.mark(name, "PASSED", **evidence)
        except ConfirmationRequired as error:
            self.journal.mark(name, "WAITING_APPROVAL", required=error.value,
                              confirmation_evidence=error.details)
            raise
        except Exception as error:
            self.journal.mark(name, "FAILED", error=str(error))
            raise

    def run(self) -> dict[str, Any]:
        self._exec_stage("preflight", self._preflight)
        self._exec_stage("tag", self._tag)
        self._exec_stage("windows_candidate", self._windows_candidate)
        self._exec_stage("macos_candidate", self._macos_candidate)
        self._exec_stage("candidate_audit", self._candidate_audit)
        self._exec_stage("prepare_publication", self._prepare_publication)
        self._exec_stage("stable_confirmation", self._stable_confirmation)
        self._exec_stage("commit_publication", self._commit_publication)
        self._exec_stage("publish_downloads", self._publish_downloads)
        self._exec_stage("post_verify", self._post_verify)
        self._exec_stage("evidence", self._evidence)
        return self.journal.data

    def _preflight(self) -> Mapping[str, Any]:
        facts = inspect_preflight(self.root, self.version)
        if facts["blockers"]:
            raise ReleaseBlocked("; ".join(facts["blockers"]))
        python = self.root / ".venv/bin/python"
        commands = [
            [str(python), "-m", "pytest", "satellite_debug_tool/tests", "-q"],
            [str(python), "scripts/update_translations.py", "check"],
            [str(python), "-m", "compileall", "-q", "satellite_debug_tool", "scripts"],
            ["git", "diff", "--check"],
        ]
        for command in commands:
            self.runner(command, cwd=self.root)
        verified = inspect_preflight(self.root, self.version)
        if verified["source_commit"] != self.commit or verified["blockers"]:
            raise ReleaseBlocked("source snapshot changed during release preflight")
        return verified

    def _tag(self) -> Mapping[str, Any]:
        existing = self.facts.get("tag_commit")
        remote = ""
        try:
            remote = _git(self.root, "ls-remote", "github", f"refs/tags/{self.tag}^{{}}")
            if remote:
                remote = remote.split()[0]
        except subprocess.CalledProcessError:
            pass
        if remote and remote != self.commit:
            raise ReleaseBlocked("remote release tag points to another commit")
        if existing and remote == self.commit:
            return {"tag": self.tag, "commit": existing, "reused": True}
        if self.confirm_tag != self.commit:
            raise ConfirmationRequired("tag", self.commit)
        if not existing:
            self.runner(["git", "tag", "-a", self.tag, "-m", self.tag], cwd=self.root)
        self.runner(["git", "push", "github", "HEAD:master"], cwd=self.root)
        self.runner(["git", "push", "github", self.tag], cwd=self.root)
        return {"tag": self.tag, "commit": self.commit, "reused": bool(existing)}

    def _windows_candidate(self) -> Mapping[str, Any]:
        run_id = self.github_run_id
        if not run_id:
            output = self.runner(["gh", "run", "list", "--repo", "xnn0703/satellite_debug_tool",
                "--workflow", "build.yml", "--commit", self.commit, "--limit", "10",
                "--json", "databaseId,headSha,headBranch,status,conclusion,url"], cwd=self.root, capture=True)
            rows = [row for row in json.loads(output)
                    if row.get("headSha") == self.commit and row.get("headBranch") == self.tag]
            if not rows:
                raise ReleaseBlocked("tag workflow is not visible yet; resume after GitHub creates the run")
            run_id = str(rows[0]["databaseId"])
        self.runner(["gh", "run", "watch", run_id, "--repo", "xnn0703/satellite_debug_tool", "--exit-status"], cwd=self.root)
        detail = json.loads(self.runner(["gh", "run", "view", run_id, "--repo", "xnn0703/satellite_debug_tool",
            "--json", "conclusion,status,url,headSha,headBranch"], cwd=self.root, capture=True))
        if (detail.get("conclusion") != "success" or detail.get("headSha") != self.commit
                or detail.get("headBranch") != self.tag):
            raise ReleaseBlocked("Windows Actions result differs from the release commit")
        return {"run_id": int(run_id), **detail}

    def _macos_candidate(self) -> Mapping[str, Any]:
        python = Path.home() / ".config/satellite-release/build-venv-3.11/bin/python"
        self.runner([str(python), "scripts/release_macos.py", "--source-commit", self.commit,
            "--version", self.version, "--edition", "both", "--release-id", self.commit,
            "--config", str(Path.home() / ".config/satellite-release/upload.json"),
            "--known-hosts", str(self.known_hosts),
            "--identity-file", str(Path.home() / ".config/satellite-release/upload_ed25519")], cwd=self.root)
        return {"user_receipt": str(self.root / f"release/candidates/{self.commit}/macos/user/receipt.json"),
                "ops_receipt": str(self.root / f"release/candidates/{self.commit}/macos/ops/receipt.json")}

    def _promote(self, *arguments: str, capture: bool = False) -> str:
        return self.runner([str(self.publish_python), "scripts/promote_release.py", *arguments,
            "--config", str(self.publish_config), "--known-hosts", str(self.known_hosts),
            "--identity-file", str(self.publish_key)], cwd=self.root, capture=capture)

    def _windows_remote(self, edition: str) -> str:
        return f"/srv/satellite-updates/incoming/{self.commit}/{APP_ID}/{edition}/win/x86_64/{self.commit}"

    def _candidate_audit(self) -> Mapping[str, Any]:
        for edition in ("user", "ops"):
            output = self.run_root / f"candidates/windows-{edition}"
            if not (output / "receipt.json").is_file():
                output.parent.mkdir(parents=True, exist_ok=True)
                self._promote("fetch", "--remote-candidate", self._windows_remote(edition), "--output", str(output))
        candidates = audit_candidates(self.run_root, self.root, version=self.version, commit=self.commit)
        self.journal.data["evidence"]["candidates"] = candidates
        self.journal.save()
        return {"receipts": {name: value["sha256"] for name, value in candidates.items()}}

    def _prepare_publication(self) -> Mapping[str, Any]:
        notes = self.root / f"doc/v{self.version}_release_notes.md"
        output = self.run_root / "publications/ops"
        manifest = output / "publication.json"
        if not manifest.is_file():
            output.parent.mkdir(parents=True, exist_ok=True)
            self._promote("prepare", "--repository", str(Path.home() / ".config/satellite-release/repository"),
                "--key-dir", str(Path.home() / ".config/satellite-release/keys"),
                "--candidate", str(self.run_root / "candidates/windows-ops"), "--min-os", "10.0.19041",
                "--candidate", str(self.root / f"release/candidates/{self.commit}/macos/ops"), "--min-os", "14.0",
                "--expected-commit", self.commit, "--output", str(output), "--public-base-url",
                "https://60.205.157.141/v1/apps/satellite-terminal-tool/",
                "--channel", "stable", "--notes-file", str(notes))
        digest = sha256_file(manifest)
        self.journal.data["evidence"]["publication"] = str(manifest)
        self.journal.data["evidence"]["publication_sha256"] = digest
        self.journal.save()
        return {"publication_sha256": digest}

    def _stable_confirmation(self) -> Mapping[str, Any]:
        expected = self.journal.data["evidence"]["publication_sha256"]
        if self.confirm_stable != expected:
            candidates = self.journal.data["evidence"].get("candidates", {})
            packages = {
                name: {"identity": row["identity"], "artifacts": row["artifacts"]}
                for name, row in sorted(candidates.items())
            }
            raise ConfirmationRequired("stable", expected, details={
                "source_commit": self.commit,
                "publication_sha256": expected,
                "packages": packages,
            })
        return {"publication_sha256": expected}

    def _commit_publication(self) -> Mapping[str, Any]:
        manifest = self.journal.data["evidence"]["publication"]
        if sha256_file(Path(manifest)) != self.journal.data["evidence"]["publication_sha256"]:
            raise ReleaseBlocked("publication differs from the approved digest")
        result = json.loads(self._promote("commit", "--bundle", manifest, capture=True))
        previous = self.journal.data["evidence"].get("publication_result", {})
        if result.get("reused") and previous.get("bundle_id") == result.get("bundle_id"):
            result = {**previous, **result}
        self.journal.data["evidence"]["publication_result"] = result
        self.journal.save()
        return {"publication_result": result}

    def _publish_downloads(self) -> Mapping[str, Any]:
        candidates = self.journal.data["evidence"]["candidates"]
        request = build_download_request(version=self.version, commit=self.commit, release_id=self.commit,
            candidates=candidates, public_base_url="https://60.205.157.141/downloads/")
        result = submit_download_request(request, config=self.publish_config,
                                         known_hosts=self.known_hosts, identity_file=self.publish_key)
        self.journal.data["evidence"]["download_result"] = result
        self.journal.save()
        return {"request_id": request["request_id"], "publication_state": result["state"],
                "release_index": result["release_index"]}

    def _post_verify(self) -> Mapping[str, Any]:
        verification = post_verify(self.journal.data["evidence"]["download_result"], root=self.root,
            version=self.version, commit=self.commit, cache_root=self.run_root / "fresh-checker")
        self.journal.data["evidence"]["post_verify"] = verification
        self.journal.save()
        return verification

    def _evidence(self) -> Mapping[str, Any]:
        evidence = {"schema": "satellite.release-validation/v1", "version": self.version,
            "source_commit": self.commit, "tag": self.tag,
            "github_actions": self.journal.stage("windows_candidate"),
            "publication_result": self.journal.data["evidence"]["publication_result"],
            "downloads": self.journal.data["evidence"]["download_result"],
            "post_verify": self.journal.data["evidence"]["post_verify"],
            "boundaries": {"windows_real_install_and_rollback": "NOT_RUN",
                           "apple_notarization": "NOT_RUN",
                           "physical_device_rf_power_motion": "NOT_RUN"}}
        output = self.run_root / "release-validation.json"
        _atomic_json(output, evidence)
        return {"validation": str(output), "sha256": sha256_file(output)}


def _print(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run", "resume", "status", "verify"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--state-root", type=Path, default=Path.home() / ".config/satellite-release/runs")
    parser.add_argument("--confirm-tag", default="")
    parser.add_argument("--confirm-stable", default="")
    parser.add_argument("--github-run-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "plan" or args.dry_run:
        _print(release_plan(args.root, args.version, args.state_root))
        return 0
    journal_path = args.state_root / ("v" + args.version) / "release-run.json"
    if args.command == "status":
        if not journal_path.is_file():
            raise ReleaseBlocked("release journal does not exist")
        _print(ReleaseJournal.load(journal_path, version=args.version).data)
        return 0
    validate_publish_runtime()
    with release_run_lock(args.state_root / ("v" + args.version) / "release-run.lock"):
        orchestrator = ReleaseOrchestrator(root=args.root, state_root=args.state_root, version=args.version,
            confirm_tag=args.confirm_tag, confirm_stable=args.confirm_stable, github_run_id=args.github_run_id)
        if args.command == "verify":
            orchestrator.journal.mark("post_verify", "PENDING")
            orchestrator.journal.mark("evidence", "PENDING")
            orchestrator._exec_stage("post_verify", orchestrator._post_verify)
            orchestrator._exec_stage("evidence", orchestrator._evidence)
            _print(orchestrator.journal.data)
            return 0
        _print(orchestrator.run())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfirmationRequired as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(3)
    except (ReleaseBlocked, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
