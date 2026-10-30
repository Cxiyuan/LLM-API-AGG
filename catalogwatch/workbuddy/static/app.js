/* LLM-API-AGG console: unified model resources (free providers + WorkBuddy bridge).
   No external dependencies, no build step. */
(() => {
  "use strict";

  const state = { view: "overview", costs: [] };

  const $ = (selector, root = document) => root.querySelector(selector);

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2), value);
      } else {
        node.setAttribute(key, value);
      }
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child && child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function cookie(name) {
    const hit = document.cookie.split("; ").find((item) => item.startsWith(name + "="));
    return hit ? decodeURIComponent(hit.slice(name.length + 1)) : "";
  }

  async function api(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    if (method !== "GET") {
      const csrf = cookie("cw_csrf");
      if (csrf) headers["X-CSRF-Token"] = csrf;
    }
    const response = await fetch(path, {
      ...options,
      method,
      headers,
      credentials: "same-origin",
    });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    if (response.status === 401) {
      showLogin();
      throw new Error("需要登录");
    }
    if (!response.ok) {
      const detail = data && data.error ? data.error.message : data && data.detail ? data.detail : text;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function showLogin() {
    $("#login").hidden = false;
    $("#app").hidden = true;
    $("#nav").hidden = true;
    $("#logout").hidden = true;
  }

  function showApp() {
    $("#login").hidden = true;
    $("#app").hidden = false;
    $("#nav").hidden = false;
    $("#logout").hidden = false;
  }

  function fmt(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  function num(value) {
    if (value === null || value === undefined || value === "") return "—";
    const n = Number(value);
    return Number.isFinite(n) ? (Math.round(n * 1000) / 1000).toString() : String(value);
  }

  function badge(text, cls) {
    return el("span", { class: "badge " + (cls || ""), text: String(text) });
  }

  function stat(label, value) {
    return el("div", { class: "stat" }, el("div", { class: "label", text: label }), el("div", { class: "value", text: String(value) }));
  }

  const WARNING_TEXT = {
    promotions_unavailable: "促销信息暂不可用：按基础倍率调度，可在「WorkBuddy 倍率」页手动配置",
  };

  function warningLabel(value) {
    return WARNING_TEXT[value] || value;
  }

  function card(title, ...children) {
    return el("section", { class: "card" }, title ? el("h2", { text: title }) : null, ...children);
  }

  function table(headers, rows) {
    const head = el("tr", {}, ...headers.map((item) => el("th", { text: item })));
    const body = rows.map((cells) => el("tr", {}, ...cells.map((cell) => el("td", {}, cell))));
    return el("table", {}, el("thead", {}, head), el("tbody", {}, ...body));
  }

  function errorBox(error) {
    return el("p", { class: "error", text: String(error && error.message ? error.message : error) });
  }

  function setView(node) {
    const view = $("#view");
    clear(view);
    view.append(node);
  }

  async function action(path, options, reload) {
    try {
      await api(path, options);
      if (reload) await reload();
    } catch (error) {
      alert(error.message || String(error));
    }
  }

  // ------------------------------------------------------------------ overview

  async function loadOverview() {
    const [data, stats, resources] = await Promise.all([
      api("/api/overview"),
      api("/stats"),
      api("/api/models"),
    ]);
    const overview = data.overview || {};
    const bridge = data.bridge || {};
    const snapshot = data.config_snapshot || {};
    const summary = resources.summary || {};
    const warnings = [];
    // /health historically answered {"status":"ok"}; accept both shapes.
    const bridgeHealthy = bridge.ok === true || String(bridge.status || "").toLowerCase() === "ok";
    if (!bridgeHealthy) warnings.push("Buddy2api 不可达：" + (bridge.error || "unknown"));
    if (snapshot.warning) warnings.push("WorkBuddy 目录：" + warningLabel(snapshot.warning));
    if (overview.cooldowns && overview.cooldowns.length) warnings.push("有 " + overview.cooldowns.length + " 个账号处于冷却/额度耗尽状态");
    if (!summary.callable) warnings.push("当前没有可调用模型：登录 WorkBuddy 账号，或给免费厂商配置 API Key");
    setView(
      el(
        "div",
        {},
        card(
          "LLM-API-AGG 总览",
          el(
            "div",
            { class: "grid" },
            stat("目录厂商", stats.providers || 0),
            stat("目录模型", stats.models_present || 0),
            stat("免费目录模型", stats.models_free_present || 0),
            stat("可调用模型", summary.callable || 0),
            stat("模型资源", summary.total || 0),
            stat("WorkBuddy 账号", overview.accounts || 0),
            stat("可用积分", num(overview.available_credits)),
            stat("活跃会话", overview.sessions || 0)
          ),
          warnings.length ? el("div", {}, ...warnings.map((item) => el("p", { class: "warn", text: item }))) : el("p", { class: "ok", text: "运行正常" })
        ),
        card(
          "资源来源",
          el(
            "div",
            { class: "grid" },
            ...(resources.sources || []).map((source) =>
              stat(source.name + "（" + source.source + "）", source.callable + " / " + source.total + " 可调用")
            )
          ),
          el("p", { class: "muted", text: "会话 ID 与智能负载路由在统一 /v1 网关上全局生效，不区分来源。" })
        ),
        card(
          "Buddy2api",
          el("pre", {}, JSON.stringify(bridge, null, 2)),
          el("p", { class: "muted", text: "内部调试地址：http://127.0.0.1:8787/（仅本机）" })
        ),
        card(
          "最近 WorkBuddy 目录快照",
          el("pre", {}, JSON.stringify(snapshot || {}, null, 2))
        )
      )
    );
  }

  // -------------------------------------------------------------- model resources

  const REASONS = {
    no_account: "无 WorkBuddy 账号",
    no_key: "未配置厂商 Key",
    paid: "付费模型（不代理）",
    catalog_only: "仅目录（未接入推理）",
  };

  async function loadResources() {
    const data = await api("/api/models");
    const items = data.items || [];
    const sources = data.sources || [];
    const summary = data.summary || {};
    let sourceFilter = "";
    let callableOnly = false;
    let query = "";

    const sourceSelect = el(
      "select",
      { onchange: (event) => { sourceFilter = event.target.value; render(); } },
      el("option", { value: "", text: "全部来源" }),
      ...sources.map((source) => el("option", { value: source.source, text: source.name + "（" + source.callable + "/" + source.total + "）" }))
    );
    const search = el("input", {
      placeholder: "搜索模型 / 厂商",
      oninput: (event) => { query = event.target.value.trim().toLowerCase(); render(); },
    });
    const only = el("input", {
      type: "checkbox",
      onchange: (event) => { callableOnly = event.target.checked; render(); },
    });
    const body = el("tbody");
    const tableNode = el("table", {}, el("thead", {}, el("tr", {},
      ...["来源", "模型", "免费", "上下文", "倍率/价格", "可调用", "说明"].map((name) => el("th", { text: name }))
    )), body);

    function render() {
      clear(body);
      const rows = items.filter((item) => {
        if (sourceFilter && item.source !== sourceFilter) return false;
        if (callableOnly && !item.callable) return false;
        if (query) {
          const hay = (item.wire_id + " " + (item.name || "") + " " + (item.provider_name || "")).toLowerCase();
          if (!hay.includes(query)) return false;
        }
        return true;
      });
      for (const item of rows) {
        const cost = item.kind === "workbuddy"
          ? (item.free ? "免费（0）" : num(item.credits))
          : (item.free ? "0" : "付费");
        body.append(el("tr", {},
          el("td", {}, badge(item.source, item.kind === "workbuddy" ? "free" : "")),
          el("td", {},
            el("strong", { text: item.wire_id }),
            el("div", { class: "muted", text: (item.name || "") + " · " + (item.provider_name || item.provider) })),
          el("td", {}, item.free ? badge("免费", "free") : badge("付费", "paid")),
          el("td", { text: fmt(item.context_length) }),
          el("td", { text: cost }),
          el("td", {}, item.callable ? badge("可调用", "ok") : badge("不可用", "bad")),
          el("td", { text: item.callable ? "—" : (REASONS[item.reason] || item.reason || "—") })
        ));
      }
      if (!rows.length) {
        body.append(el("tr", {}, el("td", { colspan: "7", class: "empty", text: "没有符合条件的模型" })));
      }
    }

    setView(
      el("div", {},
        card("模型资源（全局）",
          el("div", { class: "grid" },
            stat("资源总数", summary.total || 0),
            stat("可调用", summary.callable || 0),
            stat("免费", summary.free || 0),
            stat("来源", summary.sources || 0)),
          el("div", { class: "toolbar" }, sourceSelect, search,
            el("label", { class: "chip" }, only, " 只看可调用"),
            el("button", { text: "刷新", onclick: () => loadResources().catch((error) => setView(card("加载失败", errorBox(error)))) })),
          el("p", { class: "muted", text: "WorkBuddy 是其中一个来源；免费厂商模型只要配置了 Key 且属于免费目录，就直接由统一 /v1 调用。会话亲和与负载路由对所有可调用资源生效。" }),
          el("p", { class: "muted", text: "免费标记来自厂商目录（OpenRouter 按零价，其余按 free_tier）；实际额度以厂商账户为准。付费模型不会被代理。" }),
          tableNode)),
        card("接入说明",
          el("p", { class: "muted", text: "WorkBuddy：控制台登录账号后，模型自动上架。" }),
          el("p", { class: "muted", text: "免费厂商：在部署环境配置 OPENROUTER_API_KEY / GROQ_API_KEY / CEREBRAS_API_KEY / GEMINI_API_KEY / MISTRAL_API_KEY / COHERE_API_KEY / SAMBANOVA_API_KEY / NVIDIA_API_KEY / ZAI_API_KEY，重启容器后对应免费模型变为可调用。" }),
          el("p", { class: "muted", text: "付费模型不会被代理，避免 auto 路由产生费用。" }))
    );
    render();
  }

  // --------------------------------------------------------------- provider keys

  async function loadProviders() {
    const data = await api("/api/providers");
    const items = data.items || [];
    const body = el("tbody");

    function statusBadge(item) {
      if (item.configured) {
        return el("span", {},
          badge("已配置", "ok"),
          " ",
          el("span", { class: "muted", text: item.source === "console" ? "控制台" : "环境变量" }));
      }
      return badge("未配置", "bad");
    }

    for (const item of items) {
      const isBridge = item.id === "workbuddy" || item.id === "workbuddy-i";
      const statusCell = el("td", {}, statusBadge(item));
      const pollCell = el("td", {},
        el("div", { text: item.last_status || "—" }),
        el("div", { class: "muted", text: (item.last_error || "").slice(0, 120) }));
      const input = el("input", {
        type: "password",
        autocomplete: "new-password",
        placeholder: item.configured ? "输入新 Key 覆盖" : "粘贴 API Key",
      });
      const note = el("span", { class: "muted" });

      async function save(test) {
        const value = input.value.trim();
        if (!value) { note.textContent = "请先输入 Key"; return; }
        note.textContent = "保存中…";
        try {
          await api(`/api/providers/${encodeURIComponent(item.id)}/key`, {
            method: "PUT",
            body: JSON.stringify({ api_key: value }),
          });
          input.value = "";
          item.configured = true;
          item.source = "console";
          statusCell.replaceChildren(statusBadge(item));
          note.textContent = "已保存";
          if (test) {
            note.textContent = "已保存，拉取目录中…";
            const result = await api(`/api/providers/${encodeURIComponent(item.id)}/poll`, { method: "POST" });
            pollCell.replaceChildren(
              el("div", { text: result.status }),
              el("div", { class: "muted", text: result.ok
                ? `${result.model_count} 个模型（免费 ${result.free_model_count}）`
                : (result.error || "") }));
            note.textContent = result.ok
              ? `测试通过：${result.model_count} 个模型`
              : `保存成功，拉取失败：${result.error || result.status}`;
          }
        } catch (error) {
          note.textContent = error.message || String(error);
        }
      }

      async function clear() {
        if (!confirm(`清除 ${item.name} 的 API Key？`)) return;
        note.textContent = "清除中…";
        try {
          await api(`/api/providers/${encodeURIComponent(item.id)}/key`, { method: "DELETE" });
          item.configured = false;
          item.source = null;
          statusCell.replaceChildren(statusBadge(item));
          note.textContent = "已清除（若环境变量仍有值会继续生效）";
        } catch (error) {
          note.textContent = error.message || String(error);
        }
      }

      body.append(el("tr", {},
        el("td", {},
          el("strong", { text: item.name }),
          el("div", { class: "muted", text: item.id + (isBridge ? " · 桥接" : item.chat_enabled ? " · 可直连" : " · 仅目录") })),
        statusCell,
        el("td", {}, el("code", { text: item.key_env || "—" })),
        el("td", { text: `${item.model_count} / 免费 ${item.free_model_count}` }),
        pollCell,
        el("td", {}, input, el("div", { class: "muted" }, note)),
        el("td", {},
          isBridge
            ? el("span", { class: "muted", text: "用桥接登录" })
            : el("div", { class: "row" },
              el("button", { class: "primary", text: "保存", onclick: () => save(false) }),
              el("button", { text: "保存并测试", onclick: () => save(true) }),
              el("button", { class: "danger", text: "清除", onclick: clear })))
      ));
    }

    setView(card("厂商 API Key",
      el("p", { class: "muted", text: "在这里录入后立即生效（加密存 SQLite）；环境变量仍可作为兜底。保存并测试会立刻拉取该厂商目录，验证 Key 是否可用。" }),
      el("p", { class: "muted", text: "只有 OpenAI 兼容的免费文本模型会进入统一 /v1；WorkBuddy 用账号登录，不在这里填 Key。" }),
      el("table", {},
        el("thead", {}, el("tr", {},
          ...["厂商", "Key 状态", "环境变量", "模型 / 免费", "最近轮询", "新 Key", "操作"].map((name) => el("th", { text: name })))),
        body))
    );
    if (!items.length) {
      body.append(el("tr", {}, el("td", { colspan: "7", class: "empty", text: "没有可配置的厂商" })));
    }
  }

  // ----------------------------------------------------------------- free catalog

  async function loadCatalog() {
    const [stats, providers, free, changes, quota] = await Promise.all([
      api("/stats"),
      api("/providers"),
      api("/api/catalog/free"),
      api("/changes?since=24h"),
      api("/quota/sources"),
    ]);
    const providerRows = (providers.items || []).map((item) => [
      el("div", {}, el("strong", { text: item.name }), el("div", { class: "muted", text: item.id + " · " + item.adapter })),
      el("span", { class: item.enabled ? "ok" : "bad", text: item.enabled ? "启用" : "停用" }),
      el("code", { text: item.key_env || "—" }),
      el("span", { class: item.last_status === "ok" ? "ok" : "warn", text: item.last_status || "—" }),
      fmt(item.model_count),
      fmt(item.free_model_count),
      el("span", { class: "muted", text: (item.last_error || "").slice(0, 120) }),
    ]);
    const freeRows = (free.items || []).map((item) => [
      el("div", {}, el("strong", { text: item.wire_id }), el("div", { class: "muted", text: item.name })),
      fmt(item.provider_name),
      fmt(item.context_length),
      fmt(item.free_kind),
      item.callable ? badge("可调用", "ok") : badge("仅目录", ""),
    ]);
    const quotaRows = (quota.items || []).map((item) => [
      fmt(item.provider_id),
      fmt(item.label),
      fmt(item.last_checked_at),
      fmt(item.last_changed_at),
      item.url ? el("a", { href: item.url, target: "_blank", rel: "noopener", text: "文档" }) : "—",
    ]);
    const changeRows = (changes.items || []).map((item) => [
      fmt(item.ts),
      fmt(item.kind),
      fmt(item.provider_id),
      fmt(item.summary),
    ]);
    setView(
      el("div", {},
        card("免费目录（free-llm-api 层）",
          el("div", { class: "grid" },
            stat("厂商", stats.providers || 0),
            stat("在线模型", stats.models_present || 0),
            stat("免费模型", stats.models_free_present || 0),
            stat("额度来源", stats.quota_sources || 0),
            stat("变更事件", stats.events || 0)),
          el("p", { class: "muted", text: "近实时抓取各厂商目录与额度文档；这里只用于发现和上架，免费厂商的推理由统一 /v1 直接调用。" })),
        card("厂商", table(["厂商", "状态", "Key 环境变量", "最近轮询", "模型", "免费模型", "错误"], providerRows)),
        card("免费文本模型", table(["模型", "厂商", "上下文", "免费类型", "调用"], freeRows)),
        card("额度来源", table(["厂商", "来源", "最近检查", "最近变更", "文档"], quotaRows)),
        card("最近变更（24h）", table(["时间", "类型", "厂商", "摘要"], changeRows))
      )
    );
  }

  // ------------------------------------------------------------------ accounts

  async function loadAccounts() {
    const data = await api("/api/workbuddy/accounts");
    const items = data.items || [];
    const flow = el("div", { id: "login-flow" });
    const realm = el("select", {}, el("option", { value: "cn", text: "国内版" }), el("option", { value: "global", text: "国际版" }));
    const toolbar = el(
      "div",
      { class: "toolbar" },
      realm,
      el("button", { class: "primary", text: "添加账号", onclick: () => startLogin(realm.value, flow) }),
      el("button", { text: "同步账号", onclick: () => action("/api/workbuddy/accounts/sync", { method: "POST" }, loadAccounts) }),
      el("button", { text: "刷新积分", onclick: () => action("/api/workbuddy/credits/refresh", { method: "POST" }, loadAccounts) }),
      el("button", { text: "全部签到", onclick: () => action("/api/workbuddy/checkin/run", { method: "POST" }, loadAccounts) })
    );

    const rows = items.map((account) => {
      const id = account.account_id;
      const stateData = account.state || {};
      const credit = account.latest_credit || {};
      const status = account.status || "unknown";
      const cooldown = stateData.cooldown_until || stateData.free_exhausted_until;
      return [
        el("div", {}, el("strong", { text: account.nickname || account.name || ("#" + id) }), el("div", { class: "muted", text: "ID " + id + " · " + (account.provider || "") })),
        badge(status, status === "active" ? "free" : "bad"),
        num(account.available_credits),
        num(credit.expiring_30d_total),
        el("div", { class: "muted", text: "free " + (stateData.free_requests_today || 0) + " / paid " + (stateData.paid_requests_today || 0) }),
        cooldown ? el("span", { class: "warn", text: cooldown }) : "—",
        el(
          "div",
          { class: "row" },
          el("button", { text: "刷新", onclick: () => action(`/api/workbuddy/accounts/${id}/refresh`, { method: "POST" }, loadAccounts) }),
          el("button", { text: "签到", onclick: () => action(`/api/workbuddy/accounts/${id}/checkin`, { method: "POST" }, loadAccounts) }),
          el("button", {
            text: status === "active" ? "停用" : "启用",
            onclick: () => action(`/api/workbuddy/accounts/${id}`, {
              method: "PATCH",
              body: JSON.stringify({ status: status === "active" ? "inactive" : "active" }),
            }, loadAccounts),
          }),
          el("button", {
            text: "限额",
            onclick: () => setCaps(id),
          }),
          el("button", {
            class: "danger",
            text: "删除",
            onclick: () => {
              if (confirm("删除账号 #" + id + "？桥内登录也会被删除。")) {
                action(`/api/workbuddy/accounts/${id}`, { method: "DELETE" }, loadAccounts);
              }
            },
          })
        ),
      ];
    });

    setView(
      el(
        "div",
        {},
        card("账号管理", toolbar, flow, items.length ? table(["账号", "状态", "可用积分", "30天到期", "今日用量", "冷却", "操作"], rows) : el("p", { class: "muted", text: "还没有账号。选择区域后点“添加账号”，在浏览器完成 WorkBuddy 授权。" })),
        card("说明", el("p", { class: "muted", text: "账号 token 只保存在 Buddy2api 的加密数据库里；本控制台只保存账号 ID、额度快照和用量。" }))
      )
    );
  }

  async function setCaps(accountId) {
    const modelId = prompt("模型 ID（* = 账号级；也可填具体模型，如 glm-5.2）：", "*");
    if (modelId === null) return;
    const requests = prompt("每日请求上限（0 = 不设本地上限）：", "0");
    if (requests === null) return;
    const credits = prompt("每日积分上限（0 = 不设）：", "0");
    if (credits === null) return;
    try {
      await api(`/api/workbuddy/accounts/${accountId}/caps`, {
        method: "PUT",
        body: JSON.stringify({
          model_id: modelId.trim() || "*",
          daily_request_cap: Number(requests),
          daily_credit_cap: Number(credits),
        }),
      });
      alert("已保存");
    } catch (error) {
      alert(error.message || String(error));
    }
  }

  async function startLogin(realm, flow) {
    clear(flow);
    try {
      const started = await api("/api/workbuddy/login/start", {
        method: "POST",
        body: JSON.stringify({ realm }),
      });
      flow.append(
        el(
          "div",
          { class: "card" },
          el("p", { text: "在浏览器打开下面的地址完成授权：" }),
          el("pre", {}, started.url || "(no url)"),
          el("p", { class: "muted", text: "等待授权中…" })
        )
      );
      const timer = setInterval(async () => {
        try {
          const result = await api("/api/workbuddy/login/poll?state=" + encodeURIComponent(started.state));
          if (result.done) {
            clearInterval(timer);
            flow.append(el("p", { class: "ok", text: "登录成功：" + (result.nickname || "") }));
            await loadAccounts();
          }
        } catch (error) {
          clearInterval(timer);
          flow.append(errorBox(error));
        }
      }, 3000);
    } catch (error) {
      flow.append(errorBox(error));
    }
  }

  // -------------------------------------------------------------------- models

  async function loadModels() {
    const data = await api("/api/workbuddy/models");
    state.costs = data.items || [];
    const rows = state.costs.map((item) => [
      el("div", {}, el("strong", { text: item.model_id }), el("div", { class: "muted", text: item.provider + " · " + fmt(item.name) })),
      num(item.base_credits),
      num(item.credits),
      item.free ? badge("免费", "free") : badge("计费", "paid"),
      item.promotion_active ? badge(fmt(item.badge, "促销") + " x" + num(item.promotion_factor), "free") : (item.badge ? badge(item.badge, "") : "—"),
      fmt(item.promotion_until),
      fmt(item.context_length),
      item.cost_source || "unknown",
      el(
        "div",
        { class: "row" },
        el("button", { text: "手动倍率", onclick: () => manualCost(item) }),
        el("button", { text: "删除手动", onclick: () => deleteManual(item) })
      ),
    ]);
    setView(
      el(
        "div",
        {},
        card(
          "模型与倍率",
          el(
            "div",
            { class: "toolbar" },
            el("button", { class: "primary", text: "刷新目录", onclick: () => action("/api/workbuddy/catalog/refresh", { method: "POST" }, loadModels) }),
            el("span", { class: "muted", text: "身份：" + (data.identity || "current") })
          ),
          data.config_snapshot && data.config_snapshot.warning
            ? el("p", { class: "warn", text: "目录警告：" + warningLabel(data.config_snapshot.warning) })
            : null,
          el("p", { class: "muted", text: "免费/低倍率按当前时间重算；夜间窗口使用促销自带的时区。已过滤 auto/default 等伪模型，以及视频、音频、图像生成等非文本模型。" }),
          table(["模型", "基础倍率", "当前倍率", "状态", "促销", "截止", "上下文", "来源", "操作"], rows)
        )
      )
    );
  }

  async function manualCost(item) {
    const base = prompt("基础倍率（留空表示不覆盖）：", item.base_credits === null ? "" : String(item.base_credits));
    if (base === null) return;
    const factor = prompt("活动倍率因子（例如 0.5；留空表示无折扣）：", "");
    if (factor === null) return;
    const windows = prompt('每日窗口 JSON，例如 [{"start":"23:00","end":"08:00"}]；留空表示全天：', "[]");
    if (windows === null) return;
    let parsedWindows = [];
    try {
      parsedWindows = windows.trim() ? JSON.parse(windows) : [];
    } catch {
      alert("窗口 JSON 解析失败");
      return;
    }
    try {
      await api("/api/workbuddy/models/manual", {
        method: "PUT",
        body: JSON.stringify({
          provider: item.provider,
          model_id: item.model_id,
          base_credits: base.trim() === "" ? null : Number(base),
          factor: factor.trim() === "" ? null : Number(factor),
          daily_windows: parsedWindows,
          timezone: "Asia/Shanghai",
          enabled: true,
        }),
      });
      await loadModels();
    } catch (error) {
      alert(error.message || String(error));
    }
  }

  async function deleteManual(item) {
    try {
      await api(
        "/api/workbuddy/models/manual?provider=" + encodeURIComponent(item.provider) + "&model_id=" + encodeURIComponent(item.model_id),
        { method: "DELETE" }
      );
      await loadModels();
    } catch (error) {
      alert(error.message || String(error));
    }
  }

  // ------------------------------------------------------------------- checkin

  async function loadCheckin() {
    const settings = await api("/api/workbuddy/checkin/settings");
    let status = null;
    try {
      status = await api("/api/workbuddy/checkin");
    } catch (error) {
      status = { error: error.message };
    }
    const hour = el("input", { type: "number", min: "0", max: "23", value: String(settings.hour || 0) });
    const enabled = el("input", { type: "checkbox" });
    enabled.checked = Boolean(settings.enabled);
    const risk = el("input", { type: "checkbox" });
    const save = el("button", {
      class: "primary",
      text: "保存",
      onclick: async () => {
        try {
          await api("/api/workbuddy/checkin/settings", {
            method: "PUT",
            body: JSON.stringify({ enabled: enabled.checked, hour: Number(hour.value), confirm_risk: risk.checked }),
          });
          await loadCheckin();
        } catch (error) {
          alert(error.message || String(error));
        }
      },
    });
    setView(
      el(
        "div",
        {},
        card(
          "自动签到",
          el("p", { class: "muted", text: "默认关闭。开启后由 Buddy2api 的调度器在每天指定小时领取积分；多账号自动签到可能违反 WorkBuddy 条款，存在限流/封号风险。" }),
          el("div", { class: "form-grid" }, el("label", { class: "field" }, "启用", enabled), el("label", { class: "field" }, "执行小时（0-23）", hour), el("label", { class: "field" }, "我已知晓风险", risk)),
          el("div", { class: "toolbar" }, save, el("button", { text: "立即签到", onclick: () => action("/api/workbuddy/checkin/run", { method: "POST" }, loadCheckin) }))
        ),
        card("签到状态", el("pre", {}, JSON.stringify(status, null, 2)))
      )
    );
  }

  // ---------------------------------------------------------------------- keys

  async function loadKeys() {
    const data = await api("/api/keys");
    const name = el("input", { placeholder: "agent 名称" });
    const kind = el("select", {}, el("option", { value: "agent", text: "每 agent 独立" }), el("option", { value: "shared", text: "共享" }));
    const limit = el("input", { type: "number", min: "0", value: "0", placeholder: "每日请求上限" });
    const creditLimit = el("input", { type: "number", min: "0", value: "0", placeholder: "每日积分上限" });
    const allowed = el("input", { placeholder: "允许模型，逗号分隔（留空=全部）" });
    const create = el("button", {
      class: "primary",
      text: "创建 Key",
      onclick: async () => {
        try {
          const result = await api("/api/keys", {
            method: "POST",
            body: JSON.stringify({
              name: name.value || "pool key",
              kind: kind.value,
              daily_request_limit: Number(limit.value || 0),
              daily_credit_limit: Number(creditLimit.value || 0),
              allowed_models: allowed.value.trim() ? allowed.value.split(",").map((item) => item.trim()).filter(Boolean) : null,
            }),
          });
          alert("新 Key 只显示一次，请立即保存：\n" + result.key);
          await loadKeys();
        } catch (error) {
          alert(error.message || String(error));
        }
      },
    });
    const rows = (data.items || []).map((key) => [
      el("div", {}, el("strong", { text: key.name }), el("div", { class: "muted", text: key.key_prefix + "… · " + key.kind })),
      badge(key.status, key.status === "active" ? "free" : "bad"),
      key.daily_request_limit || 0,
      num(key.daily_credit_limit),
      key.total_requests || 0,
      num(key.total_credits),
      el(
        "div",
        { class: "row" },
        el("button", {
          text: "轮换",
          onclick: async () => {
            try {
              const rotated = await api(`/api/keys/${key.id}/rotate`, { method: "POST" });
              alert("新 Key 只显示一次，请立即保存：\n" + rotated.key);
              await loadKeys();
            } catch (error) {
              alert(error.message || String(error));
            }
          },
        }),
        el("button", { class: "danger", text: "删除", onclick: () => action(`/api/keys/${key.id}`, { method: "DELETE" }, loadKeys) })
      ),
    ]);
    setView(
      el(
        "div",
        {},
        card("池 API Key", el("div", { class: "form-grid" }, el("label", { class: "field" }, "名称", name), el("label", { class: "field" }, "类型", kind), el("label", { class: "field" }, "每日请求上限", limit), el("label", { class: "field" }, "每日积分上限", creditLimit), el("label", { class: "field" }, "模型白名单", allowed)), create),
        card("已有 Key", table(["Key", "状态", "日请求上限", "日积分上限", "总请求", "总积分", "操作"], rows))
      )
    );
  }

  // ------------------------------------------------------------------ requests

  async function loadRequests() {
    const data = await api("/api/requests?limit=200");
    const rows = (data.items || []).map((item) => [
      el("code", { text: item.request_id }),
      fmt(item.session_id),
      fmt(item.created_at),
      fmt(item.model_requested) + " → " + fmt(item.model_routed),
      fmt(item.bridge_account_id),
      badge(fmt(item.status_code), item.status_code >= 200 && item.status_code < 300 ? "free" : "bad"),
      fmt(item.total_tokens),
      num(item.credits),
      fmt(item.cost_source),
      el("button", {
        text: "详情",
        onclick: async () => {
          try {
            const detail = await api("/api/requests/" + encodeURIComponent(item.request_id));
            alert(JSON.stringify(detail, null, 2).slice(0, 4000));
          } catch (error) {
            alert(error.message || String(error));
          }
        },
      }),
    ]);
    setView(card("请求账本", table(["请求", "会话", "时间", "模型", "账号", "状态", "Tokens", "积分", "成本来源", "详情"], rows)));
  }

  // -------------------------------------------------------------------- policy

  async function loadPolicy() {
    const policy = await api("/api/policy");
    const fields = {};
    const make = (key, type, label) => {
      let input;
      if (type === "bool") {
        input = el("input", { type: "checkbox" });
        input.checked = Boolean(policy[key]);
      } else {
        input = el("input", { type: type === "number" ? "number" : "text", value: fmt(policy[key], "") });
      }
      fields[key] = input;
      return el("label", { class: "field" }, label, input);
    };
    setView(
      card(
        "调度策略",
        el(
          "div",
          { class: "form-grid" },
          make("prefer_free", "bool", "优先免费模型"),
          make("allow_night", "bool", "夜间促销优先"),
          make("max_multiplier", "number", "最大倍率（0=不限）"),
          make("allow_over_max", "bool", "允许超过最大倍率"),
          make("min_credit_reserve", "number", "最低保留积分"),
          make("max_attempts", "number", "最大尝试次数"),
          make("max_model_candidates", "number", "最多候选模型"),
          make("free_429_threshold", "number", "免费 429 阈值"),
          make("unknown_cost", "text", "未知成本策略 after_known/allow/never"),
          make("account_strategy", "text", "账号策略 expiring_first/richest_first/balance"),
          make("routing_mode", "text", "路由模式 balanced/stable/cost"),
          make("session_ttl_seconds", "number", "会话粘性 TTL（秒）"),
          make("stability_weight", "number", "稳定性权重"),
          make("load_weight", "number", "并发负载权重"),
          make("cost_weight", "number", "成本权重"),
          make("session_jitter", "number", "会话分布抖动")
        ),
        el("button", {
          class: "primary",
          text: "保存策略",
          onclick: async () => {
            const body = {};
            for (const [key, input] of Object.entries(fields)) {
              if (input.type === "checkbox") body[key] = input.checked;
              else if (input.type === "number") body[key] = Number(input.value || 0);
              else body[key] = input.value;
            }
            try {
              await api("/api/policy", { method: "PUT", body: JSON.stringify(body) });
              await loadPolicy();
            } catch (error) {
              alert(error.message || String(error));
            }
          },
        })
      )
    );
  }

  // ------------------------------------------------------------------ settings

  async function loadSettings() {
    const settings = await api("/api/settings");
    const catalog = el("input", { type: "number", value: String(settings.catalog_refresh_seconds || 60) });
    const credits = el("input", { type: "number", value: String(settings.credit_refresh_seconds || 300) });
    const accounts = el("input", { type: "number", value: String(settings.account_sync_seconds || 60) });
    const identity = el("select", {}, ["current", "desktop", "vscode"].map((item) => el("option", { value: item, text: item })));
    identity.value = settings.config_identity || "current";
    setView(
      card(
        "设置",
        el(
          "div",
          { class: "form-grid" },
          el("label", { class: "field" }, "目录刷新（秒）", catalog),
          el("label", { class: "field" }, "积分刷新（秒）", credits),
          el("label", { class: "field" }, "账号同步（秒）", accounts),
          el("label", { class: "field" }, "促销探测身份", identity)
        ),
        el("p", { class: "muted", text: "desktop/vscode 是显式兼容探测，默认 current；拿不到促销时请用手动倍率/夜间窗口。" }),
        el("button", {
          class: "primary",
          text: "保存",
          onclick: async () => {
            try {
              await api("/api/settings", {
                method: "PUT",
                body: JSON.stringify({
                  catalog_refresh_seconds: Number(catalog.value),
                  credit_refresh_seconds: Number(credits.value),
                  account_sync_seconds: Number(accounts.value),
                  config_identity: identity.value,
                }),
              });
              await loadSettings();
            } catch (error) {
              alert(error.message || String(error));
            }
          },
        })
      )
    );
  }

  // --------------------------------------------------------------------- audit

  async function loadAudit() {
    const data = await api("/api/audit?limit=200");
    const rows = (data.items || []).map((item) => [
      fmt(item.ts),
      fmt(item.actor),
      fmt(item.action),
      fmt(item.target),
      el("code", { text: JSON.stringify(item.payload || {}, null, 0).slice(0, 300) }),
    ]);
    setView(card("审计日志", table(["时间", "角色", "动作", "目标", "详情"], rows)));
  }

  // ---------------------------------------------------------------- navigation

  const loaders = {
    overview: loadOverview,
    resources: loadResources,
    catalog: loadCatalog,
    providers: loadProviders,
    accounts: loadAccounts,
    models: loadModels,
    checkin: loadCheckin,
    keys: loadKeys,
    requests: loadRequests,
    policy: loadPolicy,
    settings: loadSettings,
    audit: loadAudit,
  };

  async function loadView() {
    try {
      await loaders[state.view]();
    } catch (error) {
      setView(card("加载失败", errorBox(error)));
    }
  }

  function bindNav() {
    for (const button of document.querySelectorAll("#nav button")) {
      button.addEventListener("click", () => {
        state.view = button.dataset.view;
        for (const other of document.querySelectorAll("#nav button")) {
          other.classList.toggle("primary", other === button);
        }
        loadView();
      });
    }
  }

  async function boot() {
    bindNav();
    $("#login-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const errorNode = $("#login-error");
      errorNode.hidden = true;
      try {
        await api("/api/auth/login", {
          method: "POST",
          body: JSON.stringify({ token: $("#login-token").value }),
        });
        showApp();
        state.view = "overview";
        await loadView();
      } catch (error) {
        errorNode.textContent = error.message || String(error);
        errorNode.hidden = false;
      }
    });
    $("#logout").addEventListener("click", async () => {
      try {
        await api("/api/auth/logout", { method: "POST" });
      } catch {
        // ignore
      }
      showLogin();
    });
    try {
      await api("/api/auth/session");
      showApp();
      await loadView();
    } catch {
      showLogin();
    }
  }

  boot();
})();
