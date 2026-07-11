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
