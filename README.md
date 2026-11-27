# Sim-BIM Agent

Sim-BIM Agent 将平面图、立面图和带贴图的三维网格转换为可交互的建筑模型。模型包含楼层、空间、边界、门窗及连接关系，可以保存为一个可离线打开的 HTML 文件。

## 从工作台开始

1. 在 Windows 中双击 `Start.cmd`。工作台会在默认浏览器中打开。已经启动时，再次双击可重新打开同一个工作台。
2. 点击「工作区」，为这栋建筑选择一个独立的文件夹。素材、草稿和历次结果都保存在这里。首次使用可以选择空文件夹。
3. 在「模型设置」中登录 ChatGPT，或连接 DeepSeek 官方 API 或自定义模型服务。获取模型列表后，在任务配置中选择主控模型。协作模型可留空。
4. 拖入 PNG 图纸、GLB 网格或建筑说明 JSON，并点击「添加素材组」。每次任务至少需要一张 PNG 或一个 GLB，最多使用一个 GLB。可以分组描述资料的楼层、朝向和已知尺寸，再在「建筑概况」中填写整栋建筑的情况。
5. 点击「运行」。在「运行记录」中查看建模进度与步骤，完成后打开 Sim-BIM。可切换楼层、检查空间和构件，并在查看器中记录批注。
6. 要修改已交付的模型，从工作台打开该次结果，进入「编辑」。点选要改的空间或构件，写下目标尺寸或形状，例如「只将这扇窗的窗台抬高 0.20 米，保持窗顶高度」。整栋修改可以直接填写整体说明。确认提交后，在运行记录中打开新版本。每次提交都会保留原版本，也可以从新版本继续修改。
7. 点击「下载 HTML」保存模型文件，或在工作区的 `runs/<运行编号>/Sim-BIM.html` 找到它。复制这一个文件即可在其他电脑上查看模型。

生成结果中的尺寸、空间划分、开口与连接关系需要结合原始资料核对。模型批注保存在当前浏览器中，请使用同一浏览器从工作台重新打开对应模型。

关闭浏览器后，已提交的任务继续在本机运行。保持电脑开机并连通模型服务。再次打开工作台并选择同一工作区，即可查看运行历史。需要退出工作台服务时，从任务栏还原名为「Sim-BIM Agent」的窗口并关闭它。

## 安装与模型连接

已安装的桌面版可直接双击启动。首次从发行包安装时，把 ZIP 解压到独立目录，准备 Windows、uv 和 Python 3.12，在该目录的 PowerShell 执行 `./setup.ps1`。安装器按锁定版本下载依赖并检查环境。工作台执行建模任务还需要在本机安装 DeepSeek Harness；安装后工作台会自动查找它。

- **ChatGPT**　点击「登录 ChatGPT」，在打开的网页完成授权，然后获取模型列表。可用型号以账号返回为准。
- **DeepSeek**　在「模型设置」中填写官方 API Key 并保存。服务商按账户用量计费。
- **自定义服务**　点击「添加模型 API」，填写名称、服务的 Base URL 及所需密钥。工作台支持 OpenAI 兼容的 Chat Completions 接口，可自动获取模型列表，也可手填模型 ID。主控模型需要支持工具调用，读图的模型还需要支持图像输入。

建模时，任务所需的资料会发送到所选模型服务。模型连接与凭据保存在本机用户目录。工作区的 `materials/` 保存素材副本，`project.json` 保存已添加的素材组和任务配置，`runs/` 保存每次任务的输入快照、运行记录和交付文件。

## 命令行

也可以用命令行进行本机检查或重复执行已经配置好的任务。以下命令从程序目录的 PowerShell 执行。

```powershell
./Start.cmd doctor
./Start.cmd demo --output D:/Sim-BIM-Data/demo-001
```

`doctor` 检查本机安装。`demo` 使用内置的两房间合成平面生成示例 HTML，可在没有模型账号时使用。输出目录需要是一个新目录。

[综合建模模板](config.orchestrator.example.json) 支持将图纸、网格和建筑说明组合为一个任务。将模板复制为自己的配置 JSON，在 `input_root` 和 `run_root` 中填写本机输入、结果目录的绝对路径。资料路径相对于 `input_root`。在 `materials.images` 中填写 PNG 目录，`materials.mesh` 可填写 GLB 文件，`materials.building_input` 可填写建筑说明 JSON。

```powershell
./Start.cmd check D:/Sim-BIM-Data/config/building-a.json
./Start.cmd run D:/Sim-BIM-Data/config/building-a.json --case building-a
```

其他配置示例包括[图纸](config.example.json)、[三维网格](config.mesh.example.json)和 [OpenCode Go 图纸](config.opencode-go.example.json)。请根据自己的实际输入、可用模型和凭据位置调整模板。使用 `./Start.cmd --help` 查看命令。

查看器使用 Three.js 与 OrbitControls。[第三方许可说明](scripts/tool_scripts/vendor/README.md)
