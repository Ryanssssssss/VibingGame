# VibeGodot

基于 Godot 源码定制的游戏编辑器，将 AI Agent、玩家行为测试和失败回修集成到原生工作区中。你可以直接用自然语言描述需求，由 Agent 读取项目、修改场景与脚本，再运行游戏验证修改结果。

本仓库是 Godot 的定制分支，并非 Godot 官方发行版。目前提供 Windows 编辑器与后台组件的构建、打包流程。

## 主要功能

| 功能 | 说明 |
| --- | --- |
| 原生 Agent 工作区 | 多轮对话、历史记录、工具执行进度、取消及失败重试 |
| Markdown 回答 | 支持标题、表格、粗体、列表、行内代码与代码块等常用格式 |
| 模型与语言设置 | 顶部“设置”按钮打开弹窗，可配置主模型、视觉模型及回答语言；支持简体中文、English、跟随用户输入 |
| 图片与素材 | 添加参考图片、拖放、粘贴剪贴板图片，以及导入游戏素材 |
| 隔离修改与回退 | 在项目副本中执行修改，写回前检查磁盘版本与编辑器未保存状态；支持整轮 Agent 修改回退 |
| Tests 自动验收 | 根据用户需求和实际文件差异生成测试点，执行真实 Godot 输入、状态断言、截图和视觉判断 |
| 失败审查与回修 | 先审查测试方案是否合理，再修订测试或交回 Agent 修复游戏，保留逐轮证据 |

Agent 工作区中的对话和 Tests 使用完整宽度，设置不再占用常驻侧栏。运行、停止、Game 预览、导出与 API 文档搜索使用 Godot 自带的工具栏、工作区和菜单。

## 开始使用

1. 打开完整 Windows 发布包中的 `VibeGodot.exe`，通过项目管理器新建或打开项目。请保留同目录的 `vibe_agent` 等依赖文件，不要只复制编辑器 EXE。
2. 进入顶部 **Agent** 工作区，点击 **设置**，填写兼容模型服务的接口地址、主模型、视觉模型和 API Key。
3. 选择回答语言并点击 **保存设置**。默认使用简体中文，已有历史不会自动翻译。视觉验收需要支持图片输入的模型与接口。
4. 在 **对话** 页描述任务，例如“增加玩家死亡后的复活倒计时，倒计时结束前禁用复活按钮”。
5. Agent 修改文件后，在 **Tests** 页查看测试步骤、实际状态、截图及最终结论。需要时可手动运行、重新生成或停止测试。

API Key 使用 Windows 凭据管理器保存；模型地址、名称和回答语言保存在编辑器设置中。“清空当前项目对话”和“回退上次 Agent 修改”也位于设置弹窗内。

发布包的使用者无需安装 Python。模型请求会发送到你配置的服务；代码上下文、需求及视觉测试截图可能作为请求内容发送。

## 自动测试与回修

每次 Agent 实际修改项目文件后，自动生成 3～6 个与修改相关的测试点，覆盖正常操作、边界行为和相关回归。没有文件修改的对话不触发测试；手动保存只会使相关验证结果过期。

执行流程：

1. 检查项目结构与脚本。
2. 在独立项目副本中注入测试桥接组件，为每个用例启动独立 Godot 进程和测试存档。
3. 执行场景加载、动作或按键输入、鼠标操作、等待和节点状态断言；涉及画面时采集截图并进行视觉判断。
4. 分别记录行为、视觉和运行诊断，汇总验收结果。
5. 失败时先审查测试合理性：确认是游戏缺陷后回修游戏；确认是测试方案问题后修订方案、复核覆盖并重新测试。证据不足或审查不可用时标记受阻。

游戏自动回修和测试自动修订分别最多 **3 轮**。回修后复跑本轮全部启用测试；修订需保留原始需求、用例身份、必选状态和视觉覆盖，不能靠删除失败用例或降低标准获得通过。

