# M70 macOS 自动更新身份根因修复计划

## 已确认根因

- PyInstaller 的 macOS onedir app 将数据文件放在 `Contents/Resources`。v1.9.8 正式包与本机 v1.9.3 安装中的唯一 `build_identity.json` 均位于 `Contents/Resources/satellite_debug_tool/`。
- 更新器却从 Windows `_internal` 布局派生了 `Contents/MacOS/_internal/satellite_debug_tool/build_identity.json`，因此下载和摘要校验完成后在载荷准备阶段稳定失败。
- macOS 自动更新测试也按错误目录构造了 app，测试与真实构建共同复用了错误假设，未形成独立门禁。

## 实施范围

1. `install_platform.identity_path()` 按平台直接返回唯一正式身份路径：Windows 保持 `_internal/...`，macOS 改为 `Contents/Resources/...`；删除不再表达真实合同的共用 runtime 路径推导。
2. macOS 自动更新测试使用真实 PyInstaller bundle 布局，并覆盖候选解析、已安装身份、文件树校验、事务切换与回滚。
3. 冻结包校验不再用任意 `rglob` 证明身份存在；按正式平台路径验证唯一、完整、匹配的构建身份，防止测试和打包再次共同读错目录。
4. 不复制第二份身份、不放宽身份校验、不通过重试绕过失败。
5. 迁移边界：v1.9.2～v1.9.8 的 macOS 已安装更新器本身携带错误路径，无法通过远端新包修复其安装前检查；这些版本必须一次性安装修正版完整包。修正版以后恢复正常自动更新。Windows 不受该目录问题影响。

## 发布与验证

- 以 v1.9.9 作为修正版候选，先完成源码回归、真实 macOS 冻结 app 构建与候选包校验，再运行统一发布脚本。
- 在本机用 v1.9.8 正式归档证明新代码可解析真实身份；用真实布局的签名临时 app 执行完整事务回归。
- 服务器公开包、签名目录、fresh checker、摘要和 Range 仍按 M69 单 publication 流程验证。
- 本机当前 v1.9.3 完成一次完整包替换后，再以更高测试版本或受控候选验证后续自动更新；不能把手工替换表述为 v1.9.3 自动更新成功。
