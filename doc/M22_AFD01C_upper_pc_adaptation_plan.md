# M22 AFD01C 上位机适配计划

状态：软件实现与自动化验收完成；AFD01C 真机、RF、平台与正式工艺验收待执行

验收标准：`doc/M22_AFD01C_upper_pc_adaptation_acceptance.md`

开发记录：实施阶段维护 `doc/M22_AFD01C_upper_pc_adaptation_dev_log.md`

## 1. 已核对基线

- 上位机基线为 `master` 的 `17cdd15`，当前工作区另有一组未提交的 ESA01/Product Service 改动；M22 必须在保留这些改动的前提下实施，不覆盖或拆改其既有语义。
- 设备端基线为 `3-satlite_comm_terminal` 的 `fc6ec326`。AFD01C 是独立产品身份，不是 AFD01 的显示别名：
  - Product Service 上报型号 `AFD01C`，schema 1、service protocol 8；
  - AFD01C 固定绑定 KaTR003B 与 KA256 V2 TX/RX；
  - AFD01 与 AFD01C 使用不同 `hardware_compat_id`，Boot/App 禁止交叉刷写。
- 截图中的三个现象来自不同事实源，不能用一个型号字符串补丁混在一起处理：
  1. 客户射频页显示“等待产品服务”：上位机客户产品策略尚未注册 `afd01c`；
  2. 试产页显示“不支持的硬件”：Fleet 仅接受精确的 `afd01`，并把“支持的型号但 SN 缺失”错误归类为不支持；
  3. `SN 不支持` 与 `0.00 dB` 均来自设备 valid mask/有效遥测。上位机不得把有效零改成未知，也不得伪造未配置的 SN。

## 2. 目标

1. 把 AFD01C 作为显式注册产品接入客户 Product Service、受保护射频控制和安装姿态配置。
2. 让试产工作区正确区分“支持且身份完整”“支持但 SN 未配置”“未知型号”，并用产品匹配的 recipe 阻止 AFD01/AFD01C 混测。
3. 让客户 OTA 使用当前设备的权威产品身份校验签名包，继续禁止 AFD01 与 AFD01C 交叉刷写。
4. 保持 AFD01、ESA01、未知型号以及现有 Debug v2 工程工作区行为不回归。

## 3. 本轮边界

### 包含

- 产品注册策略、客户工作区准入和控制门禁；
- 试产 Fleet 身份状态、recipe 产品合同、批次启动门禁和对应文案；
- 客户 OTA 产品/硬件匹配；
- 类型化回归测试、i18n 更新、软件验证与 AFD01C 真机验收清单。

### 不包含

- AFD01C STL/3D 模型。结构同事提供模型及坐标基准前继续显示当前占位模型，不复制或别名复用 AFD01 STL；
- 设备端 Product Service、SN 写入、SNR 生成、KaTR003B 标定、阵面协议或 RF 时序修改；
- 未经产品/工艺确认的 AFD01C 量产阈值和正式放行 recipe；
- 自动把 `0 dB` 解释为离线或无效；
- Git commit、push 或正式发版。

> 后续授权（2026-08-30）：用户在完成实现后明确要求全盘 review，并将当前本地修改提交。
> 该授权只替代本计划原先的“不执行 Git commit”边界；push、tag 和正式发版仍不在授权范围内。

## 4. 根因修复设计

### 4.1 单一产品注册表

在产品领域保留一个肯定式注册策略，至少表达：

| hw_type | Product Service | 客户 OTA product | 试产 recipe product |
|---|---|---|---|
| `afd01` | `2..8` | `AFD01` | `afd01` |
| `afd01c` | `8` | `AFD01C` | `afd01c` |
| `esa01` | `6` | 本轮不新增 | 本轮不新增 |

- 客户页、OTA 和试产入口消费该注册事实，不再各自散落 `== "afd01"`。
- 未注册型号保持明确“不支持”；不得按名称前缀、相似能力或共享源码族推断支持。
- AFD01C 只接受已核对的 service protocol 8，不继承 AFD01 历史版本范围。

### 4.2 客户 Product Service 与射频控制

- 当 Debug Meta 与 Product Identity 均证明 `afd01c`/`AFD01C` 且 protocol 8、FAST_STATE 和能力字段有效时，客户服务状态进入 ready。
- 射频控制继续要求设备在线、MANUAL 回读、完整频率/极化能力、独立收发极化能力和空闲设备事务。
- 指令发送、设备 accepted、遥测 applied 与物理 RF 证据继续分层；AFD01C 标定/互锁不满足时由设备拒绝或保持 fail-close，上位机不得显示为已发射。
- 安装姿态配置仍要求 protocol 8 与设备 mount capability，不因型号注册绕过能力位。

### 4.3 试产身份与产品隔离

