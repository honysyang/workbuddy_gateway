# workbuddy-gateway

把本机 **WorkBuddy** 的模型封装成 **OpenAI 兼容 API**，支持本机与局域网访问。

- 单文件脚本：`workbuddy_gateway.py`
- 只依赖 Python 标准库，**不需要 pip 安装任何东西**
- 复用本机 WorkBuddy 的登录态与模型额度，**不需要第三方 API Key**

---

## 目录

- [1. 特性](#1-特性)
- [2. 工作原理](#2-工作原理)
- [3. 快速开始](#3-快速开始)
- [4. 运行方式](#4-运行方式)
- [5. 接口参考](#5-接口参考)
- [6. 客户端接入](#6-客户端接入)
- [7. 参数与配置](#7-参数与配置)
- [8. 局域网与防火墙](#8-局域网与防火墙)
- [9. 目录与文件](#9-目录与文件)
- [10. 常见问题](#10-常见问题)
- [11. 已知限制](#11-已知限制)
- [12. 安全与合规](#12-安全与合规)

---

## 0. 效果展示

![开启服务](imgimage.png)
![配置模型](imgimage.png)
![开始对话](imgimage.png)

## 1. 特性

| 能力 | 说明 |
|---|---|
| OpenAI 兼容 | `/v1/models`、`/v1/chat/completions`，支持流式（SSE） |
| 17 个 WorkBuddy 模型 | `hy4free` `hy3` `deepseek-v4.1-flash` `glm-5.3` `space-bunny` `kimi-k3.1-pro` … |
| 多轮上下文 | 自动复用上游会话，第二轮能记住前文 |
| 双上游模式 | 复用桌面端 / 自建上游，互不影响 |
| 局域网共享 | 强制 API Key + 来源网段限制 + 并发保护 |
| 零依赖 | 只用 Python 标准库 |

---

## 2. 工作原理

```
OpenAI 客户端
     │  HTTP  /v1/chat/completions
     ▼
workbuddy_gateway.py（本文件）
     │  ACP JSON-RPC over HTTP
     ▼
codebuddy --serve（本机 127.0.0.1）
     │  你的登录态 + 账号额度
     ▼
WorkBuddy 云端
```

网关做三件事：**协议转换**（OpenAI ↔ ACP）、**会话管理**（把无状态的 messages 映射到有状态的上游会话）、**访问控制**（Key / 网段 / 并发）。

### 两种上游模式

| 模式 | 触发条件 | 说明 |
|---|---|---|
| **复用桌面端** | 默认；检测到 WorkBuddy 桌面端在跑 | 自动发现它的本地服务端口和密码，完全继承登录态与产品配置，启动最快 |
| **自建上游** | 桌面端没跑，或显式 `--no-reuse` | 自己拉起 `codebuddy --serve`，启动多约 5 秒，但与桌面端完全解耦 |

两种模式都会自动注入登录态和产品配置，模型都是完整的 17 个。

### 自动处理的三件事

这些都是实际调试中踩到的坑，脚本内部已解决，了解它们有助于排错：

1. **登录态注入** —— 从 `CodeBuddyExtension\...\workbuddy-desktop.info` 读取 accessToken，通过 `CODEBUDDY_AUTH_TOKEN` 与 `ACC_PRODUCT_CONFIG_V3` 注入。
2. **产品配置还原** —— 只给 token 时模型集会退化成 CodeBuddy 默认的那 9 个；必须再注入桌面端按 userId 缓存的云端产品配置（`~/.workbuddy/local_storage/*.info`），才能拿到 WorkBuddy 的 17 个模型。
3. **会话目录预建** —— CLI 写 `projects/<slug>/<uuid>.jsonl` 时不会创建父目录，缺失会让 `session/new` 直接 500；脚本会提前创建，必要时回退到可写目录。

---

## 3. 快速开始

### 第一步：自检

```powershell
cd G:\dsh
python workbuddy_gateway.py --selftest
```

看到这些就说明链路通了：

```
[wb-gw] 找到登录态: uid=... 有效期还剩 719 小时
[wb-gw] 发现 17 个模型:
[wb-gw]   - hy4free   Hy4
[wb-gw]   - glm-5.3-flash   GLM-5.3-Flash
[wb-gw] 自检通过 ✅
```

> 自检不会启动服务，只验证「登录态 → 上游 → 模型列表 → 一次真实对话」整条链路。

### 第二步：启动

```powershell
# 仅本机
python workbuddy_gateway.py

# 局域网可访问
python workbuddy_gateway.py --host 0.0.0.0
```

### 第三步：调用

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8788/v1", api_key="任意值")
r = client.chat.completions.create(
    model="glm-5.3-flash",
    messages=[{"role": "user", "content": "你好"}],
)
print(r.choices[0].message.content)
```

> 本机模式默认不校验 Key，`api_key` 填任意值即可；但如果你显式指定了 `--api-key`，
> 则会校验。局域网模式会自动生成并持久化 Key。

---

## 4. 运行方式

### 4.1 仅本机

```powershell
python workbuddy_gateway.py
```

监听 `127.0.0.1:8788`，只有本机能访问，不校验 Key。

### 4.2 局域网

```powershell
python workbuddy_gateway.py --host 0.0.0.0
```

启动横幅会列出所有可用地址和 Key：

```
[wb-gw] 局域网模式：监听 0.0.0.0:8788
[wb-gw]   邻居可访问 -> http://10.1.21.175:8788/v1
[wb-gw]   邻居可访问 -> http://192.168.56.104:8788/v1
[wb-gw]   API Key    _qDJRGz4SFnJ5GesD3RaCKGwrNfBQR7i
[wb-gw]   允许网段   127.0.0.1/32, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 ...
[wb-gw]   最大并发   4（排队上限 60s）
```

### 4.3 保存设置（推荐）

只想敲 `python workbuddy_gateway.py` 就生效，加一次 `--save-config`：

```powershell
python workbuddy_gateway.py --host 0.0.0.0 --save-config
```

写入 `.workbuddy-gateway\gateway.json`：

```json
{
  "host": "0.0.0.0",
  "port": 8788,
  "upstream_port": 18899,
  "lan_cidr": null,
  "max_concurrency": 4,
  "queue_timeout": 60.0
}
```

以后直接 `python workbuddy_gateway.py` 即可。**命令行参数始终优先于配置文件**，临时改动用参数覆盖。

改回仅本机：

```powershell
python workbuddy_gateway.py --host 127.0.0.1 --save-config
```

### 4.4 后台常驻

```powershell
cd G:\dsh
Start-Process -FilePath 'python' `
  -ArgumentList '.\workbuddy_gateway.py' `
  -RedirectStandardOutput 'G:\dsh\gateway.log' `
  -RedirectStandardError  'G:\dsh\gateway.err' `
  -WindowStyle Hidden
```

看日志：

```powershell
Get-Content G:\dsh\gateway.log -Tail 20 -Wait
```

### 4.5 停止

```powershell
Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

### 4.6 开机自启

最简单的方式是把上面的启动命令放进启动目录：

1. `Win + R` 输入 `shell:startup`
2. 新建 `workbuddy-gateway.vbs`，内容：

```vbscript
CreateObject("WScript.Shell").Run _
  "pythonw G:\dsh\workbuddy_gateway.py", 0, False
```

用 `pythonw` 可以完全不弹黑窗。

---

## 5. 接口参考

Base URL：`http://<host>:8788`

| 方法 | 路径 | 说明 | 需要 Key |
|---|---|---|---|
| GET | `/health` | 健康检查与服务信息 | 局域网模式需要 |
| GET | `/stats` | 请求统计与活跃会话数 | 局域网模式需要 |
| GET | `/v1/models` | 模型列表（OpenAI 格式） | 局域网模式需要 |
| GET | `/v1/models/raw` | 模型原始信息（含上下文窗口等） | 局域网模式需要 |
| POST | `/v1/models/refresh` | 清空模型缓存并重新拉取 | 局域网模式需要 |
| POST | `/v1/chat/completions` | 对话补全，支持 `stream` | 局域网模式需要 |
| OPTIONS | 任意 | CORS 预检 | 否 |

### 5.1 `/health`

```powershell
Invoke-RestMethod http://127.0.0.1:8788/health
```

```json
{
  "status": "ok",
  "mode": "lan",
  "upstream": "http://127.0.0.1:50729",
  "models": 17,
  "auth_required": true,
  "lan_only": true,
  "stats": { "requests": 5 }
}
```

### 5.2 `/v1/models`

```json
{
  "object": "list",
  "data": [
    { "id": "glm-5.3-flash", "object": "model", "name": "GLM-5.3-Flash", "owned_by": "workbuddy" }
  ]
}
```

完整 17 个模型：

| ID | 名称 | ID | 名称 |
|---|---|---|---|
| `auto` | Auto | `minimax-m3.1-flash` | MiniMax-M3.1-Flash |
| `hy4free` | Hy4 | `kimi-k3.1-pro` | Kimi-K3.1-Pro |
| `hy4std` | Hy4 | `kimi-k3-1` | Kimi-K3 |
| `hy3` | Hy3 | `kimi-k2.8-preview` | Kimi-K2.8-Preview |
| `hy3-x` | Hy3-X | `deepseek-v4-pro` | DeepSeek-V4-Pro |
| `deepseek-v4.1-flash` | DeepSeek-V4.1-Flash | `step-5-preview` | Step-5-Preview |
| `glm-5.3` | GLM-5.3 | `mimo-v2.6-pro` | Xiaomi MiMo-V2.6-Pro |
| `glm-5.3-flash` | GLM-5.3-Flash | `glm-5.2` | GLM-5.2 |
| `space-bunny` | Space-Bunny | | |

> `auto` 由 WorkBuddy 侧自动选模型，不确定用哪个时选它。

### 5.3 `/v1/chat/completions`

**请求**

```json
{
  "model": "glm-5.3-flash",
  "messages": [
    { "role": "system", "content": "你是一个简洁的助手。" },
    { "role": "user", "content": "用一句话说明什么是 ACP" }
  ],
  "stream": false
}
```

**支持的字段**

| 字段 | 支持 | 说明 |
|---|---|---|
| `model` | ✅ | 上面 17 个之一；未知模型会回落到当前默认模型 |
| `messages` | ✅ | `system` / `user` / `assistant` |
| `stream` | ✅ | `true` 返回 SSE |
| `max_tokens` | ⚠️ 忽略 | 由上游模型自行决定 |
| `temperature` / `top_p` | ⚠️ 忽略 | 同上 |
| `tools` / `tool_choice` | ❌ | 上游已关闭工具，纯对话 |
| 图片输入 | ⚠️ 实验性 | `image_url` 支持 `data:image/...;base64,` 形式 |

**响应（非流式）**

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "glm-5.3-flash",
  "choices": [{
    "index": 0,
    "message": { "role": "assistant", "content": "ACP 是一种智能体通信协议……" },
    "finish_reason": "stop"
  }],
  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 }
}
```

> ⚠️ `usage` 目前通常为 0：上游不返回 token 计数。**这不代表计费为 0**，实际额度仍按 WorkBuddy credits 扣减。

**响应（流式）**

```
data: {"id":"chatcmpl-...","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}
data: {"id":"chatcmpl-...","choices":[{"index":0,"delta":{"content":"ACP"}}]}
data: {"id":"chatcmpl-...","choices":[{"index":0,"delta":{"content":" 是"}}]}
data: {"id":"chatcmpl-...","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
data: [DONE]
```

### 5.4 错误响应

所有错误统一为：

```json
{ "error": { "message": "缺少或错误的 API Key", "type": "authentication_error", "code": 401 } }
```

| HTTP | type | 触发条件 | 处理方式 |
|---|---|---|---|
| 400 | `invalid_request_error` | `messages` 为空 / JSON 非法 | 检查请求体 |
| 401 | `authentication_error` | 无 Key 或 Key 错误 | 带上正确的 `Authorization: Bearer <key>` |
| 403 | `permission_error` | 来源 IP 不在允许网段 | 调整 `--lan-cidr` |
| 404 | `invalid_request_error` | 路径不存在 | 检查 URL |
| 429 | `rate_limit_error` | 并发占满且排队超时 | 稍后重试或调大 `--max-concurrency` |
| 502 | `upstream_error` | 连不上上游 / 建会话失败 | 看网关日志，重启网关 |
| 500 | `server_error` | 网关内部异常 | 看网关日志堆栈 |

---

## 6. 客户端接入

### 6.1 Python（openai SDK）

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8788/v1", api_key="任意值")

r = client.chat.completions.create(
    model="glm-5.3-flash",
    messages=[{"role": "user", "content": "你好"}],
)
print(r.choices[0].message.content)
```

流式：

```python
for chunk in client.chat.completions.create(
    model="glm-5.3-flash",
    stream=True,
    messages=[{"role": "user", "content": "数到5"}],
):
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

多轮（关键是**客户端要每轮传完整历史**）：

```python
history = [{"role": "user", "content": "我叫小明，请记住。只回复：好的"}]
a1 = client.chat.completions.create(model="glm-5.3-flash", messages=history).choices[0].message.content
history += [{"role": "assistant", "content": a1},
            {"role": "user", "content": "我叫什么名字？"}]
print(client.chat.completions.create(model="glm-5.3-flash", messages=history)
      .choices[0].message.content)   # -> 小明
```

### 6.2 Node.js

```javascript
import OpenAI from 'openai';

const client = new OpenAI({ baseURL: 'http://127.0.0.1:8788/v1', apiKey: '任意值' });

const r = await client.chat.completions.create({
  model: 'glm-5.3-flash',
  messages: [{ role: 'user', content: '你好' }],
});
console.log(r.choices[0].message.content);
```

### 6.3 curl

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <key>" \
  -d '{"model":"glm-5.3-flash","messages":[{"role":"user","content":"你好"}]}'
```

### 6.4 第三方客户端

任何支持「自定义 OpenAI 兼容端点」的客户端（Chatbox、Cherry Studio、Continue、OpenWebUI…）都这样填：

| 配置项 | 值 |
|---|---|
| Base URL / API Host | `http://127.0.0.1:8788/v1`（局域网用本机内网 IP） |
| API Key | 本机模式默认不校验（填任意值）；若指定了 `--api-key` 或处于局域网模式，用 `gateway.key` 内容 |
| Model | 上表 17 个之一 |

---

## 7. 参数与配置

### 7.1 完整参数表

**网络**

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | `0.0.0.0` 监听全部网卡；也可填具体内网 IP |
| `--port` | `8788` | 网关端口 |
| `--upstream-port` | `18899` | 自建上游端口（与网关端口不能相同） |

**访问控制**

| 参数 | 默认 | 说明 |
|---|---|---|
| `--api-key` | 局域网模式自动生成并持久化 | 调用方需带上 |
| `--lan-cidr` | 仅私有网段 | 允许的网段，逗号分隔；`any` 表示不限制 |
| `--max-concurrency` | `4` | 同时处理上限，超出排队 |
| `--queue-timeout` | `60` | 排队上限秒数，超时返回 429 |
| `--no-auth` | 关 | 局域网也不校验 Key（危险） |

**上游**

| 参数 | 默认 | 说明 |
|---|---|---|
| `--no-reuse` | 关 | 不复用桌面端，强制自建上游 |
| `--auth-token` | 自动 | 手动指定 WorkBuddy accessToken |
| `--upstream-password` | 随机 | 自建上游的本地服务密码 |
| `--config-dir` | `~/.workbuddy` | WorkBuddy 数据目录（读登录态/产品配置） |
| `--workdir` | 当前目录 | 上游会话的工作目录 |
| `--electron` | WorkBuddy.exe 路径 | 一般不用改 |
| `--cli` | codebuddy 脚本路径 | 一般不用改 |

**其它**

| 参数 | 说明 |
|---|---|
| `--save-config` | 把本次参数写入 `gateway.json` 作为下次默认值 |
| `--selftest` | 只验证链路，不启动服务 |
| `--selftest-model` | 自检时指定用哪个模型 |

### 7.2 环境变量

参数也可以用环境变量提供（优先级：命令行 > 配置文件 > 环境变量 > 内置默认）：

| 变量 | 对应参数 |
|---|---|
| `WB_GATEWAY_KEY` | `--api-key` |
| `WB_LAN_CIDR` | `--lan-cidr` |
| `WB_MAX_CONCURRENCY` | `--max-concurrency` |
| `WB_QUEUE_TIMEOUT` | `--queue-timeout` |
| `WB_UPSTREAM_PASSWORD` | `--upstream-password` |
| `WB_AUTH_TOKEN` / `CODEBUDDY_AUTH_TOKEN` | `--auth-token` |

### 7.3 配置文件

位置（按优先级）：

1. `<当前工作目录>\.workbuddy-gateway\gateway.json`
2. `<脚本所在目录>\.workbuddy-gateway\gateway.json`

字段：`host` `port` `upstream_port` `lan_cidr` `max_concurrency` `queue_timeout`
缺失或为 `null` 的字段使用内置默认值。

---

## 8. 局域网与防火墙

### 8.1 其他设备怎么填

| 配置项 | 值 |
|---|---|
| Base URL | `http://<本机内网IP>:8788/v1` |
| API Key | 启动横幅里那串，或读 `.workbuddy-gateway\gateway.key` |

Key 持久化保存，**重启后不变**，客户端配置不用反复改。

### 8.2 放行防火墙

Windows 默认拦截入站连接。首次启动若弹出「是否允许访问」，勾选**专用网络**即可。

没有弹窗就用**管理员 PowerShell** 执行一次：

```powershell
New-NetFirewallRule -DisplayName "WorkBuddy Gateway" -Direction Inbound `
  -Action Allow -Protocol TCP -LocalPort 8788 -Profile Private
```

撤销：

```powershell
Remove-NetFirewallRule -DisplayName "WorkBuddy Gateway"
```

> `-Profile Private` 只在「专用网络」生效。若你的 WiFi 被 Windows 识别为「公用网络」，
> 需改成 `-Profile Any`，或在 设置 → 网络和 Internet 中把该网络改为专用。

### 8.3 访问控制行为

| 情况 | 返回 |
|---|---|
| 无 Key / Key 错误 | `401` |
| 来源 IP 不在允许网段 | `403` |
| 并发占满 + 排队超时 | `429` |
| 正常 | `200` |

上游服务始终只监听 `127.0.0.1`，不会被代理到外部。

### 8.4 推荐配置

**个人局域网使用**

```powershell
python workbuddy_gateway.py --host 0.0.0.0 --save-config
```

**小团队共享（限定网段 + 压低并发）**

```powershell
python workbuddy_gateway.py `
  --host 0.0.0.0 --port 8788 `
  --lan-cidr 192.168.1.0/24 `
  --max-concurrency 3 --queue-timeout 120 `
  --save-config
```

---

## 9. 目录与文件

```
G:\dsh\
├─ workbuddy_gateway.py          # 网关本体
├─ README.md   # 本文档
└─ .workbuddy-gateway\           # 运行时数据（自动创建）
   ├─ gateway.json               # 保存的配置（--save-config 生成）
   ├─ gateway.key                # 持久化 API Key
   ├─ projects\                  # 上游会话记录（*.jsonl）
   ├─ sessions\ local_storage\ logs\ traces\ plugins\
   └─ ...
```

`~/.workbuddy`（WorkBuddy 自身数据）只被**读取**登录态与产品配置；若该目录不可写，
上游数据会自动回退到 `.workbuddy-gateway\`。

---

## 10. 常见问题

### 启动相关

**Q：报「端口 18899 已被占用但握手失败」**
已有另一个网关实例在跑。换端口（两个都要换）：
`--port 9000 --upstream-port 18900`；或先停掉旧实例。

**Q：报「未找到登录态」**
WorkBuddy 登录过期。打开 WorkBuddy 重新登录一次，`workbuddy-desktop.info` 会自动刷新。

**Q：`~/.workbuddy` 提示不可写**
正常现象（沙箱/权限），脚本会自动回退到 `.workbuddy-gateway\`，不影响使用。

### 模型相关

**Q：模型只有 9 个，不是 17 个**
产品配置没读到。自检日志里应出现「已载入 WorkBuddy 产品配置（候选模型 61 个）」。
确认 `~/.workbuddy/local_storage/` 下有 `*.info` 文件。

**Q：指定的模型不生效**
模型名不在列表里时会被回落到默认模型，日志会打印「未知模型 xxx，回落到默认」。
先用 `/v1/models` 确认名称。

### 局域网相关

**Q：别的设备连不上，本机正常**
按顺序排查：
1. 启动时是否用了 `--host 0.0.0.0`？
2. 防火墙是否放行？（最常见）
3. 对方 IP 是否在 `--lan-cidr` 允许范围内？
4. 本机用 `http://<内网IP>:8788/health` 能否访问？

**Q：返回 403**
对方 IP 不在允许网段。用 `--lan-cidr 192.168.1.0/24` 加上对应网段。

**Q：返回 429**
并发占满。调大 `--max-concurrency` 或 `--queue-timeout`，或让调用方降低并发。

**Q：内网 IP 变了**
路由器 DHCP 重新分配导致。给这台机器设置静态 IP / DHCP 保留，或让对方改用新 IP。

### 运行相关

**Q：请求一直卡住不返回**
上游连接被耗尽或桌面端已关闭。重启网关即可；或在局域网共享场景加 `--no-reuse` 解耦桌面端。

**Q：流式输出中断**
客户端读超时太短。网关已发送 `Connection: close`，建议客户端超时设为 120s 以上。

**Q：怎么看日志**
前台运行时直接看终端输出；后台运行时 `Get-Content gateway.log -Tail 20 -Wait`。

---

## 11. 已知限制

| 限制 | 说明 |
|---|---|
| **无工具调用** | 上游以 `--tools ""` 启动，模型只做纯对话，不支持 function calling |
| **`usage` 为 0** | 上游不返回 token 计数，仅作占位；实际计费看 WorkBuddy 后台 |
| **忽略采样参数** | `temperature` / `top_p` / `max_tokens` 不会透传 |
| **会话池有限** | 最多缓存 24 个会话上下文，超出按 LRU 淘汰 |
| **上下文依赖完整历史** | 会话复用依靠「消息前缀哈希」匹配，客户端必须每轮传完整历史；若每轮改写历史（如动态 system prompt），会退化为新建会话（更慢） |
| **单模型单会话串行** | 同一会话的并发请求会排队，避免上游会话错乱 |
| **无 `/v1/embeddings`** | 上游不提供 embedding 接口 |
| **图片为实验性** | 仅支持 base64 data URL 形式 |
| **依赖本机环境** | 需 WorkBuddy 已安装并登录；换机器要重新部署 |

---

## 12. 安全与合规

### 安全设计

- **默认只监听 `127.0.0.1`**，局域网必须显式 `--host 0.0.0.0`。
- **局域网模式强制鉴权**：不指定 `--api-key` 就自动生成并持久化，不存在裸奔状态。
- **来源网段限制**：默认只放行私有网段（`10/8`、`172.16/12`、`192.168/16` 等）。
- **并发闸门**：防止单个调用方占满额度。
- **Key 比较使用常量时间**（`secrets.compare_digest`）。
- **上游不对外**：`codebuddy --serve` 始终只绑 `127.0.0.1`。

### 使用边界

- 请求消耗的是**你个人 WorkBuddy 账号的额度**（按 credits 计费）。
- 仅应在**你信任的私有网络**内开放，并配合 `--lan-cidr` 收窄网段。
- **不要**做端口转发 / 反向代理映射到公网：那等同于共享或转售账号额度，
  违反 WorkBuddy 服务条款，且存在**账号被封**的风险。
- Key 等同于额度凭证，泄露后请删除 `.workbuddy-gateway\gateway.key` 并重启网关换新 Key。

---

## 附：一分钟速查

```powershell
# 自检
python workbuddy_gateway.py --selftest

# 本机启动
python workbuddy_gateway.py

# 局域网启动（并记住设置）
python workbuddy_gateway.py --host 0.0.0.0 --save-config

# 看健康状态
Invoke-RestMethod http://127.0.0.1:8788/health

# 看 Key
Get-Content .\.workbuddy-gateway\gateway.key

# 停止
Get-NetTCPConnection -LocalPort 8788 -State Listen | % { Stop-Process -Id $_.OwningProcess -Force }
```
