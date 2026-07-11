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
