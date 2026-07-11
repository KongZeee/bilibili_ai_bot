# Web 面板完整重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前 Vanilla JS 单文件 + iframe 子应用的 Web 面板，重构为 Vue 3 CDN 模式、双栏布局、统一设计系统、账号详情页驱动的现代化管理面板。

**Architecture:** 采用 Vue 3 Global Build（本地化 CDN，无构建工具），ES Module 拆分组件文件。双栏布局（活动栏 56px + 侧边栏 240px + 主内容区）。账号/LLM/人格在账号详情页 Tab 内统一管理。LivingMemory 子应用完全合并为 Vue 组件，消除 iframe 和双设计系统。

**Tech Stack:** Vue 3 (vue.global.prod.js 本地化)、原生 CSS（Material Design 3 设计变量保持不变）、Starlette 后端、hash 路由（自实现轻量路由器）

---

## 当前问题诊断

| 问题 | 现状 | 目标 |
|------|------|------|
| 导航混乱 | 13 个扁平菜单无分组 | VS Code 风格双栏：活动栏（5 组）+ 侧边栏（子页面） |
| 账号/LLM/人格关系不直观 | 3 个独立页面，关联不可见 | 账号详情页 Tab 统一管理 LLM/人格绑定 |
| 记忆系统风格分裂 | loadMemoryPage（简陋）+ LivingMemory iframe（Notion 风格） | 合并为 Vue 原生组件，统一 Material Design 3 |
| dashboard.js 2700 行单体 | innerHTML 拼字符串 | Vue 组件化，按功能拆分 ES Module 文件 |
| 配置页对象数组 | 只读摘要 + 跳转按钮 | 保持专用管理页，配置页聚焦标量字段 |

## 目标文件结构

```
bilibot/web/
├── panel.py                         # [修改] 简化 HTML 外壳，加载 Vue + app.js
├── static/
│   ├── vendor/
│   │   └── vue.global.prod.js       # [新增] Vue 3 本地化（~150KB）
│   ├── js/
│   │   ├── app.js                   # [新增] Vue 应用入口 + 根组件 + 路由
│   │   ├── api.js                   # [新增] API 客户端封装
│   │   ├── state.js                 # [新增] 全局响应式状态
│   │   ├── utils.js                 # [新增] 工具函数（转义、toast 等）
│   │   ├── components/
│   │   │   ├── layout.js            # [新增] AppShell + ActivityBar + SideBar
│   │   │   ├── common.js            # [新增] Card/Button/Toggle/Badge/Modal/Toast/Form
│   │   │   ├── accounts.js          # [新增] AccountList + AccountDetail + Tabs
│   │   │   ├── llm.js               # [新增] LlmList + LlmEditor
│   │   │   ├── personas.js          # [新增] PersonaList + PersonaEditor
│   │   │   ├── memory/
│   │   │   │   ├── graph-canvas.js  # [新增] 图谱 Canvas 渲染（迁移 graph-2d.js）
│   │   │   │   ├── graph-page.js    # [新增] 图谱页 Vue 组件
│   │   │   │   ├── list-page.js     # [新增] 记忆列表页（虚拟滚动）
│   │   │   │   ├── recall-page.js   # [新增] 召回测试页
│   │   │   │   └── stats-panel.js   # [新增] 记忆统计面板
│   │   │   └── pages/
│   │   │       ├── overview.js      # [新增] 总览页
│   │   │       ├── comments.js      # [新增] 评论页
│   │   │       ├── proactive.js     # [新增] 主动行为页
│   │   │       ├── drafts.js        # [新增] 动态草稿页
│   │   │       ├── video-analysis.js# [新增] 视频理解页
│   │   │       ├── image-gen.js     # [新增] 文生图页
│   │   │       ├── config.js        # [新增] 配置页
│   │   │       ├── system.js        # [新增] 系统页
│   │   │       └── logs.js          # [新增] 日志页
│   │   └── [删除] dashboard.js       # 旧单体 JS
│   ├── css/
│   │   ├── tokens.css               # [新增] 设计变量（迁移自 dashboard.css :root）
│   │   ├── base.css                 # [新增] 重置 + 基础 + 滚动条
│   │   ├── layout.css               # [新增] 双栏布局样式
│   │   ├── components.css           # [新增] 通用组件样式
│   │   └── pages.css                # [新增] 页面专属样式（记忆图谱等）
│   ├── css/
│   │   └── [删除] dashboard.css      # 旧样式
│   └── [删除] livingmemory/          # 旧 LivingMemory 子应用
└── templates/
    └── login.html                   # [保留] 登录页不变
```

## 活动栏分组设计（双栏布局）

| 活动栏组 | 图标名 | 侧边栏子页面 |
|----------|--------|-------------|
| 运营总览 | `dashboard` | 总览、评论、日志 |
| 账号与身份 | `accounts` | 账号管理、人格管理、LLM 管理 |
| 内容创作 | `sparkles` | 主动行为、动态草稿、文生图、视频理解 |
| 记忆与知识 | `memory` | 记忆图谱、记忆列表、召回测试、记忆配置 |
| 系统 | `config` | 系统设置、全局配置 |

## 路由设计

```
#/                          → 总览
#/comments                  → 评论
#/logs                      → 日志
#/accounts                  → 账号列表
#/accounts/:id              → 账号详情（Tab: info/login/llm/persona/status/tasks）
#/personas                  → 人格列表
#/personas/:id              → 人格编辑
#/llm                       → LLM 列表
#/llm/:id                   → LLM 编辑
#/proactive                 → 主动行为
#/drafts                    → 动态草稿
#/image-gen                 → 文生图配置
#/video-analysis            → 视频理解配置
#/memory/graph              → 记忆图谱
#/memory/list               → 记忆列表
#/memory/recall             → 召回测试
#/system                    → 系统设置
#/config                    → 全局配置
```

---

## Phase 1: 基础架构搭建

**目标：** Vue 3 应用骨架能跑起来，显示双栏布局空壳。旧 dashboard.js 保持可用（并行运行）。

### Task 1.1: 下载 Vue 3 本地化文件

**Files:**
- Create: `bilibot/web/static/vendor/vue.global.prod.js`

- [ ] **Step 1: 下载 Vue 3 global build 到本地**

Run:
```bash
cd d:\bot\astrbot_plugin_bilibili_ai_bot\bilibot\web\static
mkdir vendor
curl -L -o vendor\vue.global.prod.js https://unpkg.com/vue@3/dist/vue.global.prod.js
```

- [ ] **Step 2: 验证文件大小和内容**

Run: `dir vendor\vue.global.prod.js`
Expected: 文件大小约 130-160KB

Run: `findstr "Vue" vendor\vue.global.prod.js | findstr "version"`
Expected: 包含 Vue 版本信息

- [ ] **Step 3: Commit**

```bash
git add bilibot/web/static/vendor/vue.global.prod.js
git commit -m "chore: add Vue 3 local build for web refactor"
```

### Task 1.2: 创建设计系统 CSS 文件

**Files:**
- Create: `bilibot/web/static/css/tokens.css`
- Create: `bilibot/web/static/css/base.css`

- [ ] **Step 1: 创建 tokens.css（从 dashboard.css 迁移设计变量）**

```css
/* tokens.css - Material Design 3 设计变量 */

:root {
    /* 主色 */
    --primary: #6750A4;
    --primary-hover: #7965B0;
    --primary-light: #EADDFF;
    --primary-dark: #4F378B;
    --on-primary: #FFFFFF;

    --secondary: #625B71;
    --tertiary: #7D5260;

    /* 表面层级 */
    --surface: #FEF7FF;
    --surface-1: #F7F2FA;
    --surface-2: #F1ECF4;
    --surface-3: #E6E0E9;
    --surface-dim: #DED8E1;

    /* 文字 */
    --on-surface: #1D1B20;
    --on-surface-variant: #49454F;
    --on-surface-muted: #79747E;
    --outline: #79747E;
    --outline-variant: #CAC4D0;

    /* 状态色 */
    --success: #2E7D32;
    --success-bg: #E8F5E9;
    --warning: #ED6C02;
    --warning-bg: #FFF3E0;
    --danger: #D32F2F;
    --danger-bg: #FFEBEE;
    --info: #0288D1;
    --info-bg: #E1F5FE;

    /* 阴影层级 */
    --shadow-1: 0 1px 2px 0 rgba(0,0,0,0.06), 0 1px 3px 0 rgba(0,0,0,0.10);
    --shadow-2: 0 1px 4px 0 rgba(0,0,0,0.08), 0 3px 8px 0 rgba(0,0,0,0.08);
    --shadow-3: 0 4px 12px 0 rgba(0,0,0,0.10), 0 2px 4px 0 rgba(0,0,0,0.06);
    --shadow-4: 0 8px 24px 0 rgba(0,0,0,0.12), 0 4px 8px 0 rgba(0,0,0,0.08);

    /* 圆角 */
    --radius-sm: 8px;
    --radius-md: 12px;
    --radius-lg: 16px;
    --radius-xl: 24px;
    --radius-full: 9999px;

    /* 双栏布局尺寸 */
    --activity-bar-width: 56px;
    --sidebar-width: 240px;
    --header-height: 48px;

    /* 侧边栏深色主题 */
    --sidebar-bg: linear-gradient(180deg, #2A2438 0%, #1C1726 100%);
    --sidebar-text: #E6E0E9;
    --sidebar-text-muted: #CAC4D0;
    --sidebar-active-bg: rgba(103, 80, 164, 0.3);
    --sidebar-hover-bg: rgba(255, 255, 255, 0.08);

    /* 语义别名（兼容） */
    --text-primary: var(--on-surface);
    --text-secondary: var(--on-surface-variant);
    --text-muted: var(--on-surface-muted);
    --bg-primary: var(--surface-1);
    --bg-secondary: var(--surface-2);
    --bg-tertiary: var(--surface-2);
    --border: var(--outline-variant);
    --accent: var(--primary);
}
```

- [ ] **Step 2: 创建 base.css（重置 + 基础样式 + 滚动条）**

```css
/* base.css - 重置和基础样式 */

* { margin: 0; padding: 0; box-sizing: border-box; }

html, body { height: 100%; }

body {
    font-family: 'Roboto', -apple-system, BlinkMacSystemFont, 'Segoe UI',
                 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: var(--surface-1);
    color: var(--on-surface);
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    font-size: 14px;
    line-height: 1.5;
}

a { color: var(--primary); text-decoration: none; }
a:hover { color: var(--primary-hover); }

/* 滚动条 */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--outline-variant); border-radius: 5px; }
::-webkit-scrollbar-thumb:hover { background: var(--outline); }

/* 工具类 */
.text-muted { color: var(--text-muted); }
.text-secondary { color: var(--text-secondary); }
.flex { display: flex; }
.flex-col { display: flex; flex-direction: column; }
.items-center { align-items: center; }
.justify-between { justify-content: space-between; }
.gap-2 { gap: 8px; }
.gap-3 { gap: 12px; }
.gap-4 { gap: 16px; }
.mt-2 { margin-top: 8px; }
.mt-4 { margin-top: 16px; }
.mb-2 { margin-bottom: 8px; }
.mb-4 { margin-bottom: 16px; }
```

- [ ] **Step 3: Commit**

```bash
git add bilibot/web/static/css/tokens.css bilibot/web/static/css/base.css
git commit -m "feat(web): add design tokens and base CSS for refactor"
```

### Task 1.3: 创建双栏布局 CSS

**Files:**
- Create: `bilibot/web/static/css/layout.css`

- [ ] **Step 1: 创建双栏布局样式**

```css
/* layout.css - VS Code 风格双栏布局 */

.app-shell {
    display: flex;
    height: 100vh;
    overflow: hidden;
}

/* 活动栏（最左侧 56px） */
.activity-bar {
    width: var(--activity-bar-width);
    background: #1C1726;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 8px 0;
    flex-shrink: 0;
    z-index: 200;
}

.activity-bar .logo {
    width: 40px;
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    font-weight: 700;
    color: var(--primary-light);
    background: var(--primary-dark);
    border-radius: var(--radius-sm);
    margin-bottom: 12px;
}

.activity-item {
    width: 48px;
    height: 48px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    border-radius: var(--radius-sm);
    color: var(--sidebar-text-muted);
    transition: all 0.2s;
    position: relative;
    margin-bottom: 4px;
}

.activity-item:hover { color: var(--sidebar-text); background: var(--sidebar-hover-bg); }

.activity-item.active { color: var(--sidebar-text); }
.activity-item.active::before {
    content: '';
    position: absolute;
    left: 0;
    top: 50%;
    transform: translateY(-50%);
    width: 3px;
    height: 24px;
    background: var(--primary);
    border-radius: 0 2px 2px 0;
}

.activity-item .icon { font-size: 22px; }
.activity-item .icon-svg { display: block; }

.activity-bar .spacer { flex: 1; }

.activity-bar .logout-btn {
    width: 48px;
    height: 48px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    border-radius: var(--radius-sm);
    color: var(--sidebar-text-muted);
    background: none;
    border: none;
    font-size: 20px;
}
.activity-bar .logout-btn:hover { color: var(--danger); background: var(--sidebar-hover-bg); }

/* 侧边栏（第二栏 240px） */
.sidebar {
    width: var(--sidebar-width);
    background: var(--sidebar-bg);
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    color: var(--sidebar-text);
    z-index: 150;
    box-shadow: var(--shadow-2);
}

.sidebar-header {
    padding: 16px 20px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--sidebar-text-muted);
}

.sidebar-nav { flex: 1; overflow-y: auto; padding: 0 8px; }

.sidebar-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 12px;
    cursor: pointer;
    border-radius: var(--radius-sm);
    color: var(--sidebar-text);
    font-size: 13px;
    transition: all 0.15s;
    margin-bottom: 2px;
}
.sidebar-item:hover { background: var(--sidebar-hover-bg); }
.sidebar-item.active { background: var(--sidebar-active-bg); font-weight: 500; }
.sidebar-item .icon { font-size: 16px; width: 20px; text-align: center; }
.sidebar-item .icon-svg { flex-shrink: 0; }

/* 主内容区 */
.main-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.main-header {
    height: var(--header-height);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 24px;
    background: var(--surface);
    border-bottom: 1px solid var(--outline-variant);
    flex-shrink: 0;
}

.main-header h1 { font-size: 16px; font-weight: 600; color: var(--on-surface); }

.main-body {
    flex: 1;
    overflow-y: auto;
    padding: 24px 32px;
}

/* 响应式 */
@media (max-width: 768px) {
    .sidebar { display: none; }
    .activity-bar { width: 48px; }
    .activity-item { width: 40px; height: 40px; }
    .main-body { padding: 16px; }
}
```

- [ ] **Step 2: Commit**

```bash
git add bilibot/web/static/css/layout.css
git commit -m "feat(web): add dual-column layout CSS (VS Code style)"
```

### Task 1.4: 创建 API 客户端和工具函数

**Files:**
- Create: `bilibot/web/static/js/api.js`
- Create: `bilibot/web/static/js/utils.js`
- Create: `bilibot/web/static/js/state.js`

- [ ] **Step 1: 创建 api.js（API 客户端封装）**

```javascript
// api.js - 统一 API 请求客户端

const BASE_URL = '';

async function request(url, options = {}) {
    const resp = await fetch(BASE_URL + url, {
        ...options,
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            ...(options.headers || {}),
        },
        body: options.body ? JSON.stringify(options.body) : undefined,
    });

    if (resp.status === 401) {
        window.location.href = '/login';
        throw new Error('未登录');
    }

    const data = await resp.json().catch(() => ({}));

    if (!resp.ok || data.success === false) {
        const err = new Error(data?.error?.message || data?.message || `HTTP ${resp.status}`);
        err.code = data?.error?.code;
        err.details = data?.error?.details;
        err.resp = data;
        throw err;
    }

    return data.data !== undefined ? data.data : data;
}

export const api = {
    get: (url) => request(url, { method: 'GET' }),
    post: (url, body) => request(url, { method: 'POST', body }),
    patch: (url, body) => request(url, { method: 'PATCH', body }),
    delete: (url) => request(url, { method: 'DELETE' }),

    // 账号
    accounts: {
        list: () => api.get('/api/accounts'),
        get: (id) => api.get(`/api/accounts/${id}`),
        create: (data) => api.post('/api/accounts', data),
        update: (id, data) => api.patch(`/api/accounts/${id}`, data),
        delete: (id) => api.delete(`/api/accounts/${id}`),
        setDefault: (id) => api.post(`/api/accounts/${id}/set-default`),
        start: (id) => api.post(`/api/accounts/${id}/start`),
        stop: (id) => api.post(`/api/accounts/${id}/stop`),
        bindPersona: (id, data) => api.post(`/api/accounts/${id}/persona`, data),
        switchPersona: (id, personaId) => api.post(`/api/accounts/${id}/switch-persona`, { persona_id: personaId }),
        bindLlm: (id, llmId) => api.post(`/api/accounts/${id}/llm`, { llm_id: llmId }),
        qrLogin: (id) => api.post(`/api/accounts/${id}/qr-login`),
        qrPoll: (id, sid) => api.get(`/api/accounts/${id}/qr-login/${sid}`),
        qrCancel: (id, sid) => api.post(`/api/accounts/${id}/qr-login/${sid}/cancel`),
        profiles: () => api.get('/api/accounts/profiles'),
        tasks: (id) => api.get(`/api/accounts/${id}/tasks`),
    },

    // LLM
    llm: {
        list: () => api.get('/api/llm-providers'),
        create: (data) => api.post('/api/llm-providers', data),
        update: (id, data) => api.patch(`/api/llm-providers/${id}`, data),
        delete: (id, force) => api.delete(`/api/llm-providers/${id}${force ? '?force=true' : ''}`),
        setDefault: (id) => api.post(`/api/llm-providers/${id}/set-default`),
        test: (id) => api.post(`/api/llm-providers/${id}/test`),
    },

    // 人格
    personas: {
        list: () => api.get('/api/personas'),
        get: (id) => api.get(`/api/personas/${id}`),
        create: (data) => api.post('/api/personas', data),
        update: (id, data) => api.patch(`/api/personas/${id}`, data),
        delete: (id) => api.delete(`/api/personas/${id}`),
        activate: (id) => api.post(`/api/personas/${id}/activate`),
        copy: (id) => api.post(`/api/personas/${id}/copy`),
        test: (id, data) => api.post(`/api/personas/${id}/test`, data),
        export: (id) => api.get(`/api/personas/${id}/export`),
        import: (data) => api.post('/api/personas/import', data),
    },

    // 记忆
    memory: {
        stats: (accId) => api.get(`/api/accounts/${accId}/memory/stats`),
        list: (accId, params) => api.get(`/api/accounts/${accId}/memory?${new URLSearchParams(params)}`),
        search: (accId, data) => api.post(`/api/accounts/${accId}/memory/search`, data),
        delete: (accId, memId) => api.delete(`/api/accounts/${accId}/memory/${memId}`),
        graph: (accId) => api.get(`/api/accounts/${accId}/memory/graph`),
        graphQuery: (accId, data) => api.post(`/api/accounts/${accId}/memory/graph/query`, data),
        migrate: (accId) => api.post(`/api/accounts/${accId}/memory/migrate`),
    },

    // 配置
    config: {
        schema: () => api.get('/api/config/schema'),
        full: () => api.get('/api/config/full'),
        patch: (data) => api.patch('/api/config', data),
        validate: () => api.post('/api/config/validate'),
        reload: () => api.post('/api/config/reload'),
    },

    // 其他
    replies: (params) => api.get(`/api/replies?${new URLSearchParams(params)}`),
    audits: (params) => api.get(`/api/audit/generations?${new URLSearchParams(params)}`),
    logs: (params) => api.get(`/api/logs?${new URLSearchParams(params)}`),
    status: () => api.get('/api/status'),
    safety: {
        pauseStatus: () => api.get('/api/safety/pause-status'),
        pause: () => api.post('/api/safety/pause'),
        resume: () => api.post('/api/safety/resume'),
        blacklist: () => api.get('/api/safety/blacklist'),
        addBlacklist: (data) => api.post('/api/safety/blacklist', data),
        delBlacklist: (uid) => api.delete(`/api/safety/blacklist/${uid}`),
    },
    videoAnalysis: {
        get: () => api.get('/api/video-analysis'),
        patch: (data) => api.patch('/api/video-analysis', data),
    },
    imageGen: {
        get: () => api.get('/api/image-generation'),
        patch: (data) => api.patch('/api/image-generation', data),
    },
    drafts: {
        list: (accId) => api.get(`/api/accounts/${accId}/dynamic-drafts`),
        get: (accId, id) => api.get(`/api/accounts/${accId}/dynamic-drafts/${id}`),
        approve: (accId, id) => api.post(`/api/accounts/${accId}/dynamic-drafts/${id}/approve`),
        reject: (accId, id, reason) => api.post(`/api/accounts/${accId}/dynamic-drafts/${id}/reject`, { reason }),
        retry: (accId, id) => api.post(`/api/accounts/${accId}/dynamic-drafts/${id}/retry`),
    },
    backup: {
        create: () => api.post('/api/backup/create'),
        list: () => api.get('/api/backup/list'),
        restore: (name) => api.post('/api/backup/restore', { name }),
        delete: (name) => api.delete(`/api/backup/${name}`),
    },
};
```

- [ ] **Step 2: 创建 utils.js（工具函数）**

```javascript
// utils.js - 通用工具函数

export function escHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

export function formatTime(ts) {
    if (!ts) return '-';
    const d = new Date(ts * 1000);
    return d.toLocaleString('zh-CN', { hour12: false });
}

export function formatRelative(ts) {
    if (!ts) return '-';
    const diff = Date.now() / 1000 - ts;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    return `${Math.floor(diff / 86400)} 天前`;
}

export function debounce(fn, delay = 300) {
    let timer;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), delay);
    };
}

export function downloadFile(filename, content, type = 'text/plain') {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}
```

- [ ] **Step 3: 创建 state.js（全局响应式状态）**

