# M35 固件包版本策略计划

状态：用户已确认，已实施

## 目标

客户 OTA 不限制升级方向。新生成的 `.sfpkg` 默认允许高版本、同版本和低版本安装。

## 实施范围

- 上位机制包工具默认 `version_policy=allow_downgrade`。
- 固件仓库的 Shell、Windows 和 Python 正式发布入口使用相同默认值。
- manifest 继续保存并认证版本策略；调用者仍可显式选择其他策略。
- 上位机继续严格执行包内策略，不增加忽略 manifest 的旁路。
- 已生成的 `upgrade_only` 包保持不可变，需要重新制包才能采用新策略。

## 验收标准

- 不传版本策略时，新包 manifest 为 `allow_downgrade`。
- 默认包可在当前设备版本高于、等于或低于包版本时通过版本准入。
- 显式 `upgrade_only` 和 `allow_same` 仍保持原语义。
- Shell、Windows 与 Python 发布入口默认值一致。
- 上位机固件包专项测试和固件发布工具测试通过。
