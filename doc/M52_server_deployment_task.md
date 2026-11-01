# M52 服务器部署任务与现场记录

日期：2026-11-02。执行对象：用户现有北京 ECS，Alibaba Cloud Linux 3、2 vCPU/2 GiB、3 Mbps。用户已选择由本机会话通过 Workbench 直接配置，并明确首轮使用公网 IP，域名为后续可选项。

## 当前连接与部署事实

- 本机已安装 `workbench v1.0.1` 与 `aliyun`，已有 `CredentialsCmd` 认证配置。
- OAuth 已恢复，已核对并部署实例 `i-2zec7150rri51esjj28f`，地域 `cn-beijing`，公网 IP `60.205.157.141`。早期 `invalid_grant` 是已解决的连接问题。
- 已安装 Nginx 1.24 和独立 Python 3.11 venv；系统原有 Python 3.6 与 iperf 5201–5216 保留。ECS 安全组已新增 80/443。
- Nginx service 已 enabled，22/80/443 实际监听。原配置备份位于 `/etc/ssh/sshd_config.satellite-update-backup-20261102` 和 `/etc/nginx/nginx.conf.satellite-update-original`；主机指纹保存在本机 known_hosts，不在文档展开。
- 正式 IP HTTPS 证书已签发，到期时间为 **2026-11-08 08:10:04 UTC**；实际 `https://60.205.157.141/health.txt` 返回 200。
- timestamp、snapshot、certbot、health 四个 timer 已 enabled，单次 service 均 success。真实 ACME renew dry-run 于 **2026-11-02 01:19:43 CST PASS**：模拟续签全部成功，unit Result=success、ExecMainStatus=0，同秒 Nginx journal 明确记录 Reloaded。
- 生产空 TUF 仓库已被实际客户端校验，记录版本为 root 1、targets 1、snapshot 2、timestamp 3；尚无正式应用包。版本值是本次现场快照，定时续签后会继续递增。

## 部署目标

已采用单 ECS 的 Nginx 静态文件服务、SFTP 候选上传和离线签名发布工具。当前生产应用根为 `https://60.205.157.141/v1/apps/satellite-terminal-tool/`，公开 root 与该地址已写入 `release.config.json`。客户端不访问 GitHub 下载，不部署数据库、GitLab、Windows 构建机或管理后台。

```text
/srv/satellite-updates/
  incoming/                 构建机 SFTP 写入候选，位于 web root 之外
  delivery/                 user 人工交付完整包，内部人员获取
  public/v1/apps/
    satellite-terminal-tool/
      metadata/             root、timestamp、snapshot、targets 和版本历史
      targets/              摘要前缀命名的可信目标文件
  acme/                     IP HTTPS 证书的 HTTP-01 验证目录
  receipts/                 发布与验证记录，非公开
```

上传账号仅写 `incoming`，正式发布身份负责校验和晋升，Nginx 对 `public` 只有读取权限。真实 SFTP 写入候选与流式读回 PASS；上传账号写 `public` 明确拒绝 PASS。上传 SSH 为 publickey only/internal-sftp，TCP、agent、TTY、tunnel、X11 均关闭。服务端仅保管 timestamp/snapshot 低角色密钥，root/targets 私钥留在本机 `~/.config/satellite-release/keys`。本机 upload.json、publish.json、known_hosts、SSH keys、repository 和 venv 均集中于 `~/.config/satellite-release`；本文只记录位置，不记录私钥或凭据内容。user 交付目录不建立在线更新渠道。

## 执行顺序

1. 盘点系统版本、磁盘/内存、运行服务、80/443/22 端口、已有 Nginx 配置、系统防火墙与 ECS 安全组。记录现有服务，新增独立配置文件；端口冲突先报告具体对象。
2. 建立上述目录、受限上传账号和已核对的 SSH 公钥。候选上传使用 SFTP，禁止密码和任意远端 shell；配置固定 `known_hosts` 主机指纹。验证候选目录可写、正式目录不可写。
3. 建立 IP 地址的 HTTPS。Let's Encrypt 已正式支持 IP 证书；使用支持该功能的 Certbot（webroot 模式需要 5.4 或更新版），先验证 HTTP-01，再申请正式证书。IP 证书有效期约六天，必须部署定期自动续签和成功后的 Nginx reload，并验证续签 dry-run；不暂停现有 Web 服务。[官方 Certbot 说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)
4. 用 `deploy/updates/render_config.py --host <已确认IP> --output <新文件>` 生成待审 Nginx 配置。证书签发前先仅启用挑战路径，证书就绪后装配 HTTPS 段。运行 `nginx -t` 后再加载独立配置。安全组按当前服务需要放行 80（证书验证）、443（下载）；22 保留受限管理/构建上传策略，不修改无关规则。
5. 发布者在本机受保护环境用 `scripts/update_repository.py init` 初始化 TUF 信任根并保管备份。正式晋升使用 `scripts/promote_release.py fetch / prepare / commit`：从 incoming 核验下载候选、在本机签名、通过独立 SFTP 身份只上传公开文件并实际 HTTPS 回验，最后原子切换 timestamp。root/targets 私钥留在服务器外；在线定期任务只持其所需的 timestamp 或 snapshot 角色权限，且与晋升共用排他锁。完整命令见 [本地签名与正式晋升](M52_signed_publication_tools.md)。首轮确认续签责任和运行路径；元数据过期保持客户端拒绝新更新。
6. 配置 GitHub 受保护环境与本地 Mac 的上传参数，使用 `scripts/upload_release.py` 先上传测试候选。构建方无需服务器 root 权限。分别验证上传账号仅写 incoming、发布账号可写 public/receipts、Nginx 只读 public。候选上传、正式提交与客户端 GET/Range 验收分别记录；user 完整候选由内部人员获取并人工交付，不进入 ops 在线渠道。
7. 更新客户端内嵌的公开 `release.config.json`：HTTPS IP 地址、app/repo/channel 以及可信 root。生产根的来源必须可追溯，测试根始终留在测试临时目录。
8. 验收单客户端及少量并发下载、断点续传、签名/摘要验证、上传中断、缺包保持旧渠道、元数据续签、证书续签和备份恢复。Windows 真实安装另记，不把服务器 HTTP 成功当作应用升级通过。

