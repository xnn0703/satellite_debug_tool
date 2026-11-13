# 更新服务的 systemd 模板

此目录提供本地部署文件，写入仓库不会安装或启动服务器任务。路径固定为：

2026-11-02 现场已按这些模板部署：四 timer enabled，各单次 service success；真实 ACME dry-run 与同秒 Nginx reload PASS。当前 IP 证书到期为 2026-11-08 08:10:04 UTC。见[部署记录](../../doc/M52_server_deployment_task.md)和[机器可读回执](../../doc/M52_server_deployment_evidence.json)；后文保留部署/维护步骤供后续复用。

| 内容 | 路径 |
| --- | --- |
| 最小 Python 工具 bundle | `/opt/satellite-updates/tools` |
| 发布工具 Python venv | `/opt/satellite-updates/venv` |
| Certbot venv | `/opt/satellite-updates/certbot` |
| TUF 仓库 | `/srv/satellite-updates/public/v1/apps/satellite-terminal-tool` |
| 低权在线密钥目录 | `/etc/satellite-updates/online-keys` |
| 续签与 SFTP 晋升共用锁 | `/srv/satellite-updates/receipts/satellite-terminal-tool.publication.lock` |
| 发布校验请求/回执/归档 | `/srv/satellite-updates/receipts/verification/{requests,results,archive}` |

部署前准备 `satellite_meta` 用户/组。该用户需读取工具、公开元数据及自己的 timestamp/snapshot 密钥，写入仓库续签路径、`publish.lock` 和 receipts 目录。root/targets 私钥保持离线；服务器工具 bundle 排除 GUI、客户记录和全部私钥。代码目录由 root 管理，服务用户只读。

app 根保持发布 owner 管理的 0755；部署者预创建可由 `satellite_meta` 读写的 `app/publish.lock`。metadata 目录使用共享组与 2775，receipts 目录使用共享组、sticky bit 与 3770；服务 sandbox 只开放必要目录和现有 publish.lock。`verification/requests` 由 `satellite_publish:satellite_metadata` 管理，`results/archive` 由 `root:satellite_metadata` 管理，正式发布身份对回执只读。公开元数据的服务 UMask 为 0022，确保 Nginx 可读新文件；在线密钥独立保持目录 0700、文件 0600，不受公开元数据 UMask 改变。

将本目录的 `health_check.py`、`renew_certificate.sh` 和 `renewed_reload_nginx.sh` 一并放入工具 bundle 的 `deploy/updates/`，将 `.service`/`.timer`/`.path` 复制到 `/etc/systemd/system/`。`verify_publication_server.py` 安装到工具 bundle 的 `scripts/`，启用 `satellite-updates-verify-publication.path`；正式 JSON 请求存在时触发 root oneshot。服务只读 incoming 中与 request/receipt/SHA-256 绑定的 ops 候选，复制成独立公开 inode，再对全部不可变 publication 文件执行完整 SHA-256；发布机不重复上传大包。处理后请求进入 root 归档。两个请求 watcher 均使用 `PathExistsGlob=.../requests/*.json`，临时文件不会触发空扫描，重启后的已有正式请求仍会处理。下载服务按当前 timestamp 的编号签名链读取两个 ops 目录。将 `certbot.env.example` 配置后安装为 `/etc/satellite-updates/certbot.env`，`root:root 0600`；`CERTBOT_CERT_NAME` 必须是 `certbot certificates` 显示的更新站证书名称。renew 只操作该证书，沿用首次申请保存的 webroot 配置。

| Timer | 调度 UTC | 操作账号 |
| --- | --- | --- |
| `satellite-updates-timestamp.timer` | 每日 03:15 | `satellite_meta` |
| `satellite-updates-snapshot.timer` | 每周日 03:00 | `satellite_meta` |
| `satellite-updates-certbot.timer` | 每日 00:30、12:30，随机延后最多 5 分钟 | root |
| `satellite-updates-health.timer` | 每日 00:45、06:45、12:45、18:45 | root，只读检查 |

timestamp 与 snapshot 任务都取得同一个 publication lock，snapshot 任务同时续签 timestamp。Certbot 仅在实际更新证书后运行 deploy hook；hook 先执行 `/usr/sbin/nginx -t`，成功后才 `/usr/bin/systemctl reload nginx.service`。由于 [Certbot 的 hook 失败不会直接保证 renew 返回失败](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)，renew wrapper 在 systemd 管理的 `/run/satellite-updates-certbot` 中核对 hook 的 started/completed 状态，确保 Nginx 配置或 reload 失败能使 service 失败并触发 journal 摘要。没有实际续签时不执行 hook。

后续部署仍应先在目标 Linux 运行 `systemd-analyze verify` 检查各 unit、确认所列解释器/命令及目录权限，再执行各 service 的单次检查，通过后启用四个 timer。本次服务器单次服务和真实 ACME `renew --dry-run` 已通过；后续跨真实续签周期继续观察。

所有失败通过 `OnFailure=satellite-updates-failure@%n.service` 输出最近 80 条该 unit 的 journal；没有邮件、Webhook 或外部通知。检查方式：

```bash
journalctl -u satellite-updates-timestamp.service -n 80 --no-pager
journalctl -u satellite-updates-snapshot.service -n 80 --no-pager
journalctl -u satellite-updates-certbot.service -n 80 --no-pager
journalctl -u satellite-updates-health.service -n 80 --no-pager
journalctl -t satellite-updates-failure -n 80 --no-pager
```

健康检查只读取本地 JSON 到期字段和证书到期时间，不替代 TUF 签名验证或公网证书/下载验证。root 提前 30 天、targets 14 天、snapshot 7 天、timestamp 与短期 IP 证书各提前 2 天提示；提示返回 1，已过期或读取失败返回 2，均使 service 明确失败并触发 journal 摘要。root/targets 提示需要持有相应离线密钥的发布 owner 处理。

任务执行期间被强制终止可能留下 publication lock。先检查 journal 和是否仍有晋升/续签 owner，确认所有者已结束后处理残留锁；任务不会自动删除另一个 owner 的锁。
