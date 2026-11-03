import { randomUUID } from 'node:crypto'
import { Hono } from 'hono'
import { html, raw } from 'hono/html'
import { serve } from '@hono/node-server'
import { serveStatic } from '@hono/node-server/serve-static'

const API_ORIGIN = (process.env.API_ORIGIN || 'http://127.0.0.1:8788').replace(/\/$/, '')
const PORT = Number(process.env.UI_PORT || 8790)
const APP_VERSION = process.env.APP_VERSION || '2026.11'

const app = new Hono()

/* ------------------------------------------------------------------ helpers */

class ApiError extends Error {
  constructor(message, status = 502) {
    super(message)
    this.status = status
  }
}

function cookieValue(c, name) {
  const header = c.req.header('cookie') || ''
  for (const part of header.split(';')) {
    const [key, ...rest] = part.trim().split('=')
    if (key === name) return decodeURIComponent(rest.join('='))
  }
  return ''
}

function bearer(c) {
  const value = c.req.header('authorization') || ''
  return value.toLowerCase().startsWith('bearer ') ? value.slice(7).trim() : ''
}

async function api(c, path, options = {}) {
  const method = (options.method || 'GET').toUpperCase()
  const headers = { cookie: c.req.header('cookie') || '' }
  const token = bearer(c)
  if (token) headers.authorization = `Bearer ${token}`
  if (method !== 'GET' && method !== 'HEAD') {
    const csrf = cookieValue(c, 'cw_csrf')
    if (csrf) headers['x-csrf-token'] = csrf
    headers['content-type'] = 'application/json'
  }
  let response
  try {
    response = await fetch(`${API_ORIGIN}${path}`, {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      redirect: 'manual',
    })
  } catch (error) {
    throw new ApiError(`网关 API 不可达：${error.message}`)
  }
  const text = await response.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!response.ok) {
    const detail = data?.detail ?? data?.error?.message ?? data?.error ?? data
    throw new ApiError(
      typeof detail === 'string' ? detail : JSON.stringify(detail || `HTTP ${response.status}`),
      response.status,
    )
  }
  return data
}

async function sessionInfo(c) {
  try {
    const data = await api(c, '/api/auth/session')
    return data?.ok ? data : null
  } catch {
    return null
  }
}

function flash(c) {
  return {
    notice: String(c.req.query('notice') || ''),
    error: String(c.req.query('error') || ''),
  }
}

function redirectWith(c, path, kind, message) {
  const target = new URL(path, c.req.url)
  if (message) target.searchParams.set(kind, message)
  return c.redirect(target.pathname + target.search, 303)
}

async function runAction(c, path, fn) {
  try {
    const message = await fn()
    return redirectWith(c, path, 'notice', message || '操作完成')
  } catch (error) {
    return redirectWith(c, path, 'error', error.message || String(error))
  }
}

function fmtNumber(value, digits = 2) {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  if (!Number.isFinite(number)) return String(value)
  return number.toLocaleString('zh-CN', {
    minimumFractionDigits: digits === 0 ? 0 : 0,
    maximumFractionDigits: digits,
  })
}

function fmtInt(value) {
  return fmtNumber(value, 0)
}

function fmtTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

function pct(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  return `${Math.round(number * 100)}%`
}

function toneForStatus(status) {
  if (status === 'active' || status === 'ok' || status === 'healthy') return 'ok'
  if (status === 'claimed' || status === 'free') return 'info'
  if (status === 'expired' || status === 'inactive' || status === 'failed') return 'bad'
  return 'neutral'
}

function badge(label, tone = 'neutral') {
  return html`<span class="badge badge-${tone}">${label}</span>`
}

function boolBadge(value, yes = '是', no = '否') {
  return badge(value ? yes : no, value ? 'ok' : 'neutral')
}

function pageTitle(title, subtitle = '') {
  return html`
    <div class="page-heading">
      <div>
        <h1>${title}</h1>
        ${subtitle ? html`<p>${subtitle}</p>` : ''}
      </div>
    </div>
  `
}

function empty(message) {
  return html`<div class="empty">${message}</div>`
}

/* ------------------------------------------------------------------- layout */

const NAV = [
  ['/', '总览', 'overview'],
  ['/accounts', '资源账号', 'accounts'],
  ['/models', '模型与路由', 'models'],
  ['/keys', '池 Key', 'keys'],
  ['/requests', '请求账本', 'requests'],
  ['/checkin', '签到与保活', 'checkin'],
  ['/policy', '调度策略', 'policy'],
  ['/settings', '运行设置', 'settings'],
  ['/audit', '审计日志', 'audit'],
]

function layout(c, { title, active, body, actor = '', csrf = '' }) {
  const { notice, error } = flash(c)
  const hxHeaders = JSON.stringify(csrf ? { 'X-CSRF-Token': csrf } : {})
  return html`<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${title} · LLM-API-AGG</title>
  <link rel="stylesheet" href="/static/app.css?v=${APP_VERSION}">
  <script src="/static/htmx.min.js" defer></script>
</head>
<body hx-headers="${hxHeaders}">
  <div class="shell">
    <aside class="sidebar">
      <a class="brand" href="/">
        <span class="brand-mark">LA</span>
        <span><strong>LLM-API-AGG</strong><small>统一模型聚合网关</small></span>
      </a>
      <nav class="nav">
        ${NAV.map(([href, label, key]) => html`
          <a href="${href}" class="${active === key ? 'active' : ''}">
            <span class="nav-dot"></span>${label}
          </a>`)}
      </nav>
      <div class="sidebar-foot">
        <span class="pulse"></span>统一入口在线
        <small>v${APP_VERSION}</small>
      </div>
    </aside>
    <div class="main">
      <header class="topbar">
        <div class="crumbs"><span>控制台</span><span>/</span><strong>${title}</strong></div>
        <div class="topbar-actions">
          <a class="ghost-link" href="/api/health" target="_blank" rel="noreferrer">健康检查</a>
          <span class="actor">${actor || '已登录'}</span>
          <form method="post" action="/logout"><button class="text-button" type="submit">退出</button></form>
        </div>
      </header>
      <main class="content">
        ${notice ? html`<div class="flash flash-ok">${notice}</div>` : ''}
        ${error ? html`<div class="flash flash-bad">${error}</div>` : ''}
        ${body}
      </main>
    </div>
  </div>
</body>
</html>`
}

function loginLayout(c, error = '') {
  return html`<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>登录 · LLM-API-AGG</title>
  <link rel="stylesheet" href="/static/app.css?v=${APP_VERSION}">
</head>
<body class="login-body">
  <main class="login-shell">
    <section class="login-aside">
      <a class="brand" href="/">
        <span class="brand-mark">LA</span>
        <span><strong>LLM-API-AGG</strong><small>统一模型聚合网关</small></span>
      </a>
      <div>
        <p class="eyebrow">CONTROL PLANE</p>
        <h1>一个入口，统一调度模型资源。</h1>
        <p>外部只看到统一模型 API；内部由网关在 Buddy2api 多通道与 OpenBridge 之间自动完成成本、负载和稳定性路由。</p>
      </div>
      <dl class="login-facts">
        <div><dt>入口</dt><dd>/v1</dd></div>
        <div><dt>路由</dt><dd>auto / free / cheap</dd></div>
        <div><dt>策略</dt><dd>免费优先 · 失败切换</dd></div>
      </dl>
    </section>
    <section class="login-card">
      <p class="eyebrow">ADMIN CONSOLE</p>
      <h1>登录控制台</h1>
      <p class="muted">默认账号 <code>admin</code>，密码 <code>admin</code>。生产环境请通过环境变量覆盖。</p>
      ${error ? html`<div class="flash flash-bad">${error}</div>` : ''}
      <form method="post" action="/login" class="stack">
        <label class="field"><span>账号</span><input name="username" autocomplete="username" value="admin" required></label>
        <label class="field"><span>密码</span><input name="password" type="password" autocomplete="current-password" required autofocus></label>
        <button class="button primary" type="submit">进入控制台</button>
      </form>
    </section>
  </main>
</body>
</html>`
}

/* -------------------------------------------------------------------- auth */

app.get('/login', (c) => c.html(loginLayout(c)))

