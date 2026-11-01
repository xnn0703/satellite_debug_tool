# M52 开发记录

日期：2026-11-02。用户已确认 M52 设计并要求开始实现。依据：[设计方案](M52_unified_update_release_plan.md)、[验收标准](M52_unified_update_release_acceptance.md)。

## 范围与工作区

- 首轮实现 P0–P3 的本地代码与测试，现有 ECS 的 IP HTTPS、低权维护和探针验收已执行；真实 Windows 安装与应用包发行分别保留独立证据。后续扩展保留独立边界。
- 保留现有 M47–M51 未提交改动，不提交、不打标签、不主动打包或触发发行流水线。
- 服务器使用已安装 Workbench CLI 与已有凭据链先做只读盘点；用户已确认直接使用公网 IP，不要求域名。实际部署以本次授权范围、现有业务和 IP HTTPS 证书为依据。

## 工作包状态

| 工作包 | 实现 | 验收证据 |
| --- | --- | --- |
| user 人工更新与 ops 策略 | 本地实现 | edition、公用入口、设置、冻结裁剪及源码 smoke；U01/U02/U04 |
| 完整安装、恢复与健康回执 | 本地实现，交叉评审修复完成 | 同盘切换、持久恢复、健康回执及故障注入；I01–I08 的软件证据 |
| 可信元数据与下载 | 本地实现 | 真实临时 TUF 仓库与模拟网络；A01–A09、D01–D05 |
| 构建、自动上传与服务器材料 | 本地工具及真实 ECS 部署完成，真实构建/应用包晋升待验 | 软件回归、真实 SFTP 权限与读回、IP HTTPS/Range/Downloader 探针；GitHub 受保护环境已配置 |
| Windows 文件级增量 | 本地实现 | 基线核验、文件新增/修改/删除、完整树验证及一次完整包回退；X01–X05/X07/X08 的软件证据 |
| 真机、云端与正式发布 | 云端服务/探针已验，真机与应用包正式发布待验 | 源码、服务器服务和实际应用升级分别记录 |

## 验证记录

1. 首次全量：`.venv/bin/python -m pytest satellite_debug_tool/tests -q`，2000 passed / 1 failed，236.90 秒。唯一失败为新增 17 条文案尚未同步翻译；完成 update、中文翻译与 check 后通过。该轮之后的交叉评审修复另行复测。
2. 下载/增量/准备/UI 组合最初 59 passed；追加无效安装身份完整包回退、主线程结果投递、后台安装准备与取消回归。
3. 后台检查旧闭包稳定复现设置及 UI callback 在 worker 线程执行；替换为主线程 QObject/@Slot，新增线程归属回归由失败转为通过。
4. 安装请求冻结由独立 worker 完成；handoff 分块 SHA/复制支持取消，失败只清理本次 UUID 维护目录。下载完成等待人员点击安装，不直接关闭应用。
5. user 与 ops 源码 `--smoke-test` 均退出 0；JSON 分别位于 `/tmp/m52-user-source-smoke-final.json`、`/tmp/m52-ops-source-smoke.json`。user 固定 manual、四页、Shell PASS，应用更新模块列表为空；ops 构造工程及自动化页面、Shell/退出通过。offscreen OpenGL 提示属于离屏环境，源码 smoke 不证明实际桌面渲染或冻结包。
6. 翻译资源 update/check、本轮模块 compileall 与 `git diff --check` 通过。最终集成测试结果在本记录末尾追加。

## 交叉评审修复与最终验证

- Windows updater 继承旧安装 cwd 会阻止 rename：交接、恢复 CLI 与试运行子进程均固定在维护目录工作。
- Popen 成功到 journal 写入 PID 之间存在恢复空窗：新版在 Settings 创建前持久化事务/PID/出生时间/EXE 回执；恢复结合精确启动环境识别进程，确认退出后才回退，未知或权限失败保留待恢复状态。
- 普通启动若在安装父目录创建锁，会使只读安装无法打开：Windows 统一改用规范安装路径对应的 Global 命名互斥量；普通启动无需写安装目录，互斥失败保持明确阻断。
- 安装/恢复/edition 组合最终 92 项通过；上述 Windows 系统 API 是模型回归，真实 Windows 登录、句柄和目录权限仍属实机验收。
- 外部签名晋升使用独立 `promote_release.py`，包含获取候选、本地准备、远端提交；模拟断线重试、渠道冲突、锁恢复、实际回读接口和 TUF 签名链专项通过。签名者与构建上传者分权；服务端没有 root/targets 私钥。
- 最终全量 `.venv/bin/python -m pytest satellite_debug_tool/tests -q`：**2039 passed，235.92 秒**。日志：`/tmp/m52-full-regression-final.log`。其后完成的晋升工具以单独集成组合补验。
- 最终更新/构建/晋升/翻译集成组合：`test_updater_*.py test_update_*.py test_release_*.py test_build_editions.py test_editions.py test_i18n.py`，**252 passed，11.20 秒**；日志 `/tmp/m52-update-integration-final.log`。包含新增本地签名/远端提交闭环，与全量结果有重叠，不相加计数。
- 上线配置后的同类集成组合再次复测：**253 passed，10.16 秒**，日志 `/tmp/m52-update-integration-deployed.log`；最新翻译 check 1497 条、git diff --check 均 PASS。2039/252/253 为不同阶段且有重叠的结果，不相加计数。
- 三项 Windows P1 修复均经独立只读复核；最终 compileall、diff 检查通过。没有执行 git commit、实际冻结打包、GitHub Actions 或正式发布。