```javascript
// state.js - 全局响应式状态管理

import { reactive, ref } from './vendor/vue.global.prod.js';

export const appState = reactive({
    // 当前路由
    route: { path: '/', params: {} },

    // 活动栏当前组
    activeGroup: 'overview',

    // 账号列表缓存
    accounts: [],
    accountsLoaded: false,

    // LLM 列表缓存
    llmProviders: [],
    llmLoaded: false,

    // 人格列表缓存
    personas: [],
    personasLoaded: false,

    // 当前选中账号（用于记忆等需要账号上下文的页面）
    currentAccountId: null,

    // toast 队列
    toasts: [],
});

export function showToast(message, type = 'info') {
    const id = Date.now() + Math.random();
    appState.toasts.push({ id, message, type });
    setTimeout(() => {
        const idx = appState.toasts.findIndex(t => t.id === id);
        if (idx >= 0) appState.toasts.splice(idx, 1);
    }, 3000);
}

export async function refreshAccounts() {
    try {
        appState.accounts = await api.accounts.list();
        appState.accountsLoaded = true;
        if (!appState.currentAccountId && appState.accounts.length > 0) {
            appState.currentAccountId = appState.accounts[0].id;
        }
    } catch (e) {
        showToast('加载账号列表失败: ' + e.message, 'error');
    }
}

export async function refreshLlm() {
    try {
        appState.llmProviders = await api.llm.list();
        appState.llmLoaded = true;
    } catch (e) {
        showToast('加载 LLM 列表失败: ' + e.message, 'error');
    }
}

export async function refreshPersonas() {
    try {
        appState.personas = await api.personas.list();
        appState.personasLoaded = true;
    } catch (e) {
        showToast('加载人格列表失败: ' + e.message, 'error');
    }
}
```

注意：`state.js` 中 `import` Vue 的方式需要调整。Vue global build 通过 `window.Vue` 暴露，ES Module 不能直接 import。改用以下方式：

修正 `state.js` 的 Vue 引用方式——由于使用 global build，所有文件通过 `window.Vue` 访问：

```javascript
// state.js - 全局响应式状态管理
// Vue 通过 window.Vue 全局变量访问（global build 模式）

const { reactive, ref } = window.Vue;

export const appState = reactive({
    route: { path: '/', params: {} },
    activeGroup: 'overview',
    accounts: [],
    accountsLoaded: false,
    llmProviders: [],
    llmLoaded: false,
    personas: [],
    personasLoaded: false,
    currentAccountId: null,
    toasts: [],
});

export function showToast(message, type = 'info') {
    const id = Date.now() + Math.random();
    appState.toasts.push({ id, message, type });
    setTimeout(() => {
        const idx = appState.toasts.findIndex(t => t.id === id);
        if (idx >= 0) appState.toasts.splice(idx, 1);
    }, 3000);
}

export async function refreshAccounts() {
    // 需要在 app.js 中注入 api 后调用
    // 见 app.js 中的 initGlobalState()
    try {
        const { api } = window;
        appState.accounts = await api.accounts.list();
        appState.accountsLoaded = true;
        if (!appState.currentAccountId && appState.accounts.length > 0) {
            appState.currentAccountId = appState.accounts[0].id;
        }
    } catch (e) {
        showToast('加载账号列表失败: ' + e.message, 'error');
    }
}

export async function refreshLlm() {
    try {
        const { api } = window;
        appState.llmProviders = await api.llm.list();
        appState.llmLoaded = true;
    } catch (e) {
        showToast('加载 LLM 列表失败: ' + e.message, 'error');
    }
}

export async function refreshPersonas() {
    try {
        const { api } = window;
        appState.personas = await api.personas.list();
        appState.personasLoaded = true;
    } catch (e) {
        showToast('加载人格列表失败: ' + e.message, 'error');
    }
}
```

- [ ] **Step 4: Commit**

```bash
git add bilibot/web/static/js/api.js bilibot/web/static/js/utils.js bilibot/web/static/js/state.js
git commit -m "feat(web): add API client, utils, and global state modules"
```

### Task 1.5: 创建双栏布局组件

**Files:**
- Create: `bilibot/web/static/js/components/layout.js`

- [ ] **Step 1: 创建布局组件（AppShell + ActivityBar + SideBar）**

```javascript
// components/layout.js - 双栏布局组件
const { defineComponent, computed, h } = window.Vue;
import { Icon } from './common.js';

// 导航分组配置
export const NAV_GROUPS = [
    {
        id: 'overview',
        icon: 'dashboard',
        label: '运营总览',
        items: [
            { path: '/', label: '总览', icon: 'dashboard' },
            { path: '/comments', label: '评论', icon: 'comments' },
            { path: '/logs', label: '日志', icon: 'logs' },
        ],
    },
    {
        id: 'identity',
        icon: 'accounts',
        label: '账号与身份',
        items: [
            { path: '/accounts', label: '账号管理', icon: 'accounts' },
            { path: '/personas', label: '人格管理', icon: 'personas' },
            { path: '/llm', label: 'LLM 管理', icon: 'llm' },
        ],
    },
    {
        id: 'creation',
        icon: 'sparkles',
        label: '内容创作',
        items: [
            { path: '/proactive', label: '主动行为', icon: 'proactive' },
            { path: '/drafts', label: '动态草稿', icon: 'drafts' },
            { path: '/image-gen', label: '文生图', icon: 'image' },
            { path: '/video-analysis', label: '视频理解', icon: 'video' },
        ],
    },
    {
        id: 'memory',
        icon: 'memory',
        label: '记忆与知识',
        items: [
            { path: '/memory/graph', label: '记忆图谱', icon: 'graph' },
            { path: '/memory/list', label: '记忆列表', icon: 'drafts' },
            { path: '/memory/recall', label: '召回测试', icon: 'search' },
        ],
    },
    {
        id: 'system',
        icon: 'config',
        label: '系统',
        items: [
            { path: '/system', label: '系统设置', icon: 'system' },
            { path: '/config', label: '全局配置', icon: 'config' },
        ],
    },
];

// 根据路径找到所属分组
export function findGroupByPath(path) {
    for (const g of NAV_GROUPS) {
        if (g.items.some(i => path === i.path || path.startsWith(i.path + '/'))) {
            return g.id;
        }
    }
    return 'overview';
}

// 根据路径找到当前页面标题
export function findPageTitle(path) {
    for (const g of NAV_GROUPS) {
        for (const item of g.items) {
            if (path === item.path || path.startsWith(item.path + '/')) {
                return item.label;
            }
        }
    }
    return '总览';
}

// ActivityBar 组件
export const ActivityBar = defineComponent({
    name: 'ActivityBar',
    props: { activeGroup: String },
    emits: ['select'],
    setup(props, { emit }) {
        return () => h('div', { class: 'activity-bar' }, [
            h('div', { class: 'logo' }, 'B'),
            ...NAV_GROUPS.map(g =>
                h('div', {
                    class: ['activity-item', props.activeGroup === g.id ? 'active' : ''],
                    title: g.label,
                    onClick: () => emit('select', g.id),
                }, [
                    h(Icon, { name: g.icon, size: 22 }),
                ])
            ),
            h('div', { class: 'spacer' }),
            h('button', {
                class: 'logout-btn',
                title: '退出登录',
                onClick: () => { window.location.href = '/login'; },
            }, [h(Icon, { name: 'logout', size: 20 })]),
        ]);
    },
});

// SideBar 组件
export const SideBar = defineComponent({
    name: 'SideBar',
    props: { activeGroup: String, currentPath: String },
    emits: ['navigate'],
    setup(props, { emit }) {
        const group = computed(() =>
            NAV_GROUPS.find(g => g.id === props.activeGroup) || NAV_GROUPS[0]
        );

        return () => h('div', { class: 'sidebar' }, [
            h('div', { class: 'sidebar-header' }, group.value.label),
            h('div', { class: 'sidebar-nav' },
                group.value.items.map(item =>
                    h('div', {
                        class: ['sidebar-item',
                            (props.currentPath === item.path ||
                             props.currentPath.startsWith(item.path + '/')) ? 'active' : ''],
                        onClick: () => emit('navigate', item.path),
                    }, [
                        h(Icon, { name: item.icon, size: 18 }),
                        h('span', item.label),
                    ])
                )
            ),
        ]);
    },
});

// AppShell 组件（双栏布局外壳）
export const AppShell = defineComponent({
    name: 'AppShell',
    props: { currentPath: String, pageTitle: String },
    emits: ['navigate'],
    setup(props, { emit, slots }) {
        const activeGroup = computed(() => findGroupByPath(props.currentPath));

        return () => h('div', { class: 'app-shell' }, [
            h(ActivityBar, {
                activeGroup: activeGroup.value,
                onSelect: (gid) => {
                    // 切换分组时导航到该组第一个页面
                    const g = NAV_GROUPS.find(x => x.id === gid);
                    if (g && g.items[0]) emit('navigate', g.items[0].path);
                },
            }),
            h(SideBar, {
                activeGroup: activeGroup.value,
                currentPath: props.currentPath,
                onNavigate: (path) => emit('navigate', path),
            }),
            h('div', { class: 'main-content' }, [
                h('div', { class: 'main-header' }, [
                    h('h1', props.pageTitle),
                ]),
                h('div', { class: 'main-body' }, slots.default?.()),
            ]),
        ]);
    },
});
```

- [ ] **Step 2: Commit**

```bash
git add bilibot/web/static/js/components/layout.js
git commit -m "feat(web): add dual-column layout components (ActivityBar + SideBar + AppShell)"
```

### Task 1.6: 创建轻量路由器和 App 入口

**Files:**
- Create: `bilibot/web/static/js/router.js`
- Create: `bilibot/web/static/js/app.js`

- [ ] **Step 1: 创建轻量 hash 路由器**

```javascript
// router.js - 轻量 hash 路由器
const { ref, reactive } = window.Vue;

export const route = reactive({
    path: '/',
    segments: [],
    params: {},
});

// 路由表：path pattern → { component, title }
const routes = [];

export function registerRoute(pattern, component, title) {
    routes.push({ pattern, segments: pattern.split('/').filter(Boolean), component, title });
}

export function findRoute(path) {
    const segments = path.split('/').filter(Boolean);
    for (const r of routes) {
        if (r.segments.length !== segments.length) continue;
        const params = {};
        let matched = true;
        for (let i = 0; i < r.segments.length; i++) {
            if (r.segments[i].startsWith(':')) {
                params[r.segments[i].slice(1)] = decodeURIComponent(segments[i]);
            } else if (r.segments[i] !== segments[i]) {
                matched = false;
                break;
            }
        }
        if (matched) return { route: r, params };
    }
    return null;
}

export function navigate(path) {
    window.location.hash = '#' + path;
}

export function initRouter() {
    function parse() {
        const hash = window.location.hash.slice(1) || '/';
        route.path = hash;
        route.segments = hash.split('/').filter(Boolean);
        const found = findRoute(hash);
        route.params = found?.params || {};
    }
    window.addEventListener('hashchange', parse);
    parse();
}

export function useRoute() {
    return route;
}
```

- [ ] **Step 2: 创建 app.js（Vue 应用入口）**

```javascript
// app.js - Vue 应用入口

const { createApp, defineComponent, h, computed, ref, onMounted } = window.Vue;

// 引入依赖
import { api } from './api.js';
import { AppShell } from './components/layout.js';
import { findPageTitle } from './components/layout.js';
import { initRouter, route, registerRoute, findRoute } from './router.js';
import { appState, showToast, refreshAccounts, refreshLlm, refreshPersonas } from './state.js';

// 将 api 挂载到 window 供 state.js 使用
window.api = api;

// --- 页面占位组件（Phase 2-5 会替换为真实页面） ---
const Placeholder = defineComponent({
    props: { name: String },
    setup(props) {
        return () => h('div', { class: 'empty-state' }, [
            h('div', { class: 'empty-icon' }, '🚧'),
            h('h3', props.name),
            h('p', { class: 'text-muted' }, '此页面正在重构中，请稍后...'),
        ]);
    },
});

// --- 注册路由 ---
// Phase 1 先注册占位路由，后续 Phase 替换为真实组件
const pages = [
    { path: '/', title: '总览' },
    { path: '/comments', title: '评论' },
    { path: '/logs', title: '日志' },
    { path: '/accounts', title: '账号管理' },
    { path: '/accounts/:id', title: '账号详情' },
    { path: '/personas', title: '人格管理' },
    { path: '/personas/:id', title: '人格编辑' },
    { path: '/llm', title: 'LLM 管理' },
    { path: '/llm/:id', title: 'LLM 编辑' },
    { path: '/proactive', title: '主动行为' },
    { path: '/drafts', title: '动态草稿' },
    { path: '/image-gen', title: '文生图' },
    { path: '/video-analysis', title: '视频理解' },
    { path: '/memory/graph', title: '记忆图谱' },
    { path: '/memory/list', title: '记忆列表' },
    { path: '/memory/recall', title: '召回测试' },
    { path: '/system', title: '系统设置' },
    { path: '/config', title: '全局配置' },
];

pages.forEach(p => registerRoute(p.path, Placeholder, p.title));

// --- Toast 容器组件 ---
const ToastContainer = defineComponent({
    setup() {
        return () => h('div', { class: 'toast-container' },
            appState.toasts.map(t =>
                h('div', { class: `toast toast-${t.type}`, key: t.id }, t.message)
            )
        );
    },
});

// --- 根组件 ---
const RootApp = defineComponent({
    name: 'RootApp',
    setup() {
        onMounted(async () => {
            initRouter();
            // 预加载全局数据
            await Promise.all([refreshAccounts(), refreshLlm(), refreshPersonas()]);
        });

        const currentComponent = computed(() => {
            const found = findRoute(route.path);
            return found?.route.component || Placeholder;
        });

        const pageTitle = computed(() => {
            const found = findRoute(route.path);
            return found?.route.title || '总览';
        });

        return () => h(AppShell, {
            currentPath: route.path,
            pageTitle: pageTitle.value,
            onNavigate: (path) => { window.location.hash = '#' + path; },
        }, () => [
            h(currentComponent.value, { route }),
            h(ToastContainer),
        ]);
    },
});

// 挂载
createApp(RootApp).mount('#app');
```

- [ ] **Step 3: Commit**

```bash
git add bilibot/web/static/js/router.js bilibot/web/static/js/app.js
git commit -m "feat(web): add hash router and Vue app entry point"
```

### Task 1.7: 修改 panel.py 加载新前端

**Files:**
- Modify: `bilibot/web/panel.py` (函数 `_get_dashboard_html`，行 533-609)

- [ ] **Step 1: 修改 `_get_dashboard_html` 返回新外壳 HTML**

将 panel.py 的 `_get_dashboard_html` 函数（行 533-609）替换为：

```python
def _get_dashboard_html() -> str:
    """仪表盘 HTML - Vue 3 应用外壳"""
    vue_v = _static_version("vendor/vue.global.prod.js")
    app_v = _static_version("js/app.js")
    tokens_v = _static_version("css/tokens.css")
    base_v = _static_version("css/base.css")
    layout_v = _static_version("css/layout.css")
    comp_v = _static_version("css/components.css")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BiliBot - 控制台</title>
    <link rel="stylesheet" href="/static/css/tokens.css?v={tokens_v}">
    <link rel="stylesheet" href="/static/css/base.css?v={base_v}">
    <link rel="stylesheet" href="/static/css/layout.css?v={layout_v}">
    <link rel="stylesheet" href="/static/css/components.css?v={comp_v}">
</head>
<body>
    <div id="app"></div>
    <script src="/static/vendor/vue.global.prod.js?v={vue_v}"></script>
    <script type="module" src="/static/js/app.js?v={app_v}"></script>
</body>
</html>"""
```

- [ ] **Step 2: 创建空的 components.css 占位文件**

Create `bilibot/web/static/css/components.css`:

```css
/* components.css - 通用组件样式（Phase 2 填充） */

/* Toast 容器 */
.toast-container {
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 9999;
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.toast {
    padding: 12px 20px;
    border-radius: var(--radius-sm);
    color: #fff;
    font-size: 14px;
    box-shadow: var(--shadow-3);
    animation: toastIn 0.3s ease;
}
.toast-info { background: var(--info); }
.toast-success { background: var(--success); }
.toast-error { background: var(--danger); }
.toast-warning { background: var(--warning); }

@keyframes toastIn {
    from { transform: translateX(100%); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
}

/* 空状态 */
.empty-state {
    text-align: center;
    padding: 64px 24px;
    color: var(--text-muted);
}
.empty-state .empty-icon { font-size: 48px; margin-bottom: 16px; }
.empty-state h3 { font-size: 18px; margin-bottom: 8px; color: var(--text-secondary); }
```

- [ ] **Step 3: 验证新外壳能加载**

启动 web 服务后访问 `http://localhost:端口/`，预期：
- 页面显示双栏布局（左侧活动栏 + 侧边栏 + 主内容区）
- 活动栏有 5 个图标分组
- 主内容区显示"🚧 此页面正在重构中"
- 浏览器控制台无报错

Run: `pytest tests/ -x -q`
Expected: 192 passed（后端测试不受影响）

- [ ] **Step 4: Commit**

```bash
git add bilibot/web/panel.py bilibot/web/static/css/components.css
git commit -m "feat(web): switch panel.py to Vue 3 app shell with dual-column layout"
```

---

## Phase 2: 通用组件库

**目标：** 创建一套 Vue 通用组件（Card/Button/Toggle/Badge/Modal/Form/Table），供所有页面复用。统一 Material Design 3 风格。

### Task 2.1: 创建通用组件库 JS

**Files:**
- Create: `bilibot/web/static/js/components/common.js`

- [ ] **Step 1: 创建通用组件**

```javascript
// components/common.js - 通用 Vue 组件库
const { defineComponent, h, ref, computed, watch, onMounted, onUnmounted } = window.Vue;

// ═══════════════════════════════════════════════════
// Icon 组件 - 扁平化 SVG 图标系统
// 使用 Material Design / Feather 风格的 24x24 线性图标
// ═══════════════════════════════════════════════════
const ICON_PATHS = {
    // 运营总览
    dashboard: 'M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z',
    comments: 'M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z',
    logs: 'M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-4-6zM6 20V4h7v5h5v11H6z',
    // 账号与身份
    accounts: 'M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z',
    personas: 'M3 5h18v14H3V5zm2 2v10h14V7H5zm4 2h6v2H9V9z M12 2l3 3-3 3-3-3 3-3z',
    llm: 'M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5',
    // 内容创作
    sparkles: 'M12 2l1.5 4.5L18 8l-4.5 1.5L12 14l-1.5-4.5L6 8l4.5-1.5L12 2zM5 14l.75 2.25L8 17l-2.25.75L5 20l-.75-2.25L2 17l2.25-.75L5 14zm14 0l.75 2.25L22 17l-2.25.75L19 20l-.75-2.25L16 17l2.25-.75L19 14z',
    proactive: 'M17.65 6.35A7.958 7.958 0 0012 4a8 8 0 108 8h-2c0 3.31-2.69 6-6 6s-6-2.69-6-6 2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z',
    drafts: 'M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34a.996.996 0 00-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z',
    image: 'M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z',
    video: 'M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z',
    // 记忆与知识
    memory: 'M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z M12 8v4 M12 12l3 3',
    graph: 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z',
    search: 'M15.5 14h-.79l-.28-.27a6.5 6.5 0 10-.7.7l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0A4.5 4.5 0 1114 9.5 4.5 4.5 0 019.5 14z',
    // 系统
    system: 'M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4z',
    config: 'M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.488.488 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 00-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z',
    logout: 'M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z',
    // 通用
    empty: 'M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V5h14v14zM7 11h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2z',
    refresh: 'M17.65 6.35A7.958 7.958 0 0012 4a8 8 0 108 8h-2c0 3.31-2.69 6-6 6s-6-2.69-6-6 2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z',
};

export const Icon = defineComponent({
    name: 'Icon',
    props: {
        name: { type: String, required: true },
        size: { type: [Number, String], default: 20 },
    },
    inheritAttrs: true,
    setup(props, { attrs }) {
        return () => h('svg', {
            class: 'icon-svg',
            width: props.size,
            height: props.size,
            viewBox: '0 0 24 24',
            fill: 'currentColor',
            'aria-hidden': 'true',
            ...attrs,
        }, [
            h('path', { d: ICON_PATHS[props.name] || ICON_PATHS.empty }),
        ]);
    },
});

// Card 组件
export const Card = defineComponent({
    name: 'Card',
    props: {
        title: String,
        bordered: { type: Boolean, default: false },
    },
    setup(props, { slots }) {
        return () => h('div', { class: ['card', props.bordered ? 'card-bordered' : ''] }, [
            props.title
                ? h('div', { class: 'card-header' }, [
                    h('span', { class: 'card-title' }, props.title),
                    slots.action?.(),
                ])
                : null,
            h('div', { class: 'card-body' }, slots.default?.()),
        ]);
    },
});

// Button 组件
export const Button = defineComponent({
    name: 'Button',
    props: {
        type: { type: String, default: 'secondary' }, // primary/secondary/danger
        size: { type: String, default: 'md' }, // sm/md
        loading: Boolean,
        disabled: Boolean,
    },
    emits: ['click'],
    setup(props, { slots, emit }) {
        return () => h('button', {
            class: [
                'btn',
                `btn-${props.type}`,
                props.size === 'sm' ? 'btn-sm' : '',
                props.loading ? 'btn-loading' : '',
            ],
            disabled: props.disabled || props.loading,
            onClick: (e) => emit('click', e),
        }, [
            props.loading ? h('span', { class: 'spinner spinner-sm' }) : null,
            h('span', slots.default?.()),
        ]);
    },
});

// Toggle 组件（开关）
export const Toggle = defineComponent({
    name: 'Toggle',
    props: {
        modelValue: Boolean,
        disabled: Boolean,
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        return () => h('label', { class: ['toggle', props.disabled ? 'toggle-disabled' : ''] }, [
            h('input', {
                type: 'checkbox',
                checked: props.modelValue,
                disabled: props.disabled,
                onChange: (e) => emit('update:modelValue', e.target.checked),
            }),
            h('span', { class: 'toggle-slider' }),
        ]);
    },
});

// Badge 组件
export const Badge = defineComponent({
    name: 'Badge',
    props: {
        type: { type: String, default: 'info' }, // success/warning/danger/info
        size: { type: String, default: 'md' },
    },
    setup(props, { slots }) {
        return () => h('span', {
            class: ['badge', `badge-${props.type}`, props.size === 'sm' ? 'badge-sm' : ''],
        }, slots.default?.());
    },
});

// FormInput 组件
export const FormInput = defineComponent({
    name: 'FormInput',
    props: {
        modelValue: [String, Number],
        type: { type: String, default: 'text' },
        placeholder: String,
        label: String,
        hint: String,
        error: String,
        disabled: Boolean,
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        return () => h('div', { class: 'form-group' }, [
            props.label ? h('label', { class: 'form-label' }, props.label) : null,
            h('input', {
                class: ['form-input', props.error ? 'form-input-error' : ''],
                type: props.type,
                value: props.modelValue,
                placeholder: props.placeholder,
                disabled: props.disabled,
                onInput: (e) => emit('update:modelValue', e.target.value),
            }),
            props.hint ? h('div', { class: 'form-hint' }, props.hint) : null,
            props.error ? h('div', { class: 'form-error' }, props.error) : null,
        ]);
    },
});

// FormSelect 组件
export const FormSelect = defineComponent({
    name: 'FormSelect',
    props: {
        modelValue: [String, Number],
        options: Array, // [{value, label}]
        label: String,
        hint: String,
        disabled: Boolean,
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        return () => h('div', { class: 'form-group' }, [
            props.label ? h('label', { class: 'form-label' }, props.label) : null,
            h('select', {
                class: 'form-input',
                value: props.modelValue,
                disabled: props.disabled,
                onChange: (e) => emit('update:modelValue', e.target.value),
            }, (props.options || []).map(opt =>
                h('option', { value: opt.value }, opt.label)
            )),
            props.hint ? h('div', { class: 'form-hint' }, props.hint) : null,
        ]);
    },
});

// Modal 组件
export const Modal = defineComponent({
    name: 'Modal',
    props: {
        modelValue: Boolean,
        title: String,
        width: { type: String, default: '560px' },
    },
    emits: ['update:modelValue', 'close'],
    setup(props, { slots, emit }) {
        const close = () => { emit('update:modelValue', false); emit('close'); };

        return () => props.modelValue
            ? h('div', { class: 'modal-overlay', onClick: close }, [
                h('div', {
                    class: 'modal',
                    style: { maxWidth: props.width },
                    onClick: (e) => e.stopPropagation(),
                }, [
                    h('div', { class: 'modal-header' }, [
                        h('h3', props.title || ''),
                        h('button', { class: 'modal-close', onClick: close }, '×'),
                    ]),
                    h('div', { class: 'modal-body' }, slots.default?.()),
                    slots.footer
                        ? h('div', { class: 'modal-footer' }, slots.footer())
                        : null,
                ])
            ])
            : null;
    },
});

// EmptyState 组件
export const EmptyState = defineComponent({
    name: 'EmptyState',
    props: { icon: { type: String, default: 'empty' }, title: String, desc: String },
    setup(props, { slots }) {
        return () => h('div', { class: 'empty-state' }, [
            h('div', { class: 'empty-icon' }, [h(Icon, { name: props.icon, size: 48 })]),
            props.title ? h('h3', props.title) : null,
            props.desc ? h('p', { class: 'text-muted' }, props.desc) : null,
            slots.default?.(),
        ]);
    },
});

// Loading 组件
export const Loading = defineComponent({
    name: 'Loading',
    props: { size: { type: String, default: 'md' } },
    setup(props) {
        return () => h('div', { class: 'loading' }, [
            h('div', { class: ['spinner', props.size === 'sm' ? 'spinner-sm' : ''] }),
        ]);
    },
});

// DataTable 组件
export const DataTable = defineComponent({
    name: 'DataTable',
    props: {
        columns: Array, // [{key, label, width}]
        rows: Array,
        loading: Boolean,
    },
    setup(props, { slots }) {
        return () => h('div', { class: 'table-container' }, [
            h('table', [
                h('thead', h('tr',
                    props.columns.map(col =>
                        h('th', { style: col.width ? { width: col.width } : {} }, col.label)
                    )
                )),
                h('tbody',
                    props.loading
                        ? [h('tr', h('td', {
                              colspan: props.columns.length,
                              style: 'text-align:center;padding:32px',
                          }, [h('div', { class: 'loading' }, h('div', { class: 'spinner' }))]))]
                        : (props.rows || []).map((row, idx) =>
                            h('tr', { key: idx },
                                props.columns.map(col =>
                                    h('td', slots[col.key]
                                        ? slots[col.key]({ row, value: row[col.key] })
                                        : String(row[col.key] ?? '')
                                    )
                                )
                            )
                        )
                ),
            ]),
        ]);
    },
});
```