app.post('/login', async (c) => {
  const form = await c.req.parseBody()
  const username = String(form.username || '')
  const password = String(form.password || '')
  let response
  try {
    response = await fetch(`${API_ORIGIN}/api/auth/login`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
  } catch (error) {
    return c.html(loginLayout(c, `网关不可达：${error.message}`), 502)
  }
  if (!response.ok) {
    let message = `登录失败（HTTP ${response.status}）`
    try {
      const body = await response.json()
      message = body.detail || body.error?.message || message
    } catch {
      /* keep status message */
    }
    return c.html(loginLayout(c, message), response.status)
  }
  const result = c.redirect('/', 303)
  const cookies = response.headers.getSetCookie?.() || []
  for (const value of cookies) result.headers.append('set-cookie', value)
  return result
})

app.post('/logout', async (c) => {
  try {
    await api(c, '/api/auth/logout', { method: 'POST', body: {} })
  } catch {
    /* local cookie cleanup still wins */
  }
  const result = c.redirect('/login', 303)
  result.headers.append('set-cookie', 'cw_admin=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict')
  result.headers.append('set-cookie', 'cw_csrf=; Max-Age=0; Path=/; SameSite=Strict')
  return result
})

/* ------------------------------------------------------------------ proxy */

const PROXY_HEADERS = [
  'accept',
  'accept-language',
  'authorization',
  'content-type',
  'cookie',
  'x-api-key',
  'x-csrf-token',
  'x-session-id',
  'anthropic-version',
  'anthropic-beta',
  'user-agent',
]

async function proxy(c) {
  const incoming = new URL(c.req.url)
  const target = `${API_ORIGIN}${incoming.pathname}${incoming.search}`
  const headers = new Headers()
  for (const key of PROXY_HEADERS) {
    const value = c.req.header(key)
    if (value) headers.set(key, value)
  }
  const method = c.req.method
  const body = method === 'GET' || method === 'HEAD' ? undefined : await c.req.arrayBuffer()
  let upstream
  try {
    upstream = await fetch(target, { method, headers, body, redirect: 'manual' })
  } catch (error) {
    return c.json({ detail: `上游网关不可达：${error.message}` }, 502)
  }
  const out = new Headers()
  for (const [key, value] of upstream.headers.entries()) {
    if (['set-cookie', 'content-length', 'content-encoding', 'transfer-encoding', 'connection'].includes(key.toLowerCase())) continue
    out.set(key, value)
  }
  for (const value of upstream.headers.getSetCookie?.() || []) out.append('set-cookie', value)
  return new Response(upstream.body, { status: upstream.status, headers: out })
}

app.all('/api/*', proxy)
app.get('/healthz', proxy)
app.get('/api/health', proxy)

/* ------------------------------------------------------------------- pages */

async function renderPage(c, active, title, body, subtitle = '') {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  return c.html(
    layout(c, {
      title,
      active,
      actor: session.actor || '',
      csrf: cookieValue(c, 'cw_csrf'),
      body: html`${pageTitle(title, subtitle)}${body}`,
    }),
  )
}

async function overviewBody(c) {
  const [overviewData, channelsData, requestsData, scheduleData] = await Promise.all([
    api(c, '/api/overview'),
    api(c, '/api/workbuddy/channels').catch(() => ({ channels: [] })),
    api(c, '/api/requests?limit=8').catch(() => ({ items: [] })),
    api(c, '/api/schedule').catch(() => null),
  ])
  const overview = overviewData.overview || {}
  const bridge = overviewData.bridge || {}
  const openbridge = overviewData.openbridge || {}
  const bridgeOk = bridge.ok === true || String(bridge.status || '').toLowerCase() === 'ok'
  const costs = overview.cost_summary || {}
  const channels = channelsData.channels || []
  const requests = requestsData.items || []
  const warnings = []
  if (!bridgeOk) warnings.push(`Buddy2api 不可达：${bridge.error || 'unknown'}`)
  if (!overview.accounts) warnings.push('还没有资源账号，请到「资源账号」登录或导入渠道凭据。')
  if (openbridge.configured && !openbridge.ok) warnings.push(`OpenBridge 不可达：${openbridge.error || 'unknown'}`)
  if (!openbridge.configured) warnings.push('OpenBridge 未配置；设置 OPENBRIDGE_URL 后其模型会加入统一选路。')
  if (overview.cooldowns?.length) warnings.push(`${overview.cooldowns.length} 个账号处于冷却或额度耗尽状态。`)
  return html`
    <section class="stat-grid">
      <article class="stat-card"><span>资源账号</span><strong>${fmtInt(overview.accounts)}</strong><small>已登录渠道账号</small></article>
      <article class="stat-card"><span>可路由模型</span><strong>${fmtInt(overview.models)}</strong><small>${fmtInt(overview.free_models)} 个免费/额度模型</small></article>
      <article class="stat-card"><span>可用积分</span><strong>${fmtNumber(overview.available_credits)}</strong><small>Buddy2api 积分快照</small></article>
      <article class="stat-card"><span>活跃会话</span><strong>${fmtInt(overview.sessions)}</strong><small>调度器会话绑定</small></article>
      <article class="stat-card"><span>当前时段</span><strong>${scheduleData?.period === 'night' ? '夜间' : '白天'}</strong><small>${scheduleData?.night_window?.start || '23:00'}–${scheduleData?.night_window?.end || '08:00'} 夜间窗</small></article>
    </section>

    ${warnings.length ? html`<section class="notice-list">${warnings.map((item) => html`<div class="notice-row"><span class="dot warn"></span>${item}</div>`)}</section>` : html`<div class="flash flash-ok">所有通道运行正常</div>`}

    <section class="grid-2">
      <article class="panel">
        <div class="panel-head"><div><h2>通道状态</h2><p>统一网关内部资源面</p></div><span class="badge ${bridgeOk ? 'badge-ok' : 'badge-bad'}">${bridgeOk ? '正常' : '异常'}</span></div>
        <div class="channel-list">
          ${channels.map((item) => html`
            <div class="channel-row">
              <div><strong>${item.id}</strong><small>${item.display_name || ''}</small></div>
              <div class="channel-meta">${boolBadge(item.loaded, '已加载', '未加载')}${item.enabled ? badge('启用', 'ok') : badge('停用', 'neutral')}</div>
            </div>`)}
          <div class="channel-row">
            <div><strong>openbridge</strong><small>浏览器会话渠道（可选）</small></div>
            <div class="channel-meta">${openbridge.configured ? badge(openbridge.ok ? '已连接' : '不可达', openbridge.ok ? 'ok' : 'bad') : badge('未配置', 'neutral')}<span class="muted">${fmtInt(openbridge.models)} 个模型</span></div>
          </div>
        </div>
      </article>
      <article class="panel">
        <div class="panel-head"><div><h2>成本概览</h2><p>按当前目录快照统计</p></div></div>
        <div class="metric-list">
          <div><span>模型总数</span><strong>${fmtInt(costs.total)}</strong></div>
          <div><span>免费 / 额度覆盖</span><strong>${fmtInt(costs.free)}</strong></div>
          <div><span>已知成本</span><strong>${fmtInt(costs.known_cost)}</strong></div>
          <div><span>未知成本</span><strong>${fmtInt(costs.unknown_cost)}</strong></div>
          <div><span>最近目录刷新</span><strong>${fmtTime(overview.updated_at)}</strong></div>
        </div>
        <div class="panel-actions">
          <form method="post" action="/actions/catalog/refresh"><button class="button small" type="submit">刷新模型目录</button></form>
          <form method="post" action="/actions/accounts/sync"><button class="button small ghost" type="submit">同步账号</button></form>
          <form method="post" action="/actions/credits/refresh"><button class="button small ghost" type="submit">刷新积分</button></form>
        </div>
      </article>
    </section>

    <section class="panel">
      <div class="panel-head"><div><h2>最近请求</h2><p>统一 /v1 的实时调用账本</p></div><a class="link" href="/requests">查看全部</a></div>
      ${requests.length ? requestsTable(requests) : empty('还没有请求记录。')}
    </section>`
}

function requestsTable(items) {
  return html`<div class="table-wrap"><table>
    <thead><tr><th>时间</th><th>请求模型</th><th>实际路由</th><th>渠道</th><th>状态</th><th>Tokens</th><th>积分</th><th></th></tr></thead>
    <tbody>${items.map((item) => html`<tr>
      <td class="muted nowrap">${fmtTime(item.created_at)}</td>
      <td><code>${item.model_requested || '—'}</code></td>
      <td>${item.model_routed ? html`<code>${item.model_routed}</code>` : html`<span class="muted">—</span>`}</td>
      <td>${item.provider || '—'}</td>
      <td>${badge(String(item.status_code || '—'), Number(item.status_code) === 200 ? 'ok' : Number(item.status_code) >= 400 ? 'bad' : 'neutral')}</td>
      <td class="num">${fmtInt(item.total_tokens)}</td>
      <td class="num">${fmtNumber(item.credits, 3)}</td>
      <td><a class="link" href="/requests/${encodeURIComponent(item.request_id)}">详情</a></td>
    </tr>`)}</tbody>
  </table></div>`
}

app.get('/', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  let body
  try {
    body = await overviewBody(c)
  } catch (error) {
    body = html`<div class="flash flash-bad">${error.message}</div>`
  }
  return c.html(layout(c, { title: '总览', active: 'overview', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('总览', '统一模型聚合网关的运行状态与资源路由')}<div id="overview-live" hx-get="/partials/overview" hx-trigger="every 30s" hx-swap="innerHTML">${body}</div>` }))
})

app.get('/partials/overview', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.body('', 401)
  try {
    return c.html(await overviewBody(c))
  } catch (error) {
    return c.html(html`<div class="flash flash-bad">${error.message}</div>`)
  }
})

/* ---------------------------------------------------------------- accounts */

async function accountsData(c) {
  const [accounts, checkin, channels] = await Promise.all([
    api(c, '/api/workbuddy/accounts'),
    api(c, '/api/workbuddy/checkin').catch(() => ({ accounts: [] })),
    api(c, '/api/workbuddy/channels').catch(() => ({ channels: [] })),
  ])
  const statusById = new Map()
  for (const item of checkin.accounts || []) statusById.set(Number(item.account_id), item)
  return { items: accounts.items || [], statusById, channels: channels.channels || [] }
}

function accountsTable(c, data) {
  if (!data.items.length) return empty('还没有资源账号。先登录 WorkBuddy，或在右侧导入 Qoder/千问办公/TraeWork 凭据。')
  return html`<div class="table-wrap"><table>
    <thead><tr><th>账号</th><th>渠道</th><th>状态</th><th>可用积分</th><th>30 天到期</th><th>今日用量</th><th>签到</th><th></th></tr></thead>
    <tbody>${data.items.map((account) => {
      const state = account.state || {}
      const credit = account.latest_credit || {}
      const ci = data.statusById.get(Number(account.account_id))?.checkin || {}
      const cooling = state.cooldown_until || state.free_exhausted_until
      return html`<tr>
        <td><div class="cell-main"><strong>${account.nickname || account.name || `#${account.account_id}`}</strong><small>ID ${account.account_id}</small></div></td>
        <td><code>${account.provider || '—'}</code></td>
        <td>${badge(account.status || 'unknown', toneForStatus(account.status))}${cooling ? html`<div class="cell-sub warn-text">冷却至 ${fmtTime(cooling)}</div>` : ''}</td>
        <td class="num">${fmtNumber(account.available_credits)}</td>
        <td class="num">${fmtNumber(credit.expiring_30d_total)}</td>
        <td class="cell-sub">free ${fmtInt(state.free_requests_today)} · paid ${fmtInt(state.paid_requests_today)}</td>
        <td>${ci.checked_today ? badge('已签到', 'ok') : badge(ci.ok === false ? '检查失败' : '未签到', ci.ok === false ? 'bad' : 'neutral')}</td>
        <td><a class="link" href="/accounts/${account.account_id}">管理</a></td>
      </tr>`
    })}</tbody>
  </table></div>`
}

function channelImportPanel() {
  return html`<article class="panel side-panel">
    <div class="panel-head"><div><h2>导入渠道账号</h2><p>Qoder / 千问办公 / TraeWork</p></div></div>
    <form method="post" action="/actions/accounts/import" class="stack">
      <label class="field"><span>渠道</span><select name="provider">
        <option value="qoder">Qoder 中国</option><option value="qoder-i">Qoder 国际</option>
        <option value="qwenwork">千问办公</option><option value="traework">TraeWork</option>
      </select></label>
      <label class="field"><span>凭据 JSON</span><textarea name="credentials" rows="6" placeholder='{"access_token":"PAT 或 access_token"}' required></textarea></label>
      <p class="hint">Qoder 可只填 access_token/PAT；千问办公和 TraeWork 填 token 或 refreshToken。凭据只写入 Buddy2api 加密库。</p>
      <button class="button primary" type="submit">导入账号</button>
    </form>
    <div class="panel-divider"></div>
    <p class="hint">OpenBridge 是独立 sidecar，不在这里导入；配置 OPENBRIDGE_URL 后自动加入统一路由。</p>
  </article>`
}

app.get('/accounts', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  let data
  try { data = await accountsData(c) } catch (error) { data = { items: [], channels: [], statusById: new Map(), error: error.message } }
  const body = html`
    <div class="toolbar">
      <div class="toolbar-group">
        <a class="button primary" href="/accounts/login?realm=cn">登录 WorkBuddy</a>
        <a class="button ghost" href="/accounts/login?realm=global">登录国际版</a>
      </div>
      <div class="toolbar-group">
        <form method="post" action="/actions/accounts/sync"><button class="button small" type="submit">同步账号</button></form>
        <form method="post" action="/actions/credits/refresh"><button class="button small" type="submit">刷新积分</button></form>
        <form method="post" action="/actions/checkin/run"><button class="button small" type="submit">全部签到</button></form>
      </div>
    </div>
    ${data.error ? html`<div class="flash flash-bad">${data.error}</div>` : ''}
    <section class="grid-main-side">
      <article class="panel"><div class="panel-head"><div><h2>资源账号</h2><p>统一网关可调度的账号与额度</p></div></div>${accountsTable(c, data)}</article>
      ${channelImportPanel()}
    </section>`
  return c.html(layout(c, { title: '资源账号', active: 'accounts', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('资源账号', 'WorkBuddy、Qoder、千问办公、TraeWork 的账号与凭据')}${body}` }))
})

app.get('/accounts/login', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  const realm = c.req.query('realm') === 'global' ? 'global' : 'cn'
  let started
  try {
    started = await api(c, '/api/workbuddy/login/start', { method: 'POST', body: { realm } })
  } catch (error) {
    return c.html(layout(c, { title: '登录 WorkBuddy', active: 'accounts', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('登录 WorkBuddy')}<div class="flash flash-bad">${error.message}</div><a class="button" href="/accounts">返回账号列表</a>` }))
  }
  const body = html`
    <article class="panel narrow-panel">
      <div class="panel-head"><div><h2>${realm === 'global' ? '国际版授权' : '国内版授权'}</h2><p>在浏览器完成登录后本页会自动刷新</p></div></div>
      <p class="hint">${started.note || (realm === 'global' ? '国际版建议复制链接到无痕窗口，避免复用已有登录态。' : '打开下面的授权地址，完成扫码或账号登录。')}</p>
      <div class="code-block"><code>${started.url || '（未返回授权地址）'}</code><button class="button small ghost" type="button" onclick="navigator.clipboard.writeText(${JSON.stringify(started.url || '')})">复制链接</button></div>
      ${started.url ? html`<p><a class="button primary" href="${started.url}" target="_blank" rel="noreferrer">打开授权页面</a></p>` : ''}
      <div hx-get="/partials/workbuddy-login?state=${encodeURIComponent(started.state || '')}" hx-trigger="every 3s" hx-swap="outerHTML" class="poll-box">等待授权…</div>
      <p><a class="link" href="/accounts">返回账号列表</a></p>
    </article>`
  return c.html(layout(c, { title: '登录 WorkBuddy', active: 'accounts', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('登录 WorkBuddy', '浏览器授权完成后自动同步账号')}${body}` }))
})

app.get('/partials/workbuddy-login', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.body('', 401)
  const state = c.req.query('state') || ''
  try {
    const result = await api(c, `/api/workbuddy/login/poll?state=${encodeURIComponent(state)}`)
    if (result.done) {
      c.header('HX-Redirect', `/accounts?notice=${encodeURIComponent(`账号已同步：${result.nickname || result.id || ''}`)}`)
      return c.body('')
    }
    return c.html(html`<div hx-get="/partials/workbuddy-login?state=${encodeURIComponent(state)}" hx-trigger="every 3s" hx-swap="outerHTML" class="poll-box">等待授权…</div>`)
  } catch (error) {
    return c.html(html`<div class="flash flash-bad">${error.message}</div>`)
  }
})

app.get('/accounts/:id', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  const id = Number(c.req.param('id'))
  let data
  try {
    data = await accountsData(c)
  } catch (error) {
    data = { items: [], statusById: new Map(), channels: [] }
  }
  const account = data.items.find((item) => Number(item.account_id) === id)
  if (!account) return c.notFound()
  const state = account.state || {}
  const ci = data.statusById.get(id) || {}
  const body = html`
    <div class="toolbar"><a class="link" href="/accounts">← 返回账号列表</a></div>
    <section class="grid-2">
      <article class="panel">
        <div class="panel-head"><div><h2>${account.nickname || account.name || `#${id}`}</h2><p>ID ${id} · ${account.provider}</p></div>${badge(account.status, toneForStatus(account.status))}</div>
        <div class="metric-list">
          <div><span>可用积分</span><strong>${fmtNumber(account.available_credits)}</strong></div>
          <div><span>累计积分</span><strong>${fmtNumber(account.total_credits)}</strong></div>
          <div><span>今日请求</span><strong>${fmtInt(state.daily_requests)}</strong></div>
          <div><span>今日 Tokens</span><strong>${fmtInt(state.daily_tokens)}</strong></div>
          <div><span>今日积分</span><strong>${fmtNumber(state.daily_credits)}</strong></div>
          <div><span>签到状态</span><strong>${ci.checkin?.checked_today ? '已签到' : '未签到'}</strong></div>
        </div>
        <div class="panel-actions">
          <form method="post" action="/actions/accounts/${id}/refresh"><button class="button small" type="submit">刷新</button></form>
          <form method="post" action="/actions/accounts/${id}/checkin"><button class="button small" type="submit">签到</button></form>
          <form method="post" action="/actions/accounts/${id}/toggle"><input type="hidden" name="status" value="${account.status === 'active' ? 'inactive' : 'active'}"><button class="button small ghost" type="submit">${account.status === 'active' ? '停用' : '启用'}</button></form>
          <form method="post" action="/actions/accounts/${id}/delete" onsubmit="return confirm('删除账号 #${id}？桥内登录也会被删除。')"><button class="button small danger" type="submit">删除</button></form>
        </div>
      </article>
      <article class="panel">
        <div class="panel-head"><div><h2>账号限额</h2><p>按模型或账号设置本地上限</p></div></div>
        <form method="post" action="/actions/accounts/${id}/caps" class="stack">
          <label class="field"><span>模型 ID</span><input name="model_id" value="*" placeholder="* = 账号级，或 glm-5.2"></label>
          <div class="form-grid">
            <label class="field"><span>每日请求上限</span><input name="daily_request_cap" type="number" min="0" value="0"></label>
            <label class="field"><span>每日积分上限</span><input name="daily_credit_cap" type="number" min="0" step="0.01" value="0"></label>
          </div>
          <button class="button primary" type="submit">保存限额</button>
        </form>
      </article>
    </section>`
  return c.html(layout(c, { title: '账号详情', active: 'accounts', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('账号详情', account.provider)}${body}` }))
})

/* ------------------------------------------------------------------ models */

app.get('/models', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  let models
  let schedule
  try {
    ;[models, schedule] = await Promise.all([api(c, '/api/workbuddy/models'), api(c, '/api/schedule')])
  } catch (error) {
    models = { items: [] }
    schedule = null
    models.error = error.message
  }
  const rows = models.items || []
  const body = html`
    ${models.error ? html`<div class="flash flash-bad">${models.error}</div>` : ''}
    <div class="toolbar">
      <div class="toolbar-group"><span class="muted">${fmtInt(rows.length)} 个模型 · ${schedule?.period === 'night' ? '当前夜间' : '当前白天'}</span></div>
      <form method="post" action="/actions/catalog/refresh"><button class="button small" type="submit">刷新目录</button></form>
    </div>
    <article class="panel">
      <div class="panel-head"><div><h2>模型目录</h2><p>所有已启用渠道的可路由模型；成本来自最近快照与促销规则</p></div></div>
      <div class="table-wrap"><table>
        <thead><tr><th>模型</th><th>渠道</th><th>基础价</th><th>当前价</th><th>状态</th><th>上下文</th><th>促销</th><th>路由桶</th></tr></thead>
        <tbody>${rows.map((item) => {
          const bucket = schedule?.models?.find((x) => x.provider === item.provider && x.model_id === item.model_id)
          return html`<tr>
            <td><div class="cell-main"><strong>${item.model_id}</strong><small>${item.name || ''}</small></div></td>
            <td><code>${item.provider}</code></td>
            <td class="num">${fmtNumber(item.base_credits, 3)}</td>
            <td class="num">${fmtNumber(item.credits, 3)}</td>
            <td>${item.free ? badge('免费 / 额度', 'ok') : badge('计费', 'neutral')}${bucket && bucket.eligible === false ? html` ${badge('被策略排除', 'bad')}` : ''}</td>
            <td class="num">${item.context_length ? fmtInt(item.context_length) : '—'}</td>
            <td class="cell-sub">${item.promotion_active ? `${item.promotion_label || '促销'} · x${fmtNumber(item.promotion_factor, 2)}` : item.promotion_label || '—'}</td>
            <td><code>${bucket?.bucket || '—'}</code></td>
          </tr>`
        })}</tbody>
      </table></div>
    </article>
    <section class="panel">
      <div class="panel-head"><div><h2>手动成本覆盖</h2><p>仅用于上游目录缺少成本、或需要人工设定倍率的模型</p></div></div>
      <form method="post" action="/actions/models/manual" class="form-grid wide">
        <label class="field"><span>渠道</span><input name="provider" placeholder="workbuddy / qoder / openbridge" required></label>
        <label class="field"><span>模型 ID</span><input name="model_id" required></label>
        <label class="field"><span>基础积分</span><input name="base_credits" type="number" step="0.001" min="0"></label>
        <label class="field"><span>折扣系数</span><input name="factor" type="number" step="0.01" min="0" max="1" placeholder="0.5"></label>
        <label class="field"><span>时区</span><input name="timezone" value="Asia/Shanghai"></label>
        <label class="field checkbox-field"><span>启用</span><input type="checkbox" name="enabled" value="1" checked></label>
        <button class="button primary" type="submit">保存覆盖</button>
      </form>
    </section>`
  return c.html(layout(c, { title: '模型与路由', active: 'models', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('模型与路由', '统一模型目录、成本与调度桶')}${body}` }))
})

/* -------------------------------------------------------------------- keys */

const revealKeys = new Map()

app.get('/keys', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  let data
  try { data = await api(c, '/api/keys') } catch (error) { data = { items: [], error: error.message } }
  const reveal = c.req.query('reveal')
  let revealValue = ''
  if (reveal && revealKeys.has(reveal)) {
    revealValue = revealKeys.get(reveal)
    revealKeys.delete(reveal)
  }
  const items = data.items || []
  const body = html`
    ${data.error ? html`<div class="flash flash-bad">${data.error}</div>` : ''}
    ${revealValue ? html`<div class="secret-banner"><div><strong>新 Key 只显示一次</strong><code>${revealValue}</code></div><button class="button small" type="button" onclick="navigator.clipboard.writeText(${JSON.stringify(revealValue)})">复制</button></div>` : ''}
    <article class="panel">
      <div class="panel-head"><div><h2>池 Key</h2><p>外部客户端使用这些 Key 访问统一 /v1；每个 Key 可限制模型与日额度</p></div></div>
      ${items.length ? html`<div class="table-wrap"><table>
        <thead><tr><th>名称</th><th>前缀</th><th>类型</th><th>状态</th><th>请求/日</th><th>积分/日</th><th>尝试</th><th></th></tr></thead>
        <tbody>${items.map((item) => html`<tr>
          <td><div class="cell-main"><strong>${item.name}</strong><small>创建于 ${fmtTime(item.created_at)}</small></div></td>
          <td><code>${item.key_prefix}…</code></td>
          <td>${badge(item.kind, 'neutral')}</td>
          <td>${badge(item.status, toneForStatus(item.status))}</td>
          <td class="num">${item.daily_request_limit ? fmtInt(item.daily_request_limit) : '不限'}</td>
          <td class="num">${item.daily_credit_limit ? fmtNumber(item.daily_credit_limit) : '不限'}</td>
          <td class="num">${fmtInt(item.max_attempts)}</td>
          <td><div class="row-actions">
            <form method="post" action="/actions/keys/${item.id}/rotate"><button class="link-button" type="submit">轮换</button></form>
            <form method="post" action="/actions/keys/${item.id}/toggle"><input type="hidden" name="status" value="${item.status === 'active' ? 'inactive' : 'active'}"><button class="link-button" type="submit">${item.status === 'active' ? '停用' : '启用'}</button></form>
            <form method="post" action="/actions/keys/${item.id}/delete" onsubmit="return confirm('删除 Key ${item.name}？')"><button class="link-button danger" type="submit">删除</button></form>
          </div></td>
        </tr>`)}</tbody>
      </table></div>` : empty('还没有池 Key。创建一个给 Agent 或共享客户端使用。')}
    </article>
    <section class="panel">
      <div class="panel-head"><div><h2>新建 Key</h2><p>创建后明文只显示一次</p></div></div>
      <form method="post" action="/actions/keys/create" class="form-grid wide">
        <label class="field"><span>名称</span><input name="name" placeholder="例如：xdr-agent" required></label>
        <label class="field"><span>类型</span><select name="kind"><option value="agent">agent</option><option value="shared">shared</option></select></label>
        <label class="field"><span>每日请求上限</span><input name="daily_request_limit" type="number" min="0" value="0"></label>
        <label class="field"><span>每日积分上限</span><input name="daily_credit_limit" type="number" min="0" step="0.01" value="0"></label>
        <label class="field"><span>最大尝试次数</span><input name="max_attempts" type="number" min="1" max="5" value="3"></label>
        <label class="field"><span>允许模型（逗号分隔，空=全部）</span><input name="allowed_models" placeholder="auto, workbuddy/hy3"></label>
        <button class="button primary" type="submit">创建 Key</button>
      </form>
    </section>`
  return c.html(layout(c, { title: '池 Key', active: 'keys', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('池 Key', '统一 /v1 的客户端凭据与额度')}${body}` }))
})

