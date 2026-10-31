# HANDOFF — LLM-API-AGG (catalogwatch)

> 由 2026-10-30 那个 1M 上下文会话(`9f17560b`)导出的交接文档。
> 目的:新会话/新 agent 读完这一份就能接着干,不需要原始 12MB 记录。

---

## 0. 一句话状态

主体开发已完成并 CI 全绿(commit `36b25bf`,本地 53 个测试通过),工作区干净。
**唯一未完成的是最后一步:改造 `~/Desktop/XDR/agent`,为 dsh 写 `llm-agg` 模型连接器(需支持 session-id)。这一步只做了只读探查,没写任何代码就撞上了上下文上限。**

---

## 1. 需求(用户已确认)

1. 完全个人使用,不做多租户。
2. 免费平台 API key 由用户人工注册,不自动注册账号。
3. 免费模型 / 额度目录要**近实时**(天级以内)更新。
4. 以 **WorkBuddy 免费模型**为主要供给;免费额度用完退到低倍率模型;夜间时段优先。
5. 多账号:多容器或其他隔离方式,支持多账号登录 + 自动领取每日免费积分。
6. 读取各账号积分,智能调度;前提是各账号免费额度已用完。
7. 桥接出来的模型资源由本项目平台统一管理分配。
8. WorkBuddy 账号登录 / 管理 Web 界面集成进本项目 UI。
9. 模型名为 `auto` 时做智能负载路由;**session-id 亲和**:同任务粘同一后端,失败才重绑。
10. 最终以容器运行,由 **GitHub CI 构建镜像**,不在 macOS 上跑生产。

---

## 2. 关键决策(别推翻,理由已验过)

| 决策 | 结论 | 理由 |
|---|---|---|
| 是否用 `tashfeenahmed/freellmapi` | ❌ 不用 | 只聚合你已有的 key,不产生额度;免费版目录落后约 30 天(差 300+ 模型);Ed25519 签名目录无法换源;单用户 local-first |
| 自建目录服务 | ✅ `catalogwatch` | 自己抓 `/v1/models` + 文档 + diff,近实时可控 |
| WorkBuddy 桥接选型 | ✅ **Buddy2api** v2.2.12 (`16a18af`) | Python + MIT + 已有容器化;走官方 CLI device-authorization 浏览器登录,**Linux 容器内无需桌面端**;登录态加密入库 |
| 被淘汰的候选 | `workbuddy-to-dsh`(容器内只能外部 key 网关)、`workbuddy-connect`(Windows only + 仅 Responses)、`agent2api`(Rust 体量大、凭证明文、许可证禁商用) | |
| 目录服务的"额度发现" | 抓取 + 解析 + diff | **没有任何 provider 用 API 暴露剩余免费额度**,这是行业现实,不是没做完 |

---

## 3. 仓库 / 产物

- 仓库:`https://github.com/Cxiyuan/LLM-API-AGG`(public,remote `origin`)
- 最新提交:`36b25bf feat: smart load routing with session affinity`
- 镜像:`ghcr.io/cxiyuan/llm-api-agg:{latest, main, sha-36b25bf}`
- CI:`.github/workflows/ci.yml` + `.github/workflows/docker.yml`(test → build → smoke)
- 本地测试:**53 passed**
- 端口:Buddy2api 内部 `8787`,CatalogWatch `8788`,统一池入口 `http://127.0.0.1:8788/v1`
- 进程管理:`deploy/supervisord.conf`(单容器跑两个服务)
- 本机原始笔记:`~/.claude/projects/-Users-0x16-Desktop-Free-LLM-API/memory/free-llm-project-constraints.md`(会话里被反复更新)

> ⚠️ `README.md` 被 `.gitignore` 排除,不在 GitHub 上;改动它不会进提交。原因当时未记录,恢复前先确认是刻意的。

---

## 4. 代码地图

