# 工程卫生收口验收标准

- [x] 从仓库外可以成功 `import satellite_debug_tool`。
- [x] 安装元数据版本与 `satellite_debug_tool.__version__` 一致，为 `1.0.0`。
- [x] `satellite_debug_tool.core.protocol.frame_v2.MAX_DATA_LENGTH` 仍为 `1024`，文档不再写旧的 `512` 上限。
- [x] 仿真交付文档记录当前复核测试结果，不再声称当前全量是 `532 passed`。
- [x] 全量 pytest 通过。
- [x] 不修改本次范围外的既有脏文件。