/* ---------------------------------------------------------------- requests */

app.get('/requests', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  const limit = 50
  const offset = Math.max(0, Number(c.req.query('offset') || 0) || 0)
  const status = c.req.query('status') || ''
  const model = c.req.query('model') || ''
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (status) qs.set('status_code', status)
  if (model) qs.set('model', model)
  let data
  try { data = await api(c, `/api/requests?${qs}`) } catch (error) { data = { items: [], error: error.message } }
  const items = data.items || []
  const body = html`
    ${data.error ? html`<div class="flash flash-bad">${data.error}</div>` : ''}
    <form method="get" action="/requests" class="filter-bar">
      <label class="field"><span>状态码</span><input name="status" value="${status}" placeholder="200"></label>
      <label class="field"><span>模型</span><input name="model" value="${model}" placeholder="auto / hy3"></label>
      <button class="button small" type="submit">筛选</button>
      <a class="link" href="/requests">清除</a>
    </form>
    <article class="panel"><div class="panel-head"><div><h2>请求账本</h2><p>最近 ${fmtInt(items.length)} 条 · 北京时间</p></div></div>
      ${items.length ? requestsTable(items) : empty('没有匹配的请求。')}
      <div class="pager">
        ${offset > 0 ? html`<a class="button small ghost" href="/requests?offset=${Math.max(0, offset - limit)}${status ? `&status=${encodeURIComponent(status)}` : ''}${model ? `&model=${encodeURIComponent(model)}` : ''}">上一页</a>` : ''}
        ${items.length === limit ? html`<a class="button small ghost" href="/requests?offset=${offset + limit}${status ? `&status=${encodeURIComponent(status)}` : ''}${model ? `&model=${encodeURIComponent(model)}` : ''}">下一页</a>` : ''}
      </div>
    </article>`
  return c.html(layout(c, { title: '请求账本', active: 'requests', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('请求账本', '统一入口的实际路由、用量与成本')}${body}` }))
})

app.get('/requests/:id', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  let data
  try { data = await api(c, `/api/requests/${encodeURIComponent(c.req.param('id'))}`) } catch (error) {
    return c.html(layout(c, { title: '请求详情', active: 'requests', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('请求详情')}<div class="flash flash-bad">${error.message}</div><a class="link" href="/requests">返回</a>` }))
  }
  const item = data.item || {}
  const attempts = data.attempts || []
  const body = html`
    <p><a class="link" href="/requests">← 返回请求账本</a></p>
    <section class="grid-2">
      <article class="panel"><div class="panel-head"><div><h2>请求</h2><p><code>${item.request_id}</code></p></div>${badge(String(item.status_code), Number(item.status_code) === 200 ? 'ok' : 'bad')}</div>
        <div class="metric-list">
          <div><span>请求模型</span><strong>${item.model_requested || '—'}</strong></div>
          <div><span>实际路由</span><strong>${item.model_routed || '—'}</strong></div>
          <div><span>渠道 / 账号</span><strong>${item.provider || '—'} / ${item.bridge_account_id ?? '—'}</strong></div>
          <div><span>Tokens</span><strong>${fmtInt(item.total_tokens)}</strong></div>
          <div><span>积分</span><strong>${fmtNumber(item.credits, 4)}</strong></div>
          <div><span>耗时</span><strong>${fmtInt(item.duration_ms)} ms</strong></div>
          <div><span>创建时间</span><strong>${fmtTime(item.created_at)}</strong></div>
        </div>
      </article>
      <article class="panel"><div class="panel-head"><div><h2>调度意图</h2><p>路由时的能力与上下文</p></div></div><pre class="json">${JSON.stringify(item.intent || {}, null, 2)}</pre></article>
    </section>
    <section class="panel"><div class="panel-head"><div><h2>尝试记录</h2><p>按尝试顺序</p></div></div>
      ${attempts.length ? html`<div class="table-wrap"><table><thead><tr><th>#</th><th>账号</th><th>模型</th><th>渠道</th><th>状态</th><th>积分</th><th>耗时</th></tr></thead><tbody>${attempts.map((a) => html`<tr><td>${a.attempt_no}</td><td>${a.account_id ?? '—'}</td><td><code>${a.model_id || '—'}</code></td><td>${a.provider || '—'}</td><td>${badge(String(a.status_code ?? '—'), Number(a.status_code) === 200 ? 'ok' : 'bad')}</td><td class="num">${fmtNumber(a.credits, 4)}</td><td class="num">${fmtInt(a.duration_ms)} ms</td></tr>`)}</tbody></table></div>` : empty('没有尝试记录。')}
    </section>`
  return c.html(layout(c, { title: '请求详情', active: 'requests', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('请求详情', '单次调用的完整路由事实')}${body}` }))
})

