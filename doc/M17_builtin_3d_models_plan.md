# M17 Plan：内置 3D 设备模型与 v1.1.1 修订发布

## 文档信息

| 项目 | 内容 |
|------|------|
| 日期 | 2026-07-31 |
| 状态 | 预发布验收完成，等待正式发布 |
| 基线版本 | `v1.1.0` / `ad76abe` |
| 目标版本 | `v1.1.1` |
| 验收文档 | `doc/M17_builtin_3d_models_acceptance.md` |
| 开发记录 | `doc/M17_builtin_3d_models_dev_log.md` |

## 1. 问题与证据

- 正式 `v1.1.0` Windows 归档中没有 `.stl` 文件或 `models/` 目录。
- 当前程序只读取 `~/.satellite_debug_tool/models/<hw_type>.stl`。
- 本机 `afd01.stl` 与 `esa01.stl` 均为 8,066,184 字节，SHA-256 相同。
- 全新电脑没有用户模型目录，因此只能显示默认长方体。

## 2. 目标

1. Windows 和 macOS 打包产物内置 AFD01、ESA01 两个 STL。
2. 用户目录模型保持最高优先级，便于现场覆盖。
3. 用户模型不存在或损坏时自动尝试包内模型。
4. 两种模型均不可用时保留默认长方体，不影响主界面启动。
5. 构建阶段自动检查冻结产物，缺任一模型即失败。

## 3. 实现方案

- 模型放入 `satellite_debug_tool/ui/assets/models/`：
  - `afd01.stl`
  - `esa01.stl`
- 复用现有 PyInstaller `ui/assets` 数据收集规则，不增加平台专属复制逻辑。
- 新增模型资源解析模块，候选顺序固定为：
  1. `~/.satellite_debug_tool/models/<hw_type>.stl`
  2. 包内 `ui/assets/models/<hw_type>.stl`
- `AttitudeWidget` 逐个尝试候选模型；单个文件读取失败不阻断后续回退。
- 新增构建产物检查脚本，并接入 Windows/macOS 构建脚本。
- 应用版本升至 `1.1.1`，沿用 GitHub 单文件 `.7z` 发布链路。

## 4. 工作区隔离

- 当前工作区已有未提交的 INS/姿态相关修改。
- 本次只提交 M17 模型资源、加载回退、测试、构建检查、版本和独立 M17 文档。
- 不提交、覆盖或回退既有未提交修改。

## 5. 验证与发布

1. 单测验证用户覆盖、包内回退和无模型返回。
2. 校验两个内置 STL 非空、内容一致且可被现有加载器解析。
3. 执行全量 pytest，warning 不得新增。
4. 在干净提交快照中执行 PyInstaller 构建或等价冻结资源检查。
5. 推送 `master` 与 `v1.1.1` 到 Gitee/GitHub。
6. 等待 GitHub Windows 单包发布完成。
7. 下载正式 `.7z`，核对两个 STL 的路径、大小和 SHA-256。
8. 验证 updater 发现 `v1.1.1` 且下载入口返回 HTTP 200。

## 6. 验收边界

- 自动化和归档检查证明模型已进入发布物。
- OpenGL 实机渲染方向沿用已验收的模型变换；Windows 原生 GPU 视觉仍属于人工验收。
- 模型进入公开 GitHub Release 后可被提取，本次按明确发布要求执行。
