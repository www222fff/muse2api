<h1 align="center">muse2api</h1>

<p align="center">
  <a href="README.md">English</a> | <b>中文</b>
</p>

<p align="center">
  一个把 <a href="https://muse.ai">muse.ai</a> 网页应用封装成 <b>OpenAI 兼容 API</b> 的异步网关
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi" alt="FastAPI" />
  <img src="https://img.shields.io/badge/API-OpenAI%20Compatible-green" alt="OpenAI Compatible" />
  <img src="https://img.shields.io/badge/status-v0.1%20framework-orange" alt="Status" />
  <img src="https://img.shields.io/badge/License-MIT-lightgrey" alt="License" />
</p>

---

## 目录

- [简介](#简介)
- [状态](#状态)
- [架构](#架构)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [接口示例](#接口示例)
- [配置](#配置)
- [路线图](#路线图)
- [参与贡献](#参与贡献)
- [免责声明](#免责声明)
- [Star 历史](#star-历史)

## 简介

muse2api 把 muse.ai 的聊天、文生图和文生视频能力变成标准的 OpenAI 接口（`/v1/chat/completions`、`/v1/images/generations` ……）。已有的 OpenAI SDK 和客户端（如 ChatGPT-Next-Web、LobeChat、Cherry Studio）只需改一下 `base_url` 就能用。

项目注重**工程干净和协作友好**：

- **可插拔 driver**：访问上游的方式被抽象在 `MuseDriver` 接口后面。目前有一个 mock driver 和一个浏览器（CDP）driver，还为直连协议（HTTP）driver 预留了位置。三者完全独立。
- **严格分层**：协议层、服务层、账号池、driver 各司其职，可以单独开发和测试。
- **全异步**：基于 FastAPI + asyncio；CDP 客户端用 `websockets`，没有任何请求会阻塞线程。
- **账号池**：多种调度策略、按账号的并发限制、失败后冷却、过期会话自动剔除、自动故障转移。
- **离线开发**：mock driver 不需要账号、不发任何网络请求，前端和协议层的工作可以独立推进。

## 状态

> **当前**：本地 mock-driver 测试套件通过（81 个测试，无需网络），每次推送到 main 都会跑 CI；浏览器 driver 和直连协议 driver 仍需用真实 muse.ai 账号做实测。

| 模块 | 状态 | 备注 |
|---|---|---|
| OpenAI 协议层（聊天流式/非流式、图片、视频、模型） | ✅ 已实现 | 多轮聊天、system 提示词、图片输入、模型别名 |
| 账号池（调度 / 冷却 / 失效 / 故障转移） | ✅ 已实现 | `lru`、`round_robin`、`affinity` 三种策略 |
| 后台任务（视频） | ✅ 已实现 | 先建任务再轮询结果；重启后未完成的任务会标记为中断 |
| Admin API（账号增删改查、续期、重置、状态） | ✅ 已实现 | 支持 cookie 字典、原始 `Cookie` 请求头字符串、EditThisCookie JSON 三种导入格式，还有批量导入接口 `/admin/accounts/import`；单账号额度快照 `/admin/accounts/{id}/quota` |
| Mock driver | ✅ 已实现 | 离线假数据 |
| 浏览器 driver（Chromium + CDP） | 🧪 待实测 | 聊天、图片、视频全流程已写好 |
| 会话续期 / 保活 | 🧪 待实测 | 纯 HTTP，默认开启，每 10 分钟一次；同时唤醒账号的 VM，第一条请求不用等冷启动 |
| 直连 HTTP 协议 driver | 🚧 预留 | 返回 501 |
| `/v1/images/edits`（参考图） | ✅ 已实现 | 参考图用于生成新图；不支持 `mask` |
| `/v1/responses` | 🚧 预留 | 返回 501 |
| 流式心跳（SSE keepalive） | ✅ 已实现 | 用 `MUSE2API_STREAM_HEARTBEAT_INTERVAL` 开启；等第一个 token 期间发送 `: keepalive` 注释 |
| 媒体自动清理 | ✅ 已实现 | `MUSE2API_MEDIA_RETENTION_DAYS`（默认 14 天，`0` 关闭）；每小时后台清理 |
| 服务端图片抓取的 SSRF 防护 | ✅ 已实现 | 拒绝内网和回环地址 |
| 网页控制台、请求日志、多 API key | ✅ 已实现 | 控制台在 `/dashboard`；key 增删改查在 `/admin/keys`；逐请求日志和统计聚合 |
| 额度查询 | ✅ 已实现 | 浏览器 driver 在空闲 tab 上读取 Settings → Usage（套餐、本周用量百分比、重置时间、额外额度；支持英文 / 简体 / 繁体界面），缓存 30 分钟；mock driver 返回假快照；http driver 尚未接入 |
| VM 唤醒确认 | ✅ 已实现 | 唤醒（keepalive 或 `/admin/accounts/{id}/renew`）后轮询会话，直到 VM 报告 `RUNNING`；确认 VM 没起来的账号 10 分钟内不接视频请求 |
| Cookie 导入浏览器插件 | ✅ 已实现 | MV3 弹窗插件，位于 `src/muse2api/web/extension/`，可在控制台下载 |
| 存储后端（SQLite / Redis） | 🚧 预留 | 账号仍用 JSON 存储；请求日志已是 SQLite |
| Prometheus 指标 | 🚧 预留 | `/admin/stats` 已有基础聚合 |

## 架构

```mermaid
flowchart TB
    clients["OpenAI SDK / 聊天客户端<br/>HTTP · Bearer key"]

    subgraph api["api/ — 协议层"]
        api_routes["routes · 校验 · 鉴权 · SSE"]
    end

    subgraph services["services/"]
        svc["Gateway（故障转移）· TaskManager"]
    end

    subgraph accounts["accounts/"]
        acc["账号池<br/>策略 · 冷却 · 续期"]
    end

    subgraph core["core/"]
        core_inner["模型别名<br/>提示词转换 · 媒体存储"]
    end

    subgraph drivers["drivers/ — MuseDriver 接口（唯一接触上游的层）"]
        direction LR
        d_mock["mock<br/>离线假数据"]
        d_browser["browser<br/>Chromium + CDP"]
        d_http["http<br/>直连协议（预留）"]
    end

    subgraph upstream["upstream/"]
        u["muse.ai 地址 · cookie 名 · 会话接口"]
    end

    musedotai["muse.ai"]

    clients --> api --> services
    services --> accounts & core
    accounts & core --> drivers
    drivers --> upstream --> musedotai
```

**一次聊天请求的处理流程**：路由先把模型别名解析好，把多轮 `messages` 合成一条提示词，然后交给 Gateway。Gateway 从账号池租一个账号，调用 driver 要文本流。出错时按错误类型更新账号状态：会话过期的账号下线，额度用完的账号冷却，其他错误短暂冷却。在还没向客户端发送任何内容之前，请求会在另一个账号上重试。

流式请求先拿到第一个 chunk 再返回 200。这样「没有可用账号」「上游鉴权失败」这类错误会以正常的 HTTP 状态码返回，而不是断在流中间。

完整设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 项目结构

```
muse2api/
├── src/muse2api/
│   ├── app.py                 # FastAPI 应用工厂和 lifespan
│   ├── config.py              # 配置（环境变量 / .env）
│   ├── errors.py              # 错误层级 → HTTP 状态码 + OpenAI 错误体
│   ├── api/
│   │   ├── deps.py            # 鉴权和依赖注入
│   │   ├── schemas.py         # OpenAI 请求模型
│   │   └── routes/            # chat / images / videos / media / models / admin / responses
│   ├── services/
│   │   ├── gateway.py         # 在账号池上跑 driver 调用，失败时换账号重试
│   │   ├── tasks.py           # 后台长任务
│   │   └── container.py       # 服务装配
│   ├── accounts/              # 账号模型、JSON 存储、账号池、会话保活
│   ├── core/                  # 模型注册与别名、提示词转换、媒体存储
│   ├── drivers/
│   │   ├── base.py            # MuseDriver 接口与数据类型
│   │   ├── mock.py
│   │   ├── browser/           # cdp.py / chromium.py / dom.py / driver.py
│   │   └── http/              # 预留
│   └── upstream/muse.py       # 上游常量与会话续期
├── tests/                     # mock-driver 测试，无网络访问
├── docs/ARCHITECTURE.md
├── TODO.md                    # 贡献者任务板
├── Dockerfile · docker-compose.yml · .env.example
└── CONTRIBUTING.md
```

## 快速开始

### 本地运行

```bash
git clone https://github.com/www222fff/muse2api.git
cd muse2api
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env      # 默认 MUSE2API_DRIVER=mock，不需要账号
python -m muse2api        # 默认监听 http://127.0.0.1:18610
```

如果没设置 `MUSE2API_API_KEY`，首次启动会自动生成一个 key 并保存到 `data/api_key`。

### Docker

```bash
cp .env.example .env
docker compose up -d --build
```

### 用真实账号（浏览器 driver）

1. 装好 Chromium 或 Chrome，在 `.env` 里设置 `MUSE2API_DRIVER=browser`。
2. 从已登录 muse.ai 的浏览器导出 cookies。至少需要 `hatch_sess` 和 `hatch_native_auth_device`；`hatch_gw`、`hatch_vml` 如存在会一并传递。建议导入 muse.ai 的全部 cookies（包括 `__Host-` 开头的）。
3. 通过下面的 Admin API 示例把账号导进来。

## 接口示例

所有 `/v1/*` 接口都需要请求头 `Authorization: Bearer <API_KEY>`。`/admin/*` 接口用 `MUSE2API_ADMIN_KEY`，没设置时默认和 API key 相同。

**聊天（流式）**

```bash
curl http://localhost:18610/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

**OpenAI Python SDK**

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:18610/v1", api_key="<API_KEY>")
resp = client.chat.completions.create(
    model="muse-chat",
    messages=[{"role": "user", "content": "Write a short poem about autumn"}],
)
print(resp.choices[0].message.content)
```

**多轮对话**

对话默认无状态：每个请求都在 muse.ai 开一个新对话，并发送完整的 `messages` 历史，不同客户端不会接进同一个对话。传一个固定的 `user`，同一段对话就会留在同一个对话里，后续只发送新的用户消息，速度更快。这个标识按 API key 隔离，两把 key 传相同的 `user` 也不会混在一起。每个账号同时服务 `MUSE2API_ACCOUNT_MAX_CONCURRENCY` 段对话（默认 4），每段一个标签页；并预先开好 `MUSE2API_WARM_THREADS` 个对话（默认 2），新对话约 5 秒就能回复，而不是 20～40 秒。每个 muse.ai 账号只有一台云端虚拟机，同一账号上的多段对话会互相拖慢：实测一个账号一次一个请求约 5 秒，同时三个各约 20 秒，同时十个要 33～95 秒并有超时。给一个账号多开标签页没有用；10 人左右请准备 3 个以上账号，账号池会把请求分摊过去。

**文生图**

```bash
curl http://localhost:18610/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"A cyberpunk street on a rainy night","size":"16:9","response_format":"url"}'
```

**透明背景**

muse.ai 只返回平面的 RGB 图片，所以 `"background":"transparent"` 会在本地把主体抠出来（BiRefNet + 边缘颜色估计）并返回 RGBA PNG。需要 `pip install -e '.[matting]'`；模型（约 930MB）首次使用时会下载到 `~/.u2net`。CPU 上每张图多花约 30 秒。

```bash
curl http://localhost:18610/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"A red fox sitting","background":"transparent"}'
```

**用参考图生成图片**

`"image"` 接受一张参考图或最多 4 张的列表（data URL、http(s) URL 或纯 base64）。参考图会像聊天上传一样附在发往 muse.ai 的消息里，提示词说明用法，例如让模特穿上某件卫衣，或用 "the same fox, now wearing a red scarf" 保持主体和场景。`/v1/images/edits` 用 OpenAI 风格的 multipart 上传做同样的事，所以 `client.images.edit()` 能用；`mask`（局部重绘）不支持。

```bash
curl http://localhost:18610/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"A model wearing exactly this hoodie, studio photo","image":"data:image/jpeg;base64,..."}'

curl http://localhost:18610/v1/images/edits \
  -H "Authorization: Bearer $KEY" \
  -F "image[]=@fox.png" -F "prompt=The same fox, now wearing a red scarf"
```

**异步生成图片**

同步的图片调用（页面加载加生成）可能超过反向代理的超时；Cloudflare 约 100 秒后会返回 HTTP 524。加 `"async": true` 会返回一个任务，再轮询它。结果总是存成媒体 URL。

```bash
curl http://localhost:18610/v1/images/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"A red fox sitting","async":true}'          # -> {"id":"task_xxx","object":"image.task",...}
curl http://localhost:18610/v1/images/generations/task_xxx -H "Authorization: Bearer $KEY"
```

**文生视频（异步任务）**

```bash
# 建任务，返回 {"id": "task_xxx", "status": "queued", ...}
curl http://localhost:18610/v1/videos \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"Maple leaves falling in a light breeze","duration":5,"size":"16:9"}'

# 轮询任务；成功后 result.url 指向视频
curl http://localhost:18610/v1/videos/task_xxx -H "Authorization: Bearer $KEY"
```

**导入账号**

```bash
curl http://localhost:18610/admin/accounts \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"label":"acc1","cookies":{"hatch_sess":"...","hatch_native_auth_device":"..."}}'
```

### 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` · `/readyz` | 存活 · 就绪检查（driver 状态和可用账号） |
| GET | `/v1/models` | 模型列表，含 `gpt-4o`、`dall-e-3` 等别名 |
| POST | `/v1/chat/completions` | 聊天，流式和非流式 |
| POST | `/v1/images/generations` · GET `/v1/images/generations/{id}` | 文生图（可选 `image` 参考图），返回 `url` 或 `b64_json`；`"async": true` 返回可轮询的任务 |
| POST | `/v1/images/edits` | multipart `image`/`image[]` 文件 + `prompt`，OpenAI 兼容 |
| POST | `/v1/videos` · GET `/v1/videos/{id}` | 建视频任务 · 查任务 |
| GET | `/v1/media/{name}` | 下载生成的媒体文件 |
| GET/POST/PATCH/DELETE | `/admin/accounts[/{id}]` | 账号增删改查 |
| POST | `/admin/accounts/import` | 账号批量导入，逐条报错 |
| POST | `/admin/accounts/{id}/renew` · `/reset` | 会话续期 · 重置账号状态 |
| GET | `/admin/accounts/{id}/quota[?refresh=1]` | 额度快照（缓存 30 分钟；`refresh=1` 重新读取；账号所有 tab 都在忙时返回 409 `account_busy`） |
| GET | `/admin/extension.zip` | 下载 cookie 导入 Chrome 插件 |
| GET | `/admin/status` · `/admin/tasks` | 服务状态 · 任务列表 |
| GET/POST/PATCH/DELETE | `/admin/keys[/{id}]` | 客户端 API key（创建时只返回一次明文；PATCH 改名/吊销） |
| GET | `/admin/requests` · `/admin/stats?window=1h\|24h\|7d` | 请求日志（可按 `key_id`、`account_id`、`status=2xx\|4xx\|5xx`、`path`、`since` 过滤，`hide_polls` 隐藏轮询）· 聚合统计 |
| GET | `/dashboard` | 网页控制台（在浏览器里输入 admin key） |
| POST | `/v1/responses` | 预留，目前返回 501 |

### 控制台、API key 与请求日志

打开 `http://localhost:18610/dashboard` 并输入 admin key；key 存在浏览器的 local storage 里，以 Bearer token 发给 `/admin/*`。页面展示请求量、错误率、延迟、账号池（启用/禁用、续期、重置）、实时请求日志和 API key 管理。

![控制台概览](docs/images/dashboard-overview.png)

每张账号卡片显示额度（用量条、套餐、重置日期、读取时间，点 **Quota** 按钮可重新读取）、会话 cookie 的过期时间，以及上次唤醒后 VM 是否确认在运行。**Import hàng loạt**（批量导入）每行一个 `标签 | cookie 字符串`。

![账号页](docs/images/dashboard-accounts.png)

#### Cookie 导入插件

muse.ai 的会话 cookie 都是 httpOnly，页面脚本读不到；一个小小的 Chrome / Edge 插件（Manifest V3，位于 `src/muse2api/web/extension/`）可以。

1. 在控制台的账号页点 **Tải extension**（下载插件），得到 `muse2api-extension.zip`（也可以直接访问 `GET /admin/extension.zip`），解压。
2. 打开 `chrome://extensions`，开启**开发者模式**，点**加载已解压的扩展程序**，选择解压后的文件夹。
3. 在这个浏览器里登录 [muse.ai](https://muse.ai/)。
4. 打开插件弹窗，填入网关地址（如 `http://localhost:18610`）和 admin key，可选填一个标签，然后点导入。

插件会连同过期时间一起读取 muse.ai 的 cookie，POST 到 `/admin/accounts`。它固定的主机权限只有 `muse.ai`；网关地址的访问权限在保存设置时才申请，cookie 本身从不保存在插件里。详见[插件说明](src/muse2api/web/extension/README.md)。

除了 admin key 和 `MUSE2API_API_KEY`（仍可用，日志里记为 `legacy`），`/v1/*` 接受任何未被吊销的 key：在控制台或用 `POST /admin/keys {"name": "..."}` 创建。key 长这样 `m2a-...`，只在创建时展示一次；`data/keys.json` 只存它们的 SHA-256 哈希和 8 字符前缀。每个 `/v1/*` 请求（`/v1/media/*` 下载除外）都会记到 `data/requests.db`（SQLite），含 key、模型、账号、状态码、延迟和客户端 IP（识别 `CF-Connecting-IP` / `X-Forwarded-For`）。任务状态轮询会被打标，控制台和统计可以排除它们。

## 配置

所有配置来自环境变量（前缀 `MUSE2API_`）或 `.env` 文件。完整列表见 [.env.example](.env.example)。常用的：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MUSE2API_DRIVER` | `mock` | driver：`mock` / `browser` / `http` |
| `MUSE2API_HOST` · `MUSE2API_PORT` | `127.0.0.1` · `18610` | 监听地址和端口 |
| `MUSE2API_API_KEY` | 自动生成 | `/v1/*` 接口的 key |
| `MUSE2API_ADMIN_KEY` | 同 API key | `/admin/*` 接口的 key |
| `MUSE2API_REQUEST_LOG_RETENTION_DAYS` | `14` | `data/requests.db` 里保留多少天的请求历史 |
| `MUSE2API_MEDIA_RETENTION_DAYS` | `14` | `data/media` 里生成的媒体文件保留多少天；`0` 关闭自动清理 |
| `MUSE2API_STREAM_HEARTBEAT_INTERVAL` | `0` | 等第一个流式 token 期间 SSE `: keepalive` 注释的发送间隔（秒）；`0` 关闭 |
| `MUSE2API_PUBLIC_BASE` | 空 | 媒体链接的公开 base URL；空时从请求推导 |
| `MUSE2API_POOL_STRATEGY` | `lru` | 账号调度：`lru` / `round_robin` / `affinity` |
| `MUSE2API_MAX_FAILOVER` | `2` | 换账号重试的最大次数 |
| `MUSE2API_ACCOUNT_MAX_CONCURRENCY` | `4` | 每个账号的并行请求数（browser driver：每请求一个 tab，每个 tab 约占 300 MB 内存） |
| `MUSE2API_WARM_THREADS` | `2` | 每个账号预先开好的对话数。muse.ai 新对话的第一条回复要 20～40 秒，已开好的对话约 5 秒，所以需要新对话的请求会放进预热好的对话里。每个预热对话多花一条短消息的额度；并发至少要 2 |
| `MUSE2API_CHROMIUM_PATH` | 自动探测 | 浏览器可执行文件路径 |
| `MUSE2API_KEEPALIVE_ENABLED` | `true` | 后台定期续期会话并唤醒各账号的 VM |
| `MUSE2API_KEEPALIVE_INTERVAL` | `600` | 两轮保活之间的秒数 |
| `MUSE2API_WAKE_CONFIRM_S` | `20` | 唤醒 VM 后最多等多少秒让它报告 `RUNNING` |
| `MUSE2API_QUOTA_CACHE_S` | `1800` | 额度读数复用多久（秒），过期后才重新问 muse.ai |
| `MUSE2API_MATTING_MODEL` | `birefnet-general` | `background: "transparent"` 用的抠图模型（`birefnet-general-lite` 更快） |

## 路线图

所有预留模块和未完成的工作都在 **[TODO.md](TODO.md)** 里跟踪，分四组：

- **验证与实测（先做）**：把测试跑通、用真实账号测浏览器 driver、验证图片/视频生成和会话续期。
- **Driver**：直连 HTTP 协议 driver、会话复用降首字延迟、按额度调度账号。
- **协议层**：`/v1/responses`、mask 局部重绘、仿真 tool calling。
- **账号、管理与运维**：存储后端、按 key 限流、Prometheus 指标。

每个任务都写了位置、做法和完成标准。代码里标了 `TODO(contributors)` 的地方是预留的扩展点。

## 参与贡献

欢迎从 [TODO.md](TODO.md) 里认领任务。动手前请先开一个 issue 说明你的计划；规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

```bash
pytest           # 所有测试都用 mock driver
ruff check .     # lint
```

## 免责声明

本项目仅用于学习和技术研究。请遵守 muse.ai 的服务条款和当地法律。切勿提交或公开账号 cookies。

## Star 历史

[![Star History Chart](https://api.star-history.com/svg?repos=www222fff/muse2api&type=Date)](https://star-history.com/#www222fff/muse2api&Date)

## 许可证

[MIT](LICENSE)
