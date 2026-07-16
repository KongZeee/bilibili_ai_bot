// pages/token-usage.js - Token 用量统计页
const { defineComponent, h, ref, onMounted, watch, computed } = window.Vue;
import { api } from '../api.js';
import { appState, showToast, refreshAccounts } from '../state.js';
import { Button, Badge, FormSelect, Loading, EmptyState, KpiCard } from '../components/common.js';
import { formatTime, auditSceneLabel } from '../utils.js';

function fmt(n) {
    const v = Number(n || 0);
    if (!Number.isFinite(v)) return '0';
    if (v >= 1_000_000) return (v / 1_000_000).toFixed(2) + 'M';
    if (v >= 10_000) return (v / 1000).toFixed(1) + 'k';
    return String(Math.round(v));
}

function Card(title, body, eyebrow) {
    return h('article', {
        class: 'grid gap-3',
        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
    }, [
        h('div', { class: 'card-header' }, [
            h('div', { class: 'grid gap-1' }, [
                eyebrow ? h('span', { class: 'eyebrow' }, eyebrow) : null,
                h('h2', { style: 'margin:0; font-size:1.2rem; line-height:1.15; font-weight:500;' }, title),
            ]),
        ]),
        h('div', { class: 'card-body' }, body),
    ]);
}

function BarChart(daily) {
    const rows = daily || [];
    const max = Math.max(1, ...rows.map(r => Number(r.total_tokens || 0)));
    return h('div', {
        style: 'display:grid; grid-template-columns: repeat(auto-fit, minmax(2.2rem, 1fr)); gap: .45rem; align-items:end; min-height: 9rem;',
    }, rows.map(r => {
        const t = Number(r.total_tokens || 0);
        const hPct = Math.max(t > 0 ? 8 : 2, Math.round((t / max) * 100));
        return h('div', {
            style: 'display:grid; gap:.3rem; justify-items:center;',
            title: `${r.day}: ${t} tokens / ${r.calls || 0} calls`,
        }, [
            h('div', { class: 'muted', style: 'font-size:.7rem;' }, fmt(t)),
            h('div', {
                style: `width:100%; max-width:1.6rem; height:${hPct}%; min-height:4px; border-radius:6px 6px 2px 2px; background: linear-gradient(180deg, hsl(var(--primary)), hsl(var(--accent))); opacity:${t ? 1 : .25};`,
            }),
            h('div', { class: 'muted', style: 'font-size:.68rem;' }, String(r.day || '').slice(5)),
        ]);
    }));
}

function Table(headers, rows) {
    if (!rows || !rows.length) {
        return h('span', { class: 'muted' }, '暂无数据');
    }
    return h('div', { style: 'overflow:auto;' }, [
        h('table', { style: 'width:100%; border-collapse:collapse; font-size:.92rem;' }, [
            h('thead', {}, [
                h('tr', {}, headers.map(hd => h('th', {
                    style: 'text-align:left; padding:.45rem .35rem; border-bottom:1px solid hsl(var(--border)); color:hsl(var(--muted-foreground)); font-weight:500;',
                }, hd))),
            ]),
            h('tbody', {}, rows.map((cells, i) => h('tr', {
                style: i % 2 ? 'background: hsl(var(--muted)/.25);' : '',
            }, cells.map(c => h('td', {
                style: 'padding:.5rem .35rem; border-bottom:1px solid hsl(var(--border)/.7); vertical-align:top;',
            }, c))))),
        ]),
    ]);
}

