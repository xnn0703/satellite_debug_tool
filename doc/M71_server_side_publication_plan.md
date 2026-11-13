# M71 ECS 本机晋升根因修复计划

用户已要求继续完成 v1.9.10 发布，并已授权直接配置阿里云服务器。2026-11-13 的稳定晋升在 SFTP 重复上传 Mac 运维完整包时中断；本计划替换该错误数据流。

## 已确认事实

1. 四个平台候选均已上传到 `/srv/satellite-updates/incoming/<commit>/...`，receipt、大小和 SHA-256 已通过候选审计。
2. 稳定晋升再次从发布机向公开目录上传 284 MB Mac 包，在 145326080 字节处连接超时；服务器仍保留同 bundle 锁和随机 partial。
3. 线上 `metadata/timestamp.json` 仍为旧摘要，v1.9.10 未进入稳定通道。
4. ECS 磁盘尚余 22 GB；故障不是空间不足。
5. 现有 root oneshot 只校验公开文件，不能把已验证 incoming 候选就地晋升，导致重复公网传输。

## 单一正确模型

- 发布机继续持有 TUF 私钥、签名账本和 publication 审批摘要。
- 运维候选完整包及 payload manifest 只上传一次到 incoming。
- publication 提交请求把本地已审计 candidate receipt 与远端 incoming 目录绑定；大文件不再经发布 SFTP 写入公开目录。
- ECS root oneshot 校验 candidate receipt、身份、文件名、大小和 SHA-256，在同机复制为独立公开 inode，并原子封口不可变文件。
- 小型 catalog、release record 和编号元数据仍由发布机通过 SFTP 写入。
- ECS 对全部不可变文件做完整 SHA-256，发布机做有界 HTTPS Range；两者通过后才原子切换 root/timestamp。
- 同一已审批 publication 的失败锁由 release journal 明确恢复；不同 publication 继续拒绝。
- partial、旧锁和旧服务只在新实现验证并部署后清理；清理不改变线上 timestamp。

## 实施范围

1. 将 publication verification request 升级为带 candidate source 的 stage-and-verify 请求。
2. 服务端严格解析 incoming 相对路径和 canonical receipt，拒绝符号链接、非 ops 身份、摘要不一致和多义匹配。
3. 服务端复制使用同目录临时文件、fsync 和不可覆盖封口；并发目标出现时只接受相同摘要。
4. release orchestrator 向 commit 传入 Windows/Mac ops 本地 receipt 与远端 candidate 路径，并在同一失败 journal 上恢复锁。
5. systemd 服务增加 incoming 只读、指定 public app 根可写；保留 `ProtectSystem=strict`、`NoNewPrivileges` 和 `RestrictAddressFamilies=AF_UNIX`。
6. 增加旧实现稳定复现与新实现回归；同步发布文档和 AGENTS。

## 证据边界

本变更只证明发布事务、包身份、服务器摘要、HTTPS Range 和 fresh checker。Windows 实机安装/回滚、Apple notarization 及设备现场能力仍独立记录。
