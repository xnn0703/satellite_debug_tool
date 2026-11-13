# M69 验收标准

- [x] 同次双平台发布只有一份 publication 和一次 timestamp 切换。
- [x] 两平台 fresh checker 返回同一版本与原构建 commit。
- [x] 发布 TLS 和索引读取使用现有可信 CA opener，保持证书校验。
- [x] 子进程失败原因进入 journal，保留原始返回码。
- [x] 已有大包晋升通过 ECS 完整 SHA 校验，无 SFTP 全量回读。
- [x] 上传/摘要/Range 失败时 timestamp 保持原值，同包恢复成立。
- [x] 旧双分支编排已替换，旧 journal 有明确归档恢复证据。
- [x] 定向回归及必要全量检查通过；文档与 AGENTS 同步。
- [x] v1.9.8 四包公开索引、摘要、版本和 Range 检查通过。
- [x] 完整 run 的发布结果进入阶段日志，领域 state 与执行 state 独立。
- [x] 服务器已有正式请求在监听启动后被处理，临时文件不触发扫描。
- [x] 服务器下载服务消费当前 timestamp 签名链，过期/缺失 targets 别名不影响正确结果。
- [x] 服务器 UMask=0027 下公开目录为 0755、文件公开可读，Nginx 实际 Range 通过。

真实 Windows 安装回滚、Apple notarization 和设备现场验收独立记录。