export const TokenUsagePage = defineComponent({
    name: 'TokenUsagePage',
    setup() {
        const loading = ref(true);
        const days = ref(7);
        const accountId = ref('');
        const data = ref(null);

        async function load() {
            loading.value = true;
            try {
                const params = { days: days.value };
                if (accountId.value) params.account_id = accountId.value;
                const q = new URLSearchParams(params).toString();
                data.value = await api.get(`/api/token-usage/summary?${q}`);
            } catch (e) {
                showToast('加载用量失败: ' + e.message, 'error');
                data.value = null;
            } finally {
                loading.value = false;
            }
        }

        onMounted(async () => {
            if (!appState.accountsLoaded) {
                try { await refreshAccounts(); } catch (_) {}
            }
            await load();
        });
        watch([days, accountId], () => { load(); });

        const today = computed(() => data.value?.today || {});
        const totals = computed(() => data.value?.totals || {});

        return () => {
            const accountOptions = [
                { value: '', label: '全部账号' },
                ...((appState.accounts || []).map(a => ({
                    value: a.account_id || a.id,
                    label: a.name || a.account_id || a.id,
                }))),
            ];
            const dayOptions = [
                { value: '1', label: '今天' },
                { value: '7', label: '近 7 天' },
                { value: '14', label: '近 14 天' },
                { value: '30', label: '近 30 天' },
            ];

            return h('div', { class: 'view-frame' }, [
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);',
                }, [
                    h('div', { class: 'hero-panel' }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('span', { class: 'eyebrow' }, '用量统计'),
                            h(Badge, { type: 'info' }, () => 'Token'),
                        ]),
                        h('h2', { style: 'margin:.35rem 0 .5rem;' }, 'LLM Token 消耗'),
                        h('p', { class: 'muted' }, '统计 chat / vision / embedding 等调用的 prompt、completion 与缓存命中（若提供方返回）。数据从本次部署起累计；历史调用不会回溯。'),
                        h('div', { class: 'flex gap-2 flex-wrap', style: 'margin-top:.8rem;' }, [
                            h(FormSelect, {
                                modelValue: String(days.value),
                                'onUpdate:modelValue': (v) => { days.value = Number(v) || 7; },
                                options: dayOptions,
                            }),
                            h(FormSelect, {
                                modelValue: accountId.value,
                                'onUpdate:modelValue': (v) => { accountId.value = v; },
                                options: accountOptions,
                            }),
                            h(Button, { type: 'secondary', onClick: load }, () => '刷新'),
                        ]),
                    ]),
                    h('div', { class: 'card' }, [
                        h('div', { class: 'card-header' }, [h('div', { class: 'card-title' }, '今日快览')]),
                        h('div', { class: 'card-body grid gap-2' }, [
                            h('div', {}, `总 Token：${fmt(today.value.total_tokens)}`),
                            h('div', {}, `Prompt / Completion：${fmt(today.value.prompt_tokens)} / ${fmt(today.value.completion_tokens)}`),
                            h('div', {}, `缓存命中：${fmt(today.value.cached_tokens)}`),
                            h('div', { class: 'muted' }, `调用次数：${fmt(today.value.calls)}`),
                        ]),
                    ]),
                ]),

                loading.value && !data.value
                    ? h(Loading)
                    : !data.value
                        ? h(EmptyState, { title: '暂无统计', desc: '发起 LLM 调用后将自动记录' })
                        : h('div', { class: 'grid gap-3' }, [
                            h('section', {
                                class: 'grid gap-3',
                                style: 'grid-template-columns: repeat(4, minmax(0, 1fr));',
                            }, [
                                h(KpiCard, { eyebrow: '区间总 Token', value: fmt(totals.value.total_tokens), valueLabel: '合计' }),
                                h(KpiCard, { eyebrow: 'Prompt', value: fmt(totals.value.prompt_tokens), valueLabel: '输入' }),
                                h(KpiCard, { eyebrow: 'Completion', value: fmt(totals.value.completion_tokens), valueLabel: '输出' }),
                                h(KpiCard, { eyebrow: '调用次数', value: fmt(totals.value.calls), valueLabel: 'Calls' }),
                            ]),

                            Card('每日消耗', [BarChart(data.value.daily || [])], '趋势'),

                            h('section', {
                                class: 'grid gap-3',
                                style: 'grid-template-columns: repeat(2, minmax(0, 1fr));',
                            }, [
                                Card('按类型', [
                                    Table(
                                        ['类型', 'Token', '调用'],
                                        (data.value.by_kind || []).map(r => [
                                            r.kind || '-',
                                            fmt(r.total_tokens),
                                            fmt(r.calls),
                                        ]),
                                    ),
                                ], 'Kind'),
                                Card('按场景', [
                                    Table(
                                        ['场景', 'Token', '调用'],
                                        (data.value.by_scene || []).map(r => [
                                            auditSceneLabel(r.scene) || r.scene || '-',
                                            fmt(r.total_tokens),
                                            fmt(r.calls),
                                        ]),
                                    ),
                                ], 'Scene'),
                            ]),

                            h('section', {
                                class: 'grid gap-3',
                                style: 'grid-template-columns: repeat(2, minmax(0, 1fr));',
                            }, [
                                Card('按模型', [
                                    Table(
                                        ['模型', 'Provider', 'Token', '调用'],
                                        (data.value.by_model || []).map(r => [
                                            r.model || '-',
                                            r.provider_id || '-',
                                            fmt(r.total_tokens),
                                            fmt(r.calls),
                                        ]),
                                    ),
                                ], 'Model'),
                                Card('按账号', [
                                    Table(
                                        ['账号', 'Token', '调用'],
                                        (data.value.by_account || []).map(r => [
                                            r.account_id || '-',
                                            fmt(r.total_tokens),
                                            fmt(r.calls),
                                        ]),
                                    ),
                                ], 'Account'),
                            ]),

                            Card('最近调用', [
                                Table(
                                    ['时间', '类型', '模型', 'P/C/T', '缓存', '场景'],
                                    (data.value.recent || []).map(r => [
                                        formatTime(r.ts),
                                        r.kind || '-',
                                        r.model || '-',
                                        `${fmt(r.prompt_tokens)}/${fmt(r.completion_tokens)}/${fmt(r.total_tokens)}`,
                                        fmt(r.cached_tokens),
                                        auditSceneLabel(r.scene) || r.scene || '-',
                                    ]),
                                ),
                            ], 'Recent'),
                        ]),
            ]);
        };
    },
});
