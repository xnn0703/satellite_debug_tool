# M55 v1.9.1 发版验收标准

日期：2026-11-02。

| ID | 验收要求 |
| --- | --- |
| R01 | `satellite_debug_tool.__version__`、构建身份、tag 和四份归档均为 1.9.1，且指向同一干净源码 SHA。 |
| R02 | 工作区发布范围完成审计；`git diff --check`、翻译 check、compileall 和当前全量 pytest 通过。 |
| R03 | GitHub `master` 与 `v1.9.1` 指向发布提交；不覆盖历史 tag。 |
| R04 | Windows user/ops 两个 CI 作业均完成源码测试、冻结构建、smoke、归档身份校验和候选上传。 |
| R05 | macOS user/ops 使用同一源码 SHA 构建；ad-hoc 签名、冻结 smoke、归档身份校验和候选上传通过。 |
| R06 | 四份完整归档的 edition、平台、架构、版本、源码 SHA、根目录、CRC 和 SHA-256 均通过。 |
| R07 | user 包不包含应用 updater；ops 包保留在线更新合同。 |
| R08 | ops stable TUF 元数据、签名、有效期、目标摘要和 HTTPS 下载回验通过；若生成差量，必须绑定 v1.9.0 基线并独立校验。 |
| R09 | user 完整包及 ops 完整包均有不可变 HTTPS 直链、SHA256SUMS 和发行验证记录。 |
| R10 | 最终交付明确区分软件/构建证据与真实设备、电源、摇摆台、RF、Windows 原生运行验收。 |
