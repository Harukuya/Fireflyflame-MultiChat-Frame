# 本地仪表盘与宿主接口

## 默认本地浏览器入口

`await runtime.start()` 默认启动框架自带的本地服务，浏览器打开 `http://127.0.0.1:8765/`。实际地址由 `runtime.dashboard_url` 提供，未启动或关闭后为 `None`。配置是 `{"dashboard": {"enabled": true, "port": 8765}}`；端口 `0` 可自动分配，固定端口占用会明确报错。默认仅监听 `127.0.0.1`，没有远程 host 配置，不自动打开浏览器。关闭 Runtime 同时关闭服务。

无需真实平台或模型即可预览：在框架根目录运行 `python -I -B examples/dashboard_demo.py --port 8765`，保持程序运行并在浏览器查看，按 Ctrl+C 结束。该演示使用临时数据和假模型。

本地服务只提供固定页面资源及 `DashboardService` 的管理接口；`local.js` 自动建立 `window.MultiChatPage` 桥。每次启动的临时管理令牌由服务注入页面，API 请求携带令牌，并校验 Host／Origin；令牌不放 URL 或日志，也不是模型 API Key。没有 CORS 放行。不要将这一入口直接转发到公网。

## 嵌入已有宿主

把 `dashboard.enabled` 设为 `false` 后，本地监听关闭，页面仍可嵌入宿主管理界面。由宿主在加载 `app.js` 前提供 `window.MultiChatPage`。如果没有提供，则兼容已有的 `window.AstrBotPluginPage`。直接打开静态文件而没有桥接对象时只显示接入说明，不发网络请求、不启动轮询。

推荐最小接口：

```js
window.MultiChatPage = {
  async ready() { /* 可选，等待宿主准备好 */ },
  async request({ method, path, params, body }) {
    // GET 使用 params，POST 使用 body。
    // 在这里转到你自己实现的后端；返回数据对象本身。
    // 返回 DashboardService 的成功/错误对象；传输失败则抛出 Error。
    return await myHostCall(method, path, method === 'GET' ? params : body);
  }
};
```

也可以将 `request` 命名为 `invoke`，签名相同。已有 `apiGet(path, params)` / `apiPost(path, body)` 方法的宿主可以直接使用。两套方法都有时优先使用 `apiGet/apiPost`。

| 方法 | path | 请求字段 |
| --- | --- | --- |
| GET | `overview` | 无 |
| GET | `sessions` | 无 |
| GET | `intimacy` | 无 |
| GET | `memory` | 可选 `scope` |
| GET | `sticker` | 无 |
| POST | `sessions/add` | `kind`、`id` |
| POST | `sessions/update` | `kind`、`id`、`field`、`value` |
| POST | `sessions/remove` | `kind`、`id` |
| POST | `memory/run` | `scope` |
| POST | `memory/retire` | `scope`、`kind`、`id` |
| POST | `reload` | 无 |

Python 宿主可以使用框架自带的调度器：

```python
from multichat.dashboard_api import DashboardService

dashboard = DashboardService(runtime)  # 每个 Runtime 保留一个实例
# 在宿主已经验证操作者管理权限之后：
result = await dashboard.handle(method, path, params=query, body=json_body)
```

成功统一返回 `{"ok": true, ...视图字段}`，例如会话列表是 `{"ok": true, "rows": [...], "whitelist": false}`。操作成功但无额外数据时是 `{"ok": true}`。失败统一返回 `{"ok": false, "error": {"code": "busy", "message": "当前有会话任务正在运行，请稍后重试"}}`。前端会把错误显示为提示，避免误报成功；不要再套一层 `data`。

接口数据形状沿用 `multichat/dashboard.py` 视图函数。`overview.her.name` 是角色显示名；`sessions.note` 可以覆盖默认会话权限说明。通用账号 ID 允许非数字文本，平台适配器负责最终校验、鉴权和命名空间隔离。亲密度记录的 `qq` 字段为兼容现有存档仍保留此技术名称，界面显示为账号 ID。

停用或移出会话会调用 Runtime 的停止流程，取消并等待该会话已有任务退出；主要联系人也适用。再次启用会创建新的会话实例。资料重载只允许在会话任务空闲时进行，忙碌时返回 `busy`，资料校验失败时保留原来的数据。手动整理任务归对应会话管理，停用该会话可中止它。`memory/retire` 延续原有记忆删除/关闭操作，整理进行时不能同时修改。

页面打开后读取总览、会话、亲密度和记忆；每 30 秒只刷新总览。图片预览由用户点击加载。自建宿主须保护管理接口，不要向浏览器返回 API key；嵌入页面不能代替宿主鉴权。

角色配置放在 `prompts/`，资料样例放在 `data/`；实际数据目录、持久化和热重载行为以所接入宿主为准。
