# CatalogWatch

自托管的免费 LLM **模型目录 + 额度文档** 近实时监控服务。

它解决的是 freellmapi 免费版的一个硬伤：内置目录是月度快照，新模型要等约 30 天。
CatalogWatch 不依赖它的签名 feed，而是直接轮询各家 provider 的模型接口和额度文档，
把变化写进本地 SQLite，并通过 macOS 通知 / Webhook / HTTP API 暴露出来。

它**不做**这些事：

- 不代替 freellmapi 做推理路由（两者可以并行使用）
- 不自动注册账号、不自动领取 API key（那会违反各家 ToS）
- 不承诺额度数据的秒级实时性（provider 本身不提供额度 API）

容器镜像另有一个可选的 **WorkBuddy 桥**（Buddy2api），把你自己 WorkBuddy 账号的模型
暴露成 OpenAI 兼容接口，并自动注册成 CatalogWatch 的一个 provider。见
[WorkBuddy 容器集成](#workbuddy-容器集成可选)。

## 架构

```
config/providers.yaml
        │
        ├── ModelPoller ──► GET {provider}/models ──► SQLite(models) ──► events
        │       每 15–45 分钟
        │
        └── QuotaMonitor ─► 额度/定价文档 ─► 文本归一化 + hash ─► diff ─► events
                每 60–120 分钟                              │
                                                            └─(可选) LLM 结构化提取
events ──► macOS 通知 / Webhook
SQLite ──► HTTP API :8787 (/catalog, /changes, /quota/sources)
           容器内为 :8788
```

设计要点：

- **首次抓取只做基线**，不会产生几百条“新模型”通知。
- **模型指纹只覆盖关心的字段**（上下文、输出上限、价格、免费状态），provider 的
  描述文字变化不会制造假事件。
- **异常快照保护**：provider 返回空列表或模型数骤降 50% 以上时拒绝写入，保留旧目录。
- **额度文档按内容 hash 判断变化**，再用 unified diff 给出增删行，避免把页面布局
  抖动当成额度变化。
- 可选接一个 OpenAI 兼容端点（例如本机 freellmapi），把变化后的额度文档转成结构化
  JSON 存入 `quota_snapshots`。

## 快速开始

> 最终交付是 GitHub Actions 构建的容器镜像。下面只是本地调试 CatalogWatch 的方式；
> 不要在本地构建镜像，镜像构建/依赖编译全部交给 CI。

```bash
uv sync
cp .env.example .env      # 填入你人工注册得到的各家 key
uv run catalogwatch check-config
uv run catalogwatch poll  # 首次：建立基线
uv run catalogwatch serve # API + 后台调度，默认 127.0.0.1:8787
```

打开 <http://127.0.0.1:8787/docs> 查看交互式 API 文档。

`poll` 会同时轮询公开目录（OpenRouter、SambaNova、NVIDIA 无需 key），缺失 key 的
provider 会标记为 `needs_key` 并跳过，不影响其他 provider。

### 常用命令

| 命令 | 作用 |
|---|---|
| `uv run catalogwatch init` | 建库、同步配置、打印 provider 概况 |
| `uv run catalogwatch poll` | 立即轮询所有 provider 的模型列表 |
| `uv run catalogwatch poll --provider openrouter` | 只轮询一家 |
| `uv run catalogwatch poll --quota` | 同时检查额度文档 |
| `uv run catalogwatch check-quotas` | 只检查额度/定价文档 |
| `uv run catalogwatch changes --since 24h` | 查看最近 24 小时事件 |
| `uv run catalogwatch sources` | 查看额度源状态 |
| `uv run catalogwatch status` | provider 状态与统计 |
| `uv run catalogwatch export -o exports/catalog.json` | 导出免费模型目录 |
| `uv run catalogwatch serve` | 启动 API 与调度器 |
| `uv run pytest` | 运行测试 |

## WorkBuddy 容器集成（可选）

仓库根目录的镜像把两个进程打进同一个容器：

- **Buddy2api** `:8787` — WorkBuddy 浏览器/设备码登录 + OpenAI Chat/Responses 兼容网关
- **CatalogWatch** `:8788` — 目录 API + 调度器；启动时自动把 WorkBuddy 注册为一个 provider

```
other agents ──► :8787/v1  (Chat / Responses)
                     ▲
                     │ 同一容器
CatalogWatch ────────┘  (用内部 key 轮询 /v1/models)
```

### 为什么选 Buddy2api

- 登录走 WorkBuddy 官方 CLI 的 device-authorization：容器内向官方申请 state，你在浏览器
  完成授权，容器轮询拿 token。**不需要桌面端，也不需要手工粘贴 token**。
- Python、MIT、凭证用 Fernet 加密入库；和 CatalogWatch 同栈，镜像构建简单。
- 对比：`workbuddy-to-dsh` 的 Docker 形态只支持外部 `CODEBUDDY_API_KEY` 网关，容器里
  拿不到插件登录凭据；`agent2api` 协议更全（含 Messages），但体量大、凭证明文，且许可证
  带“禁止商用/禁止分发牟利”的追加条款。

### 启动与登录

镜像由 GitHub Actions 构建并推送到 GHCR；部署端只拉取，不在本地构建：

```bash
cp .env.example .env
# 在 .env 里设置 FREE_LLM_API_IMAGE=ghcr.io/<owner>/<repo>:latest
docker compose pull
docker compose up -d
docker compose exec app workbuddy-login --realm cn   # 国际版用 global
```

终端会打印一个授权 URL；在浏览器打开并登录。成功后脚本会自动触发一次 CatalogWatch 刷新。

- 管理页/推理网关：<http://127.0.0.1:8787/>
- CatalogWatch：<http://127.0.0.1:8788/>
- 管理 token：`/data/secrets/admin_token`（或用 `CB_GATEWAY_ADMIN_TOKEN` 指定）
- 内部网关 key：`/data/secrets/gateway_key`，由启动脚本自动创建，只给 CatalogWatch 轮询用
- 给其他 agent 使用：在管理页 `API Keys` 创建 key，Base URL 填
  `http://127.0.0.1:8787/v1`。Chat 走 `/v1/chat/completions`，Codex 走
  `/v1/responses`。WorkBuddy key 默认通道选 `workbuddy`（国际版选 `workbuddy-i`）。

如果你是在管理页点「浏览器登录」而不是用 `workbuddy-login`，登录后可以手动触发一次
目录刷新，不用等下一个轮询周期：

```bash
curl -X POST 'http://127.0.0.1:8788/poll?provider=workbuddy'
```

### 第一阶段控制台：账号 / 成本目录 / 签到

`http://127.0.0.1:8788/` 是本项目自己的控制台，管理 token 由容器生成：

```bash
docker compose exec app cat /data/secrets/catalogwatch_admin_token
# 浏览器打开 http://127.0.0.1:8788/，用这个 token 登录
```

本阶段已经可用：

- **多账号**：添加国内/国际版账号、查看状态/积分/今日用量、刷新、启停、删除
- **近实时成本目录**：定期触发 bridge 的 `/admin/models/refresh`，读取
  `/admin/workbuddy/config`，展示基础倍率、当前倍率、免费状态、促销/夜间窗口
- **手动兜底**：CLI 身份拿不到促销时，在「模型与倍率」里手动设置基础倍率、
  折扣因子和每日窗口（跨午夜、时区都支持）
- **签到开关**：默认关闭；开启时要求勾选风险确认，执行由 Buddy2api 自带调度器完成
- **审计**：登录、账号操作、签到、目录刷新等动作可查

本阶段**尚未启用**：

- 统一 `/v1` 池路由、`sk-cw-` 池 Key、按积分自动选账号、请求账本
- 这些代码已经在仓库里，下一阶段接入；当前 agent 推理仍走 bridge
  `http://127.0.0.1:8787/v1`，用 bridge 管理页创建的 key

目录默认每 60 秒刷新一次（可在控制台「设置」调整）；WorkBuddy provider 的普通轮询
也调到 2 分钟，避免 `/models` 落后。多账号池化和自动签到可能违反 WorkBuddy 条款，
存在限流/封号风险；只使用你本人合法持有的账号，默认不要开启自动签到。

### 持久化与安全

只需要持久化 `/data`：

- `buddy2api/buddy2api.db` + `buddy2api/buddy2api.db.credentials.key`
- `catalogwatch/catalog.db`
- `secrets/admin_token`、`secrets/gateway_key`
- `secrets/catalogwatch_admin_token`、`secrets/pool_key_pepper`

**备份整个 `/data`**。只备份 DB 而丢掉 `.credentials.key`，已保存的 WorkBuddy token 就
无法解密。compose 只把端口绑到 `127.0.0.1`；远程服务器请用 SSH 隧道：

```bash
ssh -L 8787:127.0.0.1:8787 -L 8788:127.0.0.1:8788 user@server
```

8787 等同于你的 WorkBuddy 账号，不要暴露到公网。这是非官方接口，可能随 WorkBuddy
更新失效。多账号池化和自动签到可能违反上游条款，存在限流/封号风险；只使用你本人
合法持有的账号，自动签到默认关闭、需显式确认，不要做转售或公网托管。

### 构建与 CI

本地可以运行 `pytest` 抓代码错误；**镜像构建/依赖编译仍由 GitHub Actions 完成**，
不要在本地构建镜像。

- `.github/workflows/ci.yml`：`uv sync --frozen` + `pytest`。
- `.github/workflows/docker.yml`：
  - push/tag 构建并推送 `ghcr.io/<owner>/<repo>`（`latest`、分支、tag、sha）
  - 每次构建后在 runner 上跑容器 smoke test：两个进程健康、自动创建内部 key、
    `/v1/models` 可访问、控制台 `/` 可打开、`/api/overview` 未登录 401、
    Bearer 管理 token 可访问账号 API；不需要 WorkBuddy 登录
- 当前固定构建 `linux/amd64`，与 Buddy2api 上游镜像一致。需要 arm64 时再验证
  `wasmtime` 等依赖的 wheel。
- 镜像名由 CI 自动生成：`ghcr.io/<你的 GitHub 用户名>/<仓库名小写>`（例如仓库
  `LLM-API-AGG` 对应 `llm-api-agg`）。拉取：
  `docker pull ghcr.io/<owner>/<repo>:latest`。私有仓库需要在 GHCR 包设置里
  调整可见性；如果包是私有的，部署机先登录：

  ```bash
  echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
  ```

  token 需要 `read:packages` 权限。

### 已知限制

- 网关只支持 OpenAI Chat + Responses，**没有 Anthropic `/v1/messages`**。只讲 Messages
  的客户端（如 Claude Code）需要额外转换层；可以后续在 CatalogWatch 里加。
- WorkBuddy 的免费模型由上游策略决定（x0.00 倍率、限时活动等）。控制台的「模型与
  倍率」会按当前时间计算基础/有效倍率和免费状态；但**单模型剩余免费额度没有上游
  API**，只能用本地账本 + 429/错误码 + 可配置日上限近似。
- CLI/设备码登录身份可能拿不到 `/v3/config` 促销；控制台会显示警告，并允许在
  「模型与倍率」里手动配置倍率和夜间窗口。兼容身份探测是显式可选项，默认不启用。
- 登录态过期后重新执行 `workbuddy-login`。桥的协议变化可能需要更新
  `vendor/buddy2api`（见 `vendor/buddy2api/UPSTREAM.md`）。
- 统一 `/v1` 池路由、池 Key、跨账号积分调度和请求账本属于下一阶段；本阶段只提供
  账号/目录/签到/审计控制台。

## 配置

配置在 `config/providers.yaml`，key 只放环境变量。

```yaml
providers:
  - id: groq
    name: Groq
    adapter: openai_models          # openai_models | openrouter | google_models | workbuddy
    base_url: https://api.groq.com/openai/v1
    key_env: GROQ_API_KEY
    key_required: true              # false = 模型列表公开可读
    free_kind: free_tier            # zero_price | free_tier | credits
    poll_interval_minutes: 30
    quota_sources:
      - label: rate-limits
        url: https://console.groq.com/docs/rate-limits
        interval_minutes: 60
        # selector: "main"         # 可选：只取页面某一部分的文本
        # fields: ["requestsPerMinute", "requestsPerDay"]
        #   ^ 可选：从客户端渲染页面的内嵌 JSON 中提取指定字段（优先于 selector）
```

`free_kind` 的含义：

- `zero_price`：只有 prompt/completion 价格都为 0 的模型才算免费（OpenRouter）
- `free_tier`：模型在 provider 的免费额度内可用（包括 NVIDIA NIM 的限流免费层）
- `credits`：模型通过赠送额度/试用额度可用（保留给仍采用该模式的 provider）

新增 provider 只需要加一段 YAML；如果目录不是 OpenAI 兼容格式，在
`catalogwatch/adapters/` 里加一个适配器并注册到 `registry.py`。

### 可选的额度结构化提取

```yaml
extractor:
  enabled: true
  base_url: http://127.0.0.1:3001/v1   # 可以指向本机 freellmapi
  model: auto:fast
  api_key_env: FREELMAPI_KEY
```

开启后，只有额度文档**发生变化**时才会调用一次模型，把页面转成：

```json
{
  "provider": "Groq",
  "plans": [{"name": "Free", "limits": [{"metric": "requests/min", "value": 30, "period": "minute"}]}],
  "free_models": ["llama-3.3-70b-versatile"],
  "effective_date": null,
  "source_url": "..."
}
```

结果通过 `GET /quota/sources` 的 `extracted` 字段返回。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 + 统计 |
| GET | `/stats` | 计数与最近事件时间 |
| GET | `/providers` | provider 状态、模型数、最后错误 |
| GET | `/models?free=true&provider=&q=` | 模型查询 |
| GET | `/catalog?free_only=true` | 按 provider 分组的导出目录 |
| GET | `/changes?since=24h&kind=model.new` | 变更事件（支持逗号分隔多种 kind） |
| GET | `/quota/sources` | 额度源状态与最近一次结构化结果 |
| POST | `/poll?provider=&quota=&notify=` | 立即触发轮询 |

事件 `kind`：`model.new`、`model.changed`、`model.removed`、`provider.baseline`、
`quota.changed`、`quota.extracted`。

## 与 freellmapi 的关系

freellmapi 的目录来自 `freellmapi.co` 的签名 feed：

- 免费版跟进月度快照，README 目前标注落后约 303 个模型、约 30 天
- Premium（$19/年或 $49 买断）每天同步两次，作者当天发布的更新当天到达
- 没有配置项可以指向自建 feed，下载会校验内置 Ed25519 公钥

所以本项目的定位是**它的上游发现层**：

1. CatalogWatch 近实时发现新模型、免费状态和额度文档变化；
2. 你据此把新模型/key 加进 freellmapi（它没有公开的 provider/model 写入 API，
   这一步只能走 Dashboard 或自定义 OpenAI 兼容 provider）；
3. freellmapi 继续负责限流跟踪、冷却、故障转移和统一 `/v1` 出口。

如果你以后想要完全自主的目录驱动路由，可以直接消费本服务的 `/catalog`，自己写一
层轻量 router，而不需要 fork freellmapi 的同步逻辑。

## 已知限制

- provider 不通过 API 暴露“剩余免费额度”，额度只能以官方文档为准；本项目检测的是
  文档变化，不是账户余额。
- 部分文档页是 JS 渲染的。首次抓取后如果 `sources` 显示文本过短，给该源加 `selector`
  或改用可静态渲染的文档地址；如果页面内容完全在内嵌 JSON 里，用 `fields` 提取。
- NVIDIA NIM 的免费层是**限流模式**（FAQ 中写的是 up to 40 RPM / 10,000 请求/天，
  随模型和流量浮动），不是按 token 计费；部分模型还需要在模型页单独点 “Try API”
  注册，未注册时推理会返回 403。它没有用量/限额查询 API，个人剩余额度只能看
  build.nvidia.com 右上角。
- `free_tier` / `credits` 类 provider 的 `is_free` 表示“在免费额度内可用”，并不代表
  该模型永久免费；只有 `zero_price` 是逐模型按价格判断。
- API 无鉴权，默认只绑定 `127.0.0.1`。不要把端口暴露到公网。
- key 只从环境变量读取，不写入数据库；数据库中会有模型元数据和额度文档文本。

## 测试

```bash
uv run pytest
```

测试不访问网络，使用手工构造的 provider 响应和 SQLite 临时库。