/* ---------------------------------------------------------------- check-in */

app.get('/checkin', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  let data
  let keepalive
  try {
    ;[data, keepalive] = await Promise.all([
      api(c, '/api/workbuddy/checkin'),
      api(c, '/api/workbuddy/keepalive/settings').catch(() => ({})),
    ])
  } catch (error) {
    data = { accounts: [], global: {}, error: error.message }
  }
  const global = data.global || {}
  const items = data.accounts || []
  const body = html`
    ${data.error ? html`<div class="flash flash-bad">${data.error}</div>` : ''}
    <section class="grid-2">
      <article class="panel"><div class="panel-head"><div><h2>自动签到</h2><p>北京时间为准；默认关闭</p></div>${boolBadge(global.enabled, '已开启', '已关闭')}</div>
        <form method="post" action="/actions/checkin/settings" class="stack">
          <label class="checkbox-field"><input type="checkbox" name="enabled" value="1" ${global.enabled ? raw('checked') : ''}><span>启用自动签到</span></label>
          <div class="form-grid"><label class="field"><span>小时</span><input name="hour" type="number" min="0" max="23" value="${global.hour ?? 9}"></label><label class="field"><span>分钟</span><input name="minute" type="number" min="0" max="59" value="${global.minute ?? 0}"></label></div>
          <label class="checkbox-field"><input type="checkbox" name="confirm_risk" value="1"><span>我确认自动签到可能触发上游风控</span></label>
          <button class="button primary" type="submit">保存签到设置</button>
        </form>
        <div class="panel-divider"></div>
        <form method="post" action="/actions/checkin/run"><button class="button" type="submit">立即全部签到</button></form>
      </article>
      <article class="panel"><div class="panel-head"><div><h2>保活设置</h2><p>由 Buddy2api 执行</p></div></div>
        <form method="post" action="/actions/keepalive/settings" class="stack">
          <label class="checkbox-field"><input type="checkbox" name="enabled" value="1" ${keepalive.enabled ? raw('checked') : ''}><span>启用保活</span></label>
          <label class="field"><span>执行小时</span><input name="hour" type="number" min="0" max="23" value="${keepalive.hour ?? 22}"></label>
          <button class="button primary" type="submit">保存保活设置</button>
        </form>
      </article>
    </section>
    <article class="panel"><div class="panel-head"><div><h2>账号签到状态</h2><p>单独规则优先于全局规则</p></div></div>
      ${items.length ? html`<div class="table-wrap"><table><thead><tr><th>账号</th><th>渠道</th><th>今日状态</th><th>规则</th><th>规则时间</th><th></th></tr></thead><tbody>${items.map((item) => html`<tr>
        <td><div class="cell-main"><strong>${item.name}</strong><small>ID ${item.account_id}</small></div></td><td><code>${item.provider}</code></td>
        <td>${item.checkin?.checked_today ? badge('已签到', 'ok') : badge(item.checkin?.ok === false ? '失败' : '未签到', item.checkin?.ok === false ? 'bad' : 'neutral')}${item.checkin?.state?.last_message ? html`<div class="cell-sub">${item.checkin.state.last_message}</div>` : ''}</td>
        <td>${item.rule?.source === 'account' ? badge('单独规则', 'info') : badge('全局规则', 'neutral')} ${item.rule?.enabled ? '启用' : '停用'}</td>
        <td class="num">${String(item.rule?.hour ?? 0).padStart(2, '0')}:${String(item.rule?.minute ?? 0).padStart(2, '0')}</td>
        <td><div class="row-actions">
          <form method="post" action="/actions/accounts/${item.account_id}/checkin"><button class="link-button" type="submit">立即签到</button></form>
          <form method="post" action="/actions/checkin/accounts/${item.account_id}/rule" class="inline-rule">
            <label class="tiny-check"><input type="checkbox" name="enabled" value="1" ${item.rule?.enabled ? raw('checked') : ''}>单独</label>
            <input name="hour" type="number" min="0" max="23" value="${item.rule?.hour ?? 9}" aria-label="小时">
            <span>:</span>
            <input name="minute" type="number" min="0" max="59" value="${item.rule?.minute ?? 0}" aria-label="分钟">
            <button class="link-button" type="submit">保存规则</button>
          </form>
          <form method="post" action="/actions/checkin/accounts/${item.account_id}/clear"><button class="link-button" type="submit">清除</button></form>
        </div></td>
      </tr>`)}</tbody></table></div>` : empty('没有可签到账号。')}
    </article>`
  return c.html(layout(c, { title: '签到与保活', active: 'checkin', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('签到与保活', '账号额度维护与自动化开关')}${body}` }))
})

/* ------------------------------------------------------------------ policy */

app.get('/policy', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  let policy
  let schedule
  try {
    ;[policy, schedule] = await Promise.all([api(c, '/api/policy'), api(c, '/api/schedule')])
  } catch (error) {
    policy = {}
    schedule = null
  }
  const body = html`
    <section class="grid-2">
      <article class="panel"><div class="panel-head"><div><h2>路由策略</h2><p>统一网关在所有渠道之间应用</p></div></div>
        <form method="post" action="/actions/policy" class="stack">
          <div class="form-grid">
            <label class="field"><span>路由模式</span><select name="routing_mode">${['balanced','stable','cost'].map((v) => html`<option value="${v}" ${policy.routing_mode === v ? raw('selected') : ''}>${v}</option>`)}</select></label>
            <label class="field"><span>账号均摊</span><select name="account_sharing">${['balanced','cost_first'].map((v) => html`<option value="${v}" ${policy.account_sharing === v ? raw('selected') : ''}>${v}</option>`)}</select></label>
            <label class="field"><span>账号策略</span><select name="account_strategy">${['expiring_first','richest_first','balance'].map((v) => html`<option value="${v}" ${policy.account_strategy === v ? raw('selected') : ''}>${v}</option>`)}</select></label>
            <label class="field"><span>未知成本</span><select name="unknown_cost">${['after_known','allow','never'].map((v) => html`<option value="${v}" ${policy.unknown_cost === v ? raw('selected') : ''}>${v}</option>`)}</select></label>
          </div>
          <div class="form-grid">
            <label class="field"><span>最大倍率</span><input name="max_multiplier" type="number" step="0.01" min="0" value="${policy.max_multiplier ?? 0}"></label>
            <label class="field"><span>最低积分保留</span><input name="min_credit_reserve" type="number" step="0.01" min="0" value="${policy.min_credit_reserve ?? 0}"></label>
            <label class="field"><span>最大尝试次数</span><input name="max_attempts" type="number" min="1" max="5" value="${policy.max_attempts ?? 4}"></label>
            <label class="field"><span>最大模型候选</span><input name="max_model_candidates" type="number" min="1" max="10" value="${policy.max_model_candidates ?? 4}"></label>
            <label class="field"><span>会话 TTL（秒）</span><input name="session_ttl_seconds" type="number" min="60" value="${policy.session_ttl_seconds ?? 1800}"></label>
            <label class="field"><span>免费 429 阈值</span><input name="free_429_threshold" type="number" min="1" value="${policy.free_429_threshold ?? 3}"></label>
          </div>
          <div class="check-grid">
            ${[['prefer_free','免费优先'],['allow_night','允许夜间模型'],['deny_full_price','拒绝全价模型'],['allow_over_max','允许超倍率'],['night_auto','自动夜间窗口']].map(([key,label]) => html`<label class="checkbox-field"><input type="checkbox" name="${key}" value="1" ${policy[key] ? raw('checked') : ''}><span>${label}</span></label>`)}
          </div>
          <div class="form-grid">
            ${['free','promo','normal'].map((bucket) => html`<label class="field"><span>白天优先级 · ${bucket}</span><input name="day_${bucket}" type="number" min="0" max="999" value="${policy.day_priority?.[bucket] ?? 0}"></label>`)}
            ${['night_free','night_promo','free','promo','normal'].map((bucket) => html`<label class="field"><span>夜间优先级 · ${bucket}</span><input name="night_priority_${bucket}" type="number" min="0" max="999" value="${policy.night_priority?.[bucket] ?? 0}"></label>`)}
          </div>
          <div class="form-grid">
            <label class="field"><span>夜间开始</span><input name="night_start" value="${policy.night_start || '23:00'}"></label>
            <label class="field"><span>夜间结束</span><input name="night_end" value="${policy.night_end || '08:00'}"></label>
            <label class="field"><span>稳定性权重</span><input name="stability_weight" type="number" step="0.05" min="0" max="1" value="${policy.stability_weight ?? 0.5}"></label>
            <label class="field"><span>负载权重</span><input name="load_weight" type="number" step="0.05" min="0" max="1" value="${policy.load_weight ?? 0.2}"></label>
            <label class="field"><span>成本权重</span><input name="cost_weight" type="number" step="0.05" min="0" max="1" value="${policy.cost_weight ?? 0.2}"></label>
            <label class="field"><span>会话抖动</span><input name="session_jitter" type="number" step="0.01" min="0" max="1" value="${policy.session_jitter ?? 0.1}"></label>
          </div>
          <div class="form-grid">
            <label class="field"><span>允许模型（逗号分隔，空=全部）</span><input name="allow_models" value="${(policy.allow_models || []).join(',')}"></label>
            <label class="field"><span>拒绝模型（逗号分隔）</span><input name="deny_models" value="${(policy.deny_models || []).join(',')}"></label>
          </div>
          <details class="advanced"><summary>高级：模型优先级 JSON</summary><textarea name="model_priority" rows="4">${JSON.stringify(policy.model_priority || {}, null, 2)}</textarea></details>
          <button class="button primary" type="submit">保存策略</button>
        </form>
      </article>
      <article class="panel"><div class="panel-head"><div><h2>当前时段</h2><p>${schedule?.timezone || 'Asia/Shanghai'}</p></div>${schedule ? badge(schedule.period === 'night' ? '夜间' : '白天', schedule.period === 'night' ? 'info' : 'neutral') : ''}</div>
        ${schedule ? html`<div class="metric-list"><div><span>夜间窗口</span><strong>${schedule.night_window?.start}–${schedule.night_window?.end}</strong></div><div><span>窗口来源</span><strong>${schedule.night_window?.source}</strong></div><div><span>全价过滤</span><strong>${schedule.deny_full_price ? '开启' : '关闭'}</strong></div></div>
          <h3 class="subhead">渠道均摊</h3><div class="channel-list">${Object.entries(schedule.providers || {}).map(([provider, info]) => html`<div class="channel-row"><div><strong>${provider}</strong><small>${info.active_accounts} 个账号</small></div><div class="channel-meta"><span>${info.requests} 次</span>${badge(`目标 ${pct(info.target_share)}`, 'neutral')}</div></div>`)}</div>` : empty('调度信息不可用。')}
      </article>
    </section>`
  return c.html(layout(c, { title: '调度策略', active: 'policy', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('调度策略', '成本、负载、稳定性与会话亲和')}${body}` }))
})

