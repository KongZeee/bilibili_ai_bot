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

import { OverviewPage } from './pages/overview.js';
// 覆盖占位路由
registerRoute('/', OverviewPage, '总览');

import { PersonaListPage } from './pages/personas.js';
registerRoute('/personas', PersonaListPage, '人格管理');
import { LlmListPage } from './components/llm.js';
registerRoute('/llm', LlmListPage, 'LLM 管理');
import { AccountListPage, AccountDetailPage } from './components/accounts.js';
registerRoute('/accounts', AccountListPage, '账号管理');
registerRoute('/accounts/:id', AccountDetailPage, '账号详情');
import { MemoryRecallPage } from './components/memory/recall-page.js';
registerRoute('/memory/recall', MemoryRecallPage, '召回测试');
import { MemoryListPage } from './components/memory/list-page.js';
registerRoute('/memory/list', MemoryListPage, '记忆列表');
import { MemoryGraphPage } from './components/memory/graph-page.js';
registerRoute('/memory/graph', MemoryGraphPage, '记忆图谱');
import { LogsPage } from './pages/logs.js';
registerRoute('/logs', LogsPage, '日志');
import { CommentsPage } from './pages/comments.js';
registerRoute('/comments', CommentsPage, '评论');
import { ProactivePage } from './pages/proactive.js';
registerRoute('/proactive', ProactivePage, '主动行为');

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
