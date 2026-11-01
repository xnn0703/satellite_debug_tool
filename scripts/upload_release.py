"""只向 web root 外的候选区上传；纯 SFTP，固定主机密钥校验，按摘要幂等。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

CANDIDATE_ROOT = "/srv/satellite-updates/incoming"
COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")


def component(value: str) -> str:
    if not isinstance(value, str) or not COMPONENT.fullmatch(value) or value in (".", ".."):
        raise ValueError("candidate path component is invalid")
    return value


def candidate_directory(release_id: str, identity: dict) -> str:
    parts = [release_id, *(identity[key] for key in ("app_id", "edition", "platform", "architecture", "build_id"))]
    return str(PurePosixPath(CANDIDATE_ROOT).joinpath(*(component(part) for part in parts)))


def _attributes(sftp, path: str):
    try:
        return sftp.lstat(path)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        raise


def ensure_directory(sftp, destination: str) -> None:
    root = PurePosixPath(CANDIDATE_ROOT)
    target = PurePosixPath(destination)
    if target != root and root not in target.parents:
        raise ValueError("upload destination must be inside the fixed candidate root")
    current = PurePosixPath("/")
    for part in target.parts[1:]:
        current /= component(part)
        attributes = _attributes(sftp, str(current))
        if attributes is None:
            if current == root or root in current.parents:
                sftp.mkdir(str(current), mode=0o750)
            else:
                raise ValueError("server candidate parent directory must already exist")
        elif not stat.S_ISDIR(attributes.st_mode):
            raise ValueError("candidate directory requires real directories")


def _remote_digest(sftp, path: str, *, limit: int | None = None) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with sftp.open(path, "rb") as stream:
        file_size = stream.stat().st_size
        stream.prefetch(file_size=file_size if limit is None else min(file_size, limit), max_concurrent_requests=32)
        while limit is None or size < limit:
            block = stream.read(1024 * 1024 if limit is None else min(1024 * 1024, limit - size))
            if not block:
                break
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def _local_digest(path: Path, *, limit: int | None = None) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while limit is None or size < limit:
            block = stream.read(1024 * 1024 if limit is None else min(1024 * 1024, limit - size))
            if not block:
                break
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def upload_file(sftp, local: Path, remote: str, *, size: int, sha256: str) -> str:
    if _local_digest(local) != (size, sha256):
        raise ValueError("local candidate differs from its receipt")
    current = _attributes(sftp, remote)
    if current is not None:
        if not stat.S_ISREG(current.st_mode) or _remote_digest(sftp, remote) != (size, sha256):
            raise ValueError("immutable remote candidate has different bytes")
        return "reused"
    partial = remote + ".part"
    attributes = _attributes(sftp, partial)
    offset = 0
    if attributes is not None:
        if not stat.S_ISREG(attributes.st_mode) or attributes.st_size > size:
            raise ValueError("candidate partial file is invalid")
        offset = attributes.st_size
        if _remote_digest(sftp, partial, limit=offset) != _local_digest(local, limit=offset):
            raise ValueError("candidate partial bytes differ from the receipt input")
    with sftp.open(partial, "r+b" if attributes is not None else "wx") as output:
        output.seek(offset)
        # 连续发送写请求；文件关闭后逐字节回读校验，再提交名称。
        output.set_pipelined(True)
        with local.open("rb") as source:
            source.seek(offset)
            while block := source.read(1024 * 1024):
                output.write(block)
        output.flush()
    if _remote_digest(sftp, partial) != (size, sha256):
        raise ValueError("remote candidate failed streamed size and SHA-256 verification")
    # SFTP rename 的标准语义要求目标不存在；禁止 posix_rename 覆盖已存在候选。
    sftp.rename(partial, remote)
    return "resumed" if offset else "uploaded"


def upload_candidate(sftp, receipt_path: Path, release_id: str) -> dict:
    """本地进程锁与不可变 receipt owner 让 CI 重试可恢复远端中断。"""
    receipt_path = receipt_path.resolve(strict=True)
    with _local_owner(receipt_path) as owner:
        return _upload_candidate(sftp, receipt_path, release_id, owner)


@contextmanager
def _local_owner(receipt_path: Path):
    lock = receipt_path.parent / ".upload-local-lock"
    if lock.is_symlink():
        raise ValueError("upload ownership files require regular local files")
    with lock.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield _local_digest(receipt_path)[1].encode("ascii")
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _upload_candidate(sftp, receipt_path: Path, release_id: str, owner: bytes) -> dict:
    receipt_path = receipt_path.resolve(strict=True)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("schema") != "satellite.release-candidate/v1":
        raise ValueError("unsupported candidate receipt")
    identity = receipt["identity"]
    if (identity.get("build_id") != receipt.get("source_commit")
            or re.fullmatch(r"[0-9a-f]{40}", str(identity.get("build_id", ""))) is None):
        raise ValueError("candidate source identity differs from its receipt")
    if (identity.get("edition") not in ("user", "ops") or identity.get("platform") not in ("win", "mac")
            or identity.get("architecture") not in ("x86_64", "arm64")):
        raise ValueError("candidate identity is invalid")
    rows = receipt.get("artifacts")
    if not isinstance(rows, list) or not rows:
        raise ValueError("candidate receipt requires complete artifacts")
    names = set()
    files = []
    for row in rows:
        name = component(row["name"])
        if name == "receipt.json" or name in names or not HASH.fullmatch(str(row.get("sha256", ""))):
            raise ValueError("candidate receipt artifact is invalid")
        if type(row.get("size")) is not int or row["size"] < 0:
            raise ValueError("candidate receipt artifact size is invalid")
        names.add(name)
        path = receipt_path.parent / name
        if path.is_symlink() or _local_digest(path) != (row["size"], row["sha256"]):
            raise ValueError("candidate artifact differs from its receipt")
        files.append((path, row["size"], row["sha256"]))
    archives = [name for name in names if name.endswith(".7z" if identity["platform"] == "win" else ".zip")
                and name != "delta.zip"]
    if len(archives) != 1 or (identity["platform"] == "win" and "payload-manifest.json" not in names):
        raise ValueError("candidate requires a complete platform package and its manifest")
    directory = candidate_directory(release_id, identity)
    ensure_directory(sftp, directory)
    lock_path = directory + "/.upload-lock"
    attributes = _attributes(sftp, lock_path)
    if attributes is None:
        with sftp.open(lock_path, "wx") as lock:
            lock.write(owner)
    elif stat.S_ISREG(attributes.st_mode) and attributes.st_size == len(owner):
        with sftp.open(lock_path, "rb") as lock:
            if lock.read(len(owner) + 1) != owner:
                raise FileExistsError("candidate has another upload owner")
    else:
        raise FileExistsError("candidate has another upload owner")
    try:
        results = {}
        for path, size, digest in files:
            results[path.name] = upload_file(sftp, path, directory + "/" + path.name, size=size, sha256=digest)
        size, digest = _local_digest(receipt_path)
        if digest.encode("ascii") != owner:
            raise ValueError("candidate receipt changed during upload")
        results["receipt.json"] = upload_file(sftp, receipt_path, directory + "/receipt.json", size=size, sha256=digest)
        return {"directory": directory, "files": results, "state": "candidate-uploaded"}
    finally:
        sftp.remove(lock_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--identity-file", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    host = config.get("host", "")
    username = config.get("username", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", host):
        raise ValueError("SSH hostname is invalid")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
        raise ValueError("SSH username is invalid")
    port = config.get("port", 22)
    if type(port) is not int or not 1 <= port <= 65535 or config.get("candidate_root") != CANDIDATE_ROOT:
        raise ValueError("SSH port or candidate root is invalid")
    component(args.release_id)
    import paramiko
    client = paramiko.SSHClient()
    client.load_host_keys(str(args.known_hosts.resolve(strict=True)))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(host, port=port, username=username,
                       key_filename=str(args.identity_file.resolve(strict=True)),
                       look_for_keys=False, allow_agent=False, timeout=20, banner_timeout=20, auth_timeout=20)
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(60)
            print(json.dumps(upload_candidate(sftp, args.receipt, args.release_id)))
    finally:
        client.close()


if __name__ == "__main__":
    main()