/* ---------------------------------------------------------------- settings */

app.get('/settings', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  let settings
  let overview
  try { ;[settings, overview] = await Promise.all([api(c, '/api/settings'), api(c, '/api/overview')]) } catch { settings = {}; overview = {} }
  const bridge = overview.bridge || {}
  const openbridge = overview.openbridge || {}
  const body = html`
    <section class="grid-2">
      <article class="panel"><div class="panel-head"><div><h2>刷新周期</h2><p>后台调度器的运行节奏</p></div></div>
        <form method="post" action="/actions/settings" class="stack">
          <label class="field"><span>模型目录刷新（秒）</span><input name="catalog_refresh_seconds" type="number" min="30" max="600" value="${settings.catalog_refresh_seconds ?? 60}"></label>
          <label class="field"><span>积分刷新（秒）</span><input name="credit_refresh_seconds" type="number" min="60" max="3600" value="${settings.credit_refresh_seconds ?? 300}"></label>
          <label class="field"><span>账号同步（秒）</span><input name="account_sync_seconds" type="number" min="15" max="900" value="${settings.account_sync_seconds ?? 60}"></label>
          <label class="field"><span>WorkBuddy 目录身份</span><select name="config_identity">${['current','desktop','vscode'].map((v) => html`<option value="${v}" ${settings.config_identity === v ? raw('selected') : ''}>${v}</option>`)}</select></label>
          <button class="button primary" type="submit">保存设置</button>
        </form>
      </article>
      <article class="panel"><div class="panel-head"><div><h2>上游状态</h2><p>只读诊断</p></div></div>
        <div class="metric-list"><div><span>Buddy2api</span><strong>${bridge.ok ? '正常' : '异常'}</strong></div><div><span>OpenBridge</span><strong>${openbridge.configured ? (openbridge.ok ? '正常' : '异常') : '未配置'}</strong></div><div><span>管理 token</span><strong>文件持久化</strong></div></div>
        <div class="panel-divider"></div><p class="hint">凭据、池 Key 和账号 token 只保存在 /data 下的加密数据库或 secrets 文件中。备份 /data 即可保留全部运行状态。</p>
      </article>
    </section>`
  return c.html(layout(c, { title: '运行设置', active: 'settings', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('运行设置', '控制台与调度器的运行参数')}${body}` }))
})

/* ------------------------------------------------------------------- audit */

app.get('/audit', async (c) => {
  const session = await sessionInfo(c)
  if (!session) return c.redirect('/login', 303)
  let data
  try { data = await api(c, '/api/audit?limit=200') } catch (error) { data = { items: [], error: error.message } }
  const items = data.items || []
  const body = html`
    ${data.error ? html`<div class="flash flash-bad">${data.error}</div>` : ''}
    <article class="panel"><div class="panel-head"><div><h2>审计日志</h2><p>控制台与调度器写操作</p></div></div>
      ${items.length ? html`<div class="table-wrap"><table><thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>对象</th><th>摘要</th></tr></thead><tbody>${items.map((item) => html`<tr><td class="muted nowrap">${fmtTime(item.created_at)}</td><td>${item.actor || '—'}</td><td><code>${item.action}</code></td><td>${item.target || '—'}</td><td class="cell-sub">${item.payload ? JSON.stringify(item.payload).slice(0, 180) : '—'}</td></tr>`)}</tbody></table></div>` : empty('还没有审计记录。')}
    </article>`
  return c.html(layout(c, { title: '审计日志', active: 'audit', actor: session.actor, csrf: cookieValue(c, 'cw_csrf'), body: html`${pageTitle('审计日志', '谁在什么时候改了什么')}${body}` }))
})

/* ----------------------------------------------------------------- actions */

app.post('/actions/accounts/sync', async (c) => runAction(c, '/accounts', async () => {
  const result = await api(c, '/api/workbuddy/accounts/sync', { method: 'POST', body: {} })
  return `已同步 ${result.items?.length ?? 0} 个账号`
}))
app.post('/actions/credits/refresh', async (c) => runAction(c, '/accounts', async () => {
  await api(c, '/api/workbuddy/credits/refresh', { method: 'POST', body: {} })
  return '积分快照已刷新'
}))
app.post('/actions/catalog/refresh', async (c) => runAction(c, '/models', async () => {
  await api(c, '/api/workbuddy/catalog/refresh', { method: 'POST', body: {} })
  return '模型目录已刷新'
}))
app.post('/actions/checkin/run', async (c) => runAction(c, '/accounts', async () => {
  const result = await api(c, '/api/workbuddy/checkin/run', { method: 'POST', body: {} })
  return `签到完成：${result.claimed ?? 0} 个成功`
}))
app.post('/actions/accounts/import', async (c) => runAction(c, '/accounts', async () => {
  const form = await c.req.parseBody()
  const provider = String(form.provider || '')
  let credentials
  try {
    credentials = JSON.parse(String(form.credentials || '{}'))
  } catch {
    throw new Error('凭据必须是合法 JSON')
  }
  if (!credentials || typeof credentials !== 'object' || Array.isArray(credentials)) throw new Error('凭据必须是 JSON 对象')
  const result = await api(c, '/api/workbuddy/accounts/add', { method: 'POST', body: { ...credentials, provider } })
  return `已导入 ${provider} 账号 #${result.id}`
}))
app.post('/actions/accounts/:id/refresh', async (c) => runAction(c, `/accounts/${c.req.param('id')}`, async () => {
  await api(c, `/api/workbuddy/accounts/${c.req.param('id')}/refresh`, { method: 'POST', body: {} })
  return '账号已刷新'
}))
app.post('/actions/accounts/:id/checkin', async (c) => runAction(c, `/accounts/${c.req.param('id')}`, async () => {
  const result = await api(c, `/api/workbuddy/accounts/${c.req.param('id')}/checkin`, { method: 'POST', body: {} })
  return result.message || (result.claimed ? '签到成功' : '签到完成')
}))
app.post('/actions/accounts/:id/toggle', async (c) => runAction(c, `/accounts/${c.req.param('id')}`, async () => {
  const form = await c.req.parseBody()
  await api(c, `/api/workbuddy/accounts/${c.req.param('id')}`, { method: 'PATCH', body: { status: String(form.status || 'active') } })
  return '账号状态已更新'
}))
app.post('/actions/accounts/:id/delete', async (c) => runAction(c, '/accounts', async () => {
  await api(c, `/api/workbuddy/accounts/${c.req.param('id')}`, { method: 'DELETE' })
  return '账号已删除'
}))
app.post('/actions/accounts/:id/caps', async (c) => runAction(c, `/accounts/${c.req.param('id')}`, async () => {
  const form = await c.req.parseBody()
  await api(c, `/api/workbuddy/accounts/${c.req.param('id')}/caps`, { method: 'PUT', body: {
    model_id: String(form.model_id || '*'), daily_request_cap: Number(form.daily_request_cap || 0), daily_credit_cap: Number(form.daily_credit_cap || 0),
  } })
  return '账号限额已保存'
}))
app.post('/actions/models/manual', async (c) => runAction(c, '/models', async () => {
  const form = await c.req.parseBody()
  await api(c, '/api/workbuddy/models/manual', { method: 'PUT', body: {
    provider: String(form.provider || ''), model_id: String(form.model_id || ''),
    base_credits: form.base_credits === '' ? null : Number(form.base_credits),
    factor: form.factor === '' ? null : Number(form.factor),
    daily_windows: [], timezone: String(form.timezone || 'Asia/Shanghai'), enabled: Boolean(form.enabled),
  } })
  return '手动成本已保存'
}))
app.post('/actions/keys/create', async (c) => {
  const form = await c.req.parseBody()
  try {
    const allowed = String(form.allowed_models || '').split(',').map((x) => x.trim()).filter(Boolean)
    const result = await api(c, '/api/keys', { method: 'POST', body: {
      name: String(form.name || 'pool key'), kind: String(form.kind || 'agent'),
      daily_request_limit: Number(form.daily_request_limit || 0), daily_credit_limit: Number(form.daily_credit_limit || 0),
      max_attempts: Number(form.max_attempts || 3), allowed_models: allowed.length ? allowed : null,
    } })
    const reveal = randomUUID()
    revealKeys.set(reveal, result.key)
    return c.redirect(`/keys?reveal=${reveal}`, 303)
  } catch (error) {
    return redirectWith(c, '/keys', 'error', error.message)
  }
})
app.post('/actions/keys/:id/rotate', async (c) => {
  try {
    const result = await api(c, `/api/keys/${c.req.param('id')}/rotate`, { method: 'POST', body: {} })
    const reveal = randomUUID()
    revealKeys.set(reveal, result.key)
    return c.redirect(`/keys?reveal=${reveal}`, 303)
  } catch (error) {
    return redirectWith(c, '/keys', 'error', error.message)
  }
})
app.post('/actions/keys/:id/toggle', async (c) => runAction(c, '/keys', async () => {
  const form = await c.req.parseBody()
  await api(c, `/api/keys/${c.req.param('id')}`, { method: 'PATCH', body: { status: String(form.status || 'active') } })
  return 'Key 状态已更新'
}))
app.post('/actions/keys/:id/delete', async (c) => runAction(c, '/keys', async () => {
  await api(c, `/api/keys/${c.req.param('id')}`, { method: 'DELETE' })
  return 'Key 已删除'
}))
app.post('/actions/checkin/settings', async (c) => runAction(c, '/checkin', async () => {
  const form = await c.req.parseBody()
  await api(c, '/api/workbuddy/checkin/settings', { method: 'PUT', body: {
    enabled: Boolean(form.enabled), hour: Number(form.hour || 0), minute: Number(form.minute || 0), confirm_risk: Boolean(form.confirm_risk),
  } })
  return '签到设置已保存'
}))
app.post('/actions/keepalive/settings', async (c) => runAction(c, '/checkin', async () => {
  const form = await c.req.parseBody()
  await api(c, '/api/workbuddy/keepalive/settings', { method: 'PUT', body: { enabled: Boolean(form.enabled), hour: Number(form.hour || 22) } })
  return '保活设置已保存'
}))
app.post('/actions/checkin/accounts/:id/rule', async (c) => runAction(c, '/checkin', async () => {
  const form = await c.req.parseBody()
  await api(c, `/api/workbuddy/checkin/accounts/${c.req.param('id')}`, { method: 'PUT', body: { enabled: Boolean(form.enabled), hour: Number(form.hour || 0), minute: Number(form.minute || 0) } })
  return '账号签到规则已保存'
}))
app.post('/actions/checkin/accounts/:id/clear', async (c) => runAction(c, '/checkin', async () => {
  await api(c, `/api/workbuddy/checkin/accounts/${c.req.param('id')}`, { method: 'DELETE' })
  return '账号签到规则已清除'
}))
app.post('/actions/policy', async (c) => runAction(c, '/policy', async () => {
  const form = await c.req.parseBody()
  const bool = (key) => Boolean(form[key])
  const num = (key, fallback) => Number(form[key] ?? fallback)
  const body = {
    routing_mode: String(form.routing_mode || 'balanced'), account_sharing: String(form.account_sharing || 'balanced'),
    account_strategy: String(form.account_strategy || 'expiring_first'), unknown_cost: String(form.unknown_cost || 'after_known'),
    prefer_free: bool('prefer_free'), allow_night: bool('allow_night'), deny_full_price: bool('deny_full_price'), allow_over_max: bool('allow_over_max'),
    night_auto: bool('night_auto'), night_start: String(form.night_start || '23:00'), night_end: String(form.night_end || '08:00'),
    max_multiplier: num('max_multiplier', 0), min_credit_reserve: num('min_credit_reserve', 0), max_attempts: num('max_attempts', 4),
    max_model_candidates: num('max_model_candidates', 4), session_ttl_seconds: num('session_ttl_seconds', 1800),
    free_429_threshold: num('free_429_threshold', 3), stability_weight: num('stability_weight', 0.5),
    load_weight: num('load_weight', 0.2), cost_weight: num('cost_weight', 0.2), session_jitter: num('session_jitter', 0.1),
    allow_models: String(form.allow_models || '').split(',').map((x) => x.trim()).filter(Boolean),
    deny_models: String(form.deny_models || '').split(',').map((x) => x.trim()).filter(Boolean),
    day_priority: {
      free: Number(form.day_free || 0), promo: Number(form.day_promo || 1), normal: Number(form.day_normal || 99),
    },
    night_priority: {
      night_free: Number(form.night_priority_night_free || 0), night_promo: Number(form.night_priority_night_promo || 1),
      free: Number(form.night_priority_free ?? 2), promo: Number(form.night_priority_promo ?? 3), normal: Number(form.night_priority_normal ?? 99),
    },
  }
  try { body.model_priority = JSON.parse(String(form.model_priority || '{}')) } catch { throw new Error('模型优先级必须是 JSON 对象') }
  await api(c, '/api/policy', { method: 'PUT', body })
  return '调度策略已保存'
}))
app.post('/actions/settings', async (c) => runAction(c, '/settings', async () => {
  const form = await c.req.parseBody()
  await api(c, '/api/settings', { method: 'PUT', body: {
    catalog_refresh_seconds: Number(form.catalog_refresh_seconds || 60), credit_refresh_seconds: Number(form.credit_refresh_seconds || 300),
    account_sync_seconds: Number(form.account_sync_seconds || 60), config_identity: String(form.config_identity || 'current'),
  } })
  return '运行设置已保存'
}))

/* ------------------------------------------------------------------ static */

app.use('/static/*', serveStatic({ root: './public' }))

app.notFound((c) => c.html(html`<div class="flash flash-bad">页面不存在</div><a class="link" href="/">返回总览</a>`, 404))

serve({ fetch: app.fetch, port: PORT, hostname: '0.0.0.0' }, (info) => {
  console.log(`[llm-agg-ui] listening on http://0.0.0.0:${info.port} -> ${API_ORIGIN}`)
})