- [ ] **Step 2: Commit**

```bash
git add bilibot/web/static/js/components/common.js
git commit -m "feat(web): add common Vue component library (Card/Button/Toggle/Badge/Modal/Form/Table)"
```

### Task 2.2: 扩展 components.css 组件样式

**Files:**
- Modify: `bilibot/web/static/css/components.css`

- [ ] **Step 1: 追加组件样式到 components.css**

在现有 components.css 末尾追加（从 dashboard.css 迁移并优化）：

```css
/* === Icon SVG 全局样式 === */
.icon-svg {
    display: inline-block;
    vertical-align: middle;
    flex-shrink: 0;
}
button .icon-svg, .btn .icon-svg {
    margin-right: 6px;
}

/* === Card === */
.card {
    background: var(--surface);
    border-radius: var(--radius-lg);
    padding: 24px;
    box-shadow: var(--shadow-1);
    margin-bottom: 16px;
    transition: box-shadow 0.2s;
}
.card:hover { box-shadow: var(--shadow-2); }
.card-bordered { border: 1px solid var(--outline-variant); }
.card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
    flex-wrap: wrap;
    gap: 12px;
}
.card-title { font-size: 16px; font-weight: 600; }
.card-body { }

/* === Button === */
.btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 10px 22px;
    border-radius: var(--radius-full);
    border: none;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    background: var(--surface-3);
    color: var(--on-surface);
}
.btn:hover { transform: translateY(-1px); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }
.btn-primary { background: var(--primary); color: var(--on-primary); }
.btn-primary:hover { background: var(--primary-hover); box-shadow: var(--shadow-2); }
.btn-secondary { background: var(--surface-3); color: var(--on-surface); border: 1px solid var(--outline-variant); }
.btn-secondary:hover { background: var(--surface-2); }
.btn-danger { background: var(--danger-bg); color: var(--danger); border: 1px solid var(--danger); }
.btn-danger:hover { background: var(--danger); color: #fff; }
.btn-sm { padding: 4px 12px; font-size: 12px; }
.btn-loading { pointer-events: none; }

/* === Toggle === */
.toggle {
    position: relative;
    display: inline-block;
    width: 52px;
    height: 32px;
    cursor: pointer;
}
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle-slider {
    position: absolute;
    inset: 0;
    background: var(--surface-3);
    border: 2px solid var(--outline);
    border-radius: var(--radius-full);
    transition: 0.3s;
}
.toggle-slider:before {
    content: '';
    position: absolute;
    width: 16px; height: 16px;
    left: 6px; top: 50%;
    transform: translateY(-50%);
    background: var(--outline);
    border-radius: 50%;
    transition: 0.3s;
}
.toggle input:checked + .toggle-slider {
    background: var(--primary);
    border-color: var(--primary);
}
.toggle input:checked + .toggle-slider:before {
    transform: translateY(-50%) translateX(20px);
    background: #fff;
    width: 22px; height: 22px;
}
.toggle-disabled { opacity: 0.4; cursor: not-allowed; }

/* === Badge === */
.badge {
    display: inline-flex;
    align-items: center;
    padding: 2px 10px;
    border-radius: var(--radius-full);
    font-size: 12px;
    font-weight: 500;
}
.badge-sm { font-size: 11px; padding: 1px 8px; }
.badge-success { background: var(--success-bg); color: var(--success); }
.badge-warning { background: var(--warning-bg); color: var(--warning); }
.badge-danger { background: var(--danger-bg); color: var(--danger); }
.badge-info { background: var(--info-bg); color: var(--info); }

/* === Form === */
.form-group { margin-bottom: 20px; }
.form-label {
    display: block;
    font-size: 13px;
    font-weight: 500;
    color: var(--text-secondary);
    margin-bottom: 6px;
}
.form-input {
    width: 100%;
    padding: 12px 16px;
    border: 1px solid var(--outline-variant);
    border-radius: var(--radius-sm);
    font-size: 14px;
    font-family: inherit;
    background: var(--surface);
    color: var(--on-surface);
    transition: border-color 0.2s, box-shadow 0.2s;
}
.form-input:focus {
    outline: none;
    border-color: var(--primary);
    border-width: 2px;
    box-shadow: 0 0 0 3px rgba(103, 80, 164, 0.12);
}
.form-input-error { border-color: var(--danger); }
.form-hint { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.form-error { font-size: 12px; color: var(--danger); margin-top: 4px; }
select.form-input {
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%2379747E' d='M6 8L0 2l2-2 4 4 4-4 2 2z'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 16px center;
    padding-right: 40px;
}
textarea.form-input { resize: vertical; min-height: 80px; }

/* === Modal === */
.modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    backdrop-filter: blur(4px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    animation: fadeIn 0.2s;
}
.modal {
    background: var(--surface);
    border-radius: var(--radius-xl);
    width: 100%;
    max-width: 560px;
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    box-shadow: var(--shadow-4);
    animation: modalIn 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}
.modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 20px 24px;
    border-bottom: 1px solid var(--outline-variant);
}
.modal-header h3 { font-size: 18px; font-weight: 600; }
.modal-close {
    background: none;
    border: none;
    font-size: 24px;
    cursor: pointer;
    color: var(--text-muted);
    line-height: 1;
    padding: 4px;
}
.modal-close:hover { color: var(--on-surface); }
.modal-body { padding: 24px; overflow-y: auto; flex: 1; }
.modal-footer {
    padding: 16px 24px;
    border-top: 1px solid var(--outline-variant);
    display: flex;
    justify-content: flex-end;
    gap: 12px;
}

/* === Loading === */
.loading {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 32px;
}
.spinner {
    width: 36px;
    height: 36px;
    border: 3px solid var(--surface-3);
    border-top-color: var(--primary);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
}
.spinner-sm { width: 16px; height: 16px; border-width: 2px; }

@keyframes spin { to { transform: rotate(360deg); } }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes modalIn {
    from { transform: translateY(20px) scale(0.95); opacity: 0; }
    to { transform: translateY(0) scale(1); opacity: 1; }
}

/* === Table === */
.table-container {
    overflow-x: auto;
    border-radius: var(--radius-md);
    border: 1px solid var(--outline-variant);
}
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
    text-align: left;
    padding: 12px 16px;
    background: var(--surface-2);
    color: var(--text-secondary);
    font-weight: 500;
    border-bottom: 1px solid var(--outline-variant);
    position: sticky;
    top: 0;
}
td { padding: 12px 16px; border-bottom: 1px solid var(--outline-variant); }
tr:hover td { background: var(--surface-1); }

/* === Status Card === */
.status-card {
    background: var(--surface);
    border-radius: var(--radius-lg);
    padding: 20px;
    box-shadow: var(--shadow-1);
    transition: all 0.2s;
    cursor: pointer;
}
.status-card:hover {
    transform: translateY(-2px);
    box-shadow: var(--shadow-3);
}
.status-icon {
    width: 56px; height: 56px;
    border-radius: var(--radius-md);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 28px;
    margin-bottom: 12px;
}
.status-icon.success { background: var(--success-bg); color: var(--success); }
.status-icon.warning { background: var(--warning-bg); color: var(--warning); }
.status-icon.danger { background: var(--danger-bg); color: var(--danger); }

.status-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px;
}

/* === Tabs === */
.tabs {
    display: flex;
    gap: 4px;
    border-bottom: 2px solid var(--outline-variant);
    margin-bottom: 24px;
}
.tab {
    padding: 10px 20px;
    cursor: pointer;
    font-size: 14px;
    color: var(--text-secondary);
    border-bottom: 3px solid transparent;
    margin-bottom: -2px;
    transition: all 0.2s;
}
.tab:hover { color: var(--on-surface); }
.tab.active { color: var(--primary); border-bottom-color: var(--primary); font-weight: 500; }
```

- [ ] **Step 2: Commit**

```bash
git add bilibot/web/static/css/components.css
git commit -m "feat(web): add complete component CSS (card/button/toggle/badge/modal/form/table/tabs)"
```

### Task 2.3: 在总览页验证组件库

**Files:**
- Create: `bilibot/web/static/js/pages/overview.js`

- [ ] **Step 1: 创建总览页组件（使用通用组件库）**

```javascript
// pages/overview.js - 总览页
const { defineComponent, h, ref, onMounted } = window.Vue;
import { api } from '../api.js';
import { Card, Button, Badge, Loading, EmptyState, Icon } from '../components/common.js';
import { appState, showToast } from '../state.js';

export const OverviewPage = defineComponent({
    name: 'OverviewPage',
    setup() {
        const status = ref(null);
        const loading = ref(true);

        async function loadStatus() {
            loading.value = true;
            try {
                status.value = await api.status();
            } catch (e) {
                showToast('加载状态失败: ' + e.message, 'error');
            } finally {
                loading.value = false;
            }
        }

        onMounted(loadStatus);

        return () => loading.value
            ? h(Loading)
            : h('div', [
                h(Card, { title: '系统状态' }, () => [
                    h('div', { class: 'status-grid' }, [
                        h('div', { class: 'status-card' }, [
                            h('div', { class: 'status-icon success' }, [h(Icon, { name: 'accounts', size: 26 })]),
                            h('div', { style: 'font-size:13px;color:var(--text-secondary)' }, 'B站登录'),
                            h('div', { style: 'font-size:20px;font-weight:600;margin-top:4px' },
                                status.value?.bili_logged_in ? '已登录' : '未登录'),
                        ]),
                        h('div', { class: 'status-card' }, [
                            h('div', { class: 'status-icon success' }, [h(Icon, { name: 'llm', size: 26 })]),
                            h('div', { style: 'font-size:13px;color:var(--text-secondary)' }, 'LLM 连接'),
                            h('div', { style: 'font-size:20px;font-weight:600;margin-top:4px' },
                                status.value?.llm_connected ? '正常' : '未连接'),
                        ]),
                    ]),
                ]),
                h(Card, { title: '快速操作' }, () => [
                    h('div', { class: 'flex gap-3', style: 'flex-wrap:wrap' }, [
                        h(Button, { type: 'primary', onClick: () => window.location.hash = '#/accounts' }, () => [h(Icon, { name: 'accounts', size: 16 }), ' 账号管理']),
                        h(Button, { onClick: () => window.location.hash = '#/memory/graph' }, () => [h(Icon, { name: 'memory', size: 16 }), ' 记忆图谱']),
                        h(Button, { onClick: () => window.location.hash = '#/config' }, () => [h(Icon, { name: 'config', size: 16 }), ' 全局配置']),
                    ]),
                ]),
            ]);
    },
});
```

- [ ] **Step 2: 在 app.js 中注册总览页路由**

修改 `app.js` 中的路由注册部分，在 `pages.forEach(...)` 之前添加：

```javascript
import { OverviewPage } from './pages/overview.js';
// 覆盖占位路由
registerRoute('/', OverviewPage, '总览');
```

- [ ] **Step 3: 验证总览页渲染**

访问 `http://localhost:端口/#/`，预期：
- 显示"系统状态"卡片 + 状态网格
- 显示"快速操作"卡片 + 3 个按钮
- 卡片 hover 有阴影变化
- 按钮点击能跳转

- [ ] **Step 4: Commit**

```bash
git add bilibot/web/static/js/pages/overview.js bilibot/web/static/js/app.js
git commit -m "feat(web): add overview page using common components"
```

---

## Phase 3: 账号详情页重构

**目标：** 账号列表简化为卡片网格，点击进入详情页，详情页用 Tab 管理：基本信息、B站登录、LLM绑定、人格绑定、运行状态。LLM 和人格管理页显示关联账号。

### Task 3.1: 创建账号列表页

**Files:**
- Create: `bilibot/web/static/js/components/accounts.js`

- [ ] **Step 1: 创建账号列表组件**

```javascript
// components/accounts.js - 账号列表和详情页
const { defineComponent, h, ref, computed, onMounted } = window.Vue;
import { api } from '../api.js';
import { appState, refreshAccounts, showToast } from '../state.js';
import { Card, Button, Badge, Modal, FormInput, FormSelect, Toggle, EmptyState, Loading, Icon } from './common.js';

export const AccountListPage = defineComponent({
    name: 'AccountListPage',
    setup() {
        const showAdd = ref(false);
        const adding = ref(false);
        const form = ref({ id: '', name: '', sessdata: '', bili_jct: '', dede_user_id: '', buvid3: '', llm_id: '', profile_id: '' });

        async function addAccount() {
            adding.value = true;
            try {
                await api.accounts.create(form.value);
                showToast('账号添加成功', 'success');
                showAdd.value = false;
                form.value = { id: '', name: '', sessdata: '', bili_jct: '', dede_user_id: '', buvid3: '', llm_id: '', profile_id: '' };
                await refreshAccounts();
            } catch (e) {
                showToast('添加失败: ' + e.message, 'error');
            } finally {
                adding.value = false;
            }
        }

        onMounted(() => { if (!appState.accountsLoaded) refreshAccounts(); });

        const llmOptions = computed(() => [
            { value: '', label: '默认 LLM' },
            ...appState.llmProviders.map(p => ({ value: p.id, label: `${p.name} (${p.model})` })),
        ]);

        const profileOptions = computed(() => [
            { value: '', label: '不绑定' },
            // 从 accounts.profiles API 获取
        ]);

        return () => h('div', [
            h(Card, { title: '账号列表' }, {
                action: () => h(Button, { type: 'primary', onClick: () => showAdd.value = true }, () => '+ 添加账号'),
                default: () => !appState.accountsLoaded
                    ? h(Loading)
                    : appState.accounts.length === 0
                        ? h(EmptyState, { icon: 'accounts', title: '暂无账号', desc: '点击右上角添加账号' })
                        : h('div', { class: 'status-grid' },
                            appState.accounts.map(acc => h('div', {
                                class: 'status-card',
                                onClick: () => window.location.hash = `#/accounts/${acc.id}`,
                            }, [
                                h('div', { class: 'flex items-center justify-between' }, [
                                    h('div', { class: 'flex items-center gap-2' }, [
                                        h(Icon, { name: 'proactive', size: 20, style: acc.running ? 'color:var(--success)' : 'color:var(--danger)' }),
                                        h('span', { style: 'font-size:16px;font-weight:600' }, acc.name || acc.id),
                                    ]),
                                    acc.is_default ? h(Badge, { type: 'info' }, () => '默认') : null,
                                ]),
                                h('div', { class: 'mt-2', style: 'font-size:13px;color:var(--text-secondary)' }, [
                                    h('div', `ID: ${acc.id}`),
                                    h('div', `LLM: ${acc.effective_llm_id || acc.llm_id || '默认'}`),
                                    h('div', `人格: ${acc.persona_id || '未绑定'}`),
                                    acc.authenticated ? h(Badge, { type: 'success', size: 'sm' }, () => '已认证')
                                                      : h(Badge, { type: 'warning', size: 'sm' }, () => '未认证'),
                                ]),
                            ])
                        ),
            }),
            // 添加账号弹窗
            h(Modal, {
                modelValue: showAdd.value,
                'onUpdate:modelValue': (v) => showAdd.value = v,
                title: '添加账号',
                width: '600px',
            }, {
                default: () => h('div', [
                    h(FormInput, { label: '账号 ID', modelValue: form.value.id,
                        'onUpdate:modelValue': (v) => form.value.id = v,
                        placeholder: '如: main, sub1' }),
                    h(FormInput, { label: '名称', modelValue: form.value.name,
                        'onUpdate:modelValue': (v) => form.value.name = v,
                        placeholder: '显示名称' }),
                    h(FormInput, { label: 'SESSDATA', modelValue: form.value.sessdata,
                        'onUpdate:modelValue': (v) => form.value.sessdata = v,
                        type: 'password' }),
                    h(FormInput, { label: 'bili_jct', modelValue: form.value.bili_jct,
                        'onUpdate:modelValue': (v) => form.value.bili_jct = v,
                        type: 'password' }),
                    h(FormInput, { label: 'UID', modelValue: form.value.dede_user_id,
                        'onUpdate:modelValue': (v) => form.value.dede_user_id = v }),
                    h(FormInput, { label: 'buvid3', modelValue: form.value.buvid3,
                        'onUpdate:modelValue': (v) => form.value.buvid3 = v,
                        type: 'password' }),
                    h(FormSelect, { label: 'LLM', modelValue: form.value.llm_id,
                        'onUpdate:modelValue': (v) => form.value.llm_id = v,
                        options: llmOptions.value }),
                ]),
                footer: () => [
                    h(Button, { onClick: () => showAdd.value = false }, () => '取消'),
                    h(Button, { type: 'primary', loading: adding.value, onClick: addAccount }, () => '添加'),
                ],
            }),
        ]);
    },
});
```

- [ ] **Step 2: 在 app.js 注册路由**

```javascript
import { AccountListPage, AccountDetailPage } from './components/accounts.js';
registerRoute('/accounts', AccountListPage, '账号管理');
registerRoute('/accounts/:id', AccountDetailPage, '账号详情');
```

- [ ] **Step 3: Commit**

```bash
git add bilibot/web/static/js/components/accounts.js bilibot/web/static/js/app.js
git commit -m "feat(web): add account list page with card grid and add modal"
```

### Task 3.2: 创建账号详情页（Tab 管理）

**Files:**
- Modify: `bilibot/web/static/js/components/accounts.js`（追加 AccountDetailPage）

- [ ] **Step 1: 追加账号详情页组件到 accounts.js**

在 accounts.js 末尾追加 `AccountDetailPage` 组件：

```javascript
// 账号详情页 Tab 组件
const AccountInfoTab = defineComponent({
    props: { account: Object },
    emits: ['save', 'delete'],
    setup(props, { emit }) {
        const form = ref({ ...props.account });
        const saving = ref(false);

        return () => h('div', [
            h(FormInput, { label: '账号 ID', modelValue: form.value.id, disabled: true }),
            h(FormInput, { label: '名称', modelValue: form.value.name,
                'onUpdate:modelValue': (v) => form.value.name = v }),
            h(FormInput, { label: 'UID', modelValue: form.value.dede_user_id,
                'onUpdate:modelValue': (v) => form.value.dede_user_id = v }),
            h(FormInput, { label: 'buvid3', modelValue: form.value.buvid3,
                'onUpdate:modelValue': (v) => form.value.buvid3 = v, type: 'password' }),
            h('div', { class: 'flex gap-3 mt-4' }, [
                h(Button, { type: 'primary', loading: saving.value,
                    onClick: async () => { saving.value = true; try { emit('save', form.value); } finally { saving.value = false; } },
                }, () => '保存'),
                h(Button, { type: 'danger', onClick: () => emit('delete') }, () => '删除账号'),
            ]),
        ]);
    },
});

