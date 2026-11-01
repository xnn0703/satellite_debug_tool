# M51 自动化测试型号预置 操作流程与报告开发记录

本记录对应 `M51_automation_presets_workflow_reports_plan.md` 与
`M51_automation_presets_workflow_reports_acceptance.md`。实现过程中按工作包记录事实、测试证据和剩余现场项。

## 2026-11-01 基线

- 保留当前 M47-M50 未提交工作树，在其上增量实现，不清理或覆盖既有改动。
- P0 目标：固定配置编辑保真、采集恢复先于断电、SDB 真实完整性、夹具清单全量归档四项回归合同。
- P1 目标：增加 B2/C/D 的 12 V / 9 A 与 24 V / 5 A 六项只读预置，并支持无损复制、导入和编辑。
- P2 目标：建立跟随录制目录/自定义根目录的单一解析器和批次冻结路径。
- P3 目标：将准备、运行、收尾、完成状态落到现有单设备优先布局。
- P4 目标：所有终态生成可读报告，数据质量与执行结论分离，证据包按 manifest 完整收集。

## 实施记录

| 日期 | 工作包 | 修改与证据 | 结论 |
| --- | --- | --- | --- |
| 2026-11-01 | P0 | 配置编辑改为合并现有 payload，保留未知字段和完整测试参数；报告直接解析归档 SDB 并给出数据质量；夹具证据按全部 session 目录归档；正常完成先恢复采集再执行已验证断电。相关根因回归均已加入。 | 已完成（软件回归） |
| 2026-11-01 | P1 | 增加 AFD01B2/C/D 的 12 V / 9 A 与 24 V / 5 A 六项内置只读预置；增加来源、供电版本、测试计划、源模板及源 revision 元数据；内置项只能“复制为定制”，定制项继续支持手工创建、导入和编辑；持久化时过滤内置项并拒绝保留 ID 冲突。 | 已完成（真实供电能力待现场确认） |
| 2026-11-01 | P2 | 新增 `resolve_production_storage()` 单一保存路径解析器；默认跟随录制目录并保存到 `自动化测试/日期`，兼容已有自定义路径；批次创建后冻结绝对输出目录；主入口合并为“开始测试”，正常终态自动完成批次和生成报告，并提供“下一批次”清理终态。 | 已完成（真实设备收尾待现场确认） |
| 2026-11-01 | P3 | 沿用 M48 单设备优先布局与用户页配色，配置区显示“设备型号与供电”和解析后的“保存位置”；主按钮承担创建与启动，重试入口仅在已创建待启动状态出现。 | 已完成（窗口实机交互待用户验收） |
| 2026-11-01 | P4 | PASS/FAIL/INCOMPLETE/ABORTED 全部生成 V01；执行结论与报告结论分离，采集证据不完整会将报告结论降为 INCOMPLETE；写入 `analysis/data_quality.json`、批次 `batch_summary.json`；收集全部夹具 session；报告仅呈现已启用工况，并使用平台中文字体。DOCX 已实际渲染为 8 页并逐页抽检，修复末页证据索引截断。 | 已完成（批次规模性能专项未执行） |
| 2026-11-01 | P5 | 翻译资源 update/check 通过；M51 目标回归 57 项通过；Production/fixture/full-capture 相关 289 项通过；全量 1923 项通过；最终报告排版调整后报告专项 10 项再次通过。 | 软件回归通过 |

## 自动化验证记录

- `python3 scripts/update_translations.py update`
- `python3 scripts/update_translations.py check`：1480 条消息通过。
- M51 目标测试：57 passed。
- `PYTHONPATH=. pytest -q satellite_debug_tool/tests -k "production or fixture_runtime or full_capture"`：289 passed，1634 deselected。
- `PYTHONPATH=. pytest -q satellite_debug_tool/tests`：1923 passed。
- 最终 DOCX 索引分页修复后：`test_production_reporting.py` 10 passed。
- macOS DOCX 渲染：8 页，中文、表格、曲线、页眉页脚及证据索引均可见，无末页截断。

## 尚未关闭的验收边界

- 现有真实记录最大约 90 MiB/设备/30.8 分钟，最高平均写入速率约 50 KiB/s。尚未执行 200～256 MiB 单设备及 4 设备合计约 1 GiB 完整批次的流式分析性能专项；不再把单个 1 GiB SDB 作为必要验收规模。
- 正常完成链路仍需在真实电源、摇摆台、MS-6222 和 DUT 共用供电接线下验证“恢复采集后再断电”的物理行为。
- Windows 原生字体和分页尚未在 Windows 打包环境渲染确认。
- 本轮没有执行安装包构建、发布或 Git 提交。

## 现场验证项

- 六项预置的真实供电回读、支路额定能力和接线仍需现场确认。
- 摇摆台、MS-6222、DUT 共用供电条件下的完整正常收尾需实物批次验证。
- Windows 报告字体、分页和中文显示需在 Windows 环境确认；macOS 已完成实际渲染抽检。
