# M34 AFD01D 上位机适配验收标准

## A. 产品身份与协议

- [x] `afd01d` 精确解析为产品身份 `AFD01D`。
- [x] AFD01D 仅接受 Product Service 协议 8。
- [x] AFD01、AFD01A、AFD01B2、AFD01C 和未知型号均不会被识别为 AFD01D。
- [x] 缺少序列号与不支持协议保持两个独立状态。

## B. 客户工作区

- [x] AFD01D 在线后可建立固定 endpoint 会话并显示正确身份。
- [x] 总览、RF 控制、安装姿态、网络测试、录制回放和维护入口依据协议 8 能力呈现。
- [x] AFD01D 切换、断线和重连不会污染其他设备的 Store 或控制目标。

## C. OTA 包隔离

- [x] 制包工具接受 `AFD01D / afd01d` 并生成可验签、可解密的 `.sfpkg`。
- [x] AFD01D 会话接受匹配包。
- [x] AFD01D 会话拒绝 AFD01、AFD01A、AFD01B2、AFD01C 包。
- [x] 其他 AFD 型号会话拒绝 AFD01D 包。
- [x] OTA 传输完成和设备应用确认继续按现有独立证据合同判定。

## D. 试产工作区

- [x] AFD01D 出现在产品模板型号列表中。
- [x] AFD01D 设备以 `AFD01D + protocol 8` 通过试产身份准入。
- [x] `afd01d` 配方只匹配 AFD01D 设备。
- [x] 电压、电流和阈值来自明确的产品模板，不从 AFD01C 自动继承。
- [x] 未配置生产参数时界面明确提示缺少配置，不生成虚假默认值。

## E. 3D 模型

- [x] 用户以 `AFD01D.stl` 交付并确认开始实施；运行时不使用内部名为 `AFD01C0918_ASM` 的 STEP 文件。
- [x] 内置文件名为 `afd01d.stl`，且 STL 可以由项目加载器解析。
- [x] 模型包含有效三角面，三轴包围盒有效，无 NaN 或无穷顶点。
- [x] AFD01D 优先加载 `~/.satellite_debug_tool/models/afd01d.stl`，无覆盖时加载内置 D 型模型。
- [x] AFD01D 不回退到 AFD01、AFD01A、AFD01B2 或 AFD01C 模型。
- [x] 发行资源校验固定检查 D 型模型大小与 SHA-256。
- [x] 软件模型按真机验证后的 `+Z=机头、+X=左翼、+Y=天顶` 映射到 FLU。

## F. 自动化验证

- [x] AFD01D 产品策略、会话、RF、维护、OTA、试产配方、试产 Fleet 和模型专项测试通过。
- [x] 相邻 AFD 型号的身份和交叉包拒绝测试通过。
- [x] `python3 scripts/update_translations.py check` 通过。
- [x] `PYTHONPATH=. pytest satellite_debug_tool/tests` 全量通过。
- [ ] Windows 打包脚本会复制整个 `ui/assets` 目录，资源校验已固定要求 `afd01d.stl`；正式冻结包验证随下一次 Windows 构建执行。

## G. 真机验收边界

- [ ] AFD01D 真机回读 `hardware_type=afd01d`、`model=AFD01D`、`protocol=8`。
- [ ] AFD01D 真机客户功能完成一轮读写闭环验证。
- [ ] AFD01D 真机完成匹配 `.sfpkg` 的传输、重启、身份和版本复核。
- [ ] AFD01D 出货电压、电流、固件和参数模板由产品负责人确认后完成正式试产测试。

## H. AFD01C 共外观补充

- [x] AFD01C 使用独立文件名 `afd01c.stl`，不在运行时回退或借用 D 型文件名。
- [x] `afd01c.stl` 与用户确认的 D 型共外观几何字节一致，并分别纳入发行资源校验。
- [x] AFD01C 与 AFD01D 使用相同的 `+Z=机头、+X=左翼、+Y=天顶` CAD 轴映射。

软件专项和全量测试通过可判定软件适配 PASS；G 项没有真机证据时标记为 BLOCKED，不据此否定已通过的软件功能。