## 给阿里云 Agent 的任务描述

如果改由服务器 Agent 执行，可直接交付本文件及 `deploy/updates`、相关脚本。先完成步骤 1 的只读报告，再按既有业务情况执行独立目录/配置的部署。输出实际命令退出码、配置路径、文件属主/权限、HTTPS 证书到期时间、Nginx 检查结果、Range 回读和验证摘要；凭据值与私钥内容从报告中排除。保留现有业务和可恢复的配置备份，不创建云资源或切换实例系统。

## 现场验收记录

机器可读回执：[M52_server_deployment_evidence.json](M52_server_deployment_evidence.json)，检查时间 2026-11-01 17:21:02 UTC（北京时间 11 月 2 日）。其中保留 HTTPS、可信元数据版本、service/timer、iperf、探针和证书事实。

| 项目 | 实际结果 | 边界 |
| --- | --- | --- |
| 实例、现有业务和独立工具环境 | 北京 ECS 已部署；Nginx 1.24、Python 3.11 独立 venv，原 Python 3.6/iperf 端口保留 | 没有新增实例或变更实例系统 |
| IP HTTPS 与健康入口 | 正式证书、health.txt 200、ACME dry-run 及同秒 Nginx reload PASS | 真实跨续签周期仍需后续观察 |
| 元数据与低权维护 | 空生产仓库客户端校验 PASS；四 timer enabled，snapshot/timestamp/health 单次 PASS | 没有应用发行目标；root/targets 离线续签和备份恢复待演练 |
| SFTP 权限 | 真实上传和读回 PASS；上传账号写 public 被拒绝 PASS | 正式应用候选、签名晋升与真实 SFTP 原子提交仍待首包验收 |
| HTTPS 下载 | Range 与实际 Downloader 续传 SHA-256 PASS | 探针文件，不代表完整应用包下载或安装 |
| 少量并发 | 三客户端各 1 MiB，合计约 7.5 秒 | 仅探针；真实完整包/增量包耗时、资源峰值和业务影响待测 |
| GitHub 发行环境 | `release-candidate` 已创建，仅允许 `v*` tag，owner reviewer，上传 secrets/config variable 已配置 | 工作流源码尚未 commit/push/触发；未运行真实 CI |
| 公开目录根因修复 | app 目录原 0750 导致公开读取 404，改为公开目录 0755；相关测试 24 passed | 公开权限不授予上传账号正式写入权 |
| 客户端配置和补验 | 生产公开 root/IP 已内嵌；相关补验 47 passed | 不代表冻结包已经携带或用户已经安装该配置 |
| 部署后资源快照 | available RAM 1500 MiB、磁盘空闲 29 GiB、load 0.02/0.03/0.00；复查 iperf 5201–5216 全部监听 | 没有峰值采样，不能外推持续下载容量或全业务负载 |
| 服务入口与配置恢复材料 | Nginx service enabled；22/80/443 监听；原 SSH/Nginx 配置备份已存在 | 配置备份存在不代表签名密钥/仓库备份恢复已演练 |

已有远端 timestamp，因此第一份真实应用包的 `prepare` **不加 `--initial`**；该选项只适用于远端完全没有 timestamp 的初始化情形。

源码全量 2039 passed、历史更新集成 252 passed、上线配置后最新集成 253 passed 的证据见 [开发记录](M52_unified_update_release_dev_log.md)，测试集合存在重叠，不相加。Windows 实机、冻结构建、真实 CI、完整包/增量真包发布、密钥备份恢复仍待独立验收。
