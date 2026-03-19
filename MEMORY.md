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
├── agent.py             # Agent 游戏生成器（统一对话+工具循环）
├── web_server.py        # FastAPI Web 服务器
├── static/index.html    # Web UI（项目列表 → 项目工作区：聊天+文件+素材+预览）
├── templates/           # 5 个游戏模板（blank/platformer_2d/topdown_2d/fps_3d/tps_3d）
├── prompts/             # 工具绑定的 Godot 知识指南（自动注入到工具结果中）
│   ├── _index.json      # 索引：所有 prompt 的名称和描述
│   ├── godot4_basics.md # Godot 4.x 核心规则、类名变更、移动模板
│   ├── tscn_format.md   # .tscn 场景文件格式详解
│   ├── sprite_sheet.md  # 精灵表处理（hframes/vframes、AtlasTexture）
│   ├── physics_2d.md    # 2D 物理（碰撞体、形状、射线、信号）
│   ├── physics_3d.md    # 3D 物理（碰撞体、光照、相机）
│   ├── animation.md     # 动画系统（AnimationPlayer、AnimatedSprite2D、Tween、Timer）
│   ├── audio.md         # 音频（AudioStreamPlayer、总线、格式）
│   ├── gui_controls.md  # GUI/HUD（容器布局、30+ 控件、主题、CanvasLayer）
│   ├── input_handling.md# 输入处理（Action 映射、轮询、事件、触控、鼠标锁定）
│   ├── tilemap.md       # TileMapLayer（非 TileMap）、TileSet、程序化地图生成
│   ├── navigation.md    # 导航/寻路（NavigationRegion/Agent 2D/3D）
│   ├── particles.md     # GPU 粒子（ParticleProcessMaterial、常见效果）
│   ├── networking.md    # 多人网络（ENet/WebSocket、RPC、Spawner/Synchronizer）
│   ├── shaders.md       # 着色器（canvas_item/spatial、ShaderMaterial、常见效果）
│   └── signals_patterns.md # 信号、场景管理、Groups、Autoload、状态机、对象池
└── api_cache/           # API 知识库 JSON 缓存（web_server API 文档端点使用）
```

### 统一 Agent 模式 (v0.10.0)

**核心设计**：Agent 同时处理聊天和游戏操作，无需意图分类。LLM 的 tool_calls 机制本身就是"意图检测"——需要操作时调工具，不需要时直接回文本。

- **知识与工具绑定**：System Prompt 只包含核心身份和规则（精简），领域知识（物理/动画/着色器/GUI 等）通过 TOOL_PROMPTS 绑定到相关工具，工具执行时自动注入到 tool result 的 `__guide__` 字段中。每个 prompt 在同一 session 中只注入一次（去重）。
  - 静态绑定：`TOOL_PROMPTS` 字典定义工具→prompt 映射（如 `generate_project → [godot4_basics, tscn_format, signals_patterns]`）
  - 动态绑定：`_get_dynamic_prompts()` 根据参数上下文决定（如 `write_file("x.tscn")` → 注入 `tscn_format`）
- 所有消息统一走 `agent_generate()` 循环
- Agent 像真人开发者一样交互：先回复（"好的，我来做..."），然后调工具，最后总结
- 支持多轮对话历史（chat_history，最近 10 条注入上下文）
- 支持图片/截图发送（Ctrl+V 粘贴、拖拽、附件），通过 multimodal content blocks 传递给 LLM
- System Prompt 让 Agent 自然判断：纯聊天 → 文字回复，操作请求 → 工具调用
- 前端区分 `isPureChat`（conversational && !project）和混合场景（conversational + project）

### Agent 工具系统（15 个工具 + 自动知识注入）

**项目生成**：
- **generate_project**: 生成完整 Godot 项目（仅首次可用，调用一次后锁定）→ 自动附带 `godot4_basics` + `tscn_format` + `signals_patterns` 指南

**验证与测试**：
- **validate_project**: 验证项目（结构检查 + GDScript 语法检查）
- **run_project**: 无头运行项目捕获 stdout/stderr 错误（需要 Godot exe）

**文件操作**：
- **read_file**: 读取项目文件内容 → 根据文件扩展名自动附带对应指南（.tscn→tscn_format, .gd→godot4_basics）
- **write_file**: 完整重写文件 → 同上
- **patch_file**: 精准搜索替换 → 同上
- **delete_file**: 删除文件（不允许删 project.godot）
- **rename_file**: 重命名/移动文件（自动更新 res:// 引用）

**项目浏览**：
- **list_files**: 列出项目所有文件
- **list_assets**: 列出用户上传的资产 → 若检测到精灵表自动附带 `sprite_sheet` 指南
- **search_files**: 全项目文本/正则搜索

**项目配置**：
- **edit_project_settings**: 修改 project.godot → 自动附带 `input_handling` 指南
- **create_resource**: 创建 .tres 资源文件 → 自动附带 `tscn_format` 指南

**导出与分享**：
- **export_web**: 将项目导出为 HTML5/Web

**记忆**：
- **update_memory**: 更新项目记忆（memory.md），跨对话持久化

循环: list_assets → generate_project → validate → (read+patch/write fix) → validate → run_project → update_memory → 对话回复

### 本地模式 (local_mode)

`web_server.py` 支持本地模式，通过 `_is_local_request(request)` 检测。本地请求（127.0.0.1/localhost）绕过 session 隔离：

- **`owns_project()`**：本地模式只检查项目在 `DEFAULT_OUTPUT_DIR` 下，不要求属于特定 session
- **`list_projects`**：本地模式递归扫描 `DEFAULT_OUTPUT_DIR`（最深2层），用 `_collect_projects()` 查找所有 `project.godot`
- **`generate` / `generate_stream`**：本地模式使用 `DEFAULT_OUTPUT_DIR` 作为 base_dir
- **`create_project` / `chat`**：本地模式默认输出到 `DEFAULT_OUTPUT_DIR`
- **`download_export` / `play_game_files`**：先找 session 目录，fallback 到 `DEFAULT_OUTPUT_DIR`

### project.godot 输入格式修复

`agent.py` 中 `_tool_edit_project_settings` 修复了 input action 的序列化问题：

- **`_format_input_action()`**：将 LLM 返回的 Python dict 转为 Godot `Object(InputEventKey,...)` 格式，支持 Key/JoypadButton/JoypadMotion/MouseButton 四种事件
- **`_format_godot_value()`**：递归转换 Python 值为 Godot 配置格式（bool→true/false, None→null, dict/list 递归处理）
- 当 section=="input" 且 value 是 dict 时自动调用格式化，避免写入 Python repr 字符串

### 3D 模型 / GLB 经验总结

**已解决的问题及防范机制：**

1. **GLB 资源未导入**：上传 .glb 后 Godot 需 `--headless --import` 生成 `.godot/imported/` 缓存
   - `godot_runner.py`: `import_resources()` 方法，`run_project()` 启动前自动调用
   - `agent.py`: `_tool_run_project()` 也在运行前执行 `--import`

2. **Instance 节点子节点冲突**：GLB instance 节点自带 AnimationPlayer 等子树，在 .tscn 中给 instance 节点加子节点会被忽略
   - `_validate_instance_paths()`: 检测 instance+children 冲突并发出警告
   - `scene_generator.py`: instance 节点的 children 被跳过并 log warning
   - `3d_models.md` 规则 7/8: 禁止在 instance 节点下加子节点
   - SYSTEM_PROMPT: 新增 3D MODELS / GLB 专节说明正确/错误结构

3. **LLM 编造动画名**：LLM 常凭空编造 "IDLE NORMAL"、"Walk" 等不存在的动画名
   - `3d_models.md`: 强调 NEVER guess，提供 runtime 发现动画的安全模式
   - `_analyze_3d_model()`: 从 glTF 提取真实动画名列表
   - `_format_3d_model_info()`: 在 list_assets 结果中展示真实动画名

4. **第三人称控制错误**：
   - 移动方向必须基于 **相机方向**（`camera_pivot.global_transform.basis`），不能基于玩家朝向
   - 只旋转 **Model 子节点** 的 `rotation.y`，不能旋转整个 CharacterBody3D（否则相机也跟着转）
   - GLB 模型默认可能面向 +Z（朝向相机），需要 Model 节点旋转 180°
   - `tps_3d.py` 模板已更新为正确模式
   - `3d_models.md` 新增完整第三人称角色模板（场景结构 + 脚本）

5. **动态 Prompt 注入增强**：
   - `_get_dynamic_prompts()`: write_file/patch_file 内容含 .glb/.gltf/AnimationPlayer 时注入 3d_models 指南
   - 确保 LLM 在写入 3D 相关文件时始终能看到正确规则

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
