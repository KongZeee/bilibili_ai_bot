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
        // 规范化：后端返回 account_id，统一映射为 id 供前端使用
        list.forEach(acc => { if (!acc.id) acc.id = acc.account_id; });
        appState.accounts = list;
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
