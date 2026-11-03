# M56 服务器侧发布复核验收标准

日期：2026-11-03。

| ID | 验收要求 |
| --- | --- |
| V01 | 校验请求为规范 JSON，绑定 bundle ID、应用根、每个文件的相对路径、大小和 SHA-256；重复、越界、链接和非正则文件明确拒绝。 |
| V02 | ECS 在本机读取 Nginx 实际 public 文件并完整计算 SHA-256；回执绑定请求 SHA-256、文件数和总字节。 |
| V03 | requests 可由正式发布 SFTP 身份投递；results 只由服务端 owner 写入、发布身份只读；不为发布身份开放 shell/TTY/转发。 |
| V04 | `commit` 只在 verified 回执与当前 bundle 完全匹配后继续；缺失、失败、被篡改或过期回执均保持旧 timestamp。 |
| V05 | 发布机不再通过公网完整下载发布包；只执行首/中/尾 Range 探测，分段内容必须与 bundle 本地字节一致。 |
| V06 | 服务器完整校验或 Range 探测失败时，新 timestamp 不可见；原 bundle 在故障排除后可安全恢复。 |
| V07 | 现有 TUF 签名、timestamp 最后提交、本地账本、不可变摘要目标和客户端下载 SHA-256 验证合同保持。 |
| V08 | 单元/集成回归、翻译检查、`git diff --check` 通过；真实 ECS 部署证据记录实际路径、权限、service/path 状态和校验摘要。 |