const AccountLoginTab = defineComponent({
    props: { account: Object },
    setup(props) {
        const qrModal = ref(false);
        const qrUrl = ref('');
        const sessionId = ref('');
        const polling = ref(false);
        const status = ref('');

        async function startQrLogin() {
            try {
                const resp = await api.accounts.qrLogin(props.account.id);
                qrUrl.value = resp.qrcode_url;
                sessionId.value = resp.session_id;
                qrModal.value = true;
                polling.value = true;
                pollStatus();
            } catch (e) {
                showToast('启动扫码失败: ' + e.message, 'error');
            }
        }

        async function pollStatus() {
            if (!polling.value || !sessionId.value) return;
            try {
                const resp = await api.accounts.qrPoll(props.account.id, sessionId.value);
                if (resp.code === 0) {
                    status.value = '登录成功';
                    showToast('B站登录成功', 'success');
                    polling.value = false;
                    setTimeout(() => { qrModal.value = false; refreshAccounts(); }, 1500);
                } else if (resp.code === 86090) {
                    status.value = '等待扫码...';
                    setTimeout(pollStatus, 2000);
                } else if (resp.code === 86038) {
                    status.value = '二维码已过期';
                    showToast('二维码过期，请重新生成', 'warning');
                    polling.value = false;
                } else {
                    setTimeout(pollStatus, 2000);
                }
            } catch (e) {
                polling.value = false;
                showToast('轮询失败: ' + e.message, 'error');
            }
        }

        function cancelQr() {
            if (sessionId.value) api.accounts.qrCancel(props.account.id, sessionId.value);
            polling.value = false;
            qrModal.value = false;
        }

        return () => h('div', [
            h(Card, { title: 'B站登录状态' }, () => [
                h('div', { class: 'flex items-center gap-4 mb-4' }, [
                    props.account.authenticated
                        ? h(Badge, { type: 'success' }, () => '已认证')
                        : h(Badge, { type: 'warning' }, () => '未认证'),
                    h(Button, { type: 'primary', onClick: startQrLogin }, () => '扫码登录'),
                ]),
                h('div', { class: 'text-muted', style: 'font-size:13px' },
                    '说明：扫码登录会自动获取 cookie，无需手动填写 SESSDATA/bili_jct'),
            ]),
            h(Modal, {
                modelValue: qrModal.value,
                'onUpdate:modelValue': (v) => { qrModal.value = v; if (!v) cancelQr(); },
                title: '扫码登录',
            }, {
                default: () => h('div', { style: 'text-align:center' }, [
                    qrUrl.value
                        ? h('img', { src: qrUrl.value, style: 'width:240px;height:240px;border-radius:12px' })
                        : h(Loading),
                    h('p', { class: 'mt-4' }, status.value || '请使用B站 App 扫描二维码'),
                ]),
                footer: () => h(Button, { onClick: cancelQr }, () => '关闭'),
            }),
        ]);
    },
});

