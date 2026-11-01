"""本地公开元数据/证书到期检查；只读文件，结果写 stdout/stderr 交给 journal。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import ssl
import subprocess


def metadata_expiry(path: Path) -> datetime:
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("metadata exceeds the local health-check size limit")
    value = json.loads(path.read_text(encoding="utf-8"))["signed"]["expires"]
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("metadata expiry requires a timezone")
    return result.astimezone(timezone.utc)


def certificate_expiry(path: Path) -> datetime:
    result = subprocess.run(["/usr/bin/openssl", "x509", "-in", str(path), "-noout", "-enddate"],
                            check=True, capture_output=True, text=True, timeout=15)
    if not result.stdout.startswith("notAfter="):
        raise ValueError("certificate expiry readout is invalid")
    timestamp = ssl.cert_time_to_seconds(result.stdout.strip().split("=", 1)[1])
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def check(repository: Path, certificate: Path, *, now: datetime | None = None) -> tuple[int, dict]:
    now = now or datetime.now(timezone.utc)
    records = []
    # 六天左右的 IP 证书提前两天提示；root/targets 需要独立离线授权续签。
    inputs = [(role, repository / "metadata" / f"{role}.json", days, metadata_expiry)
              for role, days in (("root", 30), ("targets", 14), ("snapshot", 7), ("timestamp", 2))]
    inputs.append(("certificate", certificate, 2, certificate_expiry))
    code = 0
    for name, path, warning_days, reader in inputs:
        try:
            expiry = reader(path)
            seconds = (expiry - now).total_seconds()
            state = "expired" if seconds <= 0 else "renewal_due" if seconds <= warning_days * 86400 else "valid"
            code = max(code, 2 if state == "expired" else 1 if state == "renewal_due" else 0)
            records.append({"name": name, "path": str(path), "state": state,
                            "expires": expiry.isoformat(), "remaining_hours": round(seconds / 3600, 2),
                            "warning_days": warning_days})
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            code = 2
            records.append({"name": name, "path": str(path), "state": "inspection_failed", "detail": str(error)})
    return code, {"schema": "satellite.publication-health/v1", "checked_at": now.isoformat(),
                  "state": "valid" if code == 0 else "action_required", "checks": records}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--certificate", type=Path, required=True)
    args = parser.parse_args()
    code, proof = check(args.repository, args.certificate)
    print(json.dumps(proof, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
