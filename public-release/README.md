# 公开版本草稿

`plugin/` 是用于公开提交准备的 skills-only 源码版本。技能通过本地 CLI 维护计划，不含 MCP 服务或生命周期钩子；原生 `update_plan` 不会自动镜像到浮窗。当前尚未提交或在官方插件平台上架。

支持多级子任务、逐项完成状态、折叠、拖动和当前可见聊天跟随。父任务与子任务分别计数，收起子列表不会改变进度。Windows 默认使用 WPF，在固定透明宿主内绘制动画；保留 `--renderer tk` 兼容模式。

## 运行前提

- Windows 本地执行环境，PATH 中有 Python 3.10+（含 Tk）和 Node.js 22+。
- WPF 模式需要系统已有的 .NET Framework 4.8+。首次运行会使用系统编译器编译随附的 C# 源码，不下载 .NET。
- Codex 已启用本机调试接口，默认端口为 `9229`，可通过 `CODEX_PLAN_PROGRESS_CDP_PORT` 指定。插件不会开启接口或修改启动参数。
- 调用环境提供当前聊天的 `CODEX_THREAD_ID` 或 `CODEX_SESSION_ID`；两者同时存在时必须一致。

会话识别依赖 Codex 内部聊天容器和会话属性，仅支持本地 Codex 聊天。调试接口不可用、当前页面不是主聊天，或当前聊天没有计划时，浮窗隐藏。默认安装环境不保证具备这些前提。

## CLI 使用

在此目录下，将完整计划保存为 UTF-8 的 `plan.json`，然后在对应聊天的执行环境中运行：

```powershell
.\plugin\scripts\launcher.ps1 set-plan --file (Resolve-Path .\plan.json).Path
.\plugin\scripts\launcher.ps1 set-state --run-state idle
```

JSON 包含 `plan` 数组和可选的 `title`。每项使用 `step`、`status`，可用 `children` 保存真实子任务；全树最多 100 项、6 层。状态为 `pending`、`in_progress`、`completed` 或 `blocked`。每次更新提交完整计划，仅将实际完成的项目标为 `completed`。

`set-plan` 同时标记正在执行。`set-state --run-state running|idle|interrupted` 只更新执行状态，保留计划和完成项；`get` 读取当前聊天的计划，`show` 启动浮窗。没有可用环境 ID 时，仅可用 `--thread-id` 传入运行时明确提供的当前聊天 ID，不得猜测或借用其他聊天的 ID。

## 数据与限制

数据独立保存在 `%LOCALAPPDATA%\CodexPlanProgressPublic`，可用 `CODEX_PLAN_PROGRESS_DATA` 覆盖。观察器读取活动聊天编号、相关侧栏元数据和本地 ID 映射，不读取聊天正文；CLI 返回的计划内容会进入调用会话。

公开版依靠显式命令更新运行状态，无法自动识别进程被强制结束或无法继续执行命令的中断。WPF 与 Tk 读取相同计划和位置设置；动画表现取决于机器及桌面负载，不保证特定帧率。

## 可选便携构建

源码不包含 Python/Tk 或 Node.js 运行时。`build-portable.ps1` 可在已具备 PyInstaller 的 Windows 环境中冻结 Python 入口并复制已有 Node.js；它不会安装依赖、上传文件或创建 ZIP。

便携构建尚未验证。构建后的 WPF 仍依赖系统 .NET Framework 4.8+，必须保留 `scripts/overlay_wpf.cs` 和 `scripts/overlay_wpf.manifest`，不能只分发生成的可执行文件。公开提交前仍需验证干净环境安装和当前 Codex 兼容性。
