# M69 双平台发布事务根因修复计划

用户于 2026-11-13 确认继续修正发布，并要求先完整 review 历次异常。

## 已确认事实

1. Windows CI 曾暴露平台周期断言、spawn 冷启动观察、模型哈希重复来源和独立脚本 import 路径问题；M68 已完成对应修复和正式 tag 构建验证。
2. 发布 HTTPS 探测直接使用 urllib 默认 CA；发布 Python 3.13 的系统 CA 不可用。使用已安装 certifi 后同一 Range 探测通过。
3. 编排器先基于同一个旧仓库生成两份 publication；同仓库的元数据版本属于单一序列，两份分支发生版本/字节冲突。
4. capture=True 的子进程异常只显示返回码，stderr 未进入 journal，延迟了根因定位。
5. upload_file 的远端摘要实际通过 SFTP 全量读取；这会消耗公网带宽，不能称为 ECS 本机校验。正式晋升已有独立 ECS SHA 回执。

## 实施范围

- 双平台候选在同一私有暂存仓库依次合并，最终只生成和提交一份 publication，timestamp 一次原子切换；中间元数据只留本地。
- 现有客户端 HTTPS opener 成为发布探测、索引读取的共同可信 CA 来源。
- 子进程 stdout/stderr 在失败时进入明确异常和 journal。
- 已存在公开不可变文件只检查类型/长度，最终摘要由 ECS 请求回执统一确认；新上传也交 ECS 完整校验后才提交 timestamp。候选上传合同独立保留。
- v1.9.8 标签和四个构建包冻结；纠正发布基于当前签名元数据向前推进。旧失败 journal 归档保留，新单 publication journal 绑定原 commit；发布机修复提交与应用构建 commit 分别记录。
- 同步 RELEASING、AGENTS 和开发记录。
- journal 的执行阶段与发布领域结果采用独立字段，阶段记录异常进入 FAILED。
- ECS 两个请求监听使用正式 JSON 文件存在条件；下载服务从 timestamp 的编号签名链冻结唯一 targets 快照，两个平台共同消费。

## 验证

回归覆盖双平台 catalog 同时可见、失败前 timestamp 保持、重入、CA owner、stderr 透传及公开文件摘要交 ECS。现场使用两平台 fresh checker 验证版本、commit、automatic，再验证四包下载索引与有界 Range。
