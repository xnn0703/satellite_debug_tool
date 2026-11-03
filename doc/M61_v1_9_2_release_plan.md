# M61 v1.9.2 发行计划

日期：2026-11-03。用户已明确要求提交当前本地改动并按 M56/M57 新链路再次发版。

## 目标

- 将当前已审计的 M56～M60、协议/录制配套修正、测试、翻译和文档冻结为唯一版本 v1.9.2。
- GitHub `github` 远端保存源码提交和不可变 `v1.9.2` tag；Windows user/ops 由 tag Actions 构建并上传 ECS 候选区。
- macOS arm64 user/ops 从同一源码 SHA 在本机 Python 3.11 构建、验证并上传 ECS 候选区。
- 四份完整包公开到 ECS 下载目录；ops Windows/macOS 通过 TUF stable 目录分发，user 继续人工完整包交付。
- 正式晋升使用 M56 的 ECS 本机完整 SHA-256 回执和外部有界 HTTPS Range，不从发布机公网完整回读大包。

## 执行顺序

1. 审计工作区差异、未跟踪文件、版本源、发布说明、远端和标签状态，排除密钥及临时产物。
2. 更新版本、发行说明和 M61 记录；执行翻译、compileall、完整 pytest、源码 smoke 与差异检查。
3. 仅暂存已审计文件，提交后确认干净工作区；创建并推送 `v1.9.2` 到 GitHub。
4. 等待 Windows tag Actions user/ops 全部成功并核对候选回执。
5. 从同一 tag SHA 本机构建、smoke、校验并上传 macOS user/ops 候选。
6. 核对四候选身份、归档完整性、SHA-256 和源码 SHA；发布 user 完整包。
7. 使用离线签名材料依次晋升 macOS/Windows ops；服务器本机复核通过后才切换 timestamp。
8. 用 fresh checker、公开元数据、SHA256SUMS、release.json、归档身份/CRC 和 HTTPS Range 完成交付复核。

## 停止条件

- 任一测试、构建、身份、摘要、签名目录、服务器回执或公开 Range 不一致即停止，不切换正式 timestamp。
- 已存在同名 tag、候选版本/SHA 不一致、工作区包含未审计文件或秘密材料时停止。
- 不以 CI/归档成功替代 Windows 实机、Apple notarization、真实设备、RF 或摇摆台现场验收。
