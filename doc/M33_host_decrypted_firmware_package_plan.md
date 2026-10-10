# M33 上位机解密固件包计划

状态：用户已确认，已实施

## 1. 已确认决策

- 客户固件包改为对称认证加密；上位机验证并解密后，继续按现有 OTA 协议向设备发送明文 App BIN。
- 设备端协议、App OTA 接收、Bootloader、LittleFS 文件和 CRC32 合同不变。
- 当前 Ed25519 公私钥签名包被新模型替换，不保留客户侧双格式、兼容开关或回退入口。
- 加密目标是避免固件包被直接查看和提取。AES 密钥随上位机发行，具备本机管理权限的客户能够从程序中提取密钥；
  本方案不声明能抵抗客户端逆向或本机攻击。

## 2. 单一包格式

客户仍选择 `.sfpkg`，避免引入与普通工程 `.bin` 混淆的扩展名。schema 升级为 v2，ZIP 只包含：

```text
manifest.json
firmware.aesgcm
```

`manifest.json` 固定保存：

- schema、产品、硬件类型、目标版本和版本策略；
- 加密算法 `AES-256-GCM`、key ID、96-bit 随机 nonce；
- 密文文件名、明文大小、明文 SHA-256；
- 由上述不可变字段生成的规范 AAD 合同。

`firmware.aesgcm` 为 `AESGCM.encrypt()` 的完整输出，末尾包含 128-bit authentication tag。整个规范 manifest
作为 AAD；manifest、nonce、密文或 tag 任一字节改变都会导致解密失败。每个包使用新的随机 nonce，禁止复用。

## 3. 对称密钥

- 新增只含一个活动 key 的 `firmware_encryption_keys.json`，字段为 schema、key ID、label 和 32-byte Base64
  AES key。PyInstaller 将其编入 Windows 上位机。
- 本轮自动生成 `afd-host-aes-2026`，打包工具与客户上位机读取同一资源文件，不再要求操作者指定私钥、公钥或密码。
- key ID 只用于选择和轮换。密钥轮换另立迁移计划；当前包只接受活动 key，不保留旧签名模型。
- 资源文件不得被描述为服务器级秘密；它是随客户端分发的产品静态密钥。

## 4. 打包与发布流程

- `tools/build_signed_firmware_package.py` 由 `tools/build_encrypted_firmware_package.py` 取代。
- 新工具读取原始 App BIN、产品和版本，加载内置 AES key，生成临时文件，完成自身解密复核后原子提交 `.sfpkg`。
- 固件仓库正式发布入口删除 `sign`、私钥路径和 key ID 参数，改为 `package`：调用上位机加密工具，从同一次
  App artifact 生成客户包；factory HEX/BIN 仍保持 Boot+App 原始生产镜像。
- `factory-manifest.json` 记录客户包 SHA-256、加密 key ID、算法、目标版本和版本策略，不记录明文固件副本。

## 5. 上位机加载与 OTA

1. 客户 Maintenance 页面选择 `.sfpkg`。
2. 解析严格 ZIP 和 manifest，拒绝重复、额外、路径穿越、超限或未知 schema 条目。
3. 按 key ID 获取内置 AES key，使用 manifest AAD 执行 AES-GCM 认证解密。
4. 核对明文大小、SHA-256、产品、硬件类型和版本策略。
5. 明文只保存在不可变内存对象中，不写临时 BIN；现有 artifact token 绑定解密后的字节及当前设备会话。
6. OtaController 继续发送明文分片，设备返回 ACK、重启和版本回读合同不变。

包选择按钮只依据设备 OTA 能力、权威身份和加密 key store 有效性启用，不再因“未配置签名公钥”置灰。界面文案统一为
“加密固件包”“固件包已验证并解密”，删除签名、公钥和证书描述。

## 6. 被取代实现的清理

- 删除 Ed25519 `TrustedFirmwareKey`、签名 domain、签名验证分支和签名专用错误码。
- 删除 `firmware_signing_keys.json`、签名生成工具及 PyInstaller 对该资源的引用。
- 测试不再注入临时 Ed25519 密钥，统一通过显式测试 AES key store 构造包。
- 更新 `AFD01_signed_firmware_package.md`、M18 历史现状说明、M31 ProvisioningBundle 计划、AGENTS 和发布文档，
  让当前事实只描述加密包。历史日志保留当时事实时标注“已由 M33 替代”，不重写历史验收结果。

## 7. 实施顺序

1. 冻结 v2 manifest、AAD 和 AES key store 模型，增加包生成/解密纯逻辑测试。
2. 替换安全模块和打包 CLI，删除旧 Ed25519 模型。
3. 修改 Customer Maintenance 的加载、状态、错误映射和按钮门禁。
4. 修改固件正式发布工具与 shell/batch 编排，生成加密客户包并写入 manifest。
5. 更新 PyInstaller、依赖说明、翻译和当前文档。
6. 运行包安全测试、客户 Maintenance、OTA controller、i18n、生产配置相关测试和 Windows 打包静态校验。
7. 用本次实际 AFD01C App 生成包，在上位机完成加载和解密，真机 OTA 仍作为现场验收。

## 8. 不在本轮范围

- 设备端解密、设备端签名验证、安全启动、RDP、调试口锁定和每设备独立密钥。
- Boot+App factory HEX 加密；J-Flash 生产镜像保持原格式。
- 修改现有 OTA 分片、CRC32、重启确认或回滚状态机。
