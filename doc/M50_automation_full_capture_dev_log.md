# M50 开发记录

状态：2026-10-31 用户确认计划后实施；不提交、不打包、不控制现场设备。

## 根因与实现

自动化 Fleet 原先只将 Product Service 快速状态订阅提升至20 Hz，同时创建SDB recorder；它没有请求 `SET_CAPTURE_PROFILE=support_full`。因此文件可以完整收尾、无丢块，但只包含状态摘要，不包含设备只在完整采集模式发送的动态 Debug 分析数据。

本次将完整采集协商接入每个 `DeviceSession`：

- SDB 创建后读取本代际的设备能力，只有 `capture_profile_mask.bit1` 有效才申请完整采集。
- 复用 `CaptureProfileController` 的请求ID、精确响应、超时、连接代际和遥测代际合同；生产 recorder 已有独立权威租约，控制器不再申请第二个客户 recorder owner，且完整采集确认后释放短期操作门禁。
- `support_full` 响应确认后仍保持“等待数据”；只有确认之后实际收到 `DataReport` 或 `InsTraceRecord` 才发布“完整分析采集中”并允许正式工况开始。
- 请求、确认、首次出现的各类分析帧、代际变化、恢复请求及恢复结果写入设备SDB metadata event。SDB header明确记录 `production_support_full_required`。
- 连接/遥测代际变化清除旧确认及旧分析数据证据，当前代际必须重新请求、重新确认并重新收到分析帧。
- 正常完成先结束冻结的批次操作，再通过同一生产会话恢复 `customer_live`；恢复响应未完成时 recorder 和文件 owner 保留，恢复结果确认后才执行正常文件收尾和断电。
- 中止、电源故障和应用退出执行有界的尽力恢复，但不以等待恢复响应阻塞 SDB 收尾；已有现场数据优先完整落盘，安全断电仍由异常收尾链路拥有。该路径只记录已经确认的恢复事实，不把已发送请求写成恢复成功。
- 页面将“证据录制已建立”和“完整分析采集已就绪”分开呈现；开始门禁会列出未就绪设备及原因。

`CaptureProfileController` 新增两个构造策略参数，客户完整录制默认行为保持原值；生产路径选择外部 recorder owner，并在开启确认后释放操作租约。没有新增兼容分支或第二套响应解析。

## 回归与证据

新增 `test_m50_production_full_capture.py`，覆盖：

1. 旧行为复现：创建生产 recorder 后，响应未确认或没有实际分析帧均不就绪。
2. 完整流程：自动发送 `support_full`、精确确认、实际 `DataReport` 到达、SDB保存、结束时恢复并收尾。
3. 因果边界：确认前到达的分析帧不作为本次完整采集证据；连接代际变化后必须重新请求、确认并观察新分析帧。
4. 异常边界：恢复确认无法到达时，中止收尾仍生成 `complete=true` 的可分析 SDB，不把恢复请求冒充恢复完成。
5. 固件只声明摘要采集时明确 `UNSUPPORTED`，不发送完整采集请求。
6. 新会话代际撤销旧证据，必须重新确认及重新收到数据。

与采集无关的既有界面测试显式越过已由M50专项覆盖的采集门禁；正式产品代码没有测试旁路。

## 验证结果

- M50完整采集与生产工作区相关回归：32项通过。
- 生产配置工作区门禁及电源相邻回归：11项通过。
- 全量测试：1918项通过，0失败。
- 翻译目录：1470条，0条未完成；`git diff --check` 通过。
- 未控制现场硬件、未打包、未提交。真实设备是否在自动化路径按协议持续输出全部分析帧，仍需用新批次SDB核对帧类别、时间覆盖、缺口及丢块。
- “录制期间打开摇摆台或Shell偶发中断”仍是独立问题；当前源码审查和自动化测试没有形成稳定复现，本次没有将其猜测性地并入完整采集根因。

对照验收：软件侧已建立“请求、精确确认、确认后实际数据、文件完成”四层独立事实，并覆盖能力拒绝、代际变化、正常恢复及异常收尾。现场信息充分性和弹窗偶发中断不由上述软件测试替代。

## 2026-11-02 同scope供电恢复与父冻结生命周期

新增DeviceSession.prepare_capture_after_power_cycle，消费者仅正式Hardware匹配ON闭环。两类身份游标和接收时间、新Service及精确新request-ID/ACK/分析时间门共同撤销旧ready；metadata保存请求/ACK处理单调时刻、设备基线与实际data_observed时间。没有新增timer、重试或身份owner。

正式freeze回归先稳定复现capture自行申请owner导致BUSY。现有CaptureProfileController.request可借Fleet原父owner；RuntimeGateway.owns_production_operation只读确认确为当前同Production capability的冻结，借用不覆盖父context、不释放父租约。长OFF的Runtime presence失效和终止宽限后，使用Fleet原gateway/owner授权恢复原冻结；TERMINATING/他人owner期间等待，finalize先清引用。普通Customer默认仍为原独立租约。

最终相关、production、全量pytest、翻译与独立review结果保存于固件仓库doc/afd01_dynamic_closure_20261102/capture_*。实物电源循环和持续分析帧仍待板级验证。

最终冻结验证（2026-11-02）：相关93 passed/5.51s，Production+Runtime/Customer相邻394 passed/14.35s，完整2238 passed/271.75s；三段前后5生产文件摘要不变。翻译1505条全部完成，update/check及diff检查0退出。独立借用租约review通过。最终源摘要、完整命令和日志保存在固件仓库`doc/afd01_dynamic_closure_20261102/capture_final_validation.json`和`capture_*_pytest.log`。当前只证明软件合同；现场电源循环、持续分析数据、MCU物理复位和RF仍单独验收。
