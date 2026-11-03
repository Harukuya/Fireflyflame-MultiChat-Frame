# Agent 接入与配置指南

## 先检查环境，再配置框架

| 组件 | 版本与用途 |
| --- | --- |
| **Python** | **3.11+，建议 3.12.x**；运行核心、演示、本地仪表盘都需要 |
| **NumPy** | **≥1.24**，按 `requirements.txt` 安装到项目环境 |
| **Node.js** | **24.x**；完整前端验收和可选内置向量服务需要，普通运行／面板不需要 |
| **uv** | 推荐的 Python 环境管理工具；先检查是否已有，不假设已安装 |

实际已验环境：Windows、Python 3.12.12、Node 24.14.0。下面也给出 Linux／macOS 路径写法，不代表已经在这些系统完成整套验收。

### 1. 只读检查现有环境

先确认工作目录是本框架根目录，检查是否已有项目 `.venv`；再找用户指定或现有的 Python、uv，按任务需要查 Node。Windows 可用 `Get-Command python,python3,py,uv,node -ErrorAction SilentlyContinue`，Linux／macOS 可用 `command -v python3 uv node` 查位置；没有命令时不要反复尝试安装到全局。

对选定解释器执行以下检查（`python` 替换成它的实际路径）：

```sh
python -I -B -c "import sys; print(sys.executable); print(sys.version); print('prefix=', sys.prefix, 'base_prefix=', sys.base_prefix); import numpy; print('numpy=', numpy.__version__)"
uv --version
node --version
```

检查 Python 版本、NumPy 能否导入及版本、解释器属于哪个环境；不要只看 `python --version` 就认为依赖齐全。仅运行核心／面板时，缺少 Node 不阻止继续。现有环境完全满足要求，可以直接用它运行，不必另建；若要新增／升级依赖，优先使用项目专用环境，避免修改用户其他项目或系统环境。

### 2. 不满足要求时，建议在框架目录用 uv 隔离