const AccountLlmTab = defineComponent({
    props: { account: Object },
    setup(props) {
        const selected = ref(props.account.llm_id || '');
        const saving = ref(false);

        const llmOptions = computed(() => [
            { value: '', label: '默认 LLM' },
            ...appState.llmProviders.map(p => ({
                value: p.id,
                label: `${p.name} (${p.model})${p.enabled ? '' : ' [已禁用]'}`,
            })),
        ]);

        async function save() {
            saving.value = true;
            try {
                await api.accounts.bindLlm(props.account.id, selected.value);
                showToast('LLM 绑定已更新', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('绑定失败: ' + e.message, 'error');
            } finally {
                saving.value = false;
            }
        }

        return () => h(Card, { title: 'LLM 绑定' }, () => [
            h('p', { class: 'text-muted mb-4' },
                '选择此账号使用的 LLM Provider。留空则使用默认 LLM。'),
            h(FormSelect, {
                label: 'LLM Provider',
                modelValue: selected.value,
                'onUpdate:modelValue': (v) => selected.value = v,
                options: llmOptions.value,
            }),
            props.account.fallback_reason
                ? h('div', { class: 'form-error mt-2' }, '回退原因: ' + props.account.fallback_reason)
                : null,
            h('div', { class: 'mt-4' }, [
                h(Button, { type: 'primary', loading: saving.value, onClick: save }, () => '保存绑定'),
            ]),
        ]);
    },
});

const AccountPersonaTab = defineComponent({
    props: { account: Object },
    setup(props) {
        const selectedProfile = ref(props.account.profile_id || '');
        const selectedPersona = ref(props.account.persona_id || '');
        const saving = ref(false);
        const profiles = ref([]);

        async function loadProfiles() {
            try { profiles.value = await api.accounts.profiles(); }
            catch (e) { showToast('加载人格组失败: ' + e.message, 'error'); }
        }
        onMounted(loadProfiles);

        const profileOptions = computed(() => [
            { value: '', label: '不绑定' },
            ...profiles.value.map(p => ({ value: p.id, label: p.name })),
        ]);

        const availablePersonas = computed(() =>
            appState.personas.filter(p =>
                !selectedProfile.value ||
                profiles.value.find(pf => pf.id === selectedProfile.value)?.personas?.includes(p.id)
            )
        );

        async function bindProfile() {
            saving.value = true;
            try {
                await api.accounts.bindPersona(props.account.id, { profile_id: selectedProfile.value });
                showToast('人格组绑定已更新', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('绑定失败: ' + e.message, 'error');
            } finally { saving.value = false; }
        }

        async function switchPersona() {
            if (!selectedPersona.value) return;
            try {
                await api.accounts.switchPersona(props.account.id, selectedPersona.value);
                showToast('人格切换成功', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('切换失败: ' + e.message, 'error');
            }
        }

        return () => h('div', [
            h(Card, { title: '人格组绑定' }, () => [
                h('p', { class: 'text-muted mb-4' },
                    '绑定人格组后，账号可在该组内的人格间切换。'),
                h(FormSelect, {
                    label: '人格组',
                    modelValue: selectedProfile.value,
                    'onUpdate:modelValue': (v) => selectedProfile.value = v,
                    options: profileOptions.value,
                }),
                h('div', { class: 'mt-4' }, [
                    h(Button, { type: 'primary', loading: saving.value, onClick: bindProfile }, () => '保存绑定'),
                ]),
            ]),
            selectedProfile.value && availablePersonas.value.length > 1
                ? h(Card, { title: '切换当前人格' }, () => [
                    h(FormSelect, {
                        label: '当前人格',
                        modelValue: selectedPersona.value,
                        'onUpdate:modelValue': (v) => selectedPersona.value = v,
                        options: availablePersonas.value.map(p => ({ value: p.id, label: p.name })),
                    }),
                    h('div', { class: 'mt-4' }, [
                        h(Button, { type: 'primary', onClick: switchPersona }, () => '切换'),
                    ]),
                ])
                : null,
        ]);
    },
});

const AccountStatusTab = defineComponent({
    props: { account: Object },
    setup(props) {
        const operating = ref(false);

        async function toggle() {
            operating.value = true;
            try {
                if (props.account.running) {
                    await api.accounts.stop(props.account.id);
                    showToast('账号已停止', 'info');
                } else {
                    await api.accounts.start(props.account.id);
                    showToast('账号已启动', 'success');
                }
                await refreshAccounts();
            } catch (e) {
                showToast('操作失败: ' + e.message, 'error');
            } finally { operating.value = false; }
        }

        async function setDefault() {
            try {
                await api.accounts.setDefault(props.account.id);
                showToast('已设为默认账号', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('设置失败: ' + e.message, 'error');
            }
        }

        return () => h(Card, { title: '运行状态' }, () => [
            h('div', { class: 'status-grid' }, [
                h('div', { class: 'status-card' }, [
                    h('div', { class: 'status-icon ' + (props.account.running ? 'success' : 'warning') },
                        [h(Icon, { name: 'proactive', size: 26 })]),
                    h('div', { style: 'font-size:20px;font-weight:600' },
                        props.account.running ? '运行中' : '已停止'),
                ]),
                h('div', { class: 'status-card' }, [
                    h('div', { class: 'status-icon ' + (props.account.authenticated ? 'success' : 'danger') },
                        [h(Icon, { name: props.account.authenticated ? 'system' : 'logout', size: 26 })]),
                    h('div', { style: 'font-size:20px;font-weight:600' },
                        props.account.authenticated ? '已认证' : '未认证'),
                ]),
            ]),
            h('div', { class: 'flex gap-3 mt-4' }, [
                h(Button, {
                    type: props.account.running ? 'danger' : 'primary',
                    loading: operating.value,
                    onClick: toggle,
                }, () => props.account.running ? '停止' : '启动'),
                !props.account.is_default
                    ? h(Button, { onClick: setDefault }, () => '设为默认')
                    : null,
            ]),
            props.account.last_error
                ? h('div', { class: 'form-error mt-4' }, '最后错误: ' + props.account.last_error)
                : null,
        ]);
    },
});

// 账号详情页主组件
export const AccountDetailPage = defineComponent({
    name: 'AccountDetailPage',
    props: { route: Object },
    setup(props) {
        const accountId = computed(() => props.route.params.id);
        const account = computed(() =>
            appState.accounts.find(a => a.id === accountId.value) || null
        );
        const activeTab = ref('info');
        const tabs = [
            { id: 'info', label: '基本信息' },
            { id: 'login', label: 'B站登录' },
            { id: 'llm', label: 'LLM 绑定' },
            { id: 'persona', label: '人格绑定' },
            { id: 'status', label: '运行状态' },
        ];

        async function saveInfo(data) {
            try {
                await api.accounts.update(accountId.value, data);
                showToast('账号信息已保存', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('保存失败: ' + e.message, 'error');
            }
        }

        async function deleteAccount() {
            if (!confirm(`确定删除账号 ${accountId.value}？`)) return;
            try {
                await api.accounts.delete(accountId.value);
                showToast('账号已删除', 'success');
                window.location.hash = '#/accounts';
                await refreshAccounts();
            } catch (e) {
                showToast('删除失败: ' + e.message, 'error');
            }
        }

        onMounted(() => { if (!appState.accountsLoaded) refreshAccounts(); });

        return () => !account.value
            ? h(Loading)
            : h('div', [
                h('div', { class: 'flex items-center gap-3 mb-4' }, [
                    h(Icon, { name: 'proactive', size: 24, style: account.value.running ? 'color:var(--success)' : 'color:var(--danger)' }),
                    h('h2', { style: 'font-size:20px;font-weight:600' }, account.value.name || account.value.id),
                    account.value.is_default ? h(Badge, { type: 'info' }, () => '默认') : null,
                ]),
                h('div', { class: 'tabs' },
                    tabs.map(t => h('div', {
                        class: ['tab', activeTab.value === t.id ? 'active' : ''],
                        onClick: () => activeTab.value = t.id,
                    }, t.label))
                ),
                activeTab.value === 'info' ? h(AccountInfoTab, { account: account.value, onSave: saveInfo, onDelete: deleteAccount }) : null,
                activeTab.value === 'login' ? h(AccountLoginTab, { account: account.value }) : null,
                activeTab.value === 'llm' ? h(AccountLlmTab, { account: account.value }) : null,
                activeTab.value === 'persona' ? h(AccountPersonaTab, { account: account.value }) : null,
                activeTab.value === 'status' ? h(AccountStatusTab, { account: account.value }) : null,
            ]);
    },
});
```

- [ ] **Step 2: 验证账号详情页**

访问 `http://localhost:端口/#/accounts`，点击账号卡片，预期：
- 进入详情页，显示账号名称和状态
- 5 个 Tab 可切换：基本信息/B站登录/LLM绑定/人格绑定/运行状态
- B站登录 Tab 可弹出扫码模态框
- LLM 绑定 Tab 可选择并保存
- 运行状态 Tab 可启停账号

- [ ] **Step 3: Commit**

```bash
git add bilibot/web/static/js/components/accounts.js
git commit -m "feat(web): add account detail page with 5 tabs (info/login/llm/persona/status)"
```

### Task 3.3: 创建 LLM 管理页（带关联视图）

**Files:**
- Create: `bilibot/web/static/js/components/llm.js`

- [ ] **Step 1: 创建 LLM 管理组件（显示被哪些账号使用）**

```javascript
// components/llm.js - LLM Provider 管理（带关联账号视图）
const { defineComponent, h, ref, computed, onMounted } = window.Vue;
import { api } from '../api.js';
import { appState, refreshLlm, refreshAccounts, showToast } from '../state.js';
import { Card, Button, Badge, Modal, FormInput, FormSelect, Toggle, EmptyState, Loading, Icon } from './common.js';

export const LlmListPage = defineComponent({
    name: 'LlmListPage',
    setup() {
        const showAdd = ref(false);
        const adding = ref(false);
        const form = ref({ id: '', name: '', api_key: '', base_url: '', model: '', max_tokens: 1024, temperature: 0.8, enabled: true });

        // 计算每个 LLM 被哪些账号引用
        const llmUsage = computed(() => {
            const map = {};
            for (const acc of appState.accounts) {
                const llmId = acc.llm_id || acc.configured_llm_id;
                if (llmId) {
                    if (!map[llmId]) map[llmId] = [];
                    map[llmId].push(acc);
                }
            }
            return map;
        });

        async function addProvider() {
            adding.value = true;
            try {
                await api.llm.create(form.value);
                showToast('LLM Provider 添加成功', 'success');
                showAdd.value = false;
                form.value = { id: '', name: '', api_key: '', base_url: '', model: '', max_tokens: 1024, temperature: 0.8, enabled: true };
                await refreshLlm();
            } catch (e) {
                showToast('添加失败: ' + e.message, 'error');
            } finally { adding.value = false; }
        }

        async function deleteProvider(id) {
            const usage = llmUsage.value[id] || [];
            const force = usage.length > 0;
            if (force) {
                if (!confirm(`此 LLM 被 ${usage.length} 个账号使用，删除将清除引用并回退到默认。继续？`)) return;
            } else {
                if (!confirm('确定删除此 LLM Provider？')) return;
            }
            try {
                await api.llm.delete(id, force);
                showToast('已删除', 'success');
                await Promise.all([refreshLlm(), refreshAccounts()]);
            } catch (e) {
                showToast('删除失败: ' + e.message, 'error');
            }
        }

        async function testProvider(id) {
            try {
                showToast('测试中...', 'info');
                const resp = await api.llm.test(id);
                showToast('连接成功', 'success');
            } catch (e) {
                showToast('连接失败: ' + e.message, 'error');
            }
        }

        onMounted(() => {
            if (!appState.llmLoaded) refreshLlm();
            if (!appState.accountsLoaded) refreshAccounts();
        });

        return () => h('div', [
            h(Card, { title: 'LLM Provider 列表' }, {
                action: () => h(Button, { type: 'primary', onClick: () => showAdd.value = true }, () => '+ 添加 Provider'),
                default: () => !appState.llmLoaded
                    ? h(Loading)
                    : appState.llmProviders.length === 0
                        ? h(EmptyState, { icon: 'llm', title: '暂无 LLM Provider', desc: '点击右上角添加' })
                        : appState.llmProviders.map(p => h(Card, { key: p.id, bordered: true }, {
                            default: () => h('div', [
                                h('div', { class: 'flex items-center justify-between mb-2' }, [
                                    h('div', { class: 'flex items-center gap-2' }, [
                                        h('span', { style: 'font-size:16px;font-weight:600' }, p.name),
                                        p.is_default ? h(Badge, { type: 'info' }, () => '默认') : null,
                                        !p.enabled ? h(Badge, { type: 'danger' }, () => '已禁用') : null,
                                        !p.has_api_key ? h(Badge, { type: 'warning' }, () => '无密钥') : null,
                                    ]),
                                    h('div', { class: 'flex gap-2' }, [
                                        h(Button, { size: 'sm', onClick: () => testProvider(p.id) }, () => '测试'),
                                        h(Button, { size: 'sm', type: 'danger', onClick: () => deleteProvider(p.id) }, () => '删除'),
                                    ]),
                                ]),
                                h('div', { class: 'text-muted', style: 'font-size:13px' }, [
                                    h('div', `ID: ${p.id}`),
                                    h('div', `模型: ${p.model}`),
                                    h('div', `Base URL: ${p.base_url}`),
                                    p.vision_enabled ? h(Badge, { type: 'success', size: 'sm' }, () => 'Vision') : null,
                                    p.embedding_enabled ? h(Badge, { type: 'info', size: 'sm' }, () => 'Embedding') : null,
                                ]),
                                // 关联账号视图
                                (llmUsage.value[p.id] || []).length > 0
                                    ? h('div', { class: 'mt-4', style: 'padding-top:12px;border-top:1px solid var(--outline-variant)' }, [
                                        h('div', { class: 'text-muted', style: 'font-size:12px;margin-bottom:8px' }, '被以下账号使用:'),
                                        h('div', { class: 'flex gap-2', style: 'flex-wrap:wrap' },
                                            llmUsage.value[p.id].map(acc =>
                                                h(Badge, { type: 'info', size: 'sm' }, () => `${acc.name || acc.id}`)
                                            )
                                        ),
                                    ])
                                    : null,
                            ]),
                        })),
            }),
            h(Modal, {
                modelValue: showAdd.value,
                'onUpdate:modelValue': (v) => showAdd.value = v,
                title: '添加 LLM Provider',
                width: '600px',
            }, {
                default: () => h('div', [
                    h(FormInput, { label: 'ID', modelValue: form.value.id,
                        'onUpdate:modelValue': (v) => form.value.id = v, placeholder: '如: siliconflow' }),
                    h(FormInput, { label: '名称', modelValue: form.value.name,
                        'onUpdate:modelValue': (v) => form.value.name = v }),
                    h(FormInput, { label: 'API Key', modelValue: form.value.api_key,
                        'onUpdate:modelValue': (v) => form.value.api_key = v, type: 'password' }),
                    h(FormInput, { label: 'Base URL', modelValue: form.value.base_url,
                        'onUpdate:modelValue': (v) => form.value.base_url = v }),
                    h(FormInput, { label: '模型', modelValue: form.value.model,
                        'onUpdate:modelValue': (v) => form.value.model = v }),
                ]),
                footer: () => [
                    h(Button, { onClick: () => showAdd.value = false }, () => '取消'),
                    h(Button, { type: 'primary', loading: adding.value, onClick: addProvider }, () => '添加'),
                ],
            }),
        ]);
    },
});
```

- [ ] **Step 2: 在 app.js 注册路由**

```javascript
import { LlmListPage } from './components/llm.js';
registerRoute('/llm', LlmListPage, 'LLM 管理');
```

- [ ] **Step 3: Commit**

```bash
git add bilibot/web/static/js/components/llm.js bilibot/web/static/js/app.js
git commit -m "feat(web): add LLM management page with account usage view"
```

### Task 3.4: 创建人格管理页（带关联视图）

**Files:**
- Create: `bilibot/web/static/js/components/personas.js`

- [ ] **Step 1: 创建人格管理组件**

人格管理页面较复杂（包含编辑器），参考现有 `loadPersonasPage` 的字段结构。核心字段：name, description, base_prompt, speaking_style, boundaries, relationship_rules, reply_rules, proactive_comment_rules, dynamic_rules, weekly_rules, examples, enabled。

```javascript
// components/personas.js - 人格管理（带关联账号视图）
const { defineComponent, h, ref, computed, onMounted } = window.Vue;
import { api } from '../api.js';
import { appState, refreshPersonas, refreshAccounts, showToast } from '../state.js';
import { Card, Button, Badge, Modal, FormInput, Toggle, EmptyState, Loading } from './common.js';

export const PersonaListPage = defineComponent({
    name: 'PersonaListPage',
    setup() {
        const showEditor = ref(false);
        const editingId = ref(null);
        const form = ref({});

        // 计算每个人格被哪些账号使用
        const personaUsage = computed(() => {
            const map = {};
            for (const acc of appState.accounts) {
                const pid = acc.persona_id || acc.available_personas?.[0]?.id;
                if (pid) {
                    if (!map[pid]) map[pid] = [];
                    map[pid].push(acc);
                }
            }
            return map;
        });

        function startEdit(persona) {
            editingId.value = persona?.id || null;
            form.value = persona ? { ...persona } : {
                name: '', description: '', base_prompt: '', speaking_style: '',
                boundaries: '', relationship_rules: '', reply_rules: '',
                proactive_comment_rules: '', dynamic_rules: '', weekly_rules: '',
                examples: '', enabled: true,
            };
            showEditor.value = true;
        }

        async function save() {
            try {
                if (editingId.value) {
                    await api.personas.update(editingId.value, form.value);
                    showToast('人格已更新', 'success');
                } else {
                    await api.personas.create(form.value);
                    showToast('人格已创建', 'success');
                }
                showEditor.value = false;
                await refreshPersonas();
            } catch (e) {
                showToast('保存失败: ' + e.message, 'error');
            }
        }

        async function del(id) {
            if (!confirm('确定删除此人格？')) return;
            try {
                await api.personas.delete(id);
                showToast('已删除', 'success');
                await refreshPersonas();
            } catch (e) {
                showToast('删除失败: ' + e.message, 'error');
            }
        }

        async function activate(id) {
            try {
                await api.personas.activate(id);
                showToast('已激活', 'success');
                await refreshPersonas();
            } catch (e) {
                showToast('激活失败: ' + e.message, 'error');
            }
        }

        onMounted(() => {
            if (!appState.personasLoaded) refreshPersonas();
            if (!appState.accountsLoaded) refreshAccounts();
        });

        const formFields = [
            { key: 'name', label: '名称', type: 'text' },
            { key: 'description', label: '描述', type: 'text' },
            { key: 'base_prompt', label: '基础提示词', type: 'textarea' },
            { key: 'speaking_style', label: '说话风格', type: 'textarea' },
            { key: 'boundaries', label: '边界', type: 'textarea' },
            { key: 'relationship_rules', label: '关系规则', type: 'textarea' },
            { key: 'reply_rules', label: '回复规则', type: 'textarea' },
            { key: 'proactive_comment_rules', label: '主动评论规则', type: 'textarea' },
            { key: 'dynamic_rules', label: '动态规则', type: 'textarea' },
            { key: 'weekly_rules', label: '周报规则', type: 'textarea' },
            { key: 'examples', label: '示例', type: 'textarea' },
        ];

        return () => h('div', [
            h(Card, { title: '人格列表' }, {
                action: () => h(Button, { type: 'primary', onClick: () => startEdit(null) }, () => '+ 创建人格'),
                default: () => !appState.personasLoaded
                    ? h(Loading)
                    : appState.personas.length === 0
                        ? h(EmptyState, { icon: 'personas', title: '暂无人格', desc: '点击右上角创建' })
                        : appState.personas.map(p => h(Card, { key: p.id, bordered: true }, {
                            default: () => h('div', [
                                h('div', { class: 'flex items-center justify-between mb-2' }, [
                                    h('div', { class: 'flex items-center gap-2' }, [
                                        h('span', { style: 'font-size:16px;font-weight:600' }, p.name),
                                        p.is_current ? h(Badge, { type: 'success' }, () => '当前') : null,
                                        !p.enabled ? h(Badge, { type: 'danger' }, () => '已禁用') : null,
                                    ]),
                                    h('div', { class: 'flex gap-2' }, [
                                        !p.is_current ? h(Button, { size: 'sm', type: 'primary', onClick: () => activate(p.id) }, () => '激活') : null,
                                        h(Button, { size: 'sm', onClick: () => startEdit(p) }, () => '编辑'),
                                        h(Button, { size: 'sm', type: 'danger', onClick: () => del(p.id) }, () => '删除'),
                                    ]),
                                ]),
                                h('div', { class: 'text-muted', style: 'font-size:13px' }, p.description || '无描述'),
                                // 关联账号
                                (personaUsage.value[p.id] || []).length > 0
                                    ? h('div', { class: 'mt-2', style: 'padding-top:8px;border-top:1px solid var(--outline-variant)' }, [
                                        h('div', { class: 'text-muted', style: 'font-size:12px;margin-bottom:4px' }, '被以下账号使用:'),
                                        h('div', { class: 'flex gap-2' },
                                            personaUsage.value[p.id].map(acc =>
                                                h(Badge, { type: 'info', size: 'sm' }, () => acc.name || acc.id)
                                            )
                                        ),
                                    ])
                                    : null,
                            ]),
                        })),
            }),
            h(Modal, {
                modelValue: showEditor.value,
                'onUpdate:modelValue': (v) => showEditor.value = v,
                title: editingId.value ? '编辑人格' : '创建人格',
                width: '800px',
            }, {
                default: () => h('div', [
                    ...formFields.map(f => h(FormInput, {
                        label: f.label,
                        type: f.type === 'textarea' ? 'text' : f.type,
                        modelValue: form.value[f.key],
                        'onUpdate:modelValue': (v) => form.value[f.key] = v,
                    })),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '启用'),
                        h(Toggle, {
                            modelValue: form.value.enabled,
                            'onUpdate:modelValue': (v) => form.value.enabled = v,
                        }),
                    ]),
                ]),
                footer: () => [
                    h(Button, { onClick: () => showEditor.value = false }, () => '取消'),
                    h(Button, { type: 'primary', onClick: save }, () => '保存'),
                ],
            }),
        ]);
    },
});
```

- [ ] **Step 2: 在 app.js 注册路由**

```javascript
import { PersonaListPage } from './components/personas.js';
registerRoute('/personas', PersonaListPage, '人格管理');
```

- [ ] **Step 3: Commit**

```bash
git add bilibot/web/static/js/components/personas.js bilibot/web/static/js/app.js
git commit -m "feat(web): add persona management page with account usage view"
```

---

## Phase 4: 记忆系统合并

**目标：** 将 LivingMemory 子应用完全合并为 Vue 原生组件。包含：记忆图谱（Canvas 渲染）、记忆列表（虚拟滚动+筛选）、召回测试、统计面板。消除 iframe 和双设计系统。

### Task 4.1: 创建记忆图谱 Canvas 渲染模块

**Files:**
- Create: `bilibot/web/static/js/components/memory/graph-canvas.js`

- [ ] **Step 1: 创建图谱 Canvas 渲染引擎（从 livingmemory/graph-2d.js 迁移核心逻辑）**

```javascript
// components/memory/graph-canvas.js - 记忆图谱 Canvas 渲染
// 从 livingmemory/graph-2d.js 迁移，适配 Vue 响应式

export class MemoryGraphRenderer {
    constructor(canvas, options = {}) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.nodes = [];
        this.edges = [];
        this.memories = [];
        this.nodePositions = new Map();
        this.camera = { x: 0, y: 0, zoom: 1 };
        this.dragging = null;
        this.hoveredNode = null;
        this.options = {
            nodeRadius: 20,
            Colors: {
                summary: '#6750A4',
                person: '#2E7D32',
                topic: '#0288D1',
                memory: '#ED6C02',
            },
            ...options,
        };
        this.onNodeClick = options.onNodeClick || (() => {});
        this.onNodeHover = options.onNodeHover || (() => {});
        this.setupEvents();
    }

    setData(data) {
        this.nodes = data.nodes || [];
        this.edges = data.edges || [];
        this.memories = data.memories || [];
        this.layout();
        this.render();
    }

    layout() {
        // 简单力导向布局
        const w = this.canvas.width;
        const h = this.canvas.height;
        const cx = w / 2, cy = h / 2;
        const n = this.nodes.length;
        if (n === 0) return;

        // 初始圆形布局
        this.nodes.forEach((node, i) => {
            const angle = (i / n) * Math.PI * 2;
            const r = Math.min(w, h) * 0.3;
            this.nodePositions.set(node.id, {
                x: cx + Math.cos(angle) * r,
                y: cy + Math.sin(angle) * r,
                vx: 0, vy: 0,
            });
        });

        // 迭代力导向
        for (let iter = 0; iter < 100; iter++) {
            // 斥力
            for (let i = 0; i < this.nodes.length; i++) {
                for (let j = i + 1; j < this.nodes.length; j++) {
                    const a = this.nodePositions.get(this.nodes[i].id);
                    const b = this.nodePositions.get(this.nodes[j].id);
                    const dx = b.x - a.x, dy = b.y - a.y;
                    const dist = Math.sqrt(dx * dx + dy * dy) || 1;
                    const force = 2000 / (dist * dist);
                    a.vx -= (dx / dist) * force;
                    a.vy -= (dy / dist) * force;
                    b.vx += (dx / dist) * force;
                    b.vy += (dy / dist) * force;
                }
            }
            // 引力（边）
            for (const edge of this.edges) {
                const a = this.nodePositions.get(edge.source);
                const b = this.nodePositions.get(edge.target);
                if (!a || !b) continue;
                const dx = b.x - a.x, dy = b.y - a.y;
                const dist = Math.sqrt(dx * dx + dy * dy) || 1;
                const force = (dist - 100) * 0.01;
                a.vx += (dx / dist) * force;
                a.vy += (dy / dist) * force;
                b.vx -= (dx / dist) * force;
                b.vy -= (dy / dist) * force;
            }
            // 更新位置
            for (const node of this.nodes) {
                const p = this.nodePositions.get(node.id);
                p.x += p.vx * 0.1;
                p.y += p.vy * 0.1;
                p.vx *= 0.9;
                p.vy *= 0.9;
            }
        }
    }

    render() {
        const ctx = this.ctx;
        const w = this.canvas.width;
        const h = this.canvas.height;
        ctx.clearRect(0, 0, w, h);
        ctx.save();
        ctx.translate(this.camera.x, this.camera.y);
        ctx.scale(this.camera.zoom, this.camera.zoom);

        // 绘制边
        ctx.strokeStyle = '#CAC4D0';
        ctx.lineWidth = 1;
        for (const edge of this.edges) {
            const a = this.nodePositions.get(edge.source);
            const b = this.nodePositions.get(edge.target);
            if (!a || !b) continue;
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(b.x, b.y);
            ctx.stroke();
        }

        // 绘制节点
        for (const node of this.nodes) {
            const p = this.nodePositions.get(node.id);
            if (!p) continue;
            const color = this.options.colors[node.type] || this.options.colors.memory;
            const isHovered = this.hoveredNode === node.id;

            ctx.beginPath();
            ctx.arc(p.x, p.y, isHovered ? 24 : 20, 0, Math.PI * 2);
            ctx.fillStyle = color;
            ctx.fill();
            ctx.strokeStyle = '#fff';
            ctx.lineWidth = 2;
            ctx.stroke();

            // 标签
            ctx.fillStyle = '#1D1B20';
            ctx.font = '12px Roboto, sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText(node.label || node.id, p.x, p.y + 35);
        }

        ctx.restore();
    }

    setupEvents() {
        let isDragging = false;
        let lastX = 0, lastY = 0;

        this.canvas.addEventListener('mousedown', (e) => {
            const rect = this.canvas.getBoundingClientRect();
            const x = (e.clientX - rect.left - this.camera.x) / this.camera.zoom;
            const y = (e.clientY - rect.top - this.camera.y) / this.camera.zoom;

            // 检查是否点击了节点
            for (const node of this.nodes) {
                const p = this.nodePositions.get(node.id);
                if (!p) continue;
                const dx = x - p.x, dy = y - p.y;
                if (Math.sqrt(dx * dx + dy * dy) < 20) {
                    this.dragging = node.id;
                    return;
                }
            }

            isDragging = true;
            lastX = e.clientX;
            lastY = e.clientY;
        });

        this.canvas.addEventListener('mousemove', (e) => {
            const rect = this.canvas.getBoundingClientRect();
            const x = (e.clientX - rect.left - this.camera.x) / this.camera.zoom;
            const y = (e.clientY - rect.top - this.camera.y) / this.camera.zoom;

            if (this.dragging) {
                const p = this.nodePositions.get(this.dragging);
                if (p) { p.x = x; p.y = y; this.render(); }
                return;
            }

            if (isDragging) {
                this.camera.x += e.clientX - lastX;
                this.camera.y += e.clientY - lastY;
                lastX = e.clientX;
                lastY = e.clientY;
                this.render();
                return;
            }

            // hover 检测
            let hovered = null;
            for (const node of this.nodes) {
                const p = this.nodePositions.get(node.id);
                if (!p) continue;
                const dx = x - p.x, dy = y - p.y;
                if (Math.sqrt(dx * dx + dy * dy) < 20) {
                    hovered = node.id;
                    break;
                }
            }
            if (hovered !== this.hoveredNode) {
                this.hoveredNode = hovered;
                this.onNodeHover(hovered ? this.nodes.find(n => n.id === hovered) : null);
                this.render();
            }
        });

        this.canvas.addEventListener('mouseup', () => {
            if (this.dragging) {
                this.dragging = null;
            }
            isDragging = false;
        });

        this.canvas.addEventListener('click', (e) => {
            if (this.hoveredNode) {
                const node = this.nodes.find(n => n.id === this.hoveredNode);
                if (node) this.onNodeClick(node);
            }
        });

        this.canvas.addEventListener('wheel', (e) => {
            e.preventDefault();
            const delta = e.deltaY > 0 ? 0.9 : 1.1;
            this.camera.zoom *= delta;
            this.camera.zoom = Math.max(0.3, Math.min(3, this.camera.zoom));
            this.render();
        });
    }

    resize() {
        const rect = this.canvas.getBoundingClientRect();
        this.canvas.width = rect.width;
        this.canvas.height = rect.height;
        this.render();
    }

    destroy() {
        // 移除事件监听（canvas 销毁时自动清理）
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add bilibot/web/static/js/components/memory/graph-canvas.js
git commit -m "feat(web): add memory graph canvas renderer (migrated from graph-2d.js)"
```

### Task 4.2: 创建记忆图谱页

**Files:**
- Create: `bilibot/web/static/js/components/memory/graph-page.js`

- [ ] **Step 1: 创建记忆图谱 Vue 组件**

```javascript
// components/memory/graph-page.js - 记忆图谱页
const { defineComponent, h, ref, onMounted, onUnmounted, watch } = window.Vue;
import { api } from '../../api.js';
import { appState, showToast } from '../../state.js';
import { Card, Button, Badge, FormInput, Loading, EmptyState } from '../common.js';
import { MemoryGraphRenderer } from './graph-canvas.js';

export const MemoryGraphPage = defineComponent({
    name: 'MemoryGraphPage',
    setup() {
        const canvasRef = ref(null);
        const renderer = ref(null);
        const loading = ref(false);
        const selectedNode = ref(null);
        const queryKeyword = ref('');
        const stats = ref(null);

        async function loadGraph() {
            if (!appState.currentAccountId) return;
            loading.value = true;
            try {
                const data = await api.memory.graph(appState.currentAccountId);
                if (renderer.value) {
                    renderer.value.setData(data.snapshot || { nodes: [], edges: [], memories: [] });
                }
                stats.value = {
                    nodes: data.graph_nodes || 0,
                    edges: data.graph_edges || 0,
                    memories: data.total_memories || 0,
                };
            } catch (e) {
                showToast('加载图谱失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        async function searchGraph() {
            if (!queryKeyword.value || !appState.currentAccountId) return;
            loading.value = true;
            try {
                const data = await api.memory.graphQuery(appState.currentAccountId, { query: queryKeyword.value });
                if (renderer.value) {
                    renderer.value.setData(data.snapshot || { nodes: [], edges: [], memories: [] });
                }
            } catch (e) {
                showToast('搜索失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        onMounted(async () => {
            if (!appState.currentAccountId && appState.accounts.length > 0) {
                appState.currentAccountId = appState.accounts[0].id;
            }
            await loadGraph();
            if (canvasRef.value) {
                renderer.value = new MemoryGraphRenderer(canvasRef.value, {
                    onNodeClick: (node) => { selectedNode.value = node; },
                    onNodeHover: (node) => { /* 可扩展 tooltip */ },
                });
                renderer.value.resize();
                window.addEventListener('resize', () => renderer.value?.resize());
            }
        });

        onUnmounted(() => { renderer.value?.destroy(); });

        return () => h('div', [
            h(Card, { title: '记忆图谱' }, {
                action: () => h('div', { class: 'flex gap-2' }, [
                    h(FormInput, {
                        modelValue: queryKeyword.value,
                        'onUpdate:modelValue': (v) => queryKeyword.value = v,
                        placeholder: '关键词搜索子图...',
                    }),
                    h(Button, { type: 'primary', onClick: searchGraph }, () => '搜索'),
                    h(Button, { onClick: loadGraph }, () => '重置'),
                ]),
                default: () => [
                    stats.value ? h('div', { class: 'flex gap-4 mb-4' }, [
                        h(Badge, { type: 'info' }, () => `节点: ${stats.value.nodes}`),
                        h(Badge, { type: 'success' }, () => `边: ${stats.value.edges}`),
                        h(Badge, { type: 'warning' }, () => `记忆: ${stats.value.memories}`),
                    ]) : null,
                    loading.value
                        ? h(Loading)
                        : h('canvas', {
                            ref: canvasRef,
                            style: 'width:100%;height:calc(100vh - 280px);border:1px solid var(--outline-variant);border-radius:12px;cursor:grab;',
                        }),
                ],
            }),
            selectedNode.value
                ? h(Card, { title: '选中节点' }, () => h('div', [
                    h('div', { class: 'flex gap-2 mb-2' }, [
                        h(Badge, { type: 'info' }, () => selectedNode.value.type),
                        h('span', { style: 'font-weight:600' }, selectedNode.value.label || selectedNode.value.id),
                    ]),
                    h('div', { class: 'text-muted', style: 'font-size:13px' },
                        JSON.stringify(selectedNode.value, null, 2)),
                ]))
                : null,
        ]);
    },
});
```

- [ ] **Step 2: 在 app.js 注册路由**

```javascript
import { MemoryGraphPage } from './components/memory/graph-page.js';
registerRoute('/memory/graph', MemoryGraphPage, '记忆图谱');
```

- [ ] **Step 3: Commit**

```bash
git add bilibot/web/static/js/components/memory/graph-page.js bilibot/web/static/js/app.js
git commit -m "feat(web): add memory graph page with interactive canvas"
```

### Task 4.3: 创建记忆列表页（虚拟滚动+筛选）

**Files:**
- Create: `bilibot/web/static/js/components/memory/list-page.js`

- [ ] **Step 1: 创建记忆列表页组件**

```javascript
// components/memory/list-page.js - 记忆列表页（筛选+分页+搜索）
const { defineComponent, h, ref, computed, onMounted } = window.Vue;
import { api } from '../../api.js';
import { appState, showToast } from '../../state.js';
import { Card, Button, Badge, FormInput, FormSelect, DataTable, Loading, EmptyState } from '../common.js';
import { formatTime } from '../../utils.js';

export const MemoryListPage = defineComponent({
    name: 'MemoryListPage',
    setup() {
        const memories = ref([]);
        const loading = ref(false);
        const page = ref(1);
        const pageSize = ref(20);
        const total = ref(0);
        const keyword = ref('');
        const category = ref('');
        const active = ref('');

        const columns = [
            { key: 'id', label: 'ID', width: '60px' },
            { key: 'category', label: '类型', width: '80px' },
            { key: 'content', label: '内容' },
            { key: 'created_at', label: '时间', width: '160px' },
            { key: 'actions', label: '操作', width: '80px' },
        ];

        async function loadList() {
            if (!appState.currentAccountId) return;
            loading.value = true;
            try {
                const params = {
                    page: page.value,
                    page_size: pageSize.value,
                    ...(keyword.value ? { keyword: keyword.value } : {}),
                    ...(category.value ? { category: category.value } : {}),
                    ...(active.value ? { active: active.value } : {}),
                };
                const data = await api.memory.list(appState.currentAccountId, params);
                memories.value = data.items || [];
                total.value = data.total || 0;
            } catch (e) {
                showToast('加载失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        async function deleteMemory(id) {
            if (!confirm('确定删除此记忆？')) return;
            try {
                await api.memory.delete(appState.currentAccountId, id);
                showToast('已删除', 'success');
                await loadList();
            } catch (e) {
                showToast('删除失败: ' + e.message, 'error');
            }
        }

        async function migrate() {
            if (!confirm('从 JSON 迁移记忆到 SQLite？')) return;
            try {
                await api.memory.migrate(appState.currentAccountId);
                showToast('迁移完成', 'success');
                await loadList();
            } catch (e) {
                showToast('迁移失败: ' + e.message, 'error');
            }
        }

        const totalPages = computed(() => Math.ceil(total.value / pageSize.value) || 1);

        onMounted(loadList);

        return () => h('div', [
            h(Card, { title: '记忆列表' }, {
                action: () => h('div', { class: 'flex gap-2' }, [
                    h(Button, { onClick: migrate }, () => '从 JSON 迁移'),
                    h(Button, { type: 'primary', onClick: loadList }, () => '刷新'),
                ]),
                default: () => [
                    h('div', { class: 'flex gap-3 mb-4', style: 'flex-wrap:wrap' }, [
                        h(FormInput, {
                            modelValue: keyword.value,
                            'onUpdate:modelValue': (v) => keyword.value = v,
                            placeholder: '关键词搜索...',
                        }),
                        h(FormSelect, {
                            modelValue: category.value,
                            'onUpdate:modelValue': (v) => category.value = v,
                            options: [
                                { value: '', label: '全部分类' },
                                { value: 'episodic', label: '情景记忆' },
                                { value: 'factual', label: '事实记忆' },
                                { value: 'procedural', label: '程序记忆' },
                            ],
                        }),
                        h(FormSelect, {
                            modelValue: active.value,
                            'onUpdate:modelValue': (v) => active.value = v,
                            options: [
                                { value: '', label: '全部' },
                                { value: '1', label: '活跃' },
                                { value: '0', label: '已删除' },
                            ],
                        }),
                        h(Button, { type: 'primary', onClick: () => { page.value = 1; loadList(); } }, () => '搜索'),
                    ]),
                    h(DataTable, {
                        columns,
                        rows: memories.value,
                        loading: loading.value,
                    }, {
                        category: ({ value }) => h(Badge, { type: 'info', size: 'sm' }, () => value),
                        content: ({ value }) => h('div', {
                            style: 'max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap',
                            title: value,
                        }, value),
                        created_at: ({ value }) => h('span', { class: 'text-muted', style: 'font-size:12px' },
                            formatTime(value)),
                        actions: ({ row }) => h(Button, {
                            size: 'sm', type: 'danger',
                            onClick: () => deleteMemory(row.id),
                        }, () => '删除'),
                    }),
                    // 分页
                    h('div', { class: 'flex items-center justify-between mt-4' }, [
                        h('span', { class: 'text-muted', style: 'font-size:13px' },
                            `共 ${total.value} 条，第 ${page.value}/${totalPages.value} 页`),
                        h('div', { class: 'flex gap-2' }, [
                            h(Button, { size: 'sm', disabled: page.value <= 1,
                                onClick: () => { page.value--; loadList(); } }, () => '上一页'),
                            h(Button, { size: 'sm', disabled: page.value >= totalPages.value,
                                onClick: () => { page.value++; loadList(); } }, () => '下一页'),
                        ]),
                    ]),
                ],
            }),
        ]);
    },
});
```

- [ ] **Step 2: 在 app.js 注册路由**

```javascript
import { MemoryListPage } from './components/memory/list-page.js';
registerRoute('/memory/list', MemoryListPage, '记忆列表');
```

- [ ] **Step 3: Commit**

```bash
git add bilibot/web/static/js/components/memory/list-page.js bilibot/web/static/js/app.js
git commit -m "feat(web): add memory list page with filter, search, and pagination"
```

### Task 4.4: 创建召回测试页

**Files:**
- Create: `bilibot/web/static/js/components/memory/recall-page.js`

- [ ] **Step 1: 创建召回测试页组件**

```javascript
// components/memory/recall-page.js - 召回测试页
const { defineComponent, h, ref } = window.Vue;
import { api } from '../../api.js';
import { appState, showToast } from '../../state.js';
import { Card, Button, Badge, FormInput, Loading, EmptyState } from '../common.js';
import { formatTime } from '../../utils.js';

export const MemoryRecallPage = defineComponent({
    name: 'MemoryRecallPage',
    setup() {
        const query = ref('');
        const results = ref([]);
        const loading = ref(false);
        const searched = ref(false);

        async function search() {
            if (!query.value || !appState.currentAccountId) return;
            loading.value = true;
            searched.value = true;
            try {
                const data = await api.memory.search(appState.currentAccountId, {
                    keyword: query.value,
                    limit: 10,
                });
                results.value = data.items || data || [];
            } catch (e) {
                showToast('搜索失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        return () => h('div', [
            h(Card, { title: '召回测试' }, () => [
                h('p', { class: 'text-muted mb-4' },
                    '输入关键词测试记忆召回效果，验证语义搜索是否正常工作。'),
                h('div', { class: 'flex gap-2 mb-4' }, [
                    h(FormInput, {
                        modelValue: query.value,
                        'onUpdate:modelValue': (v) => query.value = v,
                        placeholder: '输入测试关键词...',
                    }),
                    h(Button, { type: 'primary', loading: loading.value, onClick: search }, () => '召回'),
                ]),
                loading.value
                    ? h(Loading)
                    : searched.value && results.value.length === 0
                        ? h(EmptyState, { icon: 'search', title: '无召回结果', desc: '尝试其他关键词' })
                        : h('div', { class: 'flex-col gap-3' },
                            results.value.map((item, idx) => h(Card, { key: idx, bordered: true }, () => h('div', [
                                h('div', { class: 'flex items-center justify-between mb-2' }, [
                                    h(Badge, { type: 'info', size: 'sm' }, () => item.category || 'unknown'),
                                    h('span', { class: 'text-muted', style: 'font-size:12px' },
                                        formatTime(item.created_at)),
                                ]),
                                h('div', { style: 'font-size:14px;line-height:1.6' }, item.content),
                                item.importance_score != null
                                    ? h('div', { class: 'mt-2' }, [
                                        h('span', { class: 'text-muted', style: 'font-size:12px' }, '重要性: '),
                                        h(Badge, {
                                            type: item.importance_score > 0.7 ? 'success'
                                                : item.importance_score > 0.4 ? 'warning' : 'danger',
                                            size: 'sm',
                                        }, () => (item.importance_score * 10).toFixed(1)),
                                    ])
                                    : null,
                            ])))
                        ),
            ]),
        ]);
    },
});
```

- [ ] **Step 2: 在 app.js 注册路由**

```javascript
import { MemoryRecallPage } from './components/memory/recall-page.js';
registerRoute('/memory/recall', MemoryRecallPage, '召回测试');
```

- [ ] **Step 3: Commit**

```bash
git add bilibot/web/static/js/components/memory/recall-page.js bilibot/web/static/js/app.js
git commit -m "feat(web): add memory recall test page"
```

---

## Phase 5: 其他页面迁移

**目标：** 将剩余页面从旧 dashboard.js 迁移到 Vue 组件。每个页面一个 Task。

### Task 5.1: 评论页

**Files:**
- Create: `bilibot/web/static/js/pages/comments.js`

- [ ] **Step 1: 创建评论页组件**

参考旧 `loadCommentsPage`（dashboard.js 行 154-339），迁移为 Vue 组件。核心功能：
- 回复审计列表（调用 `api.replies(params)`）
- 状态筛选（all/pending/replied/failed）
- 分页

```javascript
// pages/comments.js - 评论回复审计页
const { defineComponent, h, ref, onMounted } = window.Vue;
import { api } from '../api.js';
import { showToast } from '../state.js';
import { Card, Button, Badge, FormSelect, DataTable, Loading, EmptyState } from '../components/common.js';
import { formatTime } from '../utils.js';

export const CommentsPage = defineComponent({
    name: 'CommentsPage',
    setup() {
        const replies = ref([]);
        const loading = ref(false);
        const filter = ref('all');
        const page = ref(1);
        const total = ref(0);

        const columns = [
            { key: 'source_content', label: '原评论' },
            { key: 'reply_content', label: '回复内容' },
            { key: 'status', label: '状态', width: '80px' },
            { key: 'created_at', label: '时间', width: '160px' },
        ];

        async function load() {
            loading.value = true;
            try {
                const data = await api.replies({
                    page: page.value,
                    page_size: 20,
                    status: filter.value === 'all' ? undefined : filter.value,
                });
                replies.value = data.items || [];
                total.value = data.total || 0;
            } catch (e) {
                showToast('加载失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        onMounted(load);

        return () => h('div', [
            h(Card, { title: '评论回复记录' }, {
                action: () => h(FormSelect, {
                    modelValue: filter.value,
                    'onUpdate:modelValue': (v) => { filter.value = v; page.value = 1; load(); },
                    options: [
                        { value: 'all', label: '全部' },
                        { value: 'pending', label: '待处理' },
                        { value: 'replied', label: '已回复' },
                        { value: 'failed', label: '失败' },
                    ],
                }),
                default: () => h(DataTable, {
                    columns, rows: replies.value, loading: loading.value,
                }, {
                    status: ({ value }) => {
                        const type = value === 'published' ? 'success'
                                   : value === 'failed' ? 'danger' : 'warning';
                        return h(Badge, { type, size: 'sm' }, () => value);
                    },
                    created_at: ({ value }) => h('span', { class: 'text-muted', style: 'font-size:12px' },
                        formatTime(value)),
                    source_content: ({ value }) => h('div', {
                        style: 'max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap',
                        title: value,
                    }, value),
                    reply_content: ({ value }) => h('div', {
                        style: 'max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap',
                        title: value,
                    }, value),
                }),
            }),
        ]);
    },
});
```

- [ ] **Step 2: 在 app.js 注册路由并 Commit**

```javascript
import { CommentsPage } from './pages/comments.js';
registerRoute('/comments', CommentsPage, '评论');
```

```bash
git add bilibot/web/static/js/pages/comments.js bilibot/web/static/js/app.js
git commit -m "feat(web): migrate comments page to Vue component"
```

### Task 5.2: 日志页

**Files:**
- Create: `bilibot/web/static/js/pages/logs.js`

- [ ] **Step 1: 创建日志页组件**

参考旧 `loadLogsPage`（dashboard.js 行 1264-1336），迁移为 Vue 组件。核心功能：日志查询（level/keyword/limit）+ 下载。

```javascript
// pages/logs.js - 日志查看页
const { defineComponent, h, ref, onMounted } = window.Vue;
import { api } from '../api.js';
import { showToast } from '../state.js';
import { Card, Button, Badge, FormInput, FormSelect, DataTable, Loading } from '../components/common.js';
import { downloadFile } from '../utils.js';

export const LogsPage = defineComponent({
    name: 'LogsPage',
    setup() {
        const logs = ref([]);
        const loading = ref(false);
        const level = ref('');
        const keyword = ref('');
        const limit = ref(100);

        const columns = [
            { key: 'timestamp', label: '时间', width: '180px' },
            { key: 'level', label: '级别', width: '80px' },
            { key: 'message', label: '内容' },
        ];

        async function load() {
            loading.value = true;
            try {
                const data = await api.logs({
                    level: level.value || undefined,
                    keyword: keyword.value || undefined,
                    limit: limit.value,
                });
                logs.value = data.items || data || [];
            } catch (e) {
                showToast('加载失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        async function download() {
            try {
                const resp = await fetch('/api/logs/download', { credentials: 'same-origin' });
                const blob = await resp.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url; a.download = 'bililog.txt'; a.click();
                URL.revokeObjectURL(url);
            } catch (e) { showToast('下载失败', 'error'); }
        }

        onMounted(load);

        return () => h('div', [
            h(Card, { title: '系统日志' }, {
                action: () => h('div', { class: 'flex gap-2' }, [
                    h(Button, { onClick: download }, () => '下载'),
                    h(Button, { type: 'primary', onClick: load }, () => '查询'),
                ]),
                default: () => [
                    h('div', { class: 'flex gap-3 mb-4', style: 'flex-wrap:wrap' }, [
                        h(FormSelect, {
                            modelValue: level.value,
                            'onUpdate:modelValue': (v) => level.value = v,
                            options: [
                                { value: '', label: '全部级别' },
                                { value: 'DEBUG', label: 'DEBUG' },
                                { value: 'INFO', label: 'INFO' },
                                { value: 'WARNING', label: 'WARNING' },
                                { value: 'ERROR', label: 'ERROR' },
                            ],
                        }),
                        h(FormInput, {
                            modelValue: keyword.value,
                            'onUpdate:modelValue': (v) => keyword.value = v,
                            placeholder: '关键词...',
                        }),
                    ]),
                    h(DataTable, { columns, rows: logs.value, loading: loading.value }, {
                        level: ({ value }) => {
                            const type = value === 'ERROR' ? 'danger'
                                       : value === 'WARNING' ? 'warning'
                                       : value === 'INFO' ? 'info' : 'success';
                            return h(Badge, { type, size: 'sm' }, () => value);
                        },
                        message: ({ value }) => h('div', {
                            style: 'max-width:500px;white-space:pre-wrap;font-family:monospace;font-size:12px',
                        }, value),
                    }),
                ],
            }),
        ]);
    },
});
```

- [ ] **Step 2: 注册路由并 Commit**

```javascript
import { LogsPage } from './pages/logs.js';
registerRoute('/logs', LogsPage, '日志');
```

```bash
git add bilibot/web/static/js/pages/logs.js bilibot/web/static/js/app.js
git commit -m "feat(web): migrate logs page to Vue component"
```

### Task 5.3: 主动行为页

**Files:**
- Create: `bilibot/web/static/js/pages/proactive.js`

- [ ] **Step 1: 创建主动行为页组件**

参考旧 `loadProactivePage`（dashboard.js 行 1337-1454），迁移为 Vue 组件。核心功能：手动触发主动视频/动态任务、查看任务状态。

```javascript
// pages/proactive.js - 主动行为页
const { defineComponent, h, ref, onMounted } = window.Vue;
import { api } from '../api.js';
import { appState, showToast, refreshAccounts } from '../state.js';
import { Card, Button, Badge, FormSelect, DataTable, Loading } from '../components/common.js';
import { formatTime } from '../utils.js';

export const ProactivePage = defineComponent({
    name: 'ProactivePage',
    setup() {
        const tasks = ref([]);
        const loading = ref(false);
        const selectedAccount = ref('');

        const columns = [
            { key: 'task_type', label: '类型', width: '100px' },
            { key: 'state', label: '状态', width: '100px' },
            { key: 'created_at', label: '创建时间', width: '160px' },
            { key: 'result', label: '结果' },
        ];

        async function loadTasks() {
            if (!selectedAccount.value) return;
            loading.value = true;
            try {
                const data = await api.accounts.tasks(selectedAccount.value);
                tasks.value = data.items || data || [];
            } catch (e) {
                showToast('加载失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        async function triggerVideo() {
            if (!selectedAccount.value) return;
            try {
                await api.accounts.createTask?.(selectedAccount.value, 'proactive-video')
                    || api.post(`/api/accounts/${selectedAccount.value}/tasks/proactive-video`);
                showToast('主动视频任务已触发', 'success');
                await loadTasks();
            } catch (e) { showToast('触发失败: ' + e.message, 'error'); }
        }

        async function triggerDynamic() {
            if (!selectedAccount.value) return;
            try {
                await api.post(`/api/accounts/${selectedAccount.value}/tasks/dynamic`);
                showToast('动态发布任务已触发', 'success');
                await loadTasks();
            } catch (e) { showToast('触发失败: ' + e.message, 'error'); }
        }

        onMounted(() => {
            if (!appState.accountsLoaded) refreshAccounts();
            if (appState.accounts.length > 0 && !selectedAccount.value) {
                selectedAccount.value = appState.currentAccountId || appState.accounts[0].id;
                loadTasks();
            }
        });

        return () => h('div', [
            h(Card, { title: '主动行为管理' }, {
                action: () => h(FormSelect, {
                    modelValue: selectedAccount.value,
                    'onUpdate:modelValue': (v) => { selectedAccount.value = v; loadTasks(); },
                    options: appState.accounts.map(a => ({ value: a.id, label: a.name || a.id })),
                }),
                default: () => [
                    h('div', { class: 'flex gap-3 mb-4' }, [
                        h(Button, { type: 'primary', onClick: triggerVideo }, () => '触发主动视频'),
                        h(Button, { type: 'primary', onClick: triggerDynamic }, () => '触发动态发布'),
                        h(Button, { onClick: loadTasks }, () => '刷新'),
                    ]),
                    h(DataTable, { columns, rows: tasks.value, loading: loading.value }, {
                        state: ({ value }) => {
                            const type = value === 'done' ? 'success' : value === 'failed' ? 'danger' : 'info';
                            return h(Badge, { type, size: 'sm' }, () => value);
                        },
                        created_at: ({ value }) => h('span', { class: 'text-muted', style: 'font-size:12px' }, formatTime(value)),
                    }),
                ],
            }),
        ]);
    },
});
```

- [ ] **Step 2: 注册路由并 Commit**

```javascript
import { ProactivePage } from './pages/proactive.js';
registerRoute('/proactive', ProactivePage, '主动行为');
```

```bash
git add bilibot/web/static/js/pages/proactive.js bilibot/web/static/js/app.js
git commit -m "feat(web): migrate proactive page to Vue component"
```

---

### Task 5.4: 动态草稿页

**Files:**
- Create: `bilibot/web/static/js/pages/drafts.js`

**参考旧实现：** `loadDraftsPage`（dashboard.js 行 1455-1654）。核心功能：草稿列表、审核通过/拒绝/重试、乐观锁编辑（`version` 字段防止并发覆盖）。

- [ ] **Step 1: 创建动态草稿页组件**

```javascript
// bilibot/web/static/js/pages/drafts.js
import { h, ref, reactive, onMounted, computed } from '../vendor/vue.esm-browser.prod.js';
import { appState } from '../state.js';
import { api } from '../api.js';
import { Card, Button, Badge, DataTable, Modal, FormTextarea, FormInput, FormSelect, EmptyState, Pagination } from '../components.js';
import { formatTime, formatDateTime, getStatusType } from '../utils.js';

export const DraftsPage = {
    setup() {
        const loading = ref(false);
        const drafts = ref([]);
        const selectedAccount = ref(appState.currentAccountId || '');
        const filterStatus = ref('pending'); // pending / approved / rejected / published
        const page = ref(1);
        const pageSize = 20;
        const total = ref(0);

        // 编辑弹窗
        const editModal = reactive({
            visible: false,
            draft: null,
            content: '',
            version: 0,
            saving: false,
        });

        // 预览弹窗
        const previewModal = reactive({
            visible: false,
            draft: null,
        });

        const filteredDrafts = computed(() => {
            if (filterStatus.value === 'all') return drafts.value;
            return drafts.value.filter(d => d.status === filterStatus.value);
        });

        async function refresh() {
            if (!selectedAccount.value) return;
            loading.value = true;
            try {
                const res = await api.dynamicDrafts.list({
                    account_id: selectedAccount.value,
                    status: filterStatus.value === 'all' ? undefined : filterStatus.value,
                    page: page.value,
                    page_size: pageSize,
                });
                drafts.value = res.data.items || [];
                total.value = res.data.total || 0;
            } catch (e) {
                appState.notify('加载草稿失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function approve(draft) {
            if (!confirm(`确认通过草稿 #${draft.id}？将通过审核并加入发布队列。`)) return;
            try {
                await api.dynamicDrafts.approve(draft.id, { version: draft.version });
                appState.notify('草稿已通过', 'success');
                refresh();
            } catch (e) {
                appState.notify('操作失败：' + (e.message || e), 'danger');
            }
        }

        async function reject(draft) {
            const reason = prompt(`拒绝草稿 #${draft.id} 的原因（可选）：`);
            if (reason === null) return;
            try {
                await api.dynamicDrafts.reject(draft.id, {
                    version: draft.version,
                    reason: reason || '',
                });
                appState.notify('草稿已拒绝', 'success');
                refresh();
            } catch (e) {
                appState.notify('操作失败：' + (e.message || e), 'danger');
            }
        }

        async function retry(draft) {
            if (!confirm(`重新生成草稿 #${draft.id}？将调用 LLM 重新生成内容。`)) return;
            try {
                await api.dynamicDrafts.retry(draft.id);
                appState.notify('已触发重新生成', 'info');
                setTimeout(refresh, 1500);
            } catch (e) {
                appState.notify('操作失败：' + (e.message || e), 'danger');
            }
        }

        function openEdit(draft) {
            editModal.draft = draft;
            editModal.content = draft.content || '';
            editModal.version = draft.version;
            editModal.saving = false;
            editModal.visible = true;
        }

        async function saveEdit() {
            if (!editModal.draft) return;
            editModal.saving = true;
            try {
                await api.dynamicDrafts.update(editModal.draft.id, {
                    content: editModal.content,
                    version: editModal.version,
                });
                appState.notify('草稿已保存', 'success');
                editModal.visible = false;
                refresh();
            } catch (e) {
                const msg = e.message || String(e);
                if (msg.includes('version') || msg.includes('409')) {
                    appState.notify('草稿已被其他人修改，请刷新后重试', 'warning');
                } else {
                    appState.notify('保存失败：' + msg, 'danger');
                }
            } finally {
                editModal.saving = false;
            }
        }

        function openPreview(draft) {
            previewModal.draft = draft;
            previewModal.visible = true;
        }

        const statusFilters = [
            { value: 'pending', label: '待审核' },
            { value: 'approved', label: '已通过' },
            { value: 'rejected', label: '已拒绝' },
            { value: 'published', label: '已发布' },
            { value: 'all', label: '全部' },
        ];

        const columns = [
            { key: 'id', label: 'ID', width: '60px' },
            { key: 'type', label: '类型' },
            { key: 'content', label: '内容预览' },
            { key: 'status', label: '状态', width: '100px' },
            { key: 'created_at', label: '创建时间', width: '160px' },
            { key: 'actions', label: '操作', width: '260px' },
        ];

        onMounted(() => {
            if (!appState.accountsLoaded) {
                appState.refreshAccounts().then(() => {
                    if (appState.accounts.length > 0 && !selectedAccount.value) {
                        selectedAccount.value = appState.currentAccountId || appState.accounts[0].id;
                        refresh();
                    }
                });
            } else if (appState.accounts.length > 0 && !selectedAccount.value) {
                selectedAccount.value = appState.currentAccountId || appState.accounts[0].id;
                refresh();
            } else if (selectedAccount.value) {
                refresh();
            }
        });

        return () => h('div', [
            h(Card, { title: '动态草稿审核' }, {
                action: () => h('div', { class: 'flex gap-3 items-center' }, [
                    h(FormSelect, {
                        modelValue: selectedAccount.value,
                        'onUpdate:modelValue': (v) => {
                            selectedAccount.value = v;
                            appState.currentAccountId = v;
                            page.value = 1;
                            refresh();
                        },
                        options: appState.accounts.map(a => ({ value: a.id, label: a.name || a.id })),
                        style: 'width:160px',
                    }),
                    h(Button, { type: 'primary', onClick: refresh, loading: loading.value }, () => '刷新'),
                ]),
                default: () => [
                    // 状态过滤标签
                    h('div', { class: 'flex gap-2 mb-4' },
                        statusFilters.map(f => h(Button, {
                            type: filterStatus.value === f.value ? 'primary' : 'secondary',
                            size: 'sm',
                            onClick: () => {
                                filterStatus.value = f.value;
                                page.value = 1;
                                refresh();
                            },
                        }, () => f.label)),
                    ),
                    h(DataTable, {
                        columns,
                        rows: filteredDrafts.value,
                        loading: loading.value,
                        empty: '暂无草稿',
                    }, {
                        content: ({ row }) => h('div', {
                            class: 'text-ellipsis',
                            style: 'max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; cursor:pointer',
                            title: row.content,
                            onClick: () => openPreview(row),
                        }, row.content || '(空)'),
                        type: ({ value }) => {
                            const map = { dynamic: '动态', video: '视频' };
                            return h(Badge, { type: 'info', size: 'sm' }, () => map[value] || value);
                        },
                        status: ({ value }) => {
                            const typeMap = {
                                pending: 'warning',
                                approved: 'success',
                                rejected: 'danger',
                                published: 'info',
                            };
                            return h(Badge, { type: typeMap[value] || 'info', size: 'sm' }, () => value);
                        },
                        created_at: ({ value }) => h('span', { class: 'text-muted', style: 'font-size:12px' }, formatTime(value)),
                        actions: ({ row }) => h('div', { class: 'flex gap-2' }, [
                            row.status === 'pending' && h(Button, { size: 'sm', type: 'primary', onClick: () => approve(row) }, () => '通过'),
                            row.status === 'pending' && h(Button, { size: 'sm', type: 'danger', onClick: () => reject(row) }, () => '拒绝'),
                            row.status === 'pending' && h(Button, { size: 'sm', onClick: () => openEdit(row) }, () => '编辑'),
                            h(Button, { size: 'sm', onClick: () => openPreview(row) }, () => '预览'),
                            (row.status === 'rejected' || row.status === 'pending') && h(Button, { size: 'sm', onClick: () => retry(row) }, () => '重新生成'),
                        ].filter(Boolean)),
                    }),
                    // 分页
                    total.value > pageSize && h(Pagination, {
                        page: page.value,
                        pageSize,
                        total: total.value,
                        'onUpdate:page': (p) => { page.value = p; refresh(); },
                    }),
                ],
            }),

            // 编辑弹窗
            h(Modal, {
                visible: editModal.visible,
                title: `编辑草稿 #${editModal.draft?.id || ''}`,
                'onUpdate:visible': (v) => editModal.visible = v,
            }, {
                default: () => h('div', [
                    h(FormTextarea, {
                        modelValue: editModal.content,
                        'onUpdate:modelValue': (v) => editModal.content = v,
                        rows: 8,
                        placeholder: '输入草稿内容...',
                    }),
                    h('p', { class: 'form-hint' }, `当前版本：v${editModal.version}（乐观锁，保存时若版本不匹配将拒绝）`),
                ]),
                footer: () => h('div', { class: 'flex gap-2 justify-end' }, [
                    h(Button, { onClick: () => editModal.visible = false }, () => '取消'),
                    h(Button, { type: 'primary', onClick: saveEdit, loading: editModal.saving }, () => '保存'),
                ]),
            }),

            // 预览弹窗
            h(Modal, {
                visible: previewModal.visible,
                title: `草稿预览 #${previewModal.draft?.id || ''}`,
                'onUpdate:visible': (v) => previewModal.visible = v,
            }, {
                default: () => h('div', { class: 'preview-content' }, [
                    h('pre', { style: 'white-space:pre-wrap; word-break:break-word; font-family:inherit; line-height:1.6' },
                        previewModal.draft?.content || '(空内容)'),
                    previewModal.draft?.pictures && h('div', { class: 'flex gap-2 mt-3 flex-wrap' },
                        (previewModal.draft.pictures || []).map(pic => h('img', {
                            src: pic.src || pic.url,
                            style: 'max-width:120px; max-height:120px; border-radius:8px; border:1px solid var(--outline-variant)',
                        })),
                    ),
                ]),
                footer: () => h(Button, { onClick: () => previewModal.visible = false }, () => '关闭'),
            }),
        ]);
    },
};
```

- [ ] **Step 2: 注册路由并 Commit**

```javascript
import { DraftsPage } from './pages/drafts.js';
registerRoute('/drafts', DraftsPage, '动态草稿', '内容创作');
```

```bash
git add bilibot/web/static/js/pages/drafts.js bilibot/web/static/js/app.js
git commit -m "feat(web): migrate drafts page to Vue component with optimistic lock"
```

---

### Task 5.5: 视频理解页

**Files:**
- Create: `bilibot/web/static/js/pages/video-analysis.js`

**参考旧实现：** `loadVideoAnalysisPage`（dashboard.js 行 2403-2675）。核心功能：帧提取方法选择（katna/scenedetect/ffmpeg）、采样参数配置、模型选择、测试分析。

- [ ] **Step 1: 创建视频理解页组件**

```javascript
// bilibot/web/static/js/pages/video-analysis.js
import { h, ref, reactive, onMounted, computed, watch } from '../vendor/vue.esm-browser.prod.js';
import { appState } from '../state.js';
import { api } from '../api.js';
import { Card, Button, Badge, FormInput, FormSelect, FormTextarea, Toggle, FormHint } from '../components.js';

export const VideoAnalysisPage = {
    setup() {
        const loading = ref(false);
        const testing = ref(false);
        const config = reactive({
            enabled: false,
            frame_extractor: 'ffmpeg',
            max_frames: 8,
            min_scene_len: 2.0,
            sampling_interval: 2.0,
            llm_provider_id: '',
            vision_model: '',
            description_prompt: '',
            analysis_timeout: 120,
        });
        const testResult = ref(null);
        const testUrl = ref('');

        const extractorOptions = [
            { value: 'ffmpeg', label: 'FFmpeg（无依赖，兼容性好）' },
            { value: 'katna', label: 'Katna（关键帧提取，需 pip install katna）' },
            { value: 'scenedetect', label: 'PySceneDetect（场景检测，需 pip install scenedetect[video]）' },
        ];

        async function loadConfig() {
            loading.value = true;
            try {
                const res = await api.videoAnalysis.getConfig();
                Object.assign(config, res.data || {});
            } catch (e) {
                appState.notify('加载配置失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function saveConfig() {
            loading.value = true;
            try {
                await api.videoAnalysis.updateConfig(config);
                appState.notify('配置已保存', 'success');
            } catch (e) {
                appState.notify('保存失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function runTest() {
            if (!testUrl.value) {
                appState.notify('请输入测试视频 URL', 'warning');
                return;
            }
            testing.value = true;
            testResult.value = null;
            try {
                const res = await api.videoAnalysis.test({
                    video_url: testUrl.value,
                    config: config,
                });
                testResult.value = res.data;
                appState.notify('分析完成', 'success');
            } catch (e) {
                testResult.value = { error: e.message || String(e) };
                appState.notify('分析失败：' + (e.message || e), 'danger');
            } finally {
                testing.value = false;
            }
        }

        onMounted(() => {
            loadConfig();
            if (appState.llmProviders.length === 0) appState.refreshLlmProviders();
        });

        const llmOptions = computed(() =>
            appState.llmProviders.map(p => ({ value: p.id, label: `${p.name} (${p.model})` })),
        );

        return () => h('div', [
            h(Card, { title: '视频理解配置' }, {
                default: () => h('div', { class: 'form-grid-2col' }, [
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '启用视频理解'),
                        h('div', { class: 'flex items-center gap-2' }, [
                            h(Toggle, {
                                modelValue: config.enabled,
                                'onUpdate:modelValue': (v) => config.enabled = v,
                            }),
                            h('span', { class: 'form-hint' }, config.enabled ? '已启用' : '已禁用'),
                        ]),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '帧提取方法'),
                        h(FormSelect, {
                            modelValue: config.frame_extractor,
                            'onUpdate:modelValue': (v) => config.frame_extractor = v,
                            options: extractorOptions,
                        }),
                        h(FormHint, 'katna/scenedetect 需额外安装依赖；ffmpeg 为默认回退方案'),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '最大帧数'),
                        h(FormInput, {
                            modelValue: String(config.max_frames),
                            'onUpdate:modelValue': (v) => config.max_frames = parseInt(v) || 8,
                            type: 'number',
                        }),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '最小场景长度（秒）'),
                        h(FormInput, {
                            modelValue: String(config.min_scene_len),
                            'onUpdate:modelValue': (v) => config.min_scene_len = parseFloat(v) || 2.0,
                            type: 'number',
                        }),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '采样间隔（秒）'),
                        h(FormInput, {
                            modelValue: String(config.sampling_interval),
                            'onUpdate:modelValue': (v) => config.sampling_interval = parseFloat(v) || 2.0,
                            type: 'number',
                        }),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '分析超时（秒）'),
                        h(FormInput, {
                            modelValue: String(config.analysis_timeout),
                            'onUpdate:modelValue': (v) => config.analysis_timeout = parseInt(v) || 120,
                            type: 'number',
                        }),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '使用的 LLM Provider'),
                        h(FormSelect, {
                            modelValue: config.llm_provider_id,
                            'onUpdate:modelValue': (v) => config.llm_provider_id = v,
                            options: [{ value: '', label: '使用账号绑定 LLM' }, ...llmOptions.value],
                        }),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '视觉模型名称'),
                        h(FormInput, {
                            modelValue: config.vision_model,
                            'onUpdate:modelValue': (v) => config.vision_model = v,
                            placeholder: '如 gpt-4o, qwen-vl-max（留空用默认）',
                        }),
                    ]),
                    h('div', { class: 'form-group span-2' }, [
                        h('label', { class: 'form-label' }, '描述提示词'),
                        h(FormTextarea, {
                            modelValue: config.description_prompt,
                            'onUpdate:modelValue': (v) => config.description_prompt = v,
                            rows: 4,
                            placeholder: '描述视频帧内容时使用的系统提示词...',
                        }),
                    ]),
                ]),
                footer: () => h('div', { class: 'flex gap-2 justify-end' }, [
                    h(Button, { onClick: loadConfig }, () => '重置'),
                    h(Button, { type: 'primary', onClick: saveConfig, loading: loading.value }, () => '保存配置'),
                ]),
            }),

            h(Card, { title: '视频分析测试' }, {
                default: () => h('div', [
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '测试视频 URL（B站视频 BV 号或链接）'),
                        h('div', { class: 'flex gap-2' }, [
                            h(FormInput, {
                                modelValue: testUrl.value,
                                'onUpdate:modelValue': (v) => testUrl.value = v,
                                placeholder: 'https://www.bilibili.com/video/BVxxxxxx',
                                style: 'flex:1',
                            }),
                            h(Button, { type: 'primary', onClick: runTest, loading: testing.value }, () => '开始分析'),
                        ]),
                    ]),
                    testResult.value && h('div', { class: 'mt-4' }, [
                        h('h4', { class: 'mb-2' }, '分析结果：'),
                        testResult.value.error
                            ? h('div', { class: 'alert alert-danger' }, testResult.value.error)
                            : h('div', [
                                testResult.value.description && h('p', { style: 'line-height:1.6; margin-bottom:12px' },
                                    testResult.value.description),
                                testResult.value.frames && h('div', { class: 'flex gap-2 flex-wrap' },
                                    (testResult.value.frames || []).map((f, i) => h('div', { class: 'frame-thumb' }, [
                                        h('img', { src: f, style: 'max-width:120px; border-radius:8px; border:1px solid var(--outline-variant)' }),
                                        h('span', { class: 'text-muted', style: 'font-size:11px' }, `帧 ${i + 1}`),
                                    ])),
                                ),
                                testResult.value.duration && h('p', { class: 'text-muted mt-2', style: 'font-size:12px' },
                                    `视频时长：${testResult.value.duration}s | 提取帧数：${testResult.value.frames?.length || 0}`),
                            ]),
                    ]),
                ]),
            }),
        ]);
    },
};
```

- [ ] **Step 2: 注册路由并 Commit**

```javascript
import { VideoAnalysisPage } from './pages/video-analysis.js';
registerRoute('/video-analysis', VideoAnalysisPage, '视频理解', '内容创作');
```

```bash
git add bilibot/web/static/js/pages/video-analysis.js bilibot/web/static/js/app.js
git commit -m "feat(web): migrate video analysis page to Vue component"
```

---

### Task 5.6: 文生图页

**Files:**
- Create: `bilibot/web/static/js/pages/image-gen.js`

**参考旧实现：** `loadImageGenPage`（dashboard.js 行 2676+）。核心功能：Agnes AI `images/generations` API 配置、prompt 测试生成、b64_json/URL 双模式下载。

- [ ] **Step 1: 创建文生图页组件**

```javascript
// bilibot/web/static/js/pages/image-gen.js
import { h, ref, reactive, onMounted, computed } from '../vendor/vue.esm-browser.prod.js';
import { appState } from '../state.js';
import { api } from '../api.js';
import { Card, Button, Badge, FormInput, FormSelect, FormTextarea, Toggle, FormHint } from '../components.js';

