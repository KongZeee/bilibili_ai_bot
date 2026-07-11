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
