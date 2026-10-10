# M33 上位机解密固件包开发记录

状态：软件实现与本机联调完成；Windows 发布包与真机 OTA 待现场验收。

## 已冻结输入

- 上位机解密后通过现有 OTA 协议发送明文 App BIN。
- 设备固件、Bootloader 和 OTA 协议本轮未修改。
- AES-256-GCM v2 包直接替换 Ed25519 v1 包，不保留兼容入口。
- 客户和发布操作者不输入或管理密钥。
- 加密用于固件包静态内容保护，不声明抵抗客户端逆向提取密钥。

## 实施记录

### 1. 单一安全模型

- `core/security/firmware_package.py` 改为 `.sfpkg` v2：ZIP 只接受 `manifest.json` 和 `firmware.aesgcm`。
- 使用 AES-256-GCM、96-bit 随机 nonce、128-bit tag；规范 manifest 加 domain 作为完整 AAD。
- 认证解密后核对明文大小、SHA-256、产品、硬件和版本策略；明文只保存在不可变内存对象中。
- 删除 Ed25519 类型、错误码、签名验证、公钥资源与旧打包工具，旧 v1 包由严格条目/schema 校验拒绝。

### 2. 客户上位机

- Maintenance 加载内置 `firmware_encryption_keys.json`，按钮门禁、状态、确认框和错误提示统一表达加密、认证与解密事实。
- 已解密固件继续进入现有不可变 artifact token 和 `OtaController`，设备侧传输合同未改变。
- PyInstaller data 改为 AES key resource；`cryptography` 依赖继续提供 AESGCM。

### 3. 发布链

- 新增 `tools/build_encrypted_firmware_package.py`，生成后立即用运行时同一验证器认证解密复核，再原子提交。
- 固件仓库 `release.sh` / `release.bat` 把 `sign` 改为 `package`，自动发现同工作区上位机工具和 Python。
- `build_factory_release.py` 校验包内算法、产品、版本、策略和 key ID，并把包文件名、SHA-256、算法、key ID 与策略写入 `factory-manifest.json`。
- factory HEX/BIN 地址、内容和命名合同保持不变。

### 4. 文档与翻译

- 新建当前格式说明 `doc/AFD01_encrypted_firmware_package.md`，AGENTS、M31、功能定义和固件发布操作指南改为 v2 加密包事实。
- 固件仓库原 Ed25519 计划、验收和开发记录标记为已被 M33 替代，保留历史证据。
- 中文翻译目录已更新并通过同步检查。

## 验证证据

- 上位机固件包、客户 Maintenance 和操作网关注入：`62 passed`；相关扩展回归 `148 passed`。
- 固件正式发布纯合同：`6 tests OK`。
- 实际 AFD01C App `0.0.130_beta` 生成 `.sfpkg`，运行时认证解密后逐字节等于原 App BIN：
  - 明文大小：`784392` bytes
  - 明文 SHA-256：`0766cd924b48a650cff5ced5ef47c0be6e8d3ab04d1bde74ee99bf4e08471412`
  - 包 SHA-256：`dbf450aaaa48c64ac03bf1d7149f31cb5d03a67164e7c520b9cd94d74f222ed9`
- 翻译检查：`1370 messages`，全部完成并与 QM 同步。
- 全量上位机回归：`1454 passed in 72.02s`。
- 固件正式入口 `./release.sh afd01c package` 完成 Boot/App 构建、factory HEX/BIN 和加密 `.sfpkg` 生成。

## 现场边界

- Windows PyInstaller 发布包内资源与 AESGCM 运行需在 Windows 构建产物验收。
- 真机 OTA 的传输、重启、身份返回和版本回读需现场验收。
