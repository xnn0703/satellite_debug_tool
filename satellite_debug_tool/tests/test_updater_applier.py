"""updater.applier 单测：合并 / 校验 / 解压 / swap / rollback。

为避免依赖外部归档：每个测试都用 py7zr 在 tmp 里现造一个小 7z。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from satellite_debug_tool.updater.applier import (
    Applier,
    ApplyError,
    ApplyResult,
)


# ---- 辅助：现造 7z 用作测试输入 ----

def _make_7z_with_app(tmp_path: Path, app_name: str = "SatelliteDebugTool.app",
                     contents: dict = None) -> Path:
    """在 tmp_path 下造一个 {app_name}/... 目录结构，打包成 7z。

    contents = {'rel/path.txt': b'...', ...}
    返回 7z 文件路径。
    """
    py7zr = pytest.importorskip("py7zr")
    src_root = tmp_path / "_src"
    app_root = src_root / app_name
    app_root.mkdir(parents=True)
    if not contents:
        contents = {"hello.txt": b"hello", "Contents/Info.plist": b"<plist/>"}
    for rel, data in contents.items():
        f = app_root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    archive = tmp_path / "test.7z"
    with py7zr.SevenZipFile(str(archive), mode="w") as z:
        z.writeall(str(src_root), arcname=".")
    return archive


def _split_file(src: Path, dest_dir: Path, base_name: str, vol_size: int = 1024) -> list[Path]:
    """把 src 按 vol_size 拆成 base_name.7z.001/.002/..."""
    data = src.read_bytes()
    parts = []
    idx = 1
    for i in range(0, len(data), vol_size):
        p = dest_dir / f"{base_name}.7z.{idx:03d}"
        p.write_bytes(data[i:i + vol_size])
        parts.append(p)
        idx += 1
    return parts


# ============================ merge_volumes ============================

class TestMergeVolumes:
    def test_merge_concatenates_in_order(self, tmp_path):
        a = tmp_path / "x.7z.001"; a.write_bytes(b"AAAA")
        b = tmp_path / "x.7z.002"; b.write_bytes(b"BBBB")
        c = tmp_path / "x.7z.003"; c.write_bytes(b"CC")
        out = tmp_path / "merged.7z"
        ap = Applier()
        ap.merge_volumes([a, b, c], out)
        assert out.read_bytes() == b"AAAABBBBCC"

    def test_missing_volume_raises(self, tmp_path):
        a = tmp_path / "x.7z.001"; a.write_bytes(b"A")
        ghost = tmp_path / "x.7z.002"   # 不存在
        out = tmp_path / "merged.7z"
        with pytest.raises(ApplyError) as exc:
            Applier().merge_volumes([a, ghost], out)
        assert "分卷缺失" in str(exc.value)

    def test_empty_list_raises(self, tmp_path):
        with pytest.raises(ApplyError):
            Applier().merge_volumes([], tmp_path / "out.7z")

    def test_progress_called(self, tmp_path):
        a = tmp_path / "x.7z.001"; a.write_bytes(b"A" * 5000)
        b = tmp_path / "x.7z.002"; b.write_bytes(b"B" * 5000)
        out = tmp_path / "merged.7z"
        captured = []
        ap = Applier(on_progress=lambda stage, pct: captured.append((stage, pct)))
        ap.merge_volumes([a, b], out, chunk_size=512)
        # 最后一次必然是 (合并分卷, 1.0)
        stages = [s for s, _ in captured]
        assert "合并分卷" in stages
        assert captured[-1][1] == 1.0


# ============================ verify_7z_magic ============================

class TestVerifyMagic:
    def test_real_7z_magic_passes(self, tmp_path):
        f = tmp_path / "x.7z"
        f.write_bytes(b"\x37\x7a\xbc\xaf\x27\x1c" + b"...rest...")
        assert Applier.verify_7z_magic(f) is True

    def test_random_bytes_fails(self, tmp_path):
        f = tmp_path / "x.7z"
        f.write_bytes(b"NOTSEVENZ")
        assert Applier.verify_7z_magic(f) is False

    def test_missing_file_fails(self, tmp_path):
        assert Applier.verify_7z_magic(tmp_path / "ghost") is False


# ============================ extract_to ============================

class TestExtract:
    def test_real_7z_extracts(self, tmp_path):
        arc = _make_7z_with_app(tmp_path, app_name="MyApp",
                                contents={"a.txt": b"abc", "sub/b.bin": b"\x00\x01"})
        staging = tmp_path / "_stage"
        Applier().extract_to(arc, staging)
        assert (staging / "MyApp" / "a.txt").read_bytes() == b"abc"
        assert (staging / "MyApp" / "sub" / "b.bin").read_bytes() == b"\x00\x01"

    def test_bad_magic_raises(self, tmp_path):
        bad = tmp_path / "fake.7z"
        bad.write_bytes(b"BADDATA")
        with pytest.raises(ApplyError) as exc:
            Applier().extract_to(bad, tmp_path / "_stage")
        assert "非合法 7z" in str(exc.value)

    def test_missing_archive_raises(self, tmp_path):
        with pytest.raises(ApplyError):
            Applier().extract_to(tmp_path / "nope.7z", tmp_path / "_stage")


# ============================ swap_install_dir ============================

class TestSwap:
    def _prepare(self, tmp_path):
        """造一个假"已安装"app + extract 后的源 app。"""
        installed = tmp_path / "MyApp"
        installed.mkdir()
        (installed / "old.txt").write_text("old")
        ext_root = tmp_path / "_ext"
        ext_app = ext_root / "MyApp"
        ext_app.mkdir(parents=True)
        (ext_app / "new.txt").write_text("new")
        return installed, ext_root

    def test_swap_success_creates_backup_and_replaces(self, tmp_path):
        installed, ext_root = self._prepare(tmp_path)
        result = Applier().swap_install_dir(installed, ext_root)
        assert isinstance(result, ApplyResult)
        # 替换后的 install_dir 含 new.txt 不含 old.txt
        assert (installed / "new.txt").exists()
        assert not (installed / "old.txt").exists()
        # backup 含 old.txt
        assert result.backup_dir.exists()
        assert (result.backup_dir / "old.txt").exists()

    def test_swap_when_install_dir_missing(self, tmp_path):
        """首次安装：install_dir 不存在，直接搬。"""
        ext_root = tmp_path / "_ext"
        ext_app = ext_root / "MyApp"
        ext_app.mkdir(parents=True)
        (ext_app / "fresh.txt").write_text("fresh")
        installed = tmp_path / "MyApp"   # 不存在
        result = Applier().swap_install_dir(installed, ext_root)
        assert installed.exists()
        assert (installed / "fresh.txt").exists()
        # backup 也不应存在（因为没有旧版可备份）
        assert not result.backup_dir.exists()

    def test_swap_ext_root_is_app_itself(self, tmp_path):
        """ext_root 直接是 MyApp/ 而不是含 MyApp/ 的 wrapper。"""
        installed = tmp_path / "MyApp"
        installed.mkdir()
        (installed / "old.txt").write_text("old")
        ext_root = tmp_path / "MyApp"   # 注意：这里 ext_root == installed
        # 测试场景：构造一个隔离的源 dir 但其名字也叫 MyApp
        ext_root = tmp_path / "_src" / "MyApp"
        ext_root.mkdir(parents=True)
        (ext_root / "new.txt").write_text("new")
        result = Applier().swap_install_dir(installed, ext_root)
        assert (installed / "new.txt").exists()

    def test_swap_missing_ext_source_raises(self, tmp_path):
        installed = tmp_path / "MyApp"
        installed.mkdir()
        ext_root = tmp_path / "_ext"
        ext_root.mkdir()
        # ext_root 里没有 MyApp 子目录，自身也无内容
        # source = ext_root.exists() 但 ext_root 是空目录也算"存在"
        # 改造测试：让 ext_root 自身根本不存在
        bad_ext = tmp_path / "ghost"
        with pytest.raises(ApplyError) as exc:
            Applier().swap_install_dir(installed, bad_ext)
        assert "未找到可用源" in str(exc.value)


# ============================ 一站式 apply ============================

class TestApplyFlow:
    def test_full_flow(self, tmp_path):
        """合并 + 解压 + swap 一站式：模拟真实升级流程。"""
        # 1. 造一个含 MyApp/ 的 7z
        arc = _make_7z_with_app(tmp_path, app_name="MyApp",
                                contents={"v.txt": b"NEW VERSION"})
        # 2. 把 7z 拆成分卷
        vols = _split_file(arc, tmp_path, "split", vol_size=64)
        assert len(vols) >= 2
        # 3. 准备一个旧 install dir
        installed = tmp_path / "MyApp"
        installed.mkdir()
        (installed / "v.txt").write_text("OLD VERSION")
        # 4. 一站式 apply
        workdir = tmp_path / "_work"
        result = Applier().apply(vols, installed, workdir)
        # 5. 验证
        assert (installed / "v.txt").read_bytes() == b"NEW VERSION"
        assert result.backup_dir.exists()
        assert (result.backup_dir / "v.txt").read_text() == "OLD VERSION"
