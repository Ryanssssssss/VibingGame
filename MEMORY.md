# Godot Engine 项目备忘录

## 基本信息

- **项目**: Godot Engine 4.7.0-dev
- **类型**: 开源 2D/3D 跨平台游戏引擎
- **语言**: C++ (C++17)
- **构建系统**: SCons (>= 4.0)，需要 Python >= 3.9
- **许可证**: MIT
- **平台**: Windows (当前开发环境)

## 项目结构

```
godot/
├── core/          # 引擎核心（数据结构、数学、IO、脚本绑定等）
├── servers/       # 服务端抽象层（渲染、物理、音频等服务器）
├── scene/         # 场景系统（所有节点类型、GUI、动画、资源等）
├── editor/        # 编辑器
├── drivers/       # 驱动层（Vulkan、D3D12、OpenGL、音频驱动等）
├── modules/       # 功能模块（GDScript、Mono、导航、物理等）
├── platform/      # 平台层（windows、linux、macos、android、ios、web等）
├── main/          # 主入口
├── thirdparty/    # 第三方库
├── tests/         # 单元测试
├── doc/           # API 文档 (917 个 XML)
├── vibe_tools/    # GodotVibe AI 游戏开发工具
├── SConstruct     # 主构建脚本
└── pyproject.toml # Python 依赖管理 (uv)
```

## 构建与运行

```bash
uv sync                                    # 安装依赖
uv run scons platform=windows target=editor # 构建编辑器
```

---

## GodotVibe - AI 游戏开发工具

`vibe_tools/` 目录：通过 AI Agent + 自然语言方式做游戏的工具。

### 架构

```
vibe_tools/
├── __init__.py          # 包初始化
├── __main__.py          # python -m vibe_tools 入口
├── main.py              # CLI + Web 主入口
├── models.py            # 核心数据模型（ProjectPlan, NodeDesc, SceneDesc, ScriptDesc 等）
├── api_parser.py        # API 知识库构建器（doc/classes/ 917 个 XML → JSON 缓存）
├── scene_generator.py   # .tscn 场景文件生成器
├── script_generator.py  # .gd GDScript 脚本生成器
├── project_generator.py # 项目生成引擎
├── intent_parser.py     # 意图解析器
├── godot_runner.py      # Godot 编辑器集成（启动/运行/验证）
├── llm_transfer.py      # OpenAI 兼容 LLM API（支持所有模型 + 工具调用）
├── llm_provider.py      # Agent 游戏生成器（统一对话+工具循环）
├── web_server.py        # FastAPI Web 服务器
├── static/index.html    # Web UI（项目列表 → 项目工作区：聊天+文件+素材+预览）
├── templates/           # 5 个游戏模板（blank/platformer_2d/topdown_2d/fps_3d/tps_3d）
└── api_cache/           # API 知识库 JSON 缓存
```

### 统一 Agent 模式 (v0.10.0)

**核心设计**：Agent 同时处理聊天和游戏操作，无需意图分类。LLM 的 tool_calls 机制本身就是"意图检测"——需要操作时调工具，不需要时直接回文本。

- 所有消息统一走 `agent_generate()` 循环
- Agent 像真人开发者一样交互：先回复（"好的，我来做..."），然后调工具，最后总结
- 支持多轮对话历史（chat_history，最近 10 条注入上下文）
- 支持图片/截图发送（Ctrl+V 粘贴、拖拽、附件），通过 multimodal content blocks 传递给 LLM
- System Prompt 让 Agent 自然判断：纯聊天 → 文字回复，操作请求 → 工具调用
- 前端区分 `isPureChat`（conversational && !project）和混合场景（conversational + project）

### Agent 工具系统（15 个工具）

**项目生成**：
- **generate_project**: 生成完整 Godot 项目（仅首次可用，调用一次后锁定）

**验证与测试**：
- **validate_project**: 验证项目（结构检查 + GDScript 语法检查）
- **run_project**: 无头运行项目捕获 stdout/stderr 错误（需要 Godot exe）

**文件操作**：
- **read_file**: 读取项目文件内容
- **write_file**: 完整重写文件（适合新文件或大改动）
- **patch_file**: 精准搜索替换（适合小修改，比 write_file 更高效）
- **delete_file**: 删除文件（不允许删 project.godot）
- **rename_file**: 重命名/移动文件（自动更新 res:// 引用）

**项目浏览**：
- **list_files**: 列出项目所有文件
- **list_assets**: 列出用户上传的资产（图片/音频/模型/字体）
- **search_files**: 全项目文本/正则搜索（查找函数/变量/信号引用）

**项目配置**：
- **edit_project_settings**: 修改 project.godot（输入映射、窗口大小、物理层、autoload 等）
- **create_resource**: 创建 .tres 资源文件（TileSet/Theme/Material/StyleBox 等）

**导出与分享**：
- **export_web**: 将项目导出为 HTML5/Web，生成可在浏览器直接运行的游戏（无需安装 Godot）

**记忆**：
- **update_memory**: 更新项目记忆（memory.md），跨对话持久化

循环: list_assets → generate_project → validate → (read+patch/write fix) → validate → run_project → update_memory → 对话回复

### UI 架构

- **Home 页**：项目网格列表 + 新建/模板按钮
- **Project 页**：左侧(文件/素材) + 中间(聊天) + 右侧(预览/API文档)
- **聊天历史**：`{project_dir}/chat_history.json`

### 素材资产系统

- 上传方式：聊天附件(📎) 或 Assets 面板拖拽
- 格式：图片/音频/3D模型/字体
- Agent 通过 `list_assets` 自动发现并使用（res:// 路径）

### 项目记忆系统

- 每个项目下 `memory.md`，Agent 交互结束时自动更新
- 继续对话时自动加载注入 system prompt

### 依赖

- fastapi + uvicorn + python-multipart (Web 服务器)
- openai (LLM API + 工具调用)
- Pillow + requests (图片处理)
- pydantic (数据验证)

### 使用

```bash
uv run python -m vibe_tools web --port 8899  # 启动 Web UI
uv run python -m vibe_tools cli              # CLI 模式
```

### LLM 配置

- OpenAI 兼容格式，支持 Gemini/Claude 等
- 环境变量 `LLM_API_KEY` + `LLM_BASE_URL`，或 Web UI Settings 配置
- 默认模型：gemini-2.5-pro

---

## .tscn 文件格式要点

- Godot 4.x 使用 `format=3`
- 段落顺序：`[gd_scene]` → `[ext_resource]` → `[sub_resource]` → `[node]` → `[connection]`
- 根节点无 parent 字段，直接子节点 parent="."
- 资源 ID 格式：`数字_随机字符`

## GDScript 要点

- 必须使用 Tab 缩进
- extends 声明在文件顶部
- @export/@onready 注解
- 虚方法：_ready(), _process(), _physics_process(), _input()
