# M56 服务器侧发布复核开发记录

日期：2026-11-03。

## 开始前证据

- v1.9.1 已证明候选归档、TUF 目标和客户端 SHA-256 校验可以阻止字节不一致。
- 旧 `promote_release.py::_probe_public` 从发布机通过实际 HTTPS 完整读取 bundle 内所有非 root/timestamp 文件。v1.9.1 期间该长流占满 ECS 3 Mbps 公网出口，与客户端更新竞争，并触发了客户端 TLS 握手超时。
- 现有服务器安全边界是正式发布账号只允许 SFTP；root/targets 私钥和本地发布账本不上传。本实现保留该边界，不用远程 shell 代替。

## 实施记录

- `scripts/verify_publication_server.py` 实现规范请求、固定 public app 根、逐组件拒绝链接/非正则文件、完整文件 SHA-256 及 root 回执。处理后请求先在 archive 内原子写入并 fsync，再从监听目录移除；该顺序兼容 systemd `ReadWritePaths` 的独立 bind mount。
- `scripts/promote_release.py` 删除发布机公网完整回读作为提交准入的路径。commit 上传不可变文件后，通过原纯 SFTP 身份投递唯一 request ID，严格核对 root 回执的 request SHA-256、bundle ID、app ID、文件数和总字节；再对每个实际 HTTPS 目标比较首/中/尾最多 4 KiB，最后切换 timestamp。
- 增加 `satellite-updates-verify-publication.path/service`。北京 ECS 已部署并 enabled/active；`requests` 由 `satellite_publish` 投递，`results/archive` 只由 root 写入，发布身份实测可读回执但写入被拒绝。SSH 仍为 `internal-sftp`，shell/TTY/TCP/agent 转发均关闭。
- 实际对 v1.9.1 Windows Ops 217,338,488 字节完整包执行 ECS 本机 SHA-256，得到 `2f5ee632...f50fa`，与 TUF/候选回执一致。path 自动触发和真实正式 SFTP 请求都返回 verified；三个 4 KiB 公网 Range 的 SHA-256 与服务器同偏移字节逐项一致。
- 现场首轮暴露并根因修复了两个部署差异：直接脚本入口缺项目根 `sys.path`；systemd 独立 bind mount 之间不能 `rename`。最终脚本/service/path 的服务器 SHA-256 与工作区分别一致，service `Result=success, ExecMainStatus=0`，requests 为 0。
- 机器可读证据见 `doc/M56_server_release_verification_evidence.json`。

## 回归记录

- `PYTHONPATH=. .venv/bin/python -m pytest satellite_debug_tool/tests/test_release_promotion.py -q`：17 passed。
- 更新/候选/晋升相关整组：189 passed / 3.77 s。
- 全量 `satellite_debug_tool/tests`：2246 passed / 268.51 s。
- 翻译目录 check：1505 messages PASS；`compileall`、证据 JSON 解析和 `git diff --check` PASS。
- 覆盖正常提交、服务器摘要失败保留旧 timestamp、Range 不一致、已验证回执复用并归档、同 bundle 恢复和原有 TUF 版本围栏。
