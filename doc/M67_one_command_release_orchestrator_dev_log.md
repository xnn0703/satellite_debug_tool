# M67 单入口发版编排器开发记录

日期：2026-11-12。

## 开始前事实

- Windows user/ops 已由 GitHub Actions 构建并上传候选；macOS user/ops 已由发布 Mac 本地构建。
- ops 的 TUF `fetch → prepare → commit`、ECS 本机完整 SHA-256 和公网 Range 已分别有权威 owner。
- 四份友好完整包、`SHA256SUMS.txt` 与 `release.json` 尚无单一原子 owner；发布操作需要人工拼接相同 version/commit/path。
- v1.9.3 证明现有组件可完成正式发行，但手工步骤多、重复参数多，且旧流程曾经通过 3 Mbps 公网完整回读大包。

## 实施记录

- 新增 `scripts/release.py`：
  - `plan/run/resume/status/verify` 单入口；`plan` 和 `--dry-run` 不创建 journal、不执行外部写操作。
  - journal 冻结 version/tag/source commit/release ID、Actions、四候选 receipt、两个 publication 和服务器回执。
  - 同版本运行使用 `flock` 互斥；阶段完成后原子写 journal，失败可从已证实阶段续跑。
  - tag 确认绑定完整 commit，stable 确认绑定两个 publication 的 bundle-set SHA；没有通用 `--yes`。
  - 不执行 `git add` 或源码 commit；Windows/macOS/TUF/ReleaseChecker 都调用既有 owner。
- `publish_manual_delivery.py` 抽出只读候选检查和通用完整归档身份/CRC检查；user 继续额外执行 updater 裁剪边界，ops 使用其自身 edition 验证。
- 新增 `scripts/publish_release_downloads_server.py`：
  - 请求 ID 是请求正文 canonical JSON 的 SHA-256，绑定 version、commit、两份 user receipt 和两个 signed ops catalog。
  - user 只从固定 incoming 根读取；ops 通过可信 root 验证 `targets.json` 签名，再读取 catalog 指定的摘要命名 target。
  - 所有路径逐组件拒绝符号链接；四包完整 SHA-256、ZIP/7z CRC、包内 build identity 均在 ECS 本机验证。
  - 用临时目录生成四包、`SHA256SUMS.txt` 和 `release.json`，最后目录级原子 rename；同版本不同字节拒绝，完全一致返回 `reused`。
  - root 写结果和归档回执，发布账号只投递请求并读取结果。
- 新增 `satellite-updates-publish-downloads.path/service` systemd 模板；服务无网络能力，只读 incoming/正式 TUF 仓库，只写 downloads 和专用 receipts。
- 更新 `doc/RELEASING.md` 为当前唯一 SOP，并补齐单入口命令与首次部署要求。

## 当前软件证据

- 新增及相邻发行专项：64 passed。
- 发布/更新扩大回归：254 passed，2069 deselected。
- 项目全量回归：2325 passed，耗时 272.37 秒。
- 翻译目录：1558 messages，PASS。
- `compileall`：PASS。
- `git diff --check`：PASS。
- `scripts/release.py run --version 1.9.3 --dry-run` 在当前开发 HEAD 明确报告脏工作区及旧 tag 指向其它 commit，没有产生 journal 或外部写操作。
- 扩大回归首次稳定复现 `GLGridItem` 新 API 拒绝 tuple，根因修正为 `QVector3D` 后健康窗口专项 2 passed，并纳入上述全量结果。

## 未执行边界

- ECS 安装服务器脚本和 systemd path/service，核对权限与 root 回执不可写边界。
- 使用隔离请求完成四包原子发布演练；不切换真实新版本 stable。
- 一次真实新版本发版才能验证 GitHub Environment 等待、Mac 正式构建、两个 publication commit 和 fresh 客户端全链路。

Windows/macOS 实机安装与回滚、Apple notarization、真实设备/RF/电源/夹具仍为独立 `NOT_RUN` 边界。

## 首次正式运行复核（2026-11-13）

- `v1.9.4` tag 已不可变地绑定 `442cdaebbcd8be21a612b364445584545eafeb75`；Windows 两个 edition 在候选生成前的源码回归同时失败，没有候选包、TUF stable 或服务器下载目录被晋升。
- Windows 回归确认三项平台合同问题：发布锁只实现 `fcntl`、Windows 不能用 `os.open` 打开目录执行 fsync、图表布局误用可能被内容最小尺寸撑宽的 workspace 宽度。
- 发布锁改为 POSIX `flock` / Windows `msvcrt.locking` 单一跨平台 owner；服务器原子发布只在 POSIX fsync 目录，文件 fsync 和原子 rename 保持全平台合同。
- 图表列数改由批次滚动区实际 viewport 宽度决定；运动平台隔离时间线测试保留 Windows 真实调度空洞的 `timing_fault`，以不跳点、完整收包和 P95 间隔验证父进程慢处理不会系统性拖慢发送。
- 修正后版本递增为 `v1.9.5`，不移动或复用失败的 `v1.9.4` tag。
- `v1.9.5` Windows 回归进一步证明默认计时粒度使真实 20 ms UDP 时间线 P95 达到 32 ms；发送子进程现于 Windows 生命周期内成对调用 `timeBeginPeriod(1)` / `timeEndPeriod(1)`，不以放宽时序门限掩盖故障。
- Windows `chmod` 不能表达 POSIX `0600/0644`，服务器工具在 Windows 明确使用独立复制，源字节不变；权限位断言仅属于 POSIX。正式版本继续递增为 `v1.9.6`，两个失败 tag 均保持不可变且没有候选晋升。
- `v1.9.6` 证明 `timeBeginPeriod(1)` 不能改变 `multiprocessing.Queue.get(timeout)` 所用 Windows 同步对象的超时量化，真实 UDP P95 仍为 32 ms。时间线等待改为非阻塞命令轮询、高分辨率 sleep 与最后 1 ms deadline guard；回归测试明确拒绝阻塞队列等待。正式版本递增为 `v1.9.7`。
- Windows Actions 在全量源码回归前增加平台合同快速门禁，优先执行真实 UDP 时间线、发布互斥和服务器文件语义测试；它不替代后续全量回归，只把平台错误提前到候选构建前数秒暴露。
- `v1.9.7` 快速门禁在约 10 秒内暴露剩余断言仍使用父进程 UDP 接收线程时间；Windows 会批量调度该观察线程到约 32 ms。门禁改为消费时间线子进程在每次 `sendto` 后回传的权威 `p95_send_interval_ms`，同时继续要求全部 UDP 包到达；正式版本递增为 `v1.9.8`。新 tag 前先用 master CI-only 验证 Windows 快速门禁和全量回归。