```
catalogwatch/                 # 目录监控 + Web/API(原项目)
  db.py          1021 行      # SQLite schema / DAO,含 wb_* 账号、积分、账本表
  api.py          204 行      # FastAPI 组装,挂 workbuddy 子路由
  cli.py          289 行      # poll / init / serve / export / status ...
  poller.py / quota.py / extract.py / notify.py / service.py
  adapters/                   # openai_compat / openrouter / google / workbuddy
  workbuddy/                  # 第二阶段新增的账号池控制面
    store.py     1275 行      # wb_* 表读写:账号/积分/模型成本/池 Key/请求账本/会话
    router.py     621 行      # 路由打分:免费优先→稳定性→并发负载→成本→session 抖动
    gateway.py    592 行      # /v1 转发、流式、账本、限额
    messages.py   524 行      # Anthropic /v1/messages 适配(含 SSE + 工具调用)
    bridge.py     357 行      # 调 Buddy2api 管理接口
    promotions.py 318 行      # 倍率/夜间时段解析
    catalog.py    285 行      # 模型成本目录
    load.py       108 行      # in-flight 计数、EWMA 稳定性/延迟/429
    reconcile.py  108 行      # pending → 精确 credits/tokens 对账
    auth.py / api_admin.py / api_pool.py / service.py / accounts.py / dashboard.py
    static/{index.html,app.js,style.css}   # 控制台
vendor/buddy2api/             # vendored Buddy2api,已打补丁(见 §7)
deploy/                       # entrypoint.sh / supervisord.conf / workbuddy-login.py / start_catalogwatch.py / healthcheck.sh
tests/                        # 17 个测试文件,53 用例
~/.claude/plans/pure-spinning-pike.md   # 0–7 阶段实施计划(25KB,仍有参考价值)
```

---

## 5. 已完成能力(按提交顺序)

1. **目录监控**(commit 初版):多 provider 适配、`/v1/models` 抓取、定价/免费额度解析、diff 事件、SQLite、FastAPI、CLI、通知。
2. **容器化 + CI**:单镜像 supervisord 双进程;自动生成管理 token、内部网关 key;`workbuddy` provider 自动注入。
3. **`057b31b` 统一 `/v1` 池网关**:池 Key、请求账本、每日请求/积分限额、`/v1/models`、无账号时干净 503 `no_accounts`。
4. **`ffce8b9` 对账 + Anthropic 适配**:`cost_source=pending` 用 bridge 的 `client_request_id` 日志回填精确 tokens/credits;模型级限额 UI(`*` = 账号级);`/v1/messages` 适配。
5. **`36b25bf` 智能负载 + 会话粘性**:稳定性/延迟/429 EWMA、并发 in-flight 分摊、session 粘性与失败重绑、控制台策略项、账本 session 列。

### session-id 契约(已实现,服务端不用再改)

| 方式 | 值 |
|---|---|
| Header(推荐) | `X-Session-ID` |
| Header 别名 | `X-Session-Id`、`X-Claude-Code-Session-Id` |
| Body | `session_id` / `sessionId` |
| Anthropic metadata | `metadata.session_id` |

`auto` 模型默认 `routing_mode=balanced`,排序:免费层级 → 稳定性 → 并发负载 → 成本 → session 稳定哈希抖动。

---

## 6. 中断点:XDR agent 连接器(下一步就从这里开始)

**目标**:`~/Desktop/XDR/agent` 是基于 dsh(DeepSeek Harness)改造的"深瞳·安全分析智能体",全插件化。
需要新增一个 dsh `llm` 插件/适配器:当模型服务选 `llm-agg` 时,自动给请求带上 session-id;选其他 provider 时行为不变。

**已经探到的落点(只读,未落代码)**:

