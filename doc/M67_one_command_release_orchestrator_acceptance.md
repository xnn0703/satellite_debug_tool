# M67 单入口发版编排器验收标准

日期：2026-11-12。

## A. 身份与源码门禁

- [x] A01 入口拒绝脏工作区、缺失发布说明、版本源与参数不一致、tag 指向其它 commit。
- [x] A02 不执行 `git add` 和源码 commit；tag 前显示并绑定完整 source commit。
- [x] A03 已公开 tag 不可移动；源码修复必须使用新版本。
- [x] A04 四个 candidate receipt 的 version、source commit、edition、platform 和 architecture 形成完整且唯一的四元集合。

## B. 编排与恢复

- [ ] B01 `run --version` 从干净发布 commit 生成仓库外 journal，且 journal 不含密钥、配置正文或 token。
- [ ] B02 `status` 只读显示每阶段的 `PENDING/RUNNING/PASSED/FAILED/WAITING_APPROVAL` 和对应证据。
- [ ] B03 `resume` 复用摘要一致的 tag、Actions、candidate 和 publication；摘要变化时停止。
- [ ] B04 journal 原子写入；截断、未知 schema 或阶段证据缺失时 fail closed。
- [x] B05 同一版本并发运行由单一锁拒绝，其他版本不能修改当前 run journal。

## C. 审批边界

- [x] C01 tag/push 前需要绑定 source commit 的显式确认。
- [x] C02 GitHub `release-candidate` Environment 仍由 GitHub 审批，脚本不持有绕过能力。
- [x] C03 stable 前展示两个 publication 摘要和四包身份/摘要，并要求绑定 bundle-set SHA 的显式确认。
- [x] C04 不存在不绑定内容的全局 `--yes`。

## D. 现有 owner 复用

- [x] D01 Windows 构建只由 `.github/workflows/build.yml` 执行；编排器只等待和读取 tag workflow 证据。
- [x] D02 macOS 候选只由 `release_macos.py` 构建上传。
- [x] D03 TUF 签名与提交只由 `promote_release.py prepare/commit` 完成。
- [x] D04 编排器不重新实现 TUF 签名或 ReleaseChecker；候选/归档身份继续调用既有验证 owner。

## E. 服务器完整包交付

- [x] E01 server request 绑定唯一 request ID、version、source commit、两份 user receipt 和两个确定 ops catalog。
- [x] E02 root 服务拒绝路径穿越、符号链接、非正则文件、身份不一致和已存在不同字节。
- [x] E03 user 只从已核验 incoming candidate 发布；ops 只从已签名 TUF target 发布。
- [x] E04 ECS 本机工具完成四包完整 SHA-256 和 ZIP/7z CRC，不通过公网回读完整大包。
- [x] E05 `SHA256SUMS.txt`、`release.json` 和四包由同一验证集合原子生成；失败时旧目录/索引保持不变。
- [ ] E06 发布账号可投递请求、读取结果，但不能写 root 回执或直接修改下载目录。

## F. 发布后验证与证据

- [ ] F01 Windows/macOS fresh ReleaseChecker 均返回目标版本、source commit 和 `installation_mode=automatic`。
- [ ] F02 公网对元数据、索引和四包执行首/中/尾 Range，长度及片段与服务器事实一致。
- [ ] F03 自动生成的 validation JSON 包含 Actions run、两个 bundle ID、四包 URL/大小/SHA、各门禁结果和 NOT_RUN 边界。
- [ ] F04 validation JSON 的每个 PASS 都能追溯到明确工具输出或不可变回执，不能根据阶段名推断。

## G. 失败注入

- [ ] G01 模拟 Actions 失败、Environment 待审批、Mac 构建失败和 candidate 摘要冲突，均不进入 stable。
- [ ] G02 模拟 prepare 后 timestamp 变化，旧 bundle 拒绝并要求重新 prepare。
- [ ] G03 模拟服务器 SHA、CRC、root 回执或 HTTPS Range 失败，timestamp 和下载索引保持旧版本。
- [ ] G04 模拟进程中断后 resume，从最后一个已证实阶段继续，不重建不可变候选。
- [ ] G05 模拟残留发布锁，只允许同一 bundle owner 在确认旧进程结束后恢复。

## H. 回归与交付边界

- [x] H01 新增编排器和服务器工具有定向自动化测试，并通过项目全量 pytest、翻译检查、compileall 和 `git diff --check`。
- [x] H02 `--dry-run` 在不创建 journal、tag、不上传、不签名、不切 stable 的情况下完整输出执行计划。
- [ ] H03 在受控非 stable 渠道或等效隔离环境完成一次端到端演练。
- [x] H04 Windows/macOS 实机安装回滚、Apple notarization、设备/RF/电源/夹具继续作为独立验收，不因软件发版 PASS 自动通过。

勾选项为本地源码、自动化夹具或静态权限合同已经证明的事实；未勾选项需要 ECS 实际部署、隔离端到端演练或下一次真实版本发版证据，不能由本地测试推断通过。
