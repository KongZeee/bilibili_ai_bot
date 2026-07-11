// pages/proactive.js - 主动行为页（Golden Time 设计稿）
const { defineComponent, h, ref, onMounted } = window.Vue;
import { api } from '../api.js';
import { appState, showToast, refreshAccounts } from '../state.js';
import { Button, Badge, FormSelect, Loading, EmptyState, Icon } from '../components/common.js';
import { formatTime } from '../utils.js';

export const ProactivePage = defineComponent({
    name: 'ProactivePage',
    setup() {
        const tasks = ref([]);
        const loading = ref(false);
        const selectedAccount = ref('');
        const triggering = ref(false);

        async function loadTasks() {
            if (!selectedAccount.value) return;
            loading.value = true;
            try {
                const data = await api.accounts.tasks(selectedAccount.value);
                tasks.value = data.items || data || [];
            } catch (e) {
                // 后端无 GET /api/accounts/{id}/tasks 列表路由（404），显示空状态而非报错
                tasks.value = [];
            } finally { loading.value = false; }
        }

        async function triggerVideo() {
            if (!selectedAccount.value) return;
            triggering.value = true;
            try {
                await api.post(`/api/accounts/${selectedAccount.value}/tasks/proactive-video`);
                showToast('主动视频任务已触发', 'success');
                await loadTasks();
            } catch (e) { showToast('触发失败: ' + e.message, 'error'); }
            finally { triggering.value = false; }
        }

        async function triggerDynamic() {
            if (!selectedAccount.value) return;
            triggering.value = true;
            try {
                await api.post(`/api/accounts/${selectedAccount.value}/tasks/dynamic`);
                showToast('动态发布任务已触发', 'success');
                await loadTasks();
            } catch (e) { showToast('触发失败: ' + e.message, 'error'); }
            finally { triggering.value = false; }
        }

        onMounted(() => {
            if (!appState.accountsLoaded) refreshAccounts();
            if (appState.accounts.length > 0 && !selectedAccount.value) {
                const first = appState.accounts[0];
                selectedAccount.value = appState.currentAccountId || first.account_id || first.id;
                loadTasks();
            }
        });

        const tableGrid = 'minmax(0, 1.2fr) 8rem 8rem minmax(0, 1fr) 10rem';

        return () => loading.value && tasks.value.length === 0 && !selectedAccount.value
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                // ═══ hero-band：左侧任务统计 + 右侧账号选择 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左侧：hero-panel 任务统计
                    h('div', { class: 'hero-panel' }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('span', { class: 'eyebrow' }, '主动行为'),
                            h(Badge, { type: 'info' }, () => '调度'),
                        ]),
                        h('h2', {
                            style: 'margin:0; font-size:1.65rem; line-height:1.1; text-wrap:balance; word-break:keep-all;',
                        }, '主动任务'),
                        h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                            h('span', {
                                style: 'font-size:2.4rem; font-weight:500; line-height:1; font-variant-numeric:tabular-nums;',
                            }, String(tasks.value.length || 0)),
                            h('span', { class: 'muted m-0', style: 'font-size:0.9rem;' }, '条任务记录'),
                        ]),
                        h('p', { class: 'muted m-0' }, '管理主动视频生成与动态发布任务'),
                    ]),
                    // 右侧：账号选择 Card
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '账号'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '选择账号'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-2' }, [
                            h(FormSelect, {
                                modelValue: selectedAccount.value,
                                'onUpdate:modelValue': (v) => { selectedAccount.value = v; loadTasks(); },
                                options: appState.accounts.map(a => ({ value: a.account_id || a.id, label: a.name || a.account_id || a.id })),
                            }),
                            h('div', { class: 'flex items-center gap-2 flex-wrap' }, [
                                h(Button, {
                                    type: 'primary',
                                    onClick: triggerVideo,
                                    loading: triggering.value,
                                }, () => '触发视频'),
                                h(Button, {
                                    type: 'ghost',
                                    onClick: triggerDynamic,
                                }, () => '触发动态'),
                            ]),
                        ]),
                    ]),
                ]),

                // ═══ 数据表格 ═══
                h('article', {
                    class: 'grid gap-3',
                    style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                }, [
                    h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '任务队列'),
                            h('h2', {
                                style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;',
                            }, '任务记录'),
                        ]),
                        h(Button, {
                            type: 'ghost',
                            size: 'sm',
                            onClick: loadTasks,
                        }, () => '刷新'),
                    ]),
                    tasks.value.length === 0
                        ? h(EmptyState, { icon: 'folder', title: '暂无任务记录', desc: '当前账号还没有主动行为任务' })
                        : h('div', { class: 'grid', style: 'gap:0; min-width:0;' }, [
                            // 表头
                            h('div', {
                                class: 'grid items-center',
                                style: `grid-template-columns: ${tableGrid}; column-gap: calc(var(--spacing) * 2); padding-bottom: calc(var(--spacing) * 2); border-bottom: 1px solid hsl(var(--border)); color: hsl(var(--muted-foreground)); font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.14em;`,
                            }, [
                                h('span', { class: 'whitespace-nowrap' }, '任务名'),
                                h('span', { class: 'whitespace-nowrap' }, '类型'),
                                h('span', { class: 'whitespace-nowrap' }, '状态'),
                                h('span', { class: 'whitespace-nowrap' }, '下次执行'),
                                h('span', { class: 'whitespace-nowrap' }, '操作'),
                            ]),
                            // 数据行
                            ...tasks.value.map(t => h('div', {
                                key: t.id,
                                class: 'grid items-center',
                                style: `grid-template-columns: ${tableGrid}; column-gap: calc(var(--spacing) * 2); padding: calc(var(--spacing) * 2.3) 0; border-top: 1px solid hsl(var(--border)); font-size: 0.95rem;`,
                            }, [
                                h('span', { class: 'truncate' }, t.task_name || t.name || t.id || '-'),
                                h('span', {
                                    class: 'badge badge-info',
                                }, t.task_type || t.type || '-'),
                                h('span', {
                                    class: ['badge',
                                        t.state === 'done' ? 'badge-success'
                                        : t.state === 'failed' ? 'badge-danger'
                                        : t.state === 'running' ? 'badge-info'
                                        : 'badge-warning'].join(' '),
                                }, t.state || 'pending'),
                                h('span', {
                                    class: 'whitespace-nowrap truncate',
                                    style: 'color: hsl(var(--muted-foreground)); font-variant-numeric: tabular-nums; font-size:0.85rem;',
                                }, t.next_run ? formatTime(t.next_run) : (t.created_at ? formatTime(t.created_at) : '-')),
                                h('div', { class: 'flex items-center gap-1' }, [
                                    h('button', {
                                        class: 'btn btn-sm primary',
                                        onClick: () => {
                                            if (t.task_type === 'dynamic' || t.type === 'dynamic') triggerDynamic();
                                            else triggerVideo();
                                        },
                                    }, '触发'),
                                ]),
                            ])),
                        ]),
                ]),
            ]);
    },
});
