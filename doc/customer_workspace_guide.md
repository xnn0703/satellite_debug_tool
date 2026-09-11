# 客户工作区多设备指南

## 1. 功能边界

客户工作区可保存 0~4 台显式 UDP 设备，并允许多台设备同时保持连接。Customer 与 Production
共用一个进程级 UDP socket、一个 endpoint Directory 和每 endpoint 唯一的 Runtime/Core；左侧选择
设备只切换页面，不会重连、清空 Store、增加连接代际或改变已开始操作的目标。

AFD01、AFD01C 和 ESA01 可同时出现在目录中。页面只开放目标产品已经注册并由当前在线会话确认的
能力；ESA01 不会因为与 AFD01C 共用传输而获得客户 OTA 能力。

## 2. 添加、连接和切换

1. 点击左侧“设备”标题旁的 `+`，输入设备 IPv4 和设备 UDP 端口。重复 endpoint 会选择已有条目，
   不会创建副本；第五台会被明确拒绝。
2. 新增只保存并选择设备，不会自动连接。进入该设备的 Overview 后点击连接；每台设备分别保留
   自己的连接意图。
3. 左侧条目显示完整 endpoint、连接阶段和录制/事务状态。点击另一条目即可在相同子页面切换。
4. 未选中的设备继续接收、解析和更新自己的 Store；其录制、OTA 或已开始事务也继续归属于原 endpoint。

连接阶段含义：

- `DISCONNECTED`：客户未连接该设备，即使 Production 正在观察同一 Runtime，也不表示客户已连接。
- `WAITING`：已发出连接意图，正在等待完整、校验正确且可解码的设备记录。
- `ONLINE`：本会话收到新鲜有效记录；这不等于某条控制已经应用，更不等于物理 RF 已确认。
- `RECONNECTING`：曾在线但有效记录已超时，连接意图仍保留。

当前在线代次尚未验证 UID/SN 或身份来源冲突时，控制保持禁用；历史 Store/Profile 不能授权新设备。

## 3. 编辑和删除

编辑或删除前必须先断开该客户 attachment，并停止录制、OTA 和设备事务。编辑为已有 endpoint 时只
选择已有条目，源条目不变。删除当前条目后按列表顺序选择下一项；列表为空时 session-bound 页面显示
空状态，Playback 仍可使用。

Edit/Delete 只修改客户配置，并只收口目标 endpoint 的本地资源。

## 4. 工程工作区

工程顶栏提供两个明确模式：

- “共享客户 UDP”：Live、Device、Tracking Simulator 和 Settings Profile 跟随左侧当前选中的、已连接的
  客户 endpoint；它们复用同一个 Runtime/Core，不增加第二个 UDP transport。
- “工程串口”：使用独立串口 Core，不进入 UDP Directory。

存在工程录制、OTA、设备事务或尚未断开的串口连接时，模式切换会被拒绝。Playback 和 Log 始终使用
各自离线数据源。

## 5. 录制和文件

每台设备拥有独立 recorder。切换当前设备不会停止其他设备录制。默认文件名包含时间和规范化
endpoint；文件以独占创建方式保留，已有文件、等价路径和其他工作区持有的路径不会被覆盖。

客户 full-support 录制从 capture-profile 协商开始，到 writer 收尾并请求恢复 customer-live 为止，均属于
同一设备变更事务；同 endpoint 的 Production batch freeze 与它互斥。恢复响应未确认时，本地 writer 和
operation owner 仍会有界释放，该 endpoint 显示“采集模式待重新同步”，下次录制重新协商。

## 6. 未确认状态

RF、参数、OTA、Tracking 或 capture-profile 的设备终态未确认时，界面只陈述“结果未确认”，对应控制器
在有界超时后释放当前 endpoint 的本地 owner。它不会创建跨重启恢复任务，也不会阻断其他 endpoint。
新 mutation 仍必须重新满足当前 PresenceEpoch、身份一致性和能力确认；旧响应不能作为新操作证据。

`指令已发送`只证明 OS 完整写出 UDP 数据报；设备接受、遥测应用和物理 RF 是三层独立证据。

## 7. 验收边界

Host 自动化可证明单 socket 分流、会话隔离、页面固定绑定、事务互斥和文件所有权。AFD01C + ESA01
真实双机连续在线、断电恢复、设备接受、遥测应用、OTA/TX 和物理 RF 仍必须按台架流程单独验收。
