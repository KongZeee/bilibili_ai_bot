// pages/logs.js - 日志查看页（Golden Time 设计稿）
const { defineComponent, h, ref, onMounted } = window.Vue;
import { api } from '../api.js';
import { showToast } from '../state.js';
import { Button, Badge, FormInput, FormSelect, Loading, EmptyState, Icon } from '../components/common.js';

export const LogsPage = defineComponent({
    name: 'LogsPage',
    setup() {
        const logs = ref([]);
        const loading = ref(false);
        const level = ref('');
        const keyword = ref('');
        const limit = ref(100);

        const levels = [
            { value: '', label: '全部级别' },
            { value: 'DEBUG', label: 'DEBUG' },
            { value: 'INFO', label: 'INFO' },
            { value: 'WARNING', label: 'WARNING' },
            { value: 'ERROR', label: 'ERROR' },
        ];

        const levelFilters = [
            { value: '', label: '全部' },
            { value: 'INFO', label: 'INFO' },
            { value: 'WARNING', label: 'WARN' },
            { value: 'ERROR', label: 'ERROR' },
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
                showToast('日志已下载', 'success');
            } catch (e) { showToast('下载失败', 'error'); }
        }

        onMounted(load);

        const tableGrid = 'minmax(9rem, 0.7fr) 7rem minmax(0, 2.6fr)';

        return () => loading.value && logs.value.length === 0
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                // ═══ hero-band：左侧统计 + 右侧筛选 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左侧：hero-panel 统计面板
                    h('div', { class: 'hero-panel' }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('span', { class: 'eyebrow' }, '系统日志'),
                            h(Badge, { type: 'info' }, () => '实时'),
                        ]),
                        h('h2', {
                            style: 'margin:0; font-size:1.65rem; line-height:1.1; text-wrap:balance; word-break:keep-all;',
                        }, '运行日志'),
                        h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                            h('span', {
                                style: 'font-size:2.4rem; font-weight:500; line-height:1; font-variant-numeric:tabular-nums;',
                            }, String(logs.value.length || 0)),
                            h('span', { class: 'muted m-0', style: 'font-size:0.9rem;' }, `条日志 · 上限 ${limit.value}`),
                        ]),
                        h('p', { class: 'muted m-0' }, '查看服务运行状态、警告与错误信息'),
                    ]),
                    // 右侧：筛选 Card
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '检索'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '日志筛选'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-2' }, [
                            h(FormInput, {
                                modelValue: keyword.value,
                                'onUpdate:modelValue': (v) => keyword.value = v,
                                placeholder: '关键词搜索…',
                                id: 'logs-keyword',
                            }),
                            h('div', { class: 'flex items-center gap-2 flex-wrap' }, [
                                h(Button, {
                                    type: 'primary',
                                    onClick: load,
                                }, () => '查询'),
                                h(Button, {
                                    type: 'ghost',
                                    onClick: download,
                                }, () => '下载'),
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
                            h('span', { class: 'eyebrow' }, '日志流'),
                            h('h2', {
                                style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;',
                            }, '日志记录'),
                        ]),
                        // 级别筛选按钮组
                        h('div', { class: 'flex items-center gap-1 flex-wrap' },
                            levelFilters.map(f => h('button', {
                                key: f.value,
                                class: ['btn', 'btn-sm', level.value === f.value ? 'primary' : 'ghost'].join(' '),
                                onClick: () => { level.value = f.value; load(); },
                            }, f.label)),
                        ),
                    ]),
                    logs.value.length === 0
                        ? h(EmptyState, { icon: 'folder', title: '暂无日志', desc: '当前筛选条件下没有日志数据' })
                        : h('div', {
                            class: 'grid',
                            style: 'gap:0; min-width:0; max-height:600px; overflow-y:auto;',
                        }, [
                            // 表头
                            h('div', {
                                class: 'grid items-center',
                                style: `grid-template-columns: ${tableGrid}; column-gap: calc(var(--spacing) * 2); padding-bottom: calc(var(--spacing) * 2); border-bottom: 1px solid hsl(var(--border)); color: hsl(var(--muted-foreground)); font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.14em; position: sticky; top: 0; background: hsl(var(--card)); z-index: 1;`,
                            }, [
                                h('span', { class: 'whitespace-nowrap' }, '时间'),
                                h('span', { class: 'whitespace-nowrap' }, '级别'),
                                h('span', { class: 'whitespace-nowrap' }, '消息'),
                            ]),
                            // 数据行
                            ...logs.value.map((l, i) => h('div', {
                                key: i,
                                class: 'grid items-center',
                                style: `grid-template-columns: ${tableGrid}; column-gap: calc(var(--spacing) * 2); padding: calc(var(--spacing) * 2.3) 0; border-top: 1px solid hsl(var(--border)); font-size: 0.9rem;`,
                            }, [
                                h('span', {
                                    class: 'whitespace-nowrap',
                                    style: 'color: hsl(var(--muted-foreground)); font-variant-numeric: tabular-nums; font-size:0.82rem;',
                                }, l.ts || l.timestamp || '-'),
                                h('span', {
                                    class: ['badge',
                                        l.level === 'ERROR' ? 'badge-danger'
                                        : l.level === 'WARNING' ? 'badge-warning'
                                        : l.level === 'INFO' ? 'badge-info'
                                        : 'badge-muted'].join(' '),
                                }, l.level || 'INFO'),
                                h('div', {
                                    style: 'white-space:pre-wrap; word-break:break-word; font-family:monospace; font-size:0.82rem; line-height:1.5; min-width:0;',
                                }, l.line || l.message || l.msg || ''),
                            ])),
                        ]),
                ]),
            ]);
    },
});
