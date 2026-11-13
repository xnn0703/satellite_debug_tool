# M70 开发与复核记录

## 2026-11-13 根因证据

- 用户界面：当前 v1.9.3，载荷准备报错 `apply_payload identity: update payload requires a build identity`。
- v1.9.8 正式 ops ZIP 内身份：`SatelliteTerminalManagerOps.app/Contents/Resources/satellite_debug_tool/build_identity.json`，内容为 ops / mac / arm64 / 1.9.8 / `69ab71f...`。
- 本机 `/Applications/SatelliteTerminalManagerOps.app` v1.9.3 身份同样位于 `Contents/Resources/satellite_debug_tool/`，bundle 深度签名校验通过。
- 当前 `identity_path(..., "macos")` 返回 `Contents/MacOS/_internal/satellite_debug_tool/build_identity.json`；真实 app 中该目录不存在。
- `test_macos_automatic_update._app()` 人工把身份创建在上述错误目录，解释了源码全量测试通过但正式包失败。
- v1.9.3 与 v1.9.8 的身份路径代码相同；错误自 v1.9.2 引入 macOS 自动更新后持续存在。下载重试、SHA 重算和服务器换包均不能改变该本地路径错误。

## 迁移结论

旧主程序会在关闭前用自身旧代码解析新载荷；旧 updater 关闭主程序后还会用同一错误路径读取旧安装身份。因此仅发布一个修正版远端包不能让 v1.9.2～v1.9.8 自举成功，必须完成一次完整包替换。实现不得伪造为无缝自动迁移。

## 实现与源码验证

- `identity_path()` 删除虚假的共用 runtime 推导，macOS 直接指向 `Contents/Resources`，Windows 保持 `_internal`。
- `verify_edition_package.py` 与 `release_artifacts.py` 按平台正式路径要求恰好一份身份；旧目录或任意位置同名文件均被拒绝。
- macOS 自动更新 fixture 改用真实 Resources 布局，并增加旧 MacOS/_internal 布局拒绝回归。
- v1.9.8 正式 ops ZIP 完整解包后，候选根、版本、edition、codesign 和身份路径通过新代码验证。
- 更新/发布/edition 相邻回归：359 passed；定向身份与候选回归：51 passed。
- 翻译检查：1558 finished / 0 unfinished；`compileall` 与 `git diff --check` 通过。
- 稳定最终源码全量回归：2354 passed in 266.16s。

## v1.9.9 Windows 门禁拒绝

- GitHub Actions run `43363590015` 的 user/ops job 均在候选构建前停止。
- 两个 job 的产品回归结果相同：2338 passed / 14 skipped，仅新增的两项错误消息断言失败。
- 根因是测试期望字符串写死 `Contents/Resources`，Windows `Path` 正确输出 `Contents\\Resources`；被测代码已抛出预期异常，失败只在测试消息的路径分隔符表现层。
- 修复将断言绑定到领域关键词 `Resources`，不改变应用身份逻辑、不放宽构建门禁。v1.9.9 tag 保持不可变且不重建，后续发行版本为 v1.9.10。
- v1.9.10 最终本地快照定向回归 2 passed；全量回归 2354 passed in 265.00s；翻译 1558 finished / 0 unfinished。
