# M69 开发与复核记录

## 2026-11-13

- v1.9.8 源码 commit 为 69ab71f3228427977db7661f397004559314eaf7，正式 Windows Actions 43295577446 成功。
- 两份 publication 均从旧 timestamp 生成版本 23；当前本地账本为 macOS 分支。双分支提交模型需替换。
- 默认 urllib TLS 可稳定复现 CERTIFICATE_VERIFY_FAILED；使用 certifi 根证书同一 Range 探测通过。
- 上轮“服务器端计算 SHA 导致 12 分钟等待”描述不准确：upload_file 实际通过 SFTP 回读全量字节，随后才请求 ECS 本机摘要。此次修复将公开晋升完整摘要统一交给既有 ECS owner。
- 独立审查确认旧 commit_bundle 的版本围栏有效：第二分支被拒绝，未覆盖先发布的平台。“Windows 已发布”的前期判断不成立。journal 的 sort_keys 改变恢复顺序，失败子进程名称不能作为另一平台成功的证据。
- 修正前现场 fresh checker：Windows 为 1.9.3 / 57f6b1f，macOS 为 1.9.8 / 69ab71f。

## 历次异常复核

| 异常 | 根因 | 修复与证据 |
|---|---|---|
| Windows 轨迹周期断言 | 测试残留固定 20 ms，运行合同已为 50 ms | M68 按平台周期推导断言，真实 Windows 门禁通过 |
| loopback 接收零点 | spawn 冷启动期间观察器提前超时 | 冷启动预算与运行节拍分开，线程异常显式断言 |
| 冻结模型哈希不匹配 | 实机模型更新遗漏独立构建校验清单 | model_asset_manifest 成为单一来源，冻结校验与源码测试共用 |
| 独立模型脚本 import 失败 | 本地 PYTHONPATH 掩盖入口缺少根目录 | ROOT 注入及 python -I 独立解释器回归 |
| 晋升 TLS 失败 | 默认 urllib 使用缺失的系统根证书 | 发布与应用共用 build_https_opener，保留证书校验 |
| 双平台事务冲突 | 同一仓库生成两个同基线兄弟分支 | 同一暂存账本合并两候选，一份 publication、一次 timestamp；真实签名双 fresh checker 集成回归 |
| 错误仅返回 exit 1 | capture_output 的 stderr 未被投影到异常 | ReleaseCommandError 保留返回码和 stdout/stderr，写入 journal |
| 恢复耗时约 12 分钟 | SFTP 全量回读已有大包 | 公开文件类型/长度检查 + ECS 完整 SHA + HTTPS 有界 Range |
| 一键发布环境隐患 | SOP 使用缺少 paramiko 的应用 .venv | 专用发布环境运行，入口先验依赖；缺失时 journal 创建前明确停止 |
| 成功提交后 journal 报 TypeError | 发布领域 state 与执行阶段 state 同名参数冲突 | publication_result / publication_state 分离，完整 run 收尾回归；阶段写入异常归 FAILED |
| 下载请求留在队列 | PathChanged 被临时文件提前触发并漏掉正式改名 | 两个 watcher 改为 PathExistsGlob 正式 *.json；服务器实际已有请求自动启动 worker |
| 下载服务找不到已发布 Windows catalog | 直接读取过期 targets.json 别名 | 从当前 timestamp 验证编号 snapshot/targets，冻结一份 targets 给两平台使用 |
| 下载索引公网 404 | 服务 UMask=0027 将 mkdir(0755) 限制为 0750，Nginx Permission denied | 公开目录显式 chmod(0755)，受限 umask + 重用回归、实际 HTTP Range 通过 |

## 实施与恢复

- prepare_bundle 顺序合并候选，导出最终 snapshot/targets、root 链与 timestamp；中间元数据只在暂存账本中使用。
- M67 双分支字段与循环由单 publication 字段替换；journal schema 升为 v2，旧 schema 明确拒绝执行。
- 原 v1.9.8 journal 完整保存在仓库外 release-run-v1-before-M69.json；四候选重新计算摘要后迁入 v2，构建 commit 保持 69ab71f。
- 已准备纠正 publication e5b443fcfc03fe246eb872ceb2786632eef2f1ae74c2526a1a17a040b61541aa 经独立审查：timestamp 24 → snapshot 12 → targets 10 签名、摘要及两个 catalog 均通过；两平台均指向 1.9.8 / 69ab71f。
- 公开晋升既有文件只作类型/长度检查；后续既有 ECS 请求绑定本次 bundle、request、文件清单、长度和摘要。等长损坏文件回归由 ECS 拒绝，timestamp 保持原值。
- 回归覆盖 journal 序列化后的真实双平台 prepare/commit、一次 timestamp、fresh checker、失败恢复、CA owner 和 stderr；发布核心定向 36 项通过。全量结果及现场最终发布记录随后填写。
- 全量回归 2339 passed in 269.48s；最后新增的 runtime/stage/服务器入口修改继续执行相应定向回归。
- 修正 publication 实际提交成功，服务器回执 request_id 为 44aac389e45e4b4c848a5ee989b5c3f32aaf8ad13e3b4b5db6fcc1e3b8c3e489；fresh checker 两平台均为 1.9.8 / 69ab71f / automatic。
- 服务器两份 path unit 原文件保存在对应 .before-M69 文件；新监听已在 ECS 激活，立即消费留存请求。下载服务后续错误及回执保留，修复元数据读取后再处理同一已冻结请求。

## 发布完成证据

- `release-run/v2` 的全部阶段最终均为 PASSED，四包下载服务回执为 published。签名晋升、下载目录、fresh checker 和 HTTPS Range 的机器可读证据见 `M69_release_validation.json`。
- 原应用标签 `v1.9.8`、commit 69ab71f 与四包字节冻结；M69 仅修复发布机/服务器及测试文档，不重建应用。
- Windows 正式 Actions 43295577446：success；两平台 fresh checker 均为 1.9.8 / 69ab71f / automatic / delta_count=0。
- 服务器四包通过完整摘要与归档校验；发布机实际获取 release.json、SHA256SUMS 并验证四包首中尾 Range。
- 下载请求 86f54c397f5da2fcc671679eb3240e1344a0f32762029401e25a78b00128927a；失败回执留在原结果目录 `.json.failed-before-M69`，原脚本和 path unit 均保留 `.before-M69`。
- 服务器最终脚本 SHA-256：5132bf018d180f4e3bf8f416de0e510bda17e23d8555da995c7df22c873990c3。发布恢复先读取已绑定成功回执，避免重复提交已完成的服务器归档任务。
- 现场 Windows 安装/回滚、Apple notarization、物理设备/RF/运动验收继续为 NOT_RUN。