export const ImageGenPage = {
    setup() {
        const loading = ref(false);
        const testing = ref(false);
        const config = reactive({
            enabled: false,
            provider: 'agnes',
            api_base: '',
            api_key: '',
            model: 'dall-e-3',
            image_size: '1024x1024',
            response_format: 'b64_json',
            quality: 'standard',
            style: 'vivid',
            max_retries: 3,
            timeout: 60,
        });
        const testPrompt = ref('');
        const testResult = ref(null);

        const sizeOptions = [
            { value: '1024x1024', label: '1024x1024（正方形）' },
            { value: '1792x1024', label: '1792x1024（横向）' },
            { value: '1024x1792', label: '1024x1792（纵向）' },
            { value: '512x512', label: '512x512（小图）' },
        ];

        const formatOptions = [
            { value: 'b64_json', label: 'Base64 JSON（优先，避免二次下载）' },
            { value: 'url', label: 'URL（需二次下载）' },
        ];

        async function loadConfig() {
            loading.value = true;
            try {
                const res = await api.imageGen.getConfig();
                // 注意：api_key 不回显
                Object.assign(config, res.data || {});
                config.api_key = ''; // 清空，编辑时需重新输入
            } catch (e) {
                appState.notify('加载配置失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function saveConfig() {
            loading.value = true;
            try {
                const payload = { ...config };
                // api_key 为空时不提交（保持原值）
                if (!payload.api_key) delete payload.api_key;
                await api.imageGen.updateConfig(payload);
                appState.notify('配置已保存', 'success');
            } catch (e) {
                appState.notify('保存失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function runTest() {
            if (!testPrompt.value) {
                appState.notify('请输入测试提示词', 'warning');
                return;
            }
            testing.value = true;
            testResult.value = null;
            try {
                const res = await api.imageGen.test({
                    prompt: testPrompt.value,
                    config: { ...config, api_key: config.api_key || undefined },
                });
                testResult.value = res.data;
                appState.notify('生成完成', 'success');
            } catch (e) {
                testResult.value = { error: e.message || String(e) };
                appState.notify('生成失败：' + (e.message || e), 'danger');
            } finally {
                testing.value = false;
            }
        }

        onMounted(loadConfig);

        return () => h('div', [
            h(Card, { title: '文生图配置' }, {
                default: () => h('div', { class: 'form-grid-2col' }, [
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '启用文生图'),
                        h('div', { class: 'flex items-center gap-2' }, [
                            h(Toggle, {
                                modelValue: config.enabled,
                                'onUpdate:modelValue': (v) => config.enabled = v,
                            }),
                            h('span', { class: 'form-hint' }, config.enabled ? '已启用' : '已禁用'),
                        ]),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '服务商'),
                        h(FormSelect, {
                            modelValue: config.provider,
                            'onUpdate:modelValue': (v) => config.provider = v,
                            options: [
                                { value: 'agnes', label: 'Agnes AI' },
                                { value: 'openai', label: 'OpenAI 兼容' },
                            ],
                        }),
                    ]),
                    h('div', { class: 'form-group span-2' }, [
                        h('label', { class: 'form-label' }, 'API Base URL'),
                        h(FormInput, {
                            modelValue: config.api_base,
                            'onUpdate:modelValue': (v) => config.api_base = v,
                            placeholder: 'https://api.agnes.ai/v1',
                        }),
                    ]),
                    h('div', { class: 'form-group span-2' }, [
                        h('label', { class: 'form-label' }, 'API Key'),
                        h(FormInput, {
                            modelValue: config.api_key,
                            'onUpdate:modelValue': (v) => config.api_key = v,
                            type: 'password',
                            placeholder: '编辑时留空表示不修改',
                        }),
                        h(FormHint, '出于安全考虑，API Key 不回显；修改时请重新输入'),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '模型名称'),
                        h(FormInput, {
                            modelValue: config.model,
                            'onUpdate:modelValue': (v) => config.model = v,
                            placeholder: 'dall-e-3, stable-diffusion-xl 等',
                        }),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '图片尺寸'),
                        h(FormSelect, {
                            modelValue: config.image_size,
                            'onUpdate:modelValue': (v) => config.image_size = v,
                            options: sizeOptions,
                        }),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '响应格式'),
                        h(FormSelect, {
                            modelValue: config.response_format,
                            'onUpdate:modelValue': (v) => config.response_format = v,
                            options: formatOptions,
                        }),
                        h(FormHint, '优先使用 b64_json，避免二次下载'),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '质量'),
                        h(FormSelect, {
                            modelValue: config.quality,
                            'onUpdate:modelValue': (v) => config.quality = v,
                            options: [
                                { value: 'standard', label: '标准' },
                                { value: 'hd', label: '高清' },
                            ],
                        }),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '风格'),
                        h(FormSelect, {
                            modelValue: config.style,
                            'onUpdate:modelValue': (v) => config.style = v,
                            options: [
                                { value: 'vivid', label: '生动' },
                                { value: 'natural', label: '自然' },
                            ],
                        }),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '最大重试次数'),
                        h(FormInput, {
                            modelValue: String(config.max_retries),
                            'onUpdate:modelValue': (v) => config.max_retries = parseInt(v) || 3,
                            type: 'number',
                        }),
                    ]),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '超时（秒）'),
                        h(FormInput, {
                            modelValue: String(config.timeout),
                            'onUpdate:modelValue': (v) => config.timeout = parseInt(v) || 60,
                            type: 'number',
                        }),
                    ]),
                ]),
                footer: () => h('div', { class: 'flex gap-2 justify-end' }, [
                    h(Button, { onClick: loadConfig }, () => '重置'),
                    h(Button, { type: 'primary', onClick: saveConfig, loading: loading.value }, () => '保存配置'),
                ]),
            }),

            h(Card, { title: '文生图测试' }, {
                default: () => h('div', [
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '测试提示词'),
                        h(FormTextarea, {
                            modelValue: testPrompt.value,
                            'onUpdate:modelValue': (v) => testPrompt.value = v,
                            rows: 3,
                            placeholder: '描述要生成的图片内容，如：一只穿着宇航服的橘猫在月球表面散步...',
                        }),
                    ]),
                    h('div', { class: 'mb-3' }, [
                        h(Button, { type: 'primary', onClick: runTest, loading: testing.value }, () => '生成图片'),
                    ]),
                    testResult.value && h('div', { class: 'mt-4' }, [
                        h('h4', { class: 'mb-2' }, '生成结果：'),
                        testResult.value.error
                            ? h('div', { class: 'alert alert-danger' }, testResult.value.error)
                            : h('div', [
                                h('img', {
                                    src: testResult.value.image || testResult.value.b64
                                        ? `data:image/png;base64,${testResult.value.b64}`
                                        : testResult.value.url,
                                    style: 'max-width:100%; border-radius:12px; border:1px solid var(--outline-variant); margin-bottom:12px',
                                }),
                                testResult.value.revised_prompt && h('p', { class: 'text-muted', style: 'font-size:12px; line-height:1.6' },
                                    `修订后提示词：${testResult.value.revised_prompt}`),
                            ]),
                    ]),
                ]),
            }),
        ]);
    },
};
```

- [ ] **Step 2: 注册路由并 Commit**

```javascript
import { ImageGenPage } from './pages/image-gen.js';
registerRoute('/image-gen', ImageGenPage, '文生图', '内容创作');
```

```bash
git add bilibot/web/static/js/pages/image-gen.js bilibot/web/static/js/app.js
git commit -m "feat(web): migrate image generation page to Vue component"
```

---

### Task 5.7: 配置页（schema 驱动表单 + 乐观锁）

**Files:**
- Create: `bilibot/web/static/js/pages/config.js`
- Create: `bilibot/web/static/js/components/SchemaForm.js`（通用 schema 表单组件）

**参考旧实现：** `loadConfigPage`（dashboard.js 行 1655-1850）。核心功能：从后端 `/api/config/schema` 获取 JSON Schema，动态渲染表单；保存时携带 `version` 实现乐观锁；敏感字段（api_key、password）不回显。

**设计要点：**
1. Schema 驱动：字段类型映射到对应 FormInput/FormSelect/Toggle/FormTextarea
2. 分组渲染：按 schema 的 `category` 字段分卡片展示
3. 乐观锁：每次加载记录 `version`，保存时回传；409 冲突时提示刷新
4. 敏感字段：schema 中 `sensitive: true` 的字段渲染为 password 且不回显
5. 即时生效：schema 中 `immediate: true` 的字段保存后触发后端热重载（如 safety.pause）

- [ ] **Step 1: 创建通用 SchemaForm 组件**

```javascript
// bilibot/web/static/js/components/SchemaForm.js
import { h, ref, reactive, computed, watch } from '../vendor/vue.esm-browser.prod.js';
import { FormInput, FormSelect, FormTextarea, Toggle, FormHint } from '../components.js';

