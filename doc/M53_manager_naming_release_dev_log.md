# M53 开发与发布记录

2026-11-02。用户确认命名并授权发布 v1.9.0；计划与验收先行。当前 HEAD 479e014，工作区 M47–M52 为本次对话已授权实现，随本次功能版本交付；发布仅用 github 和既有 ECS。

## 进行中

- 核对名称、更新目标与完整包合同。
- Windows/Mac user/ops 完整包，首版人工迁入新在线站。
- 后续追加实际测试、commit/tag、CI、归档/上传/晋升与下载证据。

## 发布前修订

- 新建共享 package_names 单一文件名合同，Windows/macOS构建、归档、冻结启动、更新/健康/恢复均使用Manager入口；定向212项通过。
- 名称与翻译、报告更新完成，31项相关回归通过，翻译1497条通过。
- 用户选择服务器直链交付，下载目录只允许GET/HEAD，完整user包不进入在线更新渠道；本机Downloads不增加副本。
- Mac构建建立独立Python3.11环境，NumPy2.4.4实际wheel为cp311-macosx_14_0_arm64；PySide6 6.11为macosx_13_0_universal2，最低Mac声明修正14.0。Qt Windows平台参考：https://doc.qt.io/qt-6.11/windows.html （Windows10 1809+，本轮产品最低准入设10.0.19041，物理旧系统仍独立验收）。

## 源码验收

- 发布前全量：`.venv/bin/python -m pytest satellite_debug_tool/tests -q`，2063 passed / 246.05 秒，日志 `/tmp/m53-full-tests.log`。新增服务器人工交付工具若在此后完成，将单列专项及构建环境全量。
- 翻译检查1497条、compileall和git diff --check通过。
- GitHub master与本地HEAD均479e014，v1.9.0标签/Release尚不存在；本轮源码/tag使用github，应用二进制使用ECS。
- 服务器完整user交付新增13项专项通过：receipt/包内身份、CRC、SHA、公开权限、同名冲突及安全路径；只复制已校验完整包，不进入ops在线渠道。
- 本地AGENTS已按用户要求同步M47–M53实际领域owner、预置、默认录制路径、报告质量、命名、构建与直链交付。

## Mac Python 3.11 线程归属修复

- 发布专用 Python 3.11 / PySide6 6.11.0 的同进程顺序 `test_application_icon.py`、`test_customer_session_binding.py`、`test_i18n.py` 重现 SIGSEGV；Qt 原生栈为 `QIcon → QFactoryLoader::instance → QObject::moveToThread`，前面存在 `Python GC → Shiboken → QThread` 析构。同版本 Python 3.13 全量通过不能代替发布环境验收。单独换用 PySide6 6.11.2 仍重现，未把问题归因于升级已经修复的 PYSIDE-3307。
- 根因证据：发布环境 `QtCore.abi3.so` 的 `Sbk_QObjectFunc_thread` 反汇编在返回 `QThread` wrapper 后执行 `Shiboken::Object::setParent(self, result)`；`QThread.currentThread()` 的绑定没有此操作。`shiboken6.dump` 记录共享主线程 wrapper 的 Python parent 随 `EndpointSessionRuntime._assert_owner_thread` / `CustomerDeviceDirectory._assert_owner_thread` 改变。两对象销毁后原主线程 wrapper 的 `isValid` 稳定变为 False，说明一次线程归属查询污染了共享线程的包装对象生命期。这里的 Python parent 不是 Qt 原生 QObject parent。
- 两个领域 owner 均在创建线程内运行，没有 `moveToThread` 合同；构造时只从 `QThread.currentThread()` 捕获不可变 `_owner_thread`，后续入口比较当前线程与该 owner。删除原有 `QObject.thread()` 查询路径；更新器两项线程测试也改为从当前 GUI 线程捕获对照事实。没有增加进程级 keepalive、GC 开关或插件重试。
- 新增 `test_session_owner_thread.py` 覆盖 Runtime/CustomerDirectory 销毁及 GC 后主线程 wrapper、图标插件继续有效，并用真实另一条 Python 线程确认 owner 拒绝调用。旧实现 `2 failed / 2 passed`（`/tmp/m53-owner-thread-old.log`），修复后与原崩溃三文件合跑 `28 passed / 5.33s`（`/tmp/m53-owner-thread-new.log`）。
- Runtime、客户目录、客户会话绑定、更新器与新回归合跑 `55 passed / 1.04s`（`/tmp/m53-owner-thread-adjacent.log`）；项目全部 Python 源码已没有 `.thread()` 调用。发布环境全量和冻结构建由发布流程继续记录。

