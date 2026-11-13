# M71 验收标准

- [x] 官方双平台发布中，ops 完整包和 payload manifest 从 incoming 在 ECS 本机晋升，不发生第二次公网大包上传。
- [x] candidate receipt、app/edition/platform/architecture/build_id、文件名、大小及 SHA-256 全部匹配后才允许复制。
- [x] 公共目标为独立 inode；后续 incoming 变更或清理不改变已发布字节。
- [x] 服务只写固定 app public 根和 verification 状态目录，systemd 沙箱边界明确。
- [x] 小型签名文件上传、ECS 全量校验、HTTPS Range 和 timestamp 最后提交顺序保持。
- [x] 任意复制、摘要、Range 或连接失败时，旧 timestamp 保持不变。
- [x] 同一 publication journal 可显式恢复遗留锁；不同 bundle 不能恢复。
- [x] 断线时锁清理异常不覆盖首个根因，锁保留供同 bundle 恢复。
- [x] v1.9.10 四包 publication 摘要保持 `c618e7abccbacfdac0a51d6b143d4b93674842d42fa688cfc4f6950f553f5aeb`。
- [x] v1.9.10 完成稳定 publication、四包下载目录、fresh checker 和 HTTPS Range 验证。
- [x] 定向回归、全量 pytest、翻译、compileall 和 diff check 通过；文档与 AGENTS 同步。

真实 Windows 安装回滚、Apple notarization 和物理设备验收独立记录。