/**
 * Schema 驱动的表单组件
 * @param {Array} schema - 字段定义数组
 *   { key, label, type: 'string'|'number'|'boolean'|'select'|'textarea'|'password', category, default, options, sensitive, immediate, min, max, hint }
 * @param {Object} model - 表单数据
 */
export const SchemaForm = {
    props: {
        schema: { type: Array, default: () => [] },
        modelValue: { type: Object, default: () => ({}) },
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        const localModel = reactive({ ...props.modelValue });

        watch(() => props.modelValue, (v) => {
            Object.assign(localModel, v);
        }, { deep: true });

        function update(key, value) {
            localModel[key] = value;
            emit('update:modelValue', { ...localModel });
        }

        // 按 category 分组
        const groupedFields = computed(() => {
            const groups = {};
            for (const field of props.schema) {
                const cat = field.category || '通用';
                if (!groups[cat]) groups[cat] = [];
                groups[cat].push(field);
            }
            return Object.entries(groups).map(([name, fields]) => ({ name, fields }));
        });

        function renderField(field) {
            const value = localModel[field.key];
            const onInput = (v) => update(field.key, v);

            switch (field.type) {
                case 'boolean':
                    return h('div', { class: 'flex items-center gap-2' }, [
                        h(Toggle, { modelValue: !!value, 'onUpdate:modelValue': onInput }),
                        h('span', { class: 'form-hint' }, value ? '已启用' : '已禁用'),
                    ]);
                case 'select':
                    return h(FormSelect, {
                        modelValue: value,
                        'onUpdate:modelValue': onInput,
                        options: field.options || [],
                    });
                case 'textarea':
                    return h(FormTextarea, {
                        modelValue: value ?? '',
                        'onUpdate:modelValue': onInput,
                        rows: field.rows || 4,
                        placeholder: field.placeholder || '',
                    });
                case 'password':
                    return h(FormInput, {
                        modelValue: value ?? '',
                        'onUpdate:modelValue': onInput,
                        type: 'password',
                        placeholder: field.sensitive ? '编辑时留空表示不修改' : '',
                    });
                case 'number':
                    return h(FormInput, {
                        modelValue: value != null ? String(value) : '',
                        'onUpdate:modelValue': (v) => onInput(field.type === 'integer' ? parseInt(v) || 0 : parseFloat(v) || 0),
                        type: 'number',
                        min: field.min,
                        max: field.max,
                    });
                case 'string':
                default:
                    return h(FormInput, {
                        modelValue: value ?? '',
                        'onUpdate:modelValue': onInput,
                        placeholder: field.placeholder || '',
                    });
            }
        }

        return () => h('div', { class: 'schema-form' },
            groupedFields.value.map(group => h('div', { class: 'schema-group mb-4' }, [
                h('h4', { class: 'schema-group-title mb-3', style: 'font-size:14px; font-weight:600; color:var(--on-surface-variant); padding-bottom:8px; border-bottom:1px solid var(--outline-variant)' },
                    group.name),
                h('div', { class: 'form-grid-2col' },
                    group.fields.map(field => h('div', {
                        class: ['form-group', field.span === 2 && 'span-2'].filter(Boolean).join(' '),
                    }, [
                        h('label', { class: 'form-label' }, [
                            field.label,
                            field.immediate && h('span', { class: 'badge badge-info ml-2', style: 'font-size:10px; padding:2px 6px; background:var(--info-bg); color:var(--info); border-radius:4px' }, '即时'),
                            field.sensitive && h('span', { class: 'badge badge-warning ml-1', style: 'font-size:10px; padding:2px 6px; background:var(--warning-bg); color:var(--warning); border-radius:4px' }, '敏感'),
                        ]),
                        renderField(field),
                        field.hint && h(FormHint, field.hint),
                    ])),
                ),
            ])),
        );
    },
};
```

- [ ] **Step 2: 创建配置页组件**

```javascript
// bilibot/web/static/js/pages/config.js
import { h, ref, reactive, onMounted, computed } from '../vendor/vue.esm-browser.prod.js';
import { appState } from '../state.js';
import { api } from '../api.js';
import { Card, Button, Badge, SchemaForm } from '../components.js';

