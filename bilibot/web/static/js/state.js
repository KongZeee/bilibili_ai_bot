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
    /** 单 bot：始终指向唯一账号 id（无切换器） */
    currentAccountId: null,
    toasts: [],
});

// 供 api.js 解析 sole account（避免 api↔state 循环 import）
try { window.appState = appState; } catch (_) { /* ignore */ }

/**
 * 返回唯一 bot 账号 id（accounts[0]）。
 * 列表未加载或为空时返回 null。
 */
export function getSoleAccountId() {
    const list = appState.accounts || [];
    if (!list.length) return null;
    const a0 = list[0];
    return a0.account_id || a0.id || null;
}

/** 将 currentAccountId 固定为唯一账号（导出：页面勿手写 accountsLoaded） */
export function syncSoleAccountId() {
    const sole = getSoleAccountId();
    appState.currentAccountId = sole;
    return sole;
}

/** 规范化 accounts 列表并同步 sole id */
export function applyAccountsList(list) {
    const rows = Array.isArray(list) ? list : [];
    rows.forEach((acc) => {
        if (acc && !acc.id && acc.account_id) acc.id = acc.account_id;
    });
    appState.accounts = rows;
    appState.accountsLoaded = true;
    return syncSoleAccountId();
}

export function showToast(message, type = 'info') {
    const id = Date.now() + Math.random();
    appState.toasts.push({ id, message, type });
    const duration = (type === 'error' || type === 'warning') ? 7000 : 3000;
    setTimeout(() => {
        const idx = appState.toasts.findIndex(t => t.id === id);
        if (idx >= 0) appState.toasts.splice(idx, 1);
    }, duration);
}

export async function refreshAccounts() {
    // 需要在 app.js 中注入 api 后调用
    // 见 app.js 中的 initGlobalState()
    try {
        const { api } = window;
        const list = await api.accounts.list();
        applyAccountsList(list);
    } catch (e) {
        // 仍标记 loaded，避免各页永久等待 accountsLoaded
        appState.accountsLoaded = true;
        appState.accounts = appState.accounts || [];
        showToast('加载账号列表失败: ' + e.message, 'error');
    }
}

export async function refreshLlm() {
    try {
        const { api } = window;
        appState.llmProviders = await api.llm.list();
        appState.llmLoaded = true;
    } catch (e) {
        showToast('加载模型列表失败: ' + e.message, 'error');
    }
}

// refreshLlmProviders 别名（Phase 5 页面通过 appState.refreshLlmProviders() 调用）
appState.refreshLlmProviders = refreshLlm;

// notify 别名（Phase 5 页面通过 appState.notify(msg, type) 调用）
// type 映射：danger→error，其他保持不变
appState.notify = function(message, type = 'info') {
    const typeMap = { danger: 'error', success: 'success', warning: 'warning', info: 'info', error: 'error' };
    showToast(message, typeMap[type] || type);
};

export async function refreshPersonas() {
    try {
        const { api } = window;
        appState.personas = await api.personas.list();
        appState.personasLoaded = true;
    } catch (e) {
        showToast('加载人格列表失败: ' + e.message, 'error');
    }
}
