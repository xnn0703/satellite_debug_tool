# M33 上位机解密固件包验收标准

状态：软件实现与本机联调完成；Windows 发布包与真机 OTA 待现场验收

## A. 包格式与密码学合同

- [x] `.sfpkg` v2 只包含 `manifest.json` 和 `firmware.aesgcm`。
- [x] 使用 AES-256-GCM、96-bit 随机 nonce 和 128-bit tag；每次生成相同输入也得到不同密文。
- [x] 规范 manifest 完整作为 AAD，manifest、nonce、密文和 tag 任一字节变化均被拒绝。
- [x] 解密后明文大小和 SHA-256 与 manifest 一致。
- [x] 重复条目、额外条目、路径穿越、压缩炸弹、超限字段、未知 schema/算法/key ID 明确失败。

## B. 产品和版本准入

- [x] AFD01、AFD01A、AFD01B2、AFD01C 分别精确绑定产品与硬件身份，交叉选择被拒绝。
- [x] `upgrade_only`、`allow_same`、`allow_downgrade` 保持现有语义。
- [x] 设备多来源固件事实不一致、身份占位或会话变化时不允许开始 OTA。

## C. 客户上位机

- [x] 有效设备连接后“选择固件包”可用，不再依赖外部公钥配置。
- [x] 选择有效包后在内存完成认证解密，不在文件系统产生明文临时 BIN。
- [x] artifact token 绑定解密后的不可变字节、包身份和当前设备会话，选择变化或重连使旧授权失效。
- [x] UI 只显示加密包、验证和解密事实，不残留签名、公钥或证书文案。
- [ ] Windows PyInstaller 包含 AES key resource 和 cryptography AESGCM 运行依赖。

## D. 发布工具

- [x] 单条命令从 App BIN 生成加密 `.sfpkg`，不要求私钥、公钥、密码或 key ID 输入。
- [x] 工具生成后立即以同一公共解析器解密复核，失败不留下正式输出。
- [x] AFD01 正式发布入口使用 `package`，删除 `sign` 和全部私钥环境变量。
- [x] factory manifest 记录客户包算法、key ID、版本、策略、文件名和 SHA-256。
- [x] Boot+App factory HEX/BIN 的地址、命名和摘要合同不受影响。

## E. 旧模型清理

- [x] 删除 Ed25519 类型、签名验证、签名工具、签名公钥资源和 PyInstaller 引用。
- [x] 客户入口明确拒绝旧 v1 签名包，不存在兼容开关、旧公钥 fallback 或双格式选择器。
- [x] 当前 AGENTS、发布文档和 M31 计划只描述 v2 加密包；历史日志标注被 M33 替代。

## F. 回归与现场边界

- [x] 固件包、安全错误、Customer Maintenance、Session/OtaController、生产配置和 i18n 相关测试通过。
- [x] 翻译 update/check、Python 编译和 `git diff --check` 通过。
- [x] 使用实际 AFD01C App 生成、加载、解密和逐字节比对通过。
- [ ] Windows 无源码环境能够选择并解密包；真机 OTA 完成传输、重启和版本回读验收。
- [x] 文档明确 AES key 可从客户端提取，本方案不声明抵抗客户端逆向。