- 适配器接口在 `agent/dsh/packages/llm/llm/src/types.ts`(`LlmAdapter` / `registerAdapter` / `StreamChunk` / `ContentBlock` / `TokenUsage`)。
- 可参照的现成实现:`packages/llm/llm-deepseek/`(`adapter.ts` + `translate.ts`,含 SSE → StreamChunk 转换),`packages/llm/llm-pi-ai/`。
- 配置注入模式:`packages/llm/llm-deepseek-api-key/src/config.ts`(`registerConfigurableProviders` / `settingsNs`),以及 `packages/client` 里列 provider 的地方。
- UI 侧:`agent/plugins/xdr-ui/src/{system.ts,index.ts,http.ts}`(有 `saveProvider` / `saveModel` / `saveKey`),插件以 `cordis.patch.yml` 挂载。
- 存在 patch:`agent/dsh/patches/@earendil-works__pi-ai@1.1.0.patch`,里面已 grep 过 session/header/fetch 相关。
- 构建入口:`agent/Dockerfile`、`docker/entrypoint.sh`、`docker-compose.yml`;CI 在 `~/Desktop/XDR/.github/workflows/ci.yml`。

**建议做法**:新建 `agent/plugins/llm-agg/`(或 `packages/llm/llm-agg`)实现适配器,base_url 指向 `http://<catalogwatch>:8788/v1`,默认 header 注入 `X-Session-ID`,复用 dsh 现有的 OpenAI/Anthropic 转换逻辑,避免改 `llm-deepseek` 本体。先读 `pure-spinning-pike.md` 的 Phase 说明再动手。

**先验证再写码**:`~/Desktop/XDR/agent` 不在本仓库,改之前确认它的 git 状态和 CI 约束。

---

## 7. 踩过的坑(别再踩)

1. **同名方法互相覆盖**:`store.py` 里管理员会话和池会话方法同名,后者覆盖前者,session 绑定查不到。已改名为 `*_admin` / `pool_sessions` 系列。
2. **容器里 `pkg_resources` 缺失**:supervisord 依赖它。Dockerfile 必须 pin `setuptools==80.9.0`(新版移除了 `pkg_resources`)。
3. **supervisord 日志**:`/dev/stdout` 在容器里不可靠,改 `/proc/self/fd/1`、`/proc/self/fd/2`。
4. **CI 打包**:static 资源重复进 wheel → `.gitignore`/pytest 收集互相干扰,已修。
5. **NVIDIA NIM 额度**:credits 体系已退役,现在是 ~40 RPM / 10,000 请求/天,`free_kind` 应为 `free_tier` 而非 `credits`。网上大量文章是过期的。
6. **`workbuddy-to-dsh` 在容器里拿不到免费模型**:它的 Docker 形态只支持外部 key 网关,读不到桌面端插件凭据 —— 所以当初没选它。

---

## 8. 待办 / 未验证

- [ ] XDR agent 的 `llm-agg` connector(见 §6)— **阻塞点**
- [ ] 真实 WorkBuddy 账号登录、多账号隔离、每日签到、积分读取 —— 用户明确说**等部署到真实机器再测**,本地不用造数据
- [ ] 夜间时段/倍率策略在真实账号下的表现
- [ ] `README.md` 为何被 gitignore 出仓库(见 §3 警告)

---

## 9. 常用命令

```bash
cd ~/Desktop/Free-LLM-API
uv sync
uv run pytest -q                              # 期望 53 passed
uv run catalogwatch check-config
uv run catalogwatch poll --no-notify
uv run catalogwatch serve --port 8791
docker compose up --build                     # 本地容器验证
gh run list --repo Cxiyuan/LLM-API-AGG --limit 5
```

---

## 10. 原始会话指针

- 会话文件:`~/.claude/projects/-Users-0x16-Desktop-Free-LLM-API/9f17560b-9f7a-4986-a345-10567c207913.jsonl`(12MB / 3656 条 / 1012 次请求 / 峰值 1,008,992 tokens)
- 结构化摘要:`HANDOFF-digest.md`(同目录,按轮次列出所有命令与文件改动)
- 会话是在执行 `/compact` 时因 1,031,071 + 32,000 > 1,048,576 失败而终止的;恢复方式见 `HANDOFF-digest.md` 或直接用 `CLAUDE_CODE_MAX_OUTPUT_TOKENS=8000 claude --resume` 再 `/compact`。
