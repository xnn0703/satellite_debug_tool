# M53 产品命名与 v1.9.0 发布计划

2026-11-02（北京时间）。用户已确认中文“卫通终端管理工具”、英文“Satellite Terminal Manager”，并授权发版；包含 Windows/macOS user、ops 四份完整包。此前 M47–M52 已授权实现，随本版本统一交付。

## 实施范围

1. 可见名称、应用文件、发行归档统一 Manager 命名；安装名称没有版本号，客户归档名包含平台、架构、版本；全量运维版归档额外包含 ops。
2. 新 Windows 主程序为 SatelliteTerminalManager.exe；两版目录分别为 SatelliteTerminalManager、SatelliteTerminalManagerOps。Mac 对应同名 app。内部 Python 包、配置目录、bundle identifier、可信 app/repo ID 保持既有身份。
3. 更新器/健康回执/恢复、构建、校验、工作流统一消费新程序文件名。v1.8.1 及以前旧 GitHub 更新入口不迁入新在线站；首次人工取得完整 v1.9.0，之后 ops 使用新更新合同。旧版本历史资产保持。
4. 单一版本源升至 1.9.0，写发布说明，按本次功能范围审计并提交。GitHub 托管源码/tag/Windows Actions；Mac 本机构建，四包上传 ECS。
5. 完整 user 包供人工交付，ops 完整包签名晋升 stable。首份正式可信包没有上一基线，不发布无基线差量。
6. 完整用户包通过服务器 /downloads/v1.9.0/ 提供稳定 HTTPS 直链和 SHA-256；本机只保留必要构建/验收材料，不在 Downloads 再存副本。客户无需旧程序或 Python 环境。
7. 发布构建环境出现的原生崩溃先定位并修复。会话与客户设备目录的线程归属由构造时的唯一 owner 表达，避开 PySide `QObject.thread()` 返回值父子所有权错误；对象回收与跨线程拒绝均纳入回归。未公开的预发布标签在修复验证后更新，四包重新使用同一最终 SHA。
8. SFTP 大文件传输采用有界流水线并保留完整 SHA-256 校验、不可变候选与原子发布合同，验证中途写入错误能阻止封口。

9. 用户最新确认：客户版安装目录、macOS app、完整包归档及版本栏使用产品本名，只有全量运维版保留 Ops/ops 标识；客户版本栏显示 vX.Y.Z。内部 Edition.USER、build identity、配置路径与 bundle identifier 保持原合同。

## 验收标准

详见 M53_manager_naming_release_acceptance.md。实现过程中维护 M53_manager_naming_release_dev_log.md。
