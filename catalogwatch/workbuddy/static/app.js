/* WorkBuddy pool console. No external dependencies, no build step. */
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
    const data = await api("/api/overview");
    const overview = data.overview || {};
    const bridge = data.bridge || {};
    const snapshot = data.config_snapshot || {};
    const warnings = [];
    if (!bridge.ok) warnings.push("Buddy2api 不可达：" + (bridge.error || "unknown"));
    if (snapshot.warning) warnings.push("目录警告：" + snapshot.warning);
    if (overview.cooldowns && overview.cooldowns.length) warnings.push("有 " + overview.cooldowns.length + " 个账号处于冷却/额度耗尽状态");
    setView(
      el(
        "div",
        {},
        card(
          "总览",
          el(
            "div",
            { class: "grid" },
            stat("账号", overview.accounts || 0),
            stat("可用积分", num(overview.available_credits)),
            stat("免费模型", overview.free_models || 0),
            stat("模型总数", overview.models || 0)
          ),
          warnings.length ? el("div", {}, ...warnings.map((item) => el("p", { class: "warn", text: item }))) : el("p", { class: "ok", text: "运行正常" })
        ),
        card(
          "Buddy2api",
          el("pre", {}, JSON.stringify(bridge, null, 2)),
          el("p", { class: "muted", text: "内部调试地址：http://127.0.0.1:8787/（仅本机）" })
        ),
        card(
          "最近目录快照",
          el("pre", {}, JSON.stringify(snapshot || {}, null, 2))
        )
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
            ? el("p", { class: "warn", text: "目录警告：" + data.config_snapshot.warning })
            : null,
          el("p", { class: "muted", text: "免费/低倍率按当前时间重算；夜间窗口使用促销自带的时区。" }),
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
    setView(card("请求账本", table(["请求", "时间", "模型", "账号", "状态", "Tokens", "积分", "成本来源", "详情"], rows)));
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
          make("account_strategy", "text", "账号策略 expiring_first/richest_first/balance")
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
