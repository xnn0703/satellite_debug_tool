#!/usr/bin/env python3
"""本地静态仓库签名发布 CLI；远端上传与渠道审批由外部流程持有。"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from satellite_debug_tool.updater.publishing import (  # noqa: E402
    initialize_repository, load_signers, promotion_lock, publish_targets, publish_candidate, refresh_metadata, refresh_online_metadata, rotate_root,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "publish", "publish-candidate", "refresh", "refresh-timestamp", "refresh-snapshot", "rotate-root"))
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--key-dir", required=True, type=Path, help="独立私钥目录；root/targets/snapshot/timestamp.pem")
    parser.add_argument("--old-key-dir", type=Path)
    parser.add_argument("--targets", type=Path, help="JSON 对象：逻辑 target 路径映射到本地文件路径")
    parser.add_argument("--app-id", default="satellite-terminal-tool")
    parser.add_argument("--repo-id", default="satellite-terminal-tool-production")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--expected-commit")
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    parser.add_argument("--min-os")
    parser.add_argument("--min-updater-version", default="1.0.0")
    parser.add_argument("--config-schema-min", type=int, default=1)
    parser.add_argument("--config-schema-max", type=int, default=1)
    parser.add_argument("--notes-file", type=Path)
    parser.add_argument("--manual-root", type=Path)
    parser.add_argument("--public-target-base-url", help="ops 正式 HTTPS target 基址，发布前实际流式回验")
    parser.add_argument("--promotion-lock", type=Path, help="服务器续签必须与外部 SFTP 晋升共用的排他锁路径")
    args = parser.parse_args(argv)
    if args.command == "init":
        result = initialize_repository(args.repository, load_signers(args.key_dir), app_id=args.app_id, repo_id=args.repo_id)
    elif args.command == "rotate-root":
        if args.old_key_dir is None:
            parser.error("rotate-root requires --old-key-dir")
        result = rotate_root(args.repository, load_signers(args.old_key_dir, ("root",))["root"], load_signers(args.key_dir))
    elif args.command in ("refresh-timestamp", "refresh-snapshot"):
        snapshot = args.command == "refresh-snapshot"
        roles = ("snapshot", "timestamp") if snapshot else ("timestamp",)
        with promotion_lock(args.promotion_lock) if args.promotion_lock else nullcontext():
            result = refresh_online_metadata(args.repository, load_signers(args.key_dir, roles), refresh_snapshot=snapshot)
    elif args.command == "publish-candidate":
        if args.candidate is None or args.expected_commit is None or args.min_os is None:
            parser.error("publish-candidate requires --candidate, --expected-commit and --min-os")
        result = publish_candidate(args.repository, args.candidate,
            load_signers(args.key_dir, ("targets", "snapshot", "timestamp")),
            expected_commit=args.expected_commit, channel=args.channel, min_os=args.min_os,
            min_updater_version=args.min_updater_version, config_schema_min=args.config_schema_min,
            config_schema_max=args.config_schema_max,
            notes=args.notes_file.read_text(encoding="utf-8") if args.notes_file else "",
            manual_root=args.manual_root, public_target_base_url=args.public_target_base_url)
    else:
        signers = load_signers(args.key_dir, ("targets", "snapshot", "timestamp"))
        if args.command == "refresh":
            result = refresh_metadata(args.repository, signers)
        else:
            if args.targets is None:
                parser.error("publish requires --targets")
            entries = json.loads(args.targets.read_text(encoding="utf-8"))
            if not isinstance(entries, dict):
                parser.error("--targets requires a JSON object")
            paths = {name: (args.targets.parent / path).resolve() for name, path in entries.items()}
            result = publish_targets(args.repository, paths, signers)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