## 试产测试夹具的控件所有权修复

- 发布环境全量越过原线程崩点后，在约 58% 的 `test_production_workspace.py` 夹具初始化触发新的 SIGSEGV。cocoa 与默认 offscreen 均出现 `QApplication.allWidgets → QList<QWidget*> 转 Python list → PySide::getWrapperForQObject` 的坏对象访问，属于独立的测试夹具问题。原始证据为 `/tmp/m53-mac-release-final.log`、`/tmp/m53-python311-offscreen-fixed.log` 与 `Python-2026-11-02-020321.ips`。
- 纯 Qt 最小复现 `/tmp/m53_allwidgets_gc_repro.py`：暂停 GC 创建 100 个自循环 QWidget，每个带 QTableWidget、QDialogButtonBox 和布局，释放局部引用，将 GC 阈值设为 `(1, 1, 1)`，恢复 GC 后调用 `app.allWidgets()`。cocoa/offscreen 均 exit 139；同程序保持 GC 关闭可完成并返回 2300 个控件。诊断报告 `Python-2026-11-02-020514.ips` / `Python-2026-11-02-020536.ips` 同样包含 `getWrapperForQObject → QList<QWidget*> 转换 → allWidgets`；前序控件只由循环引用维持，返回列表包装分配触发回收，使列表后续原生指针失效。
- 夹具改为在构造边界收集本测试新建的 MainWindow/ProductionWorkspace，明确持有到受控 close、DeferredDelete 与 GC 收尾；删除两次进程全局 `allWidgets()` 枚举及 baseline 集合。生产源码没有 `allWidgets()` 调用。本修复没有禁用 GC、延迟回收或修改业务代码。
- 新增 `test_production_widget_ownership.py`：禁止全局枚举，确认本项实例在局部引用消失后仍由夹具持有、退出时回收，并且预存工作区继续有效。旧实现稳定因调用全局枚举失败（`/tmp/m53-production-widget-owner-old.log`）；修复后与全部工作区测试合跑 `29 passed / 3.62s`（`/tmp/m53-production-widget-owner-new.log`）。发布环境全量继续单独记录。

## 大文件上传与候选更新

- SFTP 写启用流水线；远端摘要与候选拉取按已打开文件的实际长度、32 个并发请求预取。封口仍由关闭后的完整 size/SHA-256 回读确认，任何关闭、读取异常或内容差异均阻止 rename。Paramiko 4.0.0 的流水线响应实现与文档中的关闭错误承诺有差异，因此以最终字节验证作为权威事实。
- 上传/晋升专项 `29 passed / 0.94s`，覆盖续传、并发上界、关闭/预取异常、空文件/截断/等长篡改拒绝封口；实际速率留待正式包传输记录。
- 初始发布提交 `d9ecd9d52fc6fc017ef980d755d0a01a92a1b5f5` 的 Actions `41333023236` 已取消，包尚未进入服务器候选区或公开目录；完成上述修复后，未公开的 v1.9.0 标签和四包统一指向新提交。

## 客户版使用产品本名

- 用户最新确认“客户版本不用注明 user，只全量版本名称注明”。先更新 M53 计划与验收，再修改唯一文件名合同：客户目录/app 为 `SatelliteTerminalManager` / `SatelliteTerminalManager.app`，客户归档为 `satellite_terminal_manager-{platform}-{arch}-v{version}.{extension}`；运维全量版保持目录/app `Ops` 和归档 `-ops-`。`UserWindow` 版本栏仅显示 `vX.Y.Z`，中英文产品名称沿用已确认名称。
- Windows/macOS 构建、冻结 smoke、发行候选及人工交付均消费 `package_names.py`；同步 spec/构建脚本注释、BUILDING、RELEASING、v1.9.0 安装说明和 AGENTS。内部 Edition.USER、构建身份、bundle identifier、配置路径及权限边界保持既有合同。
- 命名、冻结入口、两版构建、人工交付和客户窗口回归在旧源码上为 `18 failed / 30 passed`（`/tmp/m53-customer-plain-name-old.log`）；失败包括客户目录、归档及版本栏断言。修复后加入相邻发行候选回归，共 `64 passed / 3.49s`（`/tmp/m53-customer-plain-name-new.log`）。
- 翻译 update/check 通过，删除已失效的 User edition 文案，现有 1496 条。真实冻结构建、最终 SHA 四包、上传及公开下载继续由发布步骤验证，本次专项不代替这些证据。


## 命名候选构建验证（已被后续 Windows 修复取代）