## 实现边界与部署事实

- `release.config.json` 已配置真实生产公开 root 与 `https://60.205.157.141` IP HTTPS 入口；生产空仓库已通过实际客户端校验，不使用测试密钥或静默回退 GitHub。
- 根因替换：旧 GitHub 检查/未认证下载、跨盘目录 move、启动 Popen 即标记完成、短 wait 后丢弃 QThread 的模型已移除。TUF、下载、安装 journal、健康回执分别拥有各自事实。
- Windows 完整包和增量共用安装事务。CI 默认完整候选；增量发布人员显式提供上一正式文件树，客户端自动选用适用增量。没有基线的首版使用完整包；尚未自动获取基线或测量真实两个版本对的节省比例。
- 构建固定直接依赖并记录完整环境回执；各平台传递依赖 hash lock、实际 Windows Runner/冻结产物和系统代码签名尚未验收。TUF 签名不替代 Authenticode/Developer ID。
- 本机 Workbench OAuth 已恢复，实例 `i-2zec7150rri51esjj28f`（cn-beijing，60.205.157.141）已完成部署。Nginx 1.24、Python 3.11 独立 venv 与安全组 80/443 已就绪，原 Python 3.6 和 iperf 5201–5216 保留。详细现场记录见 [部署记录](M52_server_deployment_task.md)。
- 生产空仓库实际客户端校验版本为 root1/targets1/snapshot2/timestamp3；timestamp/snapshot/certbot/health timer 已 enabled，单次 service 均 success。正式证书到期为 2026-11-08 08:10:04 UTC；真实 ACME dry-run 于 2026-11-02 01:19:43 CST PASS，ExecMainStatus=0，同秒 Nginx journal 记录 Reloaded。
- 真实 SFTP 上传与读回、上传账号写 public 拒绝、HTTPS Range、实际 Downloader 续传 SHA 均 PASS。三并发各 1 MiB 探针合计约 7.5 秒；这些结果不代表正式应用包性能或应用升级验收。
- GitHub `release-candidate` 已设置 `v*` tag 限制、owner reviewer、上传 secrets/config variable；本轮工作流源码仍未 commit/push/触发。root/targets 私钥仅位于本机 `~/.config/satellite-release/keys`，服务端仅持低角色密钥。
- 部署后 RAM available 1500 MiB、磁盘空闲 29 GiB、load 0.02/0.03/0.00；iperf 5201–5216 复查全部监听。该数据只是现场快照，没有峰值采样。上传 SSH 的 publickey only/internal-sftp 及禁用转发/TTY 已核验。
- Nginx service 已 enabled，22/80/443 监听；SSH/Nginx 原配置备份路径见 [部署记录](M52_server_deployment_task.md)。主机指纹保存在本机 known_hosts，配置备份与签名密钥备份恢复验收分开。
- 公开 app 目录 0750 导致 404 的根因已修复为新公开目录 0755，相关 24 项测试通过；生产 release config 等补验 47 项通过。它们与已有 2039/252/253 测试可能重叠，不累计成新的全量总数。
- 真实 Windows 文件占用、登录恢复、断电、冻结构建与升级后进程，实际 CI、应用真包晋升/下载/安装、跨证书续签周期、密钥和仓库备份恢复仍待现场验收。当前没有正式应用包；已有远端 timestamp，首份真实应用包 prepare 不使用 `--initial`。

现场可复核汇总为 [M52_server_deployment_evidence.json](M52_server_deployment_evidence.json)。服务端与探针证据不替代应用包发行、Windows 实机或密钥备份恢复验收。
