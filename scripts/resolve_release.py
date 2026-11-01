"""发布输入只解析已存在的 SemVer tag，并冻结其确切 commit。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from satellite_debug_tool.versioning import parse_version


def resolve(tag: str, root: Path = ROOT) -> dict[str, str]:
    if not re.fullmatch(r"v[0-9A-Za-z.+-]{1,100}", tag):
        raise ValueError("release requires a v-prefixed version tag")
    parse_version(tag, complete=True)
    commit = subprocess.check_output(["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
                                     cwd=root, text=True).strip()
    source = subprocess.check_output(["git", "show", f"{commit}:satellite_debug_tool/__init__.py"],
                                     cwd=root, text=True)
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    if match is None or tag != "v" + match.group(1):
        raise ValueError("tag differs from its source version")
    return {"tag": tag, "commit": commit}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default=os.environ.get("RELEASE_TAG", ""))
    args = parser.parse_args()
    result = resolve(args.tag)
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with Path(destination).open("a", encoding="utf-8") as output:
            for key, value in result.items():
                output.write(f"{key}={value}\n")
    print(result)


if __name__ == "__main__":
    main()
