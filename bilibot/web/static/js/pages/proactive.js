// pages/proactive.js - 主动行为页（Golden Time 设计稿）
const { defineComponent, h, ref, onMounted, watch } = window.Vue;
import { api } from '../api.js';
import { appState, showToast, refreshAccounts } from '../state.js';
import { Button, Badge, FormSelect, Loading, EmptyState, Icon } from '../components/common.js';
import { formatTime, auditSceneLabel, auditStatusLabel, auditStatusBadgeType } from '../utils.js';

export const ProactivePage = defineComponent({
    name: 'ProactivePage',
    setup() {
        const tasks = ref([]);
        const loading = ref(false);
        const selectedAccount = ref('');
        const triggering = ref(false);
        let loadSeq = 0;

        async function loadTasks() {
            if (!selectedAccount.value) return;
            const seq = ++loadSeq;
            loading.value = true;
            try {
                const data = await api.accounts.tasks(selectedAccount.value, { page_size: 100 });
                if (seq !== loadSeq) return;
                tasks.value = data.items || (Array.isArray(data) ? data : []);
            } catch (e) {
                if (seq !== loadSeq) return;
                showToast('加载任务列表失败: ' + e.message, 'error');
                tasks.value = [];
            } finally {
                if (seq === loadSeq) loading.value = false;
            }
        }

        async function triggerVideo() {
            if (!selectedAccount.value) return;
            triggering.value = true;
            try {
                const data = await api.accounts.triggerProactiveVideo(selectedAccount.value);
                // 202 信封：无 task_id 不得报成功（防空响应误 toast）
                const taskId = data?.task_id || data?.id || '';
                if (!taskId) {
                    showToast('触发未返回任务 ID，请刷新任务列表确认', 'warning');
                    await loadTasks();
                    return;
                }
                const scene = data?.scene || 'proactive_video';
                showToast(
                    `主动视频已排队（${auditSceneLabel(scene)} · ${String(taskId).slice(0, 8)}…）`,
                    'success',
                );
                await loadTasks();
            } catch (e) { showToast('触发失败: ' + e.message, 'error'); }
            finally { triggering.value = false; }
        }

        async function triggerDynamic() {
            if (!selectedAccount.value) return;
            triggering.value = true;
            try {
                const data = await api.accounts.triggerDynamic(selectedAccount.value);
                const taskId = data?.task_id || data?.id || '';
                if (!taskId) {
                    showToast('触发未返回任务 ID，请刷新任务列表确认', 'warning');
                    await loadTasks();
                    return;
                }
                const scene = data?.scene || 'dynamic_post';
                showToast(
                    `动态发布已排队（${auditSceneLabel(scene)} · ${String(taskId).slice(0, 8)}…）`,
                    'success',
                );
                await loadTasks();
            } catch (e) { showToast('触发失败: ' + e.message, 'error'); }
            finally { triggering.value = false; }
        }

        async function triggerBangumi() {
            if (!selectedAccount.value) return;
            triggering.value = true;
            try {
                const data = await api.accounts.triggerBangumi(selectedAccount.value);
                const taskId = data?.task_id || data?.id || '';
                if (!taskId) {
                    showToast('触发未返回任务 ID，请刷新任务列表确认', 'warning');
                    await loadTasks();
                    return;
                }
                showToast(
                    `追番检查已排队（${String(taskId).slice(0, 8)}…）`,
                    'success',
                );
                await loadTasks();
            } catch (e) { showToast('触发追番失败: ' + e.message, 'error'); }
            finally { triggering.value = false; }
        }

        function triggerForScene(scene) {
            if (scene === 'dynamic' || scene === 'dynamic_post') return triggerDynamic();
            if (scene === 'proactive_video') return triggerVideo();
            if (scene === 'bangumi' || scene === 'bangumi_comment') return triggerBangumi();
            showToast(`“${auditSceneLabel(scene)}”暂不支持手动触发`, 'warning');
            return undefined;
        }

        async function ensureAccountAndLoad() {
            if (!appState.accountsLoaded) {
                await refreshAccounts();
            }
            if (!selectedAccount.value && appState.accounts.length > 0) {
                const first = appState.accounts[0];
                selectedAccount.value = appState.currentAccountId || first.account_id || first.id;
                // selectedAccount watch 统一 loadTasks，避免赋值后再手动 load 双请求
                return;
            }
            if (selectedAccount.value) await loadTasks();
        }

        onMounted(ensureAccountAndLoad);

        watch(() => appState.accountsLoaded, (loaded) => {
            if (loaded && !selectedAccount.value && appState.accounts.length > 0) {
                const first = appState.accounts[0];
                selectedAccount.value = appState.currentAccountId || first.account_id || first.id;
                // selectedAccount watch 会触发 loadTasks
            }
        });

        watch(() => appState.currentAccountId, (id) => {
            if (id && id !== selectedAccount.value) {
                selectedAccount.value = id;
                // selectedAccount watch 统一 loadTasks
            }
        });

        watch(selectedAccount, (id, prev) => {
            if (id && id !== prev) {
                tasks.value = [];
                loadTasks();
            } else if (id && !tasks.value.length) {
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
                        h('p', { class: 'muted m-0' }, '管理主动视频、动态发布与追番检查任务'),
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
                                'onUpdate:modelValue': (v) => { selectedAccount.value = v; },
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
                                    loading: triggering.value,
                                }, () => '触发动态'),
                                h(Button, {
                                    type: 'ghost',
                                    onClick: triggerBangumi,
                                    loading: triggering.value,
                                }, () => '检查追番'),
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
                                key: t.task_id,
                                class: 'grid items-center',
                                style: `grid-template-columns: ${tableGrid}; column-gap: calc(var(--spacing) * 2); padding: calc(var(--spacing) * 2.3) 0; border-top: 1px solid hsl(var(--border)); font-size: 0.95rem;`,
                            }, [
                                h('span', {
                                    class: 'truncate',
                                    title: t.task_id || '',
                                }, auditSceneLabel(t.scene) !== '-' ? auditSceneLabel(t.scene) : (t.task_id || '-')),
                                h('span', {
                                    class: 'badge badge-info',
                                }, ({
                                    manual: '手动触发',
                                    scheduled: '定时',
                                    cron: '定时',
                                    system: '系统',
                                })[t.trigger_type] || auditSceneLabel(t.scene) || t.trigger_type || '-'),
                                h('span', {
                                    class: `badge badge-${auditStatusBadgeType(t.status)}`,
                                }, auditStatusLabel(t.status) || '待处理'),
                                h('span', {
                                    class: 'whitespace-nowrap truncate',
                                    style: 'color: hsl(var(--muted-foreground)); font-variant-numeric: tabular-nums; font-size:0.85rem;',
                                }, t.scheduled_at ? formatTime(t.scheduled_at) : (t.created_at ? formatTime(t.created_at) : '-')),
                                h('div', { class: 'flex items-center gap-1' }, [
                                    h('button', {
                                        class: 'btn btn-sm primary',
                                        onClick: () => triggerForScene(t.scene),
                                        disabled: ![
                                            'dynamic', 'dynamic_post', 'proactive_video',
                                            'bangumi', 'bangumi_comment',
                                        ].includes(t.scene),
                                    }, '触发'),
                                ]),
                            ])),
                        ]),
                ]),
            ]);
    },
});
