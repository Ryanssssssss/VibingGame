# Vibe Agent 原生迁移能力对应表

| 能力 | 原生入口或实现 |
|---|---|
| 项目选择与创建 | Godot 原生项目管理器 |
| 多轮聊天、历史、工具步骤 | 顶部 `Agent` 工作区的“对话”页，SSE 实时事件 |
| 模型与视觉模型 | Agent 设置；非敏感值保存到 Editor Settings |
| API Key | Windows Credential Manager |
| 图片附件、拖放和剪贴板 | `.godot/agent/attachments`，历史只存元数据 |
| 外部游戏素材 | 校验并去重导入 `res://assets`，触发文件系统扫描 |
| 文件读取、搜索、写入、补丁、删除、重命名 | 原 Agent 工具在项目隔离副本中执行 |
| 未保存内容、版本冲突与恢复 | 写前哈希检查、事务日志、冲突感知的任务回退 |
| 取消、超时、重试与后台恢复 | 单项目任务锁、协作式取消、重启按钮、父进程看门狗 |
| 多编辑器实例 | 每实例随机端口、令牌和握手文件 |
| 验收计划与证据 | `Tests` 子页；Given / When / Then、单项/全部运行、JSON 报告 |
| 修改后重新验证 | 项目指纹变化后将既有测试结果标记为过期 |
| 项目运行和预览 | Godot 原生运行栏和 `Game` 工作区 |
| Web / Windows 导出 | Godot 原生 Export 对话框及版本匹配的导出模板 |
| Godot API 文档 | Godot 原生 Search Help |
| 快速模板 | Agent 侧栏五类快速创建提示，在当前空项目中生成 |
| 分发 | 定制编辑器与 PyInstaller onedir sidecar；目标机无需 Python |

CLI、Agent、Harness、提示词和 API 知识库继续保留。旧网页仅在阶段验收期间保留为对照，确认能力齐全后再删除。