若 uv 不存在，先说明缺少该工具并给出 [uv 官方安装指引](https://docs.astral.sh/uv/getting-started/installation/)；不要偷偷向系统 Python 执行 `pip install uv`。创建前核对 `.venv`：可用则复用，不属于本项目或不可用则说明情况，选择另一个项目内目录或由用户决定处理方式，不能使用 `--clear`、`--allow-existing` 或直接删除覆盖。

仅在目标 `.venv` 不存在时，在框架根目录执行：

```sh
uv venv --python 3.12 .venv
```

Windows PowerShell：

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
.venv/Scripts/python.exe -I -B examples/console_demo.py
```

Linux／macOS：

```sh
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -I -B examples/console_demo.py
```

显式指定目标解释器，避免当前 `VIRTUAL_ENV` 或 PATH 指向其他项目。不要使用裸 `pip`、`--system`、`--break-system-packages`，也不改用户全局 PATH。新环境不继承系统 site-packages；`.venv/` 已在 `.gitignore` 中。若本机没有所选 Python，uv 可能下载托管解释器；项目依赖在 `.venv`，下载的解释器和缓存可位于 uv 用户目录，不意味着所有文件都在框架目录里。这些行为见 [uv 环境说明](https://docs.astral.sh/uv/pip/environments/)和 [Python 管理说明](https://docs.astral.sh/uv/guides/install-python/)。

**uv 不管理 Node。** 确实需要 Node 而缺少 24.x 时，建议使用用户已有的版本管理器选择 24.x，或给出独立安装方案；不要卸载、覆盖其他项目使用的 Node。只对当前运行终端选择合适版本，不修改全局默认。

### 3. 验证后再执行接入步骤

用选定的项目解释器重新检查 Python／NumPy，再运行离线演示；需要完整验收时确认当前终端 `node --version` 为 24.x，然后执行 `tests/run_all.py`。报告实际解释器路径、依赖版本及结果。

**下文所有 `python` 命令都指这个已验证解释器**；使用 uv 环境时换为 `.venv/Scripts/python.exe`（Windows）或 `.venv/bin/python`（Linux／macOS），无需激活环境。本文只要求检查和建议隔离，不授权顺手升级／替换用户系统组件。

适用于 **MultiChat v0.1.0**。先读本文件，再按 [接入说明](../接入说明.md) 写宿主适配；整体机制看 [README 图解](../README.md)。目标是接入自己的平台、模型和角色，保留核心的会话状态与工具协议。

## 先确认四件事

| 必须由接入方决定 | 代码／资料入口 | 完成标准 |
| --- | --- | --- |
| 消息来自哪个平台，哪些会话允许接管 | `multichat/ports.py` 的 `Transport`、`IncomingMessage` | 能接收消息／撤回；只有真实发送成功才返回 `True`；宿主做权限、去重、自身消息过滤 |
| 调用哪个模型，支持哪些能力 | `multichat/model_port.py` 的 `ModelProvider.complete` | 正确转换正文、图片、工具调用和完整工具结果历史；能力声明与实际一致 |
| 角色是谁、怎么说话、知道什么 | `prompts/`、`data/`、`config.example.json` | 替换或清空虚构样例；配置改名不会自动改写资料 |
| 数据放哪里、共享给哪些会话 | `Runtime(data_root=..., config=...)` | 一个角色一个独立可写数据目录；不同 Runtime 不同时写同一目录 |

真实平台 SDK、模型地址和凭据放宿主适配层。框架没有统一的 `api_key`／`base_url` 配置字段，不要凭空加到角色 JSON 并期待核心读取。用宿主环境变量或秘密存储提供凭据，不提交到仓库，不放进 `provider_config`、仪表盘或日志。

## 按这个顺序接入

1. **先离线跑通。** 完成上面的环境检查或项目隔离安装后，用选定解释器执行 `python -I -B examples/console_demo.py`。只用假模型、终端发送和临时数据，不需要平台或模型账号。
2. **准备角色副本。** 修改下面的 prompts 和资料样例；保持三个 JSON 文件合法。实际运行数据放源码目录以外。
3. **读取配置。** 将 `config.example.json` 复制到宿主配置目录，读取为字典后传给 `Runtime(config=...)`。Runtime 不自动查找或加载这个示例文件。
4. **实现两套端口。** `Transport` 接平台；`ModelProvider` 接模型。SDK 适配放 `adapters/` 或宿主项目，核心不引入平台 SDK。细节见 [接入说明](../接入说明.md)。
5. **登记允许的会话。** 用 `runtime.register("private" / "group", id, ...)`，再把消息交给 `receive()`、撤回交给 `recall()`。未登记的会话不会自动加入。
6. **启动和停止。** `await runtime.start()` 启动心跳及默认本地仪表盘；读取 `runtime.dashboard_url`，在浏览器打开。退出时 `await runtime.close()`。宿主自己调 `tick()` 时不要再启动另一套重复心跳。
7. **先验假输入，再接真实服务。** 跑完整测试；用宿主自己的假事件验证 @、撤回、图片、失败发送和工具续接。接入真实模型、平台后再验效果，不拿离线测试当作真实供应商兼容保证。

## 配置清单

字段位于传给 Runtime 的 `config` 字典；除角色资料文件外均有默认值。下面的“按需”不表示可忽略业务决定，例如共享是否适合目标会话仍须确认。

| 字段 | 默认／约束 | 什么时候改 |
| --- | --- | --- |
| `character_name` | `小伴`，单行、不超过 80 字 | 换角色必改，并同步正文资料 |
| `primary_user_name`、`primary_user_id` | `主要联系人`、空 ID | 可不设；设置才启用该身份的专属流程，不自动建立恋爱关系 |
| `schedule_mode` | `daily` 或 `always` | 需要全天在线时选 `always` |
| `online_start`、`online_end` | `08:00`、`23:00`，`HH:MM`，不能相同 | 每日在线区间，使用宿主进程本地时区 |
| `busy_start`、`busy_end` | 都为空；填写时须成对且不同 | 忙碌时段；即使 `always`，忙碌段仍优先 |
| `share_recent`、`share_memory` | 都为 `true` | 分别控制跨会话近期池和长期记忆；需要隔离则关闭或分开 Runtime |
| `decline_primary` | `true` | 是否允许主动不想回复主要联系人；关闭仍可自然选择无需回复 |
| `offline_reply` | `true` | 是否发送离线状态通知 |
| `offline_text_rest`、`offline_text_busy`、`busy_ack_text` | 见示例 JSON | 替换休息／忙碌通知及主要联系人忙碌应答 |
| `failure_notice`、`split_sentences` | `true`、`false` | 失败通知和私聊按句拆分；不改变角色的事实内容 |
| `look_per_hour`、`look_block_minutes`、`look_block_max` | `1.5`、`60`、`2` | 开启主动阅读的群使用的阅读概率和分块额度 |
| `dashboard.enabled`、`dashboard.port` | `true`、`8765` | 默认监听 `127.0.0.1`；嵌入宿主时可关闭，测试或多实例可用端口 `0` |

`judge_provider_for` 可省略，默认沿用主模型选择器；它不是 JSON 字段。`caption_provider_for` 按需提供：主模型不能看图时用于转述；未提供则没有额外视觉模型。`embedder` 同样由构造器注入，默认字面检索，不自动下载模型或请求付费向量服务。

## 哪些文件需要填

| 文件 | 要填什么 | 不要放什么 |
| --- | --- | --- |
| `prompts/identity.md` | 身份、相处定位；可使用 `{character_name}`、`{primary_user_name}` | 未获用户设定支持的共同经历 |
| `prompts/voice.md` | 口吻、语气与表达习惯，也是记事口吻依据 | 把时间标签或技术字段当作说话样例 |
| `prompts/boundaries.md` | 知情边界、不能凭空补出的设定 | 真实平台权限规则的唯一实现 |
| `data/knowledge/kb.json` | 角色可知的资料与检索入口 | 不应让角色知道的秘密 |
| `data/people.json` | 设定人物和关系资料 | 真实用户动态印象的预填副本 |
| `data/style.json` | 代表性表达样例，说明来源／语境 | 把虚构范例写成真实聊天记录 |
| `data/self/她对自己的认知.md` | 基础自我认知；正文可清空，文件可不存在 | 未替换的旧角色身份 |
| `stickers/中文类别/` | 自备图片，目录名作为可选类别 | 随意放入别人的私有图片 |

前三个 prompts 文件须存在、非空并使用 UTF-8。三个 JSON 即使不使用也保留合法的空结构；确切字段和清空方法见 [data/README.md](../data/README.md)。不要盲目全局替换人名和历史记录。空闲时可 `await runtime.reload_data()` 重新加载；运行数据、主要联系人或监听端口的变化应关闭旧实例后重建。

## 会话与接口约定

- 私聊会话 ID 必须与该用户 `sender_id` 相同。不同平台先加命名空间，例如 `service:user:abc`；同一个人跨群／私聊复用稳定 ID，不能用昵称猜身份。
- `register(..., memory=True, proactive=False)` 默认接管并开记忆，主动行为需显式开启。已有会话的开关保留，修改用 `update_session`。发布插件“默认全开”不是框架的默认策略。
- 群聊 `called=True` 由宿主识别 @、叫词、引用后填写。普通聊天与明确呼叫不能混同，否则判断窗口会失去意义。
- 撤回及时调用 `recall`；不要等模型答完。附件先由宿主限大小、验格式，再 `runtime.add_image(local_path)`；不要把入站任意路径当作本机文件。
- 工具结果按 `tool_history` 的顺序、调用 ID 和供应商续接数据完整转交；不能把工具理由直接发送到聊天。不支持工具时明确声明文字能力，不能假装支持。
- `send_text`／`send_image` 只在平台接受发送时返回严格 `True`。未知或失败返回 `False`；不支持图片可返回 `False`，typing 可为空操作。

## 本地仪表盘与嵌入

默认 `await runtime.start()` 后浏览器打开 `http://127.0.0.1:8765/`，以 `runtime.dashboard_url` 返回的实际地址为准。它与 Runtime 同生命周期；不自动启动浏览器，也不提供公网绑定。

```json
{"dashboard": {"enabled": true, "port": 8765}}
```

端口被占用时启动明确报错，改为另一个端口或 `0` 后重建实例；不要静默连接到别的进程。多角色请同时分开端口与数据目录。只体验面板可运行 `python -I -B examples/dashboard_demo.py --port 8765`，然后浏览器查看，按 Ctrl+C 结束；演示使用假数据和假模型。

已有管理后台可用 `{"dashboard": {"enabled": false}}` 关闭本地监听，改用 `DashboardService` 和 `window.MultiChatPage`。宿主负责认证管理者；嵌入协议见 [页面桥说明](../pages/dashboard/README.md)。默认本地服务使用临时管理令牌及同源检查；这不替代远程部署的登录鉴权，不要直接转发到公网。

## 验收与交付

```sh
python -I -B tests/run_all.py
```

完整入口需要 Node 24 跑前端检查，运行框架和本地面板本身不因此依赖 Node。所有测试使用假模型、假平台和临时目录；成功标准是退出码 0 且最后 `FRAMEWORK ACCEPTANCE` 的通过数等于总数，不把旧版本次数写成本次结果。

交付时说明实际配置文件、适配器入口、启动命令、面板地址、数据目录、测试结果和未实现的平台能力。不得附真实密钥／账号清单／聊天记录；不自动调用真实模型，不擅自安装、打包或公开发布。后续改动前看根目录 `AGENTS.md`，版本由用户确认。