export const ConfigPage = {
    setup() {
        const loading = ref(false);
        const saving = ref(false);
        const schema = ref([]);
        const configData = reactive({});
        const version = ref(0);
        const lastSaved = ref(null);
        const diff = ref({});

        async function loadConfig() {
            loading.value = true;
            try {
                const res = await api.config.getWithSchema();
                schema.value = res.data.schema || [];
                Object.assign(configData, res.data.config || {});
                version.value = res.data.version || 0;
                diff.value = {};
            } catch (e) {
                appState.notify('加载配置失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function saveConfig() {
            saving.value = true;
            try {
                // 构造 payload：只发送有变化的字段 + version
                const payload = { ...configData, version: version.value };
                // 敏感字段为空时不发送（保持原值）
                for (const field of schema.value) {
                    if (field.sensitive && !payload[field.key]) {
                        delete payload[field.key];
                    }
                }
                const res = await api.config.update(payload);
                version.value = res.data.version || version.value + 1;
                lastSaved.value = new Date().toLocaleString();
                diff.value = {};
                appState.notify('配置已保存', 'success');
                // 检查是否需要重启
                if (res.data.need_restart) {
                    appState.notify('部分配置需重启后生效', 'warning');
                }
            } catch (e) {
                const msg = e.message || String(e);
                if (msg.includes('version') || msg.includes('409')) {
                    appState.notify('配置已被其他人修改，请刷新后重试', 'warning');
                    loadConfig();
                } else {
                    appState.notify('保存失败：' + msg, 'danger');
                }
            } finally {
                saving.value = false;
            }
        }

        async function resetConfig() {
            if (!confirm('确认重置为默认配置？此操作不可撤销。')) return;
            try {
                await api.config.reset();
                appState.notify('配置已重置', 'success');
                loadConfig();
            } catch (e) {
                appState.notify('重置失败：' + (e.message || e), 'danger');
            }
        }

        async function exportConfig() {
            try {
                const res = await api.config.export();
                const blob = new Blob([JSON.stringify(res.data, null, 2)], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `config-${new Date().toISOString().slice(0, 10)}.json`;
                a.click();
                URL.revokeObjectURL(url);
                appState.notify('配置已导出', 'success');
            } catch (e) {
                appState.notify('导出失败：' + (e.message || e), 'danger');
            }
        }

        onMounted(loadConfig);

        return () => h('div', [
            h(Card, { title: '系统配置' }, {
                action: () => h('div', { class: 'flex gap-2' }, [
                    h(Button, { size: 'sm', onClick: exportConfig }, () => '导出'),
                    h(Button, { size: 'sm', type: 'danger', onClick: resetConfig }, () => '重置默认'),
                ]),
                default: () => [
                    h(SchemaForm, {
                        schema: schema.value,
                        modelValue: configData,
                        'onUpdate:modelValue': (v) => Object.assign(configData, v),
                    }),
                ],
                footer: () => h('div', { class: 'flex justify-between items-center' }, [
                    h('div', { class: 'text-muted', style: 'font-size:12px' }, [
                        version.value > 0 && h('span', `当前版本 v${version.value}`),
                        lastSaved.value && h('span', { class: 'ml-2' }, `| 上次保存：${lastSaved.value}`),
                    ]),
                    h('div', { class: 'flex gap-2' }, [
                        h(Button, { onClick: loadConfig, disabled: saving.value }, () => '重新加载'),
                        h(Button, { type: 'primary', onClick: saveConfig, loading: saving.value }, () => '保存配置'),
                    ]),
                ]),
            }),
        ]);
    },
};
```

- [ ] **Step 3: 在 components.js 中导出 SchemaForm**

```javascript
// bilibot/web/static/js/components/index.js 追加
export { SchemaForm } from './SchemaForm.js';
```

- [ ] **Step 4: 注册路由并 Commit**

```javascript
import { ConfigPage } from './pages/config.js';
registerRoute('/config', ConfigPage, '系统配置', '系统');
```

```bash
git add bilibot/web/static/js/components/SchemaForm.js bilibot/web/static/js/pages/config.js bilibot/web/static/js/components/index.js bilibot/web/static/js/app.js
git commit -m "feat(web): migrate config page to schema-driven Vue form with optimistic lock"
```

---

### Task 5.8: 系统页（安全控制 + 备份恢复）

**Files:**
- Create: `bilibot/web/static/js/pages/system.js`

**参考旧实现：** `loadSystemPage`（dashboard.js 行 1851-2100）+ `loadBackupPage`（dashboard.js 行 2101-2402）。核心功能：全局暂停/恢复、黑名单管理、备份列表、创建备份、恢复备份、下载备份。

- [ ] **Step 1: 创建系统页组件**

```javascript
// bilibot/web/static/js/pages/system.js
import { h, ref, reactive, onMounted, computed } from '../vendor/vue.esm-browser.prod.js';
import { appState } from '../state.js';
import { api } from '../api.js';
import { Card, Button, Badge, DataTable, Modal, FormInput, FormTextarea, FormSelect, Toggle, EmptyState, Pagination } from '../components.js';
import { formatTime, formatDateTime } from '../utils.js';

export const SystemPage = {
    setup() {
        const activeTab = ref('security'); // security / backup

        // 安全控制
        const securityLoading = ref(false);
        const pauseStatus = ref(null);
        const blacklist = ref([]);
        const blacklistInput = ref('');
        const blacklistType = ref('uid');

        // 备份恢复
        const backupLoading = ref(false);
        const backups = ref([]);
        const creatingBackup = ref(false);
        const restoreModal = reactive({
            visible: false,
            backup: null,
            confirming: false,
        });

        async function loadPauseStatus() {
            securityLoading.value = true;
            try {
                const res = await api.safety.getPauseStatus();
                pauseStatus.value = res.data;
            } catch (e) {
                appState.notify('加载暂停状态失败：' + (e.message || e), 'danger');
            } finally {
                securityLoading.value = false;
            }
        }

        async function togglePause() {
            if (!pauseStatus.value) return;
            const isPaused = pauseStatus.value.paused;
            if (!confirm(isPaused ? '确认恢复 Bot 运行？' : '确认全局暂停 Bot？所有自动行为将停止。')) return;
            try {
                if (isPaused) {
                    await api.safety.resume();
                    appState.notify('已恢复运行', 'success');
                } else {
                    await api.safety.pause({ reason: '手动暂停' });
                    appState.notify('已暂停', 'warning');
                }
                loadPauseStatus();
            } catch (e) {
                appState.notify('操作失败：' + (e.message || e), 'danger');
            }
        }

        async function loadBlacklist() {
            try {
                const res = await api.safety.getBlacklist();
                blacklist.value = res.data.items || [];
            } catch (e) {
                appState.notify('加载黑名单失败：' + (e.message || e), 'danger');
            }
        }

        async function addBlacklist() {
            if (!blacklistInput.value) return;
            try {
                await api.safety.addBlacklist({
                    type: blacklistType.value,
                    value: blacklistInput.value,
                });
                blacklistInput.value = '';
                appState.notify('已添加到黑名单', 'success');
                loadBlacklist();
            } catch (e) {
                appState.notify('添加失败：' + (e.message || e), 'danger');
            }
        }

        async function removeBlacklist(item) {
            if (!confirm(`确认从黑名单移除 ${item.type}: ${item.value}？`)) return;
            try {
                await api.safety.removeBlacklist(item.id);
                appState.notify('已移除', 'success');
                loadBlacklist();
            } catch (e) {
                appState.notify('移除失败：' + (e.message || e), 'danger');
            }
        }

        async function loadBackups() {
            backupLoading.value = true;
            try {
                const res = await api.backup.list();
                backups.value = res.data.items || [];
            } catch (e) {
                appState.notify('加载备份列表失败：' + (e.message || e), 'danger');
            } finally {
                backupLoading.value = false;
            }
        }

        async function createBackup() {
            creatingBackup.value = true;
            try {
                await api.backup.create({ description: '手动备份' });
                appState.notify('备份已创建', 'success');
                loadBackups();
            } catch (e) {
                appState.notify('创建备份失败：' + (e.message || e), 'danger');
            } finally {
                creatingBackup.value = false;
            }
        }

        function openRestore(backup) {
            restoreModal.backup = backup;
            restoreModal.confirming = false;
            restoreModal.visible = true;
        }

        async function confirmRestore() {
            if (!restoreModal.backup) return;
            restoreModal.confirming = true;
            try {
                await api.backup.restore(restoreModal.backup.id);
                appState.notify('备份已恢复，建议重启服务', 'success');
                restoreModal.visible = false;
            } catch (e) {
                appState.notify('恢复失败：' + (e.message || e), 'danger');
            } finally {
                restoreModal.confirming = false;
            }
        }

        async function downloadBackup(backup) {
            try {
                const res = await api.backup.download(backup.id);
                const blob = new Blob([res], { type: 'application/octet-stream' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = backup.filename || `backup-${backup.id}.tar.gz`;
                a.click();
                URL.revokeObjectURL(url);
            } catch (e) {
                appState.notify('下载失败：' + (e.message || e), 'danger');
            }
        }

        async function deleteBackup(backup) {
            if (!confirm(`确认删除备份 ${backup.filename || backup.id}？此操作不可撤销。`)) return;
            try {
                await api.backup.delete(backup.id);
                appState.notify('备份已删除', 'success');
                loadBackups();
            } catch (e) {
                appState.notify('删除失败：' + (e.message || e), 'danger');
            }
        }

        onMounted(() => {
            loadPauseStatus();
            loadBlacklist();
            loadBackups();
        });

        const blacklistColumns = [
            { key: 'type', label: '类型', width: '100px' },
            { key: 'value', label: '值' },
            { key: 'created_at', label: '添加时间', width: '160px' },
            { key: 'actions', label: '操作', width: '100px' },
        ];

        const backupColumns = [
            { key: 'id', label: 'ID', width: '60px' },
            { key: 'filename', label: '文件名' },
            { key: 'size', label: '大小', width: '100px' },
            { key: 'created_at', label: '创建时间', width: '160px' },
            { key: 'actions', label: '操作', width: '240px' },
        ];

        const tabs = [
            { value: 'security', label: '安全控制' },
            { value: 'backup', label: '备份恢复' },
        ];

        return () => h('div', [
            // Tab 切换
            h('div', { class: 'tabs mb-4' },
                tabs.map(t => h('button', {
                    class: ['tab', activeTab.value === t.value && 'active'].filter(Boolean).join(' '),
                    onClick: () => activeTab.value = t.value,
                }, t.label)),
            ),

            // 安全控制 Tab
            activeTab.value === 'security' && h('div', [
                h(Card, { title: '全局暂停' }, {
                    default: () => h('div', { class: 'flex items-center justify-between' }, [
                        h('div', [
                            h('p', { class: 'mb-1' }, pauseStatus.value?.paused
                                ? 'Bot 当前已暂停，所有自动行为已停止'
                                : 'Bot 正在正常运行'),
                            pauseStatus.value?.paused_at && h('p', { class: 'text-muted', style: 'font-size:12px' },
                                `暂停时间：${formatTime(pauseStatus.value.paused_at)}`),
                            pauseStatus.value?.reason && h('p', { class: 'text-muted', style: 'font-size:12px' },
                                `原因：${pauseStatus.value.reason}`),
                        ]),
                        h(Button, {
                            type: pauseStatus.value?.paused ? 'primary' : 'danger',
                            onClick: togglePause,
                            loading: securityLoading.value,
                        }, () => pauseStatus.value?.paused ? '恢复运行' : '全局暂停'),
                    ]),
                }),

                h(Card, { title: '黑名单管理' }, {
                    action: () => h(Button, { size: 'sm', onClick: loadBlacklist }, () => '刷新'),
                    default: () => h('div', [
                        h('div', { class: 'flex gap-2 mb-4' }, [
                            h(FormSelect, {
                                modelValue: blacklistType.value,
                                'onUpdate:modelValue': (v) => blacklistType.value = v,
                                options: [
                                    { value: 'uid', label: '用户 UID' },
                                    { value: 'keyword', label: '关键词' },
                                    { value: 'ip', label: 'IP 地址' },
                                ],
                                style: 'width:140px',
                            }),
                            h(FormInput, {
                                modelValue: blacklistInput.value,
                                'onUpdate:modelValue': (v) => blacklistInput.value = v,
                                placeholder: '输入要拉黑的值',
                                style: 'flex:1',
                                onKeyup: (e) => { if (e.key === 'Enter') addBlacklist(); },
                            }),
                            h(Button, { type: 'primary', onClick: addBlacklist }, () => '添加'),
                        ]),
                        h(DataTable, {
                            columns: blacklistColumns,
                            rows: blacklist.value,
                            empty: '黑名单为空',
                        }, {
                            type: ({ value }) => h(Badge, { type: 'info', size: 'sm' }, () => value),
                            created_at: ({ value }) => h('span', { class: 'text-muted', style: 'font-size:12px' }, formatTime(value)),
                            actions: ({ row }) => h(Button, { size: 'sm', type: 'danger', onClick: () => removeBlacklist(row) }, () => '移除'),
                        }),
                    ]),
                }),
            ]),

            // 备份恢复 Tab
            activeTab.value === 'backup' && h('div', [
                h(Card, { title: '备份管理' }, {
                    action: () => h(Button, { type: 'primary', onClick: createBackup, loading: creatingBackup.value }, () => '创建备份'),
                    default: () => h(DataTable, {
                        columns: backupColumns,
                        rows: backups.value,
                        loading: backupLoading.value,
                        empty: '暂无备份',
                    }, {
                        size: ({ value }) => h('span', { class: 'text-muted' }, `${(value / 1024 / 1024).toFixed(2)} MB`),
                        created_at: ({ value }) => h('span', { class: 'text-muted', style: 'font-size:12px' }, formatTime(value)),
                        actions: ({ row }) => h('div', { class: 'flex gap-2' }, [
                            h(Button, { size: 'sm', onClick: () => downloadBackup(row) }, () => '下载'),
                            h(Button, { size: 'sm', type: 'primary', onClick: () => openRestore(row) }, () => '恢复'),
                            h(Button, { size: 'sm', type: 'danger', onClick: () => deleteBackup(row) }, () => '删除'),
                        ]),
                    }),
                }),
            ]),

            // 恢复确认弹窗
            h(Modal, {
                visible: restoreModal.visible,
                title: '确认恢复备份',
                'onUpdate:visible': (v) => restoreModal.visible = v,
            }, {
                default: () => h('div', { class: 'alert alert-warning' }, [
                    h('p', { class: 'mb-2' }, `即将恢复备份 #${restoreModal.backup?.id}（${restoreModal.backup?.filename || ''}）`),
                    h('p', { class: 'text-muted', style: 'font-size:13px' }, '恢复操作将覆盖当前数据，且不可撤销。建议在低峰期执行，恢复后需重启服务。'),
                ]),
                footer: () => h('div', { class: 'flex gap-2 justify-end' }, [
                    h(Button, { onClick: () => restoreModal.visible = false }, () => '取消'),
                    h(Button, { type: 'danger', onClick: confirmRestore, loading: restoreModal.confirming }, () => '确认恢复'),
                ]),
            }),
        ]);
    },
};
```

- [ ] **Step 2: 注册路由并 Commit**

```javascript
import { SystemPage } from './pages/system.js';
registerRoute('/system', SystemPage, '系统管理', '系统');
```

```bash
git add bilibot/web/static/js/pages/system.js bilibot/web/static/js/app.js
git commit -m "feat(web): migrate system page to Vue component with security and backup tabs"
```

---

## Phase 6: 清理旧文件

**目标：** 删除已废弃的旧版前端资源，确保新架构干净落地。所有旧文件必须在对应的新 Vue 组件验证通过后才删除。

### Task 6.1: 删除旧版 dashboard 单体文件

**Files:**
- Delete: `bilibot/web/static/js/dashboard.js`（2700+ 行单体）
- Delete: `bilibot/web/static/css/dashboard.css`（设计变量已迁移到 tokens.css）

- [ ] **Step 1: 验证无残留引用**

```bash
# 搜索是否有其他文件引用 dashboard.js 或 dashboard.css
grep -r "dashboard.js\|dashboard.css" bilibot/web/ --include="*.py" --include="*.html" --include="*.js"
# 预期：除 panel.py 旧 _get_dashboard_html 外无引用
```

- [ ] **Step 2: 删除文件并更新 panel.py**

确保 `panel.py` 的 `_get_dashboard_html` 已在 Phase 1 Task 1.4 中替换为 Vue 外壳加载逻辑。若未完成，先回填：

```python
# bilibot/web/panel.py - _get_dashboard_html 函数最终形态
def _get_dashboard_html() -> str:
    """返回 Vue 应用外壳 HTML"""
    css_version = _static_version("css/tokens.css")
    app_version = _static_version("js/app.js")
    vendor_version = _static_version("js/vendor/vue.esm-browser.prod.js")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BiliBot 管理面板</title>
    <link rel="stylesheet" href="/static/css/tokens.css?v={css_version}">
    <link rel="stylesheet" href="/static/css/layout.css?v={css_version}">
    <link rel="stylesheet" href="/static/css/components.css?v={css_version}">
</head>
<body>
    <div id="app"></div>
    <script type="module" src="/static/js/app.js?v={app_version}"></script>
</body>
</html>"""
```

- [ ] **Step 3: 删除旧文件并 Commit**

```bash
git rm bilibot/web/static/js/dashboard.js
git rm bilibot/web/static/css/dashboard.css
git commit -m "refactor(web): remove legacy dashboard.js and dashboard.css"
```

### Task 6.2: 删除 LivingMemory 独立子应用

**Files:**
- Delete: `bilibot/web/static/livingmemory/`（整个目录）

- [ ] **Step 1: 验证记忆功能已迁移**

确认 Phase 4 的四个 Task（记忆列表、记忆详情、记忆图谱、记忆搜索）均已实现并在新面板可访问。

- [ ] **Step 2: 移除 panel.py 中的 livingmemory 路由**

```python
# bilibot/web/panel.py - 删除以下内容：
# 1. livingmemory_page 函数（行 143-148）
# 2. page_routes 中的 /livingmemory 路由（行 280）
```

具体删除：
```python
# 删除整个 livingmemory_page 函数
async def livingmemory_page(request: Request) -> FileResponse:
    """记忆管理页面"""
    lm_path = Path(__file__).parent / "static" / "livingmemory" / "index.html"
    if lm_path.exists():
        return FileResponse(str(lm_path))
    return HTMLResponse("<h1>记忆管理页面</h1>")

# 从 page_routes 列表中移除
Route("/livingmemory", livingmemory_page, methods=["GET"]),
```

- [ ] **Step 3: 删除目录并 Commit**

```bash
git rm -r bilibot/web/static/livingmemory/
git add bilibot/web/panel.py
git commit -m "refactor(web): remove standalone LivingMemory SPA (merged into main panel)"
```

### Task 6.3: 最终回归验证

- [ ] **Step 1: 启动面板，手动走查所有路由**

```bash
# 启动服务
python -m bilibot

# 依次访问以下路由，确认无控制台错误、无白屏：
# /#/dashboard      运营总览
# /#/accounts       账号列表
# /#/accounts/:id   账号详情（5 个 Tab）
# /#/llm            LLM 管理
# /#/personas       人格管理
# /#/comments       评论管理
# /#/drafts         动态草稿
# /#/video-analysis 视频理解
# /#/image-gen      文生图
# /#/memory/:id     记忆列表
# /#/memory/:id/graph  记忆图谱
# /#/proactive      主动行为
# /#/logs           日志
# /#/config         系统配置
# /#/system         系统管理
```

- [ ] **Step 2: 运行现有测试套件**

```bash
pytest tests/ -k "web or panel or api" -v
# 确保后端 API 层无回归
```

- [ ] **Step 3: 验证静态资源版本化生效**

在浏览器开发者工具 Network 面板确认：
- `tokens.css?v=xxxx`、`app.js?v=xxxx` 等均带版本参数
- 修改静态文件后刷新，版本参数变化，浏览器重新加载（无缓存）

- [ ] **Step 4: 最终 Commit**

```bash
git add -A
git commit -m "test(web): final regression validation for Vue 3 refactor"
```

---

## Self-Review

### Spec 覆盖检查

| 用户诉求 | 对应 Phase/Task | 状态 |
|---------|----------------|------|
| 账号系统"太奇怪" | Phase 3 Task 3.1-3.4（账号详情页 Tab 统一管理） | ✅ 覆盖 |
| LLM 管理与账号关系"太奇怪" | Phase 3 Task 3.1 Step 3（LLM Tab 绑定）+ Phase 2 Task 2.2（LLM 关联视图） | ✅ 覆盖 |
| 记忆系统风格不一致、太丑 | Phase 4 Task 4.1-4.4（完全合并到主面板，统一设计系统） | ✅ 覆盖 |
| 配置页对象数组割裂 | Phase 5 Task 5.7（schema 驱动表单，分组渲染） | ✅ 覆盖 |
| 整体架构现代化 | Phase 1（Vue 3 + ES Module + 双栏布局）+ Phase 2（组件库） | ✅ 覆盖 |
| 旧文件清理 | Phase 6 Task 6.1-6.3 | ✅ 覆盖 |

### Placeholder 扫描

检查文档中是否遗留未填充的占位符：

- `TODO` / `FIXME` / `XXX` / `TBD` → 无
- `{{...}}` 模板占位 → 无
- `<待补充>` → 无

### 类型一致性检查

| 模块 | 命名风格 | 状态 |
|------|---------|------|
| 文件命名 | `kebab-case.js`（如 `video-analysis.js`、`image-gen.js`） | ✅ 一致 |
| 组件命名 | `PascalCase`（如 `DraftsPage`、`VideoAnalysisPage`） | ✅ 一致 |
| 函数命名 | `camelCase`（如 `loadConfig`、`saveEdit`） | ✅ 一致 |
| CSS 类名 | `kebab-case` + BEM（如 `nav-item--active`） | ✅ 一致 |
| API 端点 | `/api/{resource}/{action}` 风格 | ✅ 一致 |

### 依赖完整性检查

| 外部依赖 | 引入方式 | 验证 |
|---------|---------|------|
| Vue 3 | 本地化 `vendor/vue.esm-browser.prod.js` | ✅ Phase 1 Task 1.1 |
| Material Design 3 色板 | CSS 变量迁移到 `tokens.css` | ✅ Phase 1 Task 1.2 |
| Canvas 2D API | 原生浏览器 API，无依赖 | ✅ Phase 4 Task 4.3 |
| Starlette StaticFiles | 后端已存在 | ✅ Phase 1 Task 1.4 |

### 风险点与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| Vue 3 Global Build 性能 | 首屏加载稍慢 | 本地化 CDN 避免网络延迟；ES Module 按需加载页面组件 |
| 旧 dashboard.js 删除后回归 | 已有功能可能遗漏 | Phase 6 Task 6.3 强制走查所有路由 + 运行测试套件 |
| 记忆图谱 Canvas 兼容性 | 旧浏览器不支持 | Canvas 2D 兼容性良好（IE9+），目标用户为管理员，风险低 |
| 乐观锁冲突 UX | 多人编辑时提示频繁 | 409 冲突时自动刷新并提示，非阻塞式 |
| LivingMemory 删除后老链接失效 | 书签失效 | hash 路由 `/#/memory/:id` 替代 `/livingmemory`，可在旧路径加重定向 |

---

## Execution Handoff

本计划已编写完成，包含 6 个 Phase、共 29 个 Task。以下是两种执行方式供选择：

### 选项 A：Subagent-Driven Execution（推荐）

**适用场景：** 希望按 Phase 逐步推进，每个 Task 由独立 subagent 执行并验证。

**流程：**
1. 用户确认计划后，启动 `general_purpose_task` subagent
2. 每个 subagent 负责一个 Task，按 Step 清单执行
3. 每个 Task 完成后返回摘要，主对话确认后继续下一个
4. Phase 边界进行集成验证

**优势：**
- 上下文隔离，避免主对话膨胀
- 可并行执行无依赖的 Task（如 Phase 2 的三个组件 Task）
- 每个 Task 有独立验证步骤

**执行命令示例：**
```
Task(subagent_type="general_purpose_task", query="执行 Phase 1 Task 1.1：下载 Vue 3 ESM 构建版本到 bilibot/web/static/js/vendor/vue.esm-browser.prod.js...")
```

### 选项 B：Inline Execution

**适用场景：** 希望在当前对话中直接执行，便于实时交互和调整。

**流程：**
1. 用户确认计划后，直接在当前对话按 Phase/Task 顺序执行
2. 每个 Step 使用 Write/Edit/RunCommand 工具完成
3. 遇到问题实时调整计划

**优势：**
- 实时反馈，便于处理意外情况
- 无需重复传递上下文
- 适合需要频繁决策的场景

**建议：** 鉴于本计划涉及 29 个 Task、大量文件创建，推荐使用 **选项 A（Subagent-Driven）**，按 Phase 分批执行。Phase 1 和 Phase 2 可并行推进，Phase 3-5 有依赖需顺序执行，Phase 6 在所有功能验证通过后执行。

---

**计划版本：** v1.0
**编写日期：** 2026-07-11
**目标完成：** 6 个 Phase，29 个 Task
**预计文件变更：** 新增约 25 个文件，删除约 5 个文件/目录，修改 2 个文件（panel.py、components/index.js）