- 命名候选源码为 `677d5f90bb7718245c03dd29f0fa5534c0cdb028`；客户版使用产品本名，运维版保留 Ops。前一命名候选的 Windows run `41335191501` 按用户新命名要求主动取消，旧名 Mac 客户直链已撤回，线上 ops 元数据尚未切入旧候选。
- 发布环境 Python 3.11.15 / PySide6 6.11.0 / offscreen，全量 `2086 passed / 239.80s`；翻译 `1496` 条通过。Windows run `41336187180` 出现测试失败及原生 MessageBox 等待，主动取消并继续修复，不作为 Windows 放行证据。
- Mac arm64 两版均完成构建身份/模块边界、ad-hoc 深度严格签名验证、归档 CRC 与 Cocoa 原生冻结启动；客户版包含四个客户页面且应用在线更新模块为空，运维版使用 online 策略。最终包与服务端校验以本轮 JSON 验证记录为准。
- 诊断时非标准 cocoa UI 子集的 SIGABRT 已核对原生 `Python-2026-11-02-020455.ips`：`QTest::sendKeyEvent(QWindow*) → qt_assert`，测试将桌面焦点对象作为键盘目标时得到空目标。项目自动化 UI 验收保持 conftest 的 offscreen 合同；真实 Cocoa 启动由独立冻结 smoke 验证，与全局枚举/垃圾回收故障分别记录。

## Windows 原生测试门禁与资源修复

- 初始 Actions `41333023236` 的源码 step 表面成功，日志实际包含 pytest 失败及 `0xc0000374` 原生堆损坏，后续翻译命令返回 0 覆盖了 PowerShell step 退出码。工作流拆成独立 pytest 与翻译 step；pytest 独立接收完整测试结果并输出长等待调用栈，失败阻止构建。新门禁回归旧工作流 1 failed，修复后 1 passed。
- Windows `Path.home()` 读取 USERPROFILE，旧测试只改 HOME，配置和 ProfileCache 跨项串扰。全局逐项 fixture 同时隔离 HOME、USERPROFILE 和导入时生成的默认缓存目录；两项相同键/型号的回归旧实现 2 failed，修复及相邻配置测试 115 passed。
- `PlaybackSeriesProvider.load_window()` 原 `with sqlite3.connect()` 只结束事务，未关闭连接，Windows 文件句柄阻止缓存清理。改为 `contextlib.closing`；原始/峰值查询与异常物化三条路径旧实现 3 failed，新实现与候选包测试 22 passed。
- Shell 先入队断开原因、再设置 stop Event 的双路径会让线程跳过队列而回报 `closed`。worker 统一冻结首个停止意图并唤醒，controller 单次提交，正常与满环收尾共用原因；删除断开命令和重复收尾分支。屏障回归旧实现稳定失败，新管线 11 passed，worker/controller/UI/M49/YMODEM 邻接 109 passed。读取预算、等待窗口和吞吐标准保持原合同。
- Windows 测试的归档路径按 POSIX manifest 合同对比，文本日志按实际 UTF-8 读取；回原点稳定窗口从首个反馈已被消费的事件开始计算，避免把队列写入当作已收到反馈。GNSS 换行按真实字体 sizeHint 判定，保留控件完整显示与互不重叠条件。
- GNSS 独立首次展示仍能重现控件被裁切：全局焦点样式添加 1px 边框，而复选框/单选框普通态没有边框，焦点进入后控件尺寸发生改变。普通态预留同宽透明边框，焦点只改颜色，保留原生 FlowLayout 与 Qt 尺寸合同。三主题两类控件旧实现 6 failed；修复后 GNSS 独立首次展示通过，样式/状态条/布局及其它 Windows 可移植性专项共 193 passed。
- `41336187180` 取消后的栈证实主线程停在 `updater.main._show_windows_failure → MessageBoxW`，由失败事务测试触发原生交互。测试隔离提示框边界并核对提示内容；实际更新器继续向用户显示错误。此证据替代此前对 TimeRangeControl 卡顿的推测。
- 更新器测试同时隔离 HKCU 登录恢复注册/清除边界，真实归档、文件锁、安装切换和回滚继续执行；各路径核对提示次数、阶段、详情和日志路径以及注册动作。原生 UI fail-fast guard 下旧失败场景确实进入 MessageBox，新模块 30 passed，与既有 fake-winreg API 验收合跑 46 passed。
- 两轮旧 Mac 客户包已撤回，公开下载目录为空；旧候选仅留 incoming，正式 TUF targets 仍为空。后续四包使用统一的新源码 SHA 重新构建和校验后再公开。
- 上述修复合并后，发布 Python 3.11.15 / PySide6 6.11.0 / offscreen 全量 `2102 passed / 278.17s`，日志 `/tmp/m53-final-windows-fixes-mac-tests.log`；1496 条翻译、compileall 与 diff 检查通过。Windows 原生结果仍由下一轮独立 step 验证。
- `98fad1f` 的 Windows run `41339064257` 两版均被独立 pytest step 正确阻止（各 753 passed 后 1 failed，后续构建全部 skipped）。失败为 GNSS 测试在 1024 px 写死摘要字号 12，Windows 实际合法降档为 11；此前的宽度、完整显示和互不重叠检查均通过。按 M15 原合同，字号由实际字体与可用宽度决定。测试加入 150% 字宽稳定复现旧固定值失败，保留最小 10px、宽度增大字号单调、实际足够宽时恢复 12/14px，并加强文本实际边界完整显示检查；生产字号算法保持。
- 下一轮 Windows 运行完整测试集以同时收集剩余故障，独立退出码及失败阻止构建规则保持。先完成 Windows 源码门禁，再启动新的 Mac 正式候选，避免构建已被替代的源码。
- 字宽回归普通/宽字两项通过，GNSS、主题、状态条和 FlowLayout 邻接共 144 passed / 2.61s；CI 门禁专项 1 passed，diff 检查通过。

