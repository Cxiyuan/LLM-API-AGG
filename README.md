# LLM-API-AGG · WorkBuddy 资源池

把你自己合法持有的 WorkBuddy 账号（国内版 `workbuddy` / 国际版 `workbuddy-i`）通过
Buddy2api 桥接成统一的 OpenAI 兼容 `/v1` 接口，并提供多账号、积分/倍率、签到/保活、
池 Key、请求账本和调度控制台。

> 本项目**不再包含**免费厂商目录或直连推理（OpenRouter / Groq / Cerebras / Gemini /
> Mistral / Cohere / SambaNova / NVIDIA / Z.ai 等已全部移除）。模型只来自 WorkBuddy
> 桥接；`nvidia/...`、`groq/...` 这类旧的直连模型 ID 会被明确拒绝。

## 架构

```
外部客户端（agent / SDK）
        │  OpenAI Chat / Responses / Anthropic Messages
        ▼
LLM-API-AGG 池网关 :8788  /v1/*        （池 Key、账号/模型路由、session 亲和、账本）
        │  内部 gateway key + X-WorkBuddy-Account
        ▼
Buddy2api 桥 :8787  /v1/*              （WorkBuddy token、刷新、上游调用）
        │
        ▼
WorkBuddy 上游

控制台 :8788/  /api/*                   （账号、积分、倍率、签到、池 Key、账本、策略、审计）
```

- 一个容器内由 supervisord 同时运行 Buddy2api 与控制台/网关；两个端口都只应绑定本机。
- SQLite（默认 `/data/catalogwatch/catalog.db`）保存 WorkBuddy 账号快照、模型/倍率、
  池 Key、请求账本、session 绑定、审计与设置。旧版本留下的免费目录表不会被删除或读取。

## 模型 ID

| ID | 含义 |
|---|---|
| `auto` | 在 WorkBuddy 账号池里按“免费优先 → 低倍率 → 稳定性/负载”选择 |
| `free` | 只选当前免费的 WorkBuddy 模型 |
| `cheap` | 选当前有效倍率最低的模型 |
| `workbuddy/<model>` | 指定国内版 WorkBuddy 模型 |
| `workbuddy-i/<model>` | 指定国际版 WorkBuddy 模型 |

显式 `nvidia/...`、`groq/...` 等非 WorkBuddy 前缀返回 404（`model_unavailable`），
不会再悄悄落到 WorkBuddy 的同名模型。`/v1/models` 只列出别名和上述两类 WorkBuddy 资源。

## 控制台

登录 token 由容器生成在 `/data/secrets/catalogwatch_admin_token`（或环境变量
`CATALOGWATCH_ADMIN_TOKEN`）。页面：

- **总览**：账号数、可用积分、模型/免费模型、活跃会话、桥接健康、最近目录快照
- **WorkBuddy 账号**：登录/导入、启停、刷新、测试、积分、冷却、每日上限
- **模型与倍率**：WorkBuddy 模型、基础/当前倍率、促销窗口、手动覆盖
- **签到**：默认关闭；开启前有风险确认
- **池 Key**：创建/轮换/禁用，按模型白名单与每日请求/积分限额
- **请求账本**：路由到的账号/模型、成本来源、尝试记录、耗时与 token
- **调度策略 / 设置 / 审计**

## 部署

镜像由 GitHub Actions 构建（本仓库不在本地构建镜像）。在服务器上：

```bash
cp .env.example .env        # 填写镜像名与可选 token
docker compose up -d
docker compose exec app workbuddy-login --realm cn   # 或 --realm global
```

从 `/data/secrets/catalogwatch_admin_token` 取控制台 token，打开
<http://127.0.0.1:8788/>。WorkBuddy 登录也可在控制台的「WorkBuddy 账号」页发起。

关键环境变量：

| 变量 | 作用 |
|---|---|
| `BUDDY2API_URL` | 桥地址，容器内默认 `http://127.0.0.1:8787` |
| `CB_GATEWAY_ADMIN_TOKEN` | Buddy2api 管理 token（容器自动生成/持久化） |
| `WORKBUDDY_GATEWAY_KEY` | 池网关调用桥的内部 key（bootstrap 自动创建） |
| `CATALOGWATCH_ADMIN_TOKEN` | 控制台登录 token |
| `CATALOGWATCH_KEY_PEPPER` | 池 Key 的 HMAC pepper；丢失会使所有池 Key 失效 |
| `CATALOGWATCH_DB` | SQLite 路径，默认 `/data/catalogwatch/catalog.db` |

`/data` 是唯一需要备份的卷；丢失 `pool_key_pepper` 会让池 Key 全部失效，丢失
`buddy2api` 的凭证密钥会导致已登录的 WorkBuddy token 无法解密。

## 开发与测试

纯 Python 测试，不构建镜像：

```bash
uv sync
uv run pytest -q
uv run catalogwatch serve --host 127.0.0.1 --port 8788
```

CI（`.github/workflows/ci.yml`）运行测试；`.github/workflows/docker.yml` 构建镜像并做
容器冒烟（健康检查、控制台鉴权、池 Key、无账号 503、WorkBuddy-only 模型面）。

## 风险与边界

- 只能使用你本人合法持有的账号；多账号池化、自动签到/保活可能违反 WorkBuddy 条款，
  存在限流或封号风险。签到默认关闭，需显式确认。
- 上游模型/倍率/促销可能变化；控制台显示的是最近一次目录快照与探测结果。
- 模型预算耗尽或长时间无响应时，客户端应自行重试/降级；池网关会返回结构化错误，
  并对不可用模型做冷却。
- 本项目不实现自动注册、绕过验证、批量养号、反检测或凭证共享。
