# M35 固件包版本策略开发日志

## 2026-10-10

- 用户确认不要求只能升级。
- 根因确认：实际 AFD01C 包 manifest 为 `0.0.130_beta + upgrade_only`，同版本设备按合同拒绝。
- 包已通过产品、硬件、AES-GCM 认证、解密、大小和 SHA-256 校验；拒绝仅来自版本策略。
- 上位机制包工具默认值改为 `allow_downgrade`。
- 固件 `release.sh`、`release.bat` 和 `build_factory_release.py` 默认值统一改为 `allow_downgrade`。
- 包内策略继续作为 AES-GCM AAD 的一部分参与认证；上位机不忽略旧包的明确策略。
- 上位机固件包与维护页面专项测试：`59 passed in 2.03s`。
- 固件 factory release 测试：`7 tests OK`；`release.sh` 语法检查通过。
- 重新生成 AFD01C `0.0.130_beta` 测试包，manifest 和 factory manifest 均为 `allow_downgrade`；
  以当前版本 `V9.9.9` 验证仍可通过版本准入。
- 新测试包位于：
  `code/cmake-build-release/release/afd01c/afd01c_factory_boot-0.0.3_beta_app-0.0.130_beta_20261010T111143Z/afd01c_app-0.0.130_beta_20261010T111143Z.sfpkg`。
- 当前两个仓库都有未提交修改，因此新测试包的 factory manifest 如实记录 `source_git.dirty=true`，不作为正式发布包。