## Windows 剩余故障的接收顺序与吞吐证据

- `8334c1c` / Actions `41339575019` 在完整源码门禁中再次出现失败，修复期间主动取消；没有进入构建或上传。进度与 collect 顺序映射到持续 Shell 吞吐及跟踪仿真 STOP 用例，取消前没有最终断言汇总，不能将映射当作完整故障栈。取消时打印的原生访问异常也不作为独立自然崩溃证明。
- 跟踪仿真将单调接收时间当作严格先后序列，同一时钟刻度内新收到的 STOP 回读会被判为旧数据。固定 StateStore 的接收时钟，原既有网关测试的状态 1→0、旧缓存 0→新同值 0 两条路径均稳定无法确认停止。StateStore 改为唯一每报告递增 revision；同值回读推进接收序列，clear/过期保留递增基线，时间字段继续表示新鲜度。STOP 冻结并比较 revision，旧时间因果 cursor 及证据键已移除。
- 旧状态模型回归 6 failed / 13 passed，修复后的 StateStore、网关、跟踪仿真、SessionCore、LegacyV2 和状态呈现邻接 101 passed；证据 `/tmp/m53-state-revision-old-fail.log`、`/tmp/m53-state-revision-targeted.log`。两个 Tracking UI 测试的 finally 显式停止自己拥有的 timer、关闭并定向销毁窗口。
- Shell 吞吐测试原在断言之后才关闭 workspace/controller，性能失败会遗留线程及满载文档。修复测试 owner 收尾并在原测量窗口加入有界帧级诊断，分别记录帧间隔、等待、解析及投影耗时；生产帧预算与 92,160 B/s 门槛保持。下一轮真实 Windows 数据用于定位吞吐原因，Mac 专项通过不代替 Windows 验收。
- 测试 cleanup 消费既有异步关闭合同，先等待 `shutdown_complete` 完成分帧排空，再销毁本项窗口。原缺少 finally 的两项回归失败；异步 pending 场景也在旧直接销毁路径下两项失败。最终吞吐与 M49 收尾专项 23 passed / 22.41s，证据 `/tmp/m53-shell-async-cleanup-old.log`、`/tmp/m53-shell-async-cleanup-new.log`。发布 Python 3.11 的 StateStore/Tracking 邻接复跑 101 passed / 1.28s。
- 本轮合并后发布环境全量 `2112 passed / 283.18s`，日志 `/tmp/m53-revision-shell-diagnostics-full.log`；1496 条翻译、compileall 与 diff 检查通过。Windows 同 SHA 验收继续执行。

## Windows 实际帧周期与运动反馈测试修复

