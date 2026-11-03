# M61 v1.9.2 发版记录

## 发布基线

- 开始日期：2026-11-03。
- 发布前版本：1.9.1；tag `v1.9.1` 指向 `3f89817f6e9b31dbc77c798b2f63e9d58697e244`。
- 本轮范围：M56 服务器侧发布复核、M57 macOS 运维版自动安装、M58 预录制与运动可靠性、M59 轨迹编译和缓停回正、M60 独立运动时间线，以及相邻协议、测试、翻译和文档修正。
- 目标版本：1.9.2；GitHub 为正式源码/tag 远端，Windows 使用 Actions，macOS 使用本机 Python 3.11，二进制由 ECS 分发。

## 发布进度

- [x] 工作区与秘密材料审计
- [x] 发布前测试、翻译、compileall、源码 smoke 和差异检查
- [ ] 源码提交、`v1.9.2` tag 与 GitHub 推送
- [ ] Windows user/ops 候选完成
- [ ] macOS user/ops 候选完成
- [ ] 四包候选身份、CRC、SHA-256 与源码 SHA 复核
- [ ] user 完整包公开
- [ ] macOS/Windows ops TUF 正式晋升
- [ ] fresh checker、公开下载和最终发行记录复核

## 发布前门禁

- 当前 80 个变更/新增路径已逐项归入 M56～M61、相邻协议/录制修正、测试、翻译和文档；未发现构建目录、下载缓存、私钥或访问密钥特征。
- 全量测试：`2266 passed in 276.53s`。
- 翻译：1,508 条完成、0 unfinished；`compileall` 和 `git diff --check` PASS。
- user 源码 smoke：版本 1.9.2、`application_update_policy=manual`、operations modules 为空、Shell/总览 PASS、正常退出。
- ops 源码 smoke：版本 1.9.2、`application_update_policy=online`、运维模块存在、Shell/总览 PASS、正常退出。
- offscreen 的 OpenGL context 警告来自无图形上下文测试平台；两个进程均返回 0 且结构化 smoke 证据完整，不作为原生 GPU 验收。

## 证据边界

- v1.9.2 是首个包含 M57 macOS 自动安装器的桥接版本；v1.9.1 到本版仍需人工替换。
- macOS 当前为 arm64 ad-hoc 签名，未完成 Apple notarization；Windows 系统签名、真实系统升级和设备/夹具/RF 现场验收独立记录。

## v1.9.2 停止结论

- 源码提交/tag：`9268ba850608f718103fd0382fa5e2548fe7e5f4` / `v1.9.2`，保持不可变。
- tag Actions `41621070816` 的 Windows user 源码回归稳定报告 1 项失败：Windows 墙钟分辨率使 FULL 覆盖区间为 0。
- v1.9.2 未进入正式 TUF 或 `/downloads` 目录，不作为正式已发布版本。
- 修复转入 M62 和新版本 v1.9.3；不改写 v1.9.2 tag 或候选字节。
