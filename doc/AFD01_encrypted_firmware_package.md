# AFD01 系列客户加密固件包

## 适用范围

客户 Maintenance 只接受 `.sfpkg`；工程 Device OTA 继续接受受控开发使用的原始 `.bin`。
设备端 OTA 协议、Bootloader 和 CRC32 合同不变，上位机完成认证解密后把明文 App BIN 交给现有 OTA 控制器。

## 安全边界

- 包使用 AES-256-GCM，提供密文保密、manifest 完整性和密文认证。
- `satellite_debug_tool/resources/firmware_encryption_keys.json` 随 Windows 程序分发；客户无需导入密钥或输入密码。
- 该静态密钥可以被具备本机管理权限或逆向能力的人员从客户端提取，因此本方案只用于避免固件包被直接查看，不能替代设备安全启动、设备端解密或硬件密钥保护。
- 客户选择产生一个不可变 artifact token，绑定解密后的固件字节、包身份和当前设备会话；重新选择、身份变化或重连会使旧 token 失效。

## `.sfpkg` v2 格式

ZIP 只允许两个条目：

```text
manifest.json
firmware.aesgcm
```

`manifest.json` 包含 schema、算法、产品、硬件类型、版本、版本策略、明文文件名、密文文件名、明文大小、明文 SHA-256、key ID 和 96-bit 随机 nonce。完整规范 manifest 加固定 domain 构成 AES-GCM AAD；nonce、manifest、密文或 128-bit tag 任一字节改变都会认证失败。

每次生成使用新的随机 nonce，同一 App 和版本重复打包也应得到不同密文。解密后的明文只保存在内存中，不生成临时 BIN。

版本策略：

- `upgrade_only`：目标版本必须更新；
- `allow_same`：允许同版本或更新版本；
- `allow_downgrade`：允许任意可解析目标版本。

## 生成命令

从上位机仓库根目录执行：

```bash
.venv/bin/python tools/build_encrypted_firmware_package.py \
  path/to/afd01c_application.bin \
  release/afd01c-v0.0.131.sfpkg \
  --product AFD01C \
  --version 0.0.131 \
  --version-policy upgrade_only
```

工具读取上位机内置活动 AES key，原子写入包，并用运行时同一解析器立即认证解密复核。操作者不提供私钥、公钥、密码或 key ID。

固件仓库的正式入口会同时构建 Boot/App、factory HEX/BIN 和客户包：

```bash
./release.sh afd01c package
```

Windows 使用：

```bat
release.bat afd01c package
```

AFD01、AFD01A、AFD01B2 和 AFD01C 的 product 与 hardware identity 独立；包不能交叉选择。当前设备的 Debug 与 Product 固件来源必须一致，身份必须完整，版本策略必须通过，客户页面才允许启动 OTA。

`OTA_END=VERIFIED` 只证明设备接受了传输及校验。最终完成仍要求同一 endpoint 返回、升级前冻结的身份重新匹配、所有固件来源重新回报且一致，并匹配目标版本。

## 发布验收

1. 复算 `.sfpkg` SHA-256，并与 factory manifest 一致。
2. 使用上位机运行时解析器认证解密，确认明文逐字节等于本次 App artifact。
3. 验证 manifest、nonce、密文、tag 篡改，以及未知 key、跨产品、跨硬件和不允许版本均被拒绝。
4. 在无源码 Windows 发布包中选择并解密包。
5. 真机完成 OTA 传输、重启、身份与版本回读确认。
