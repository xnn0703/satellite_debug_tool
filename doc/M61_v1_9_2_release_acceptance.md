# M61 v1.9.2 发行验收标准

日期：2026-11-03。状态：用户已确认执行。

| ID | 验收要求 |
| --- | --- |
| A01 | `__version__`、源码提交、四包身份和 `v1.9.2` tag 完全一致，tag 在发布前不存在且发布后保持不可变。 |
| A02 | 当前工作区改动均完成范围审计，无密钥、下载缓存、构建目录或临时文件进入提交。 |
| A03 | 全量 pytest、翻译 check、compileall、`git diff --check` 和 user/ops 源码 smoke 通过。 |
| A04 | Windows tag Actions 的 user/ops 测试、冻结 smoke、身份/归档校验及 ECS 候选上传全部成功。 |
| A05 | macOS arm64 user/ops 使用同一 tag SHA 构建，原生冻结 smoke、ad-hoc codesign、身份和归档校验通过并上传候选。 |
| A06 | 四份完整包的 edition、version、platform、architecture、build ID、源码 SHA、大小和 SHA-256 与候选回执一致。 |
| A07 | user 包只进入人工下载目录，不进入应用更新目录；ops 包进入签名 TUF stable 目录。 |
| A08 | ops 正式晋升由 ECS 本机完整 SHA-256 回执准入，外部 Range 与同偏移服务器字节一致，最后才切换 timestamp。 |
| A09 | fresh checker 分别接受 Windows/macOS v1.9.2 目录；macOS target 声明 automatic，Windows保持 automatic，user保持manual。 |
| A10 | `/downloads/v1.9.2/release.json`、`SHA256SUMS.txt` 和四个完整包可访问；归档 CRC、包内身份及公开 SHA-256 复核通过。 |
| A11 | M57 桥接边界明确：v1.9.0/v1.9.1 Mac ops 到 v1.9.2 需最后一次人工替换，v1.9.2 之后才具备应用内自动安装。 |
| A12 | Windows 签名、Apple notarization/x86_64、Windows 实机升级、设备/RF/供电/摇摆台现场结果保持独立未外推。 |