- Fleet 收到 Product Identity 后按以下顺序判定：
  1. 型号未注册为试产产品：`UNSUPPORTED`；
  2. 型号已注册但 SN 未置 valid bit 或为空：新增肯定式“身份待配置”状态，不再显示“不支持的硬件”；
  3. 型号已注册且 SN 有效：`IDENTIFIED`；
  4. SN/UID/MAC 冲突：`CONFLICT`。
- 身份待配置设备可以继续显示 endpoint、型号和设备上报的 SNR，但不得加入批次、落正式设备目录或开始测试。
- recipe schema 1 显式接受 `afd01` 或 `afd01c`；批次启动时，每个参与设备的权威型号必须与 `recipe.product` 精确一致。
- 不提供 AFD01C 默认正式 recipe。没有经工艺确认的 AFD01C recipe 时，只完成识别、取证和工程预览，不声明量产放行。
- 批次默认名称和“正在发现 AFD01”文案改为由当前 recipe/注册产品驱动，或使用中性的“支持的设备”，避免事实错误。

### 4.4 客户 OTA 防交叉刷写

- 验签成功后生成不可变 artifact token，同时绑定包字节、manifest、当前连接代际、Product Identity、Debug hw_type 和固件版本事实。
- AFD01C 调用签名包校验时使用 `expected_product="AFD01C"`、`expected_hardware="afd01c"`；AFD01 保持原合同。
- Product Identity 与 Debug hw_type 不一致、缺少不可变 SN/UID、Debug/Product 序列号或固件事实冲突、会话变化或产品未注册 OTA 时，拒绝载入/启动 OTA。
- 工程 raw BIN 与客户签名包使用两种显式 artifact 类型；任一替换都使旧 token 失效，活跃传输期间不允许替换字节。
- 只消费当前 OTA 阶段的带上下文响应；参数、Debug 或旧分片响应不能推动 OTA 状态机。
- 保持签名、SHA-256、版本策略和硬件白名单校验；AFD01 包在 AFD01C 上必须稳定拒绝，反向同样拒绝。
- `OTA_END=VERIFIED` 只证明传输与设备校验被接受；在 WAIT_REBOOT 窗口内，同 endpoint 返回的 Debug SN、Product SN 和 UID 中凡是传输前捕获为有效事实的，都必须重新匹配；传输前捕获的每个 Debug/Product 固件来源也必须都重新回报、语义一致并匹配签名包目标版本。其他 endpoint、任一身份变化/缺失、固件来源缺失/冲突或目标版本未回读均 fail-close；同版本包不宣称已应用。

## 5. 实施顺序

1. 扩展产品注册策略并补 AFD01C protocol 8、未知协议及型号不一致回归。
2. 接通客户服务、RF/安装姿态页面，并验证有效性、事务和 applied readback 门禁不被削弱。
3. 重构 Fleet 身份分类，增加“支持但身份待配置”状态和 UI 文案。
4. 扩展 recipe 产品合同与参与设备产品匹配门禁，覆盖混插、缺 SN 和批次冻结。
5. 将客户 OTA 的期望产品改为权威注册结果，补双向交叉包拒绝测试。
6. 更新中英文翻译与用户/试产说明，维护开发记录。
7. 执行定向测试、全量 pytest、翻译检查和 diff 检查，再按验收标准进行 AFD01C 真机验证。

## 6. 验证命令

```bash
PYTHONPATH=. pytest -q \
  satellite_debug_tool/tests/test_customer_product_policy.py \
  satellite_debug_tool/tests/test_customer_rf_control_view.py \
  satellite_debug_tool/tests/test_customer_maintenance_view.py \
  satellite_debug_tool/tests/test_firmware_package.py \
  satellite_debug_tool/tests/test_production_fleet.py \
  satellite_debug_tool/tests/test_production_recipe.py \
  satellite_debug_tool/tests/test_production_workspace.py \
  satellite_debug_tool/tests/test_device_session_core.py \
  satellite_debug_tool/tests/test_device_view_capabilities.py \
  satellite_debug_tool/tests/test_product_service_protocol.py \
  satellite_debug_tool/tests/test_frame_v2.py \
  satellite_debug_tool/tests/test_codec_v2.py

PYTHONPATH=. pytest -q satellite_debug_tool/tests
python3 scripts/update_translations.py update
python3 scripts/update_translations.py check
git diff --check
```

## 7. 完成定义

- 软件测试全部通过，且新增测试能在旧实现上稳定复现截图中的错误分类/准入问题。
- AFD01C 真机能进入客户产品服务 ready；受保护控制只在设备能力与回读允许时启用。
- 试产页不再把已注册但未配置 SN 的 AFD01C 称为“不支持的硬件”；配置有效 SN 后才允许按 AFD01C recipe 入批。
- AFD01/AFD01C 签名包交叉选择均被拒绝。
- 仍未完成的正式 recipe、签名包、RF/阵面/标定及 3D 模型验收在开发记录中明确保留，不以软件测试代替。
