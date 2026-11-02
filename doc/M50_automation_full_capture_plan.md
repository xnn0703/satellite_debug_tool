# M50 自动化测试完整采集计划

状态：2026-10-31 用户已确认范围并授权实施。

## 已确认的问题

自动化 Fleet 将 Product Service 快速状态订阅提升到20 Hz，DeviceSession 创建 SDB v3 recorder 并保存收到的报文，但没有请求 SET_CAPTURE_PROFILE=support_full。手动客户完整录制由 CaptureProfileController 协商 support_full，设备才开启动态 Debug 数据。因此文件正常收尾与采集内容足够分析是两个独立事实。

现场文件临时副本核对（原文件保持）：
- BATCH-20261030-2136：complete=True，3687 RX chunks，dropped_chunks=0，gap_markers=0；ServiceFastState=1314，ServiceRfLockStatus=1315；DataReport=0，InsTraceRecord=0。
- 同设备手动 support_192-168-1-12_4004_20261030_233356.sdb：complete=True，119002 RX chunks，dropped_chunks=0；DataReport=14093，InsTraceRecord=82845，另有GNSS天空/CNR、状态及事件数据。

依据：core/production/fleet.py 的 arm_recording/订阅链路，ui/live_view.py 的完整录制准备链路，core/session/product_controller.py 的 CaptureProfileController，doc/DEBUG设备协议接口规范_v2.md 的 SET_CAPTURE_PROFILE 合同。

## 实施范围

1. 将完整采集协商复用于自动化设备会话，权威 owner 放在 core/，页面只显示准备状态。产品能力、请求响应、会话代际、采集准入及恢复共用单一合同；不能直接借客户 LiveView 或绕过现有设备事务门禁。
2. 创批次可断电，SDB 提前准备；设备上电并确认身份后请求 support_full，确认响应和分析所需数据实际到达后进入正式观察。错误、超时或不支持明确报告采集准备状态，不把低信息录制称为完整采集。
3. 每台参与设备独立保存原始报文、身份/固件/协议与采集模式确认、批次/阶段/供电/夹具及断连事件。保留现有SDB和夹具证据来源，检查分析所需元数据的实际覆盖后补齐缺项。
4. 动态上电/设备重启后重新协商本代际的完整采集；正常结束、中止、异常及退出统一收尾并按协议恢复采集前Debug状态。共享电源断电阶段遵守现有M47流程。
5. 输出逐设备采集质量摘要：文件收尾、丢块/缺口、实际帧类别及数据起止、完整采集请求/确认分开呈现。不能用命令已发送或文件complete代替内容充分。

## 边界

本次修复自动化完整采集，不新增设备固件尚未提供的字段；完整指协议可提供的原始分析数据。弹窗造成偶发录制停止仍是独立未复现问题。实现时保留既有M47/M48/M49变更，不提交、不打包、不控制现场硬件。

## 验证交付

新增稳定复现旧自动化仅订阅状态的回归；涵盖上电前创建、能力拒绝、成功响应但数据缺失、正常采集、重启重协商、多设备隔离、中止与恢复、SDB回放。完成相关与全量pytest、翻译检查，维护开发记录，对照验收标准交付；现场硬件及信息完整性另行验证。

## 2026-11-02 已确认的电源循环恢复修订

实际OFF/ON不必让首条已观测uptime倒退。已知供电恢复由现Hardware闭环通知DeviceSession；ON后两类新身份接收游标、匹配冻结SN/UID、新Service、本次request-ID精确ACK及时序相容分析帧重新建立采集。开窗依据该组事实，不能伪造epoch或沿用断电前ready。实际主机/设备时间及nonce写入metadata；无boot ID时不宣称绝对排除数值碰撞旧包。

正式批次借用Fleet原有生产冻结授权，不能新建第二gate或临时解冻。长OFF使Runtime在presence超时及终止宽限后结束旧gate时，新身份/Service后通过原gateway恢复同批次owner的PRODUCTION_FROZEN，再由既有controller单次申请采集。TERMINATING/他人owner期间等待；finalize先清恢复引用，Fleet唯一释放父租约。共享controller借用前必须通过RuntimeGateway只读owns_production_operation验证当前Production capability/owner确已冻结；IDLE不允许借用入口凭空创建MUTATING。