**脚本检查通过不等于行为验收通过。** 截图成功也不等于视觉验收通过。进程正常退出、记录完整且仅有退出资源未释放提示时，保留警告，不覆盖已通过的行为与视觉结论。未知运行错误、模型不可用或证据不足显示“受阻”，不会直接当作游戏缺陷回修。

Tests 支持单项运行、全部运行、重新生成、停止及高级 JSON 计划编辑。修改项目、计划或判定规则后需要重新验证；单项通过不代表整套通过。停止后保留已经完成的报告。

## 日志与证据

以下路径均相对于当前游戏项目：

| 路径 | 内容 |
| --- | --- |
| `chat_history.json` | 对话历史 |
| `.godot/agent/sidecar.log` | 后台诊断日志 |
| `.godot/agent/acceptance_plan.json` | 版本 2 验收计划 |
| `.godot/agent/test_artifacts/` | 逐轮报告、运行记录、日志、截图与独立测试存档 |
| `.godot/agent/plan_history/` | 历史测试计划 |

`runtime.json` 中视觉步骤的 `pending` 表示截图已采集、尚待判断；最终视觉结论以 `report.json` 为准。更完整的操作说明见 [Windows 使用说明](misc/vibe_agent/README.zh-CN.md) 和 [能力对应表](misc/vibe_agent/CAPABILITIES.md)。

## Windows 构建

需要可构建本仓库 Godot 源码的 Windows C++ 工具链、Windows SDK、Python 和 SCons。后台打包脚本使用 `.build/pythonlibs` 中的 Python 依赖，包括 PyInstaller、FastAPI、Uvicorn、OpenAI SDK、Pydantic、Pillow、Requests 和 pygltflib。先准备依赖，再在仓库根目录执行：

```powershell
$env:PYTHONPATH="$PWD\.build\pythonlibs;$PWD"
python -m SCons platform=windows target=editor production=yes debug_symbols=no d3d12=no -j4
.\misc\vibe_agent\build_sidecar.ps1 -PythonExe (Get-Command python).Source
.\misc\vibe_agent\package_windows.ps1 -PythonExe (Get-Command python).Source -SkipBuild -SkipSidecarBuild
```

默认输出目录为 `.build/VibeGodot-Windows-x86_64`。打包脚本会替换指定的输出目录，请勿将游戏项目放在该目录内。构建产物不随源码提交；游戏导出需要与定制编辑器版本匹配的导出模板。

## 开发验证

在上述 Python 依赖环境中执行：

```powershell
$env:PYTHONPATH="$PWD\.build\pythonlibs;$PWD"
$env:GODOT_TEST_EXE="$PWD\bin\godot.windows.editor.x86_64.exe"
python -m unittest discover -s vibe_tools/tests -v
```

设置 `GODOT_TEST_EXE` 可启用真实 Godot 进程集成测试；未设置时对应测试会跳过。编排回归使用离线模型替身，不能据此宣称真实主模型或视觉服务调用已验证；外部模型链路需使用实际配置单独测试。

## 项目结构

- `editor/plugins/agent_editor_plugin.*`：原生 Agent 与 Tests 界面。
- `vibe_tools/editor_sidecar.py`：编辑器接口、任务事件与修改／测试／回修编排。
- `vibe_tools/editor_*.py`：隔离工作区、测试计划、行为执行、判定、合理性审查及语言设置。
- `vibe_tools/tests/`：Python 回归与 Godot 进程集成测试。
- `misc/vibe_agent/`：Windows 构建、打包脚本与使用文档。

## 上游与许可证

VibeGodot 基于 [Godot Engine](https://godotengine.org) 开发，保留 Godot 的原生 2D、3D 编辑能力。引擎源码采用 [MIT 许可证](LICENSE.txt)；贡献者及第三方版权信息见 [AUTHORS.md](AUTHORS.md)、[COPYRIGHT.txt](COPYRIGHT.txt) 和 [LOGO_LICENSE.txt](LOGO_LICENSE.txt)。随包依赖保留各自许可证。

本分支特有问题请在本仓库反馈；通用 Godot 资料可参考官方文档与上游项目。
