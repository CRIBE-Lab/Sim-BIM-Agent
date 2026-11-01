# Sim-BIM Agent

将建筑图纸或带贴图的三维网格转换为轻量建筑信息模型（BIM），并生成可在浏览器中查看的三维模型。

输出包含楼层、空间、墙、门窗及其宿主关系，以 JSON、GLB 和 HTML 查看器三种形式保存。模型调用全部经 OpenRouter 完成，几何计算由确定性代码完成：视觉模型负责读取图纸上的尺寸、墙线和标高，代码负责比例换算、墙线吸附、空间围合和门窗定位，并把发现的问题按对象反馈给模型修正。

## 工作方式

图纸模式处理一个文件夹中的 PNG 或 TIFF 图幅，同一张图幅可以同时排有多张平面和立面。

1. 代码在每张图幅上找出分开的墨迹区域并编号，读图模型把这些区域归并为各个平面和立面视图。
2. 协调者（文本模型）指定各层平面共用的基准点和楼层名称，并发调度读图代理。
3. 平面读图代理读取尺寸链、墙中心线、房间和门窗，提交后立即编译，按对象修正编译报告的问题，并对照叠加预览检查。立面读图代理读取标高和门窗外框。
4. 代码把平面和立面组装为建筑：楼层标高取自立面，门窗的窗台和窗顶高度来自立面与平面开口的保序对齐，未能匹配的开口记入未解决事项。协调者可以根据组装报告要求重读个别视图。

网格模式读取一个 GLB 文件，把网格对齐到墙的主方向，正交渲染四个立面，由读图模型标出建筑主体范围、地面线、檐口线、楼层线和门窗；代码据此确定楼层、外轮廓墙和门窗位置。网格只能观察到外观，每层生成一个整层空间。

## 快速开始

需要 Docker（含 Compose 插件）和一个 OpenRouter API key。

```bash
cp .env.example .env            # 填写 OPENROUTER_API_KEY
mkdir -p inputs runs
docker compose up -d phoenix viewer
docker compose --profile cli run --rm app demo --out runs/demo
```

`demo` 不调用模型，用内置的读图结果生成一个两房间示例。浏览器打开 <http://127.0.0.1:8080/demo/viewer.html> 查看结果，<http://127.0.0.1:6006> 是 Phoenix 追踪界面。

转换图纸时，把同一建筑的图幅放进 `inputs/` 下的一个文件夹：

```bash
docker compose --profile cli run --rm app run inputs/my-house --out runs/my-house-001 --brief inputs/my-house.txt
```

`--brief` 是可选的文字说明，写建筑名称、用途、尺寸单位等已知信息，第一行作为建筑名称。运行目录中已有运行时，命令拒绝覆盖；中断后加 `--resume` 从各代理最近完成的一轮继续。

转换网格时需要给出尺度，二选一：

```bash
docker compose --profile cli run --rm app mesh inputs/house.glb --out runs/house-mesh --unit-m 1.0
docker compose --profile cli run --rm app mesh inputs/scan.glb --out runs/scan --height-m 6.2
```

`--unit-m` 是网格每单位对应的米数；`--height-m` 是地面到檐口的实际高度，摄影测量网格通常没有可靠的尺度，需要用它换算。

已有 `building.json` 时，`render` 命令重新生成 GLB 和查看器。

## 输出

运行目录包含：

| 文件 | 内容 |
|---|---|
| `building.json` | BIM 数据：楼层、空间、墙、门窗，以及假设、未解决事项和来源 |
| `building.glb` | 带颜色的三维模型，y 轴向上 |
| `viewer.html`、`assets/` | 浏览器查看器，可直接打开，也可经 viewer 服务访问 |
| `events.jsonl` | 每次模型响应的服务商、token、缓存命中、费用和耗时 |
| `state.json`、`histories/` | 续跑所需的运行状态和各代理消息历史 |

viewer 服务只提供查看所需的文件，运行状态、消息历史和日志不对外提供。

## 配置

设置按“环境变量 → TOML 文件（`--config`）→ 默认值”的优先级加载。环境变量前缀为 `SIM_BIM_`，嵌套字段用 `__` 分隔，例如 `SIM_BIM_READER__MAX_TOKENS=16000`。

```toml
cost_limit_usd = 1.0          # 一次运行（含续跑）全部代理的费用上限
max_concurrent_readers = 8
request_limit = 200           # 一次运行全部代理的模型请求上限

[coordinator]
model = "deepseek/deepseek-v4.1-flash-1017"
providers = ["together", "fireworks"]
max_tokens = 24000
reasoning_effort = "high"

[reader]
model = "qwen/qwen3.8-27b"
providers = ["near-ai", "chutes"]
max_tokens = 24000
reasoning_effort = "medium"
max_images = 4
parallel_tool_calls = true
```

读图默认使用价格低、缓存命中降价的服务商。需要更快时可改为 `providers = ["cerebras"]`，并设 `max_images = 2`、`parallel_tool_calls = false`：Cerebras 每秒约输出 1000 token，但输入单价约为 Near AI 的 25 倍，且缓存命中不降价，一次图纸转换的费用会高出一个数量级。

`providers` 按顺序尝试，不会路由到列表以外的服务商。选择服务商时需要注意以下几点：

- 思考 token 计入 `max_tokens`，这是唯一可靠的思考长度上限；OpenRouter 的 `reasoning.max_tokens` 在多数服务商上不生效。模型因用尽 `max_tokens` 停止时，运行报错并提示调高该角色的 `max_tokens`。
- 读图需要支持图片输入的服务商，并且该服务商要支持结构化输出所需的 `tool_choice`。Cerebras 每个请求最多接受 2 张图片，`max_images` 必须与之一致。
- 缓存断点放在系统指令、工具定义和最新消息上，命中情况记录在 `events.jsonl` 的 `cache_read_tokens` 中。部分服务商命中缓存后不降价。

## 开发

```bash
uv sync
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
uv run ty check src tests
```

测试不调用模型：代理流程用 Pydantic AI 的 `FunctionModel` 驱动，几何、渲染和查看器用合成数据验证。

源码结构：

| 路径 | 职责 |
|---|---|
| `src/sim_bim/drawing/` | 尺寸解析、读图数据模型、平面与立面编译、图像工具 |
| `src/sim_bim/bim/` | BIM 数据模型、组装、三角网格与 GLB 导出 |
| `src/sim_bim/agents/` | OpenRouter 模型封装、读图与协调代理、运行记录、追踪 |
| `src/sim_bim/mesh/` | GLB 读取、立面光栅化、网格模式流水线 |
| `src/sim_bim/viewer/` | 查看器模板与 three.js 资源 |
