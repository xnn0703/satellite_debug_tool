# M56 服务器侧发布复核计划

日期：2026-11-03。目标是将正式发布中的公网完整包回读改为 ECS 本机完整校验，避免 3 Mbps 公网出口被发布复核占满，同时保持签名私钥只在发布机、发布账号只能 SFTP 的现有安全边界。

## 单一事实源

- GitHub Actions 生成候选回执，固定版本、edition、平台、架构、源码 SHA、文件大小和 SHA-256。
- 发布 bundle 是本次待公开字节和 TUF 签名元数据的唯一 owner。
- ECS 校验服务只消费 bundle 派生的规范化请求，不持有 root/targets 私钥，不选择版本，不切换 timestamp。
- 发布机只在 ECS 回执的 bundle ID、请求 SHA-256、文件数和总字节全部匹配，且 HTTPS 分段探测通过后提交 timestamp。

## 实施顺序

1. 增加服务器校验请求/回执模型和本机校验 CLI；固定 public/requests/results 根目录，拒绝链接、越界路径、重复路径和非正则文件。
2. `promote_release.py commit` 上传不可变目标后投递校验请求，等待 ECS 只读回执；删除发布机的公网整包回读路径。
3. 公网侧仅对本次目标执行首/中/尾小分段 Range 校验，证明 HTTPS 映射到与服务器校验相同的字节。
4. 增加回归：正常提交、服务器摘要失败、回执伪造/过期、Range 不一致、timestamp 不推进、同 bundle 恢复。
5. 通过 Workbench 在北京 ECS 部署校验脚本和 systemd path/service，实测目录权限、本机完整 SHA-256、小分段 HTTPS 与旧 timestamp 安全保留。
6. 更新发布 SOP、AGENTS 架构说明和开发记录，对照验收表复核。

## 边界

- SHA-256 + 大小证明文件字节一致；TUF 签名证明这些摘要属于授权发布。
- ZIP/7z CRC 继续在构建侧验证归档内部结构，不替代 SHA-256。
- 服务器校验 PASS 不代表 Windows/macOS 安装、设备、RF 或现场验收。