- `f5ca4dd` / Actions `41341875594` 的 Windows user 完整源码结果为 `2 failed, 2105 passed, 5 skipped / 576.25s`（`/tmp/m53-windows-f5ca-user-api.log`），失败分别为运动测试的 `controller feedback lost during motion` 和持续 Shell 吞吐。StateStore 新接收序列与其余回归通过。构建被独立 pytest step 阻止。
- 同轮 Windows ops 为 `1 failed, 2106 passed, 5 skipped / 877.45s`，仅 Shell 持续吞吐失败（56,832 B/s，注入91,238 B/s）；平均帧间隔71.943ms，帧后空等44.026ms，frame/screen最大229.393/217.282ms。两版均跳过构建与上传，原始日志 `/tmp/m53-windows-f5ca-ops-api.log`。运动测试在两台 Runner 上结果不同，与已独立复现的接收调度前置问题一致。
- Shell 114 帧全部消费 4096 字节；典型处理耗时 16.468ms，帧后空等中位数 46.545ms，平均帧起点间隔 69.807ms，持续消费 58,368 B/s，注入 90,317 B/s。单次 timer 原在处理完成后重新等待 33ms，且默认使用 CoarseTimer。已改为一个 PreciseTimer/deadline owner，下一周期从本次实际起点计算，处理结束只等待剩余时间；超期只排一次异步回调，不循环追帧。同步消费不启动实时排程；重复 pending、断链、终排空共用同一 owner。
- 假时钟旧实现 4 failed / 4 passed，新模型 8 passed，证明帧处理时间包含在周期内、超期/早醒边界、同步入口、重复通知与终排空。Shell、交互、断开、M49/YMODEM 邻接 `129 passed / 29.88s`（`/tmp/m53-shell-cadence-specialized.log`）；Mac 实际持续消费 92,288 B/s，注入 93,082 B/s，积压峰值 10KiB，帧 p50/p95/max 为 14.403/22.764/66.948ms，输入 P99 42.3ms，洪水断开 49ms，300KiB 收尾 1.42s。Windows 新模型仍需独立验收；旧 Windows 的 265ms 长帧没有达到 M45 的150ms退化目标，继续保留分段耗时证据。
- 运动测试原把反馈入队后 sleep 当成已建立稳定窗口；接收调度延迟可让两条反馈在同一刻度被消费，生产正确拒绝。四条移动/回原点/残差场景统一确认首帧已消费，并依据公开接收时间与实际配置证明稳定窗口已到后再注入第二帧。仓库加入慢接收同刻度的实际动作结果回归，旧 sleep 流程稳定失败，新流程与全部 component 测试共14 passed；生产运动算法和门限保持。证据 `/tmp/m53-motion-repo-regression-old-sleep.log`、`/tmp/m53-motion-repo-regression-fixed.log`。
- 性能诊断只在原8秒窗口被动订阅 GC callback，按代际和实际帧/阶段记录重合耗时，finally 移除；没有主动收集或改变阈值。Mac 单项长 screen 帧57.158ms中有48.563ms与GC重合，此事实只证明该次Mac测量。新增观测与清理专项7 passed；单条有界JSON在 `capfd.disabled()` 中输出并flush，成功测试也能保留指标，其它pytest捕获保持。Windows的GC原因留待同平台实测。
- 帧调度改后的阶段全量为 `2123 passed / 280.71s`（`/tmp/m53-cadence-motion-full.log`），但被动数据记录frame max159.367ms、screen154.102ms，其中147.914ms与GC重合；软件断言通过未被写成150ms退化目标通过。独立单项与全量GC长尾不同，说明全进程活对象也影响停顿，不能只凭阶段标签归因给某个类型。
- 纯真实Screen的5000行探针在三个独立发布Python3.11进程保留329,934个Cell及9,998个Style，默认GC enabled/(700,10,10)。在读取 `__dict__` 前取得tracemalloc实际占用：基线36,773,822B，Style/Cell slots候选23,176,446B，slots+行尾直接追加23,176,382B，约减少37%。100字符普通追加的Cell构造从200个临时blank+100个glyph降为100个glyph。直接读取 `__dict__` 会物化Python3.11的延迟字典，因此未用该访问后的表观字节夸大收益；原始记录 `/tmp/m53-terminal-budget-{baseline,slots,slots_append}.json`。
- 根据分配链和实测，Style/Cell保持冻结值语义并使用slots，正常width1行尾追加在组合字处理和容量检查之后直接保存最终单元；覆盖、宽字与游标补空继续使用既有模型。资源预算回归旧源码稳定2 failed / 39 passed，修复后终端语义、cadence及运动组件共63 passed；独立review确认字段读取、style快照、dataclasses.replace与光标/宽字边界一致。GC策略保持原样，完整帧耗时与Windows结果继续独立记录。
- 最终合并工作树发布环境全量 `2130 passed / 276.63s`（`/tmp/m53-cadence-cell-motion-full.log`）；真实观测窗口累计消费737,280B，即92,160B/s，积压峰值10KiB，frame p50/p95/max为14.350/21.690/134.798ms，最长帧GC重合119.823ms。此轮Mac满足150ms退化目标，Windows继续用相同源码独立验证。1496条翻译、compileall与diff检查通过。
