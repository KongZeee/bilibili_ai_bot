// pages/companion.js - 陪伴生活页
const { defineComponent, h, ref, onMounted, watch, computed } = window.Vue;
import { api } from '../api.js';
import { appState, showToast, refreshAccounts } from '../state.js';
import { Button, Badge, FormSelect, Loading, EmptyState } from '../components/common.js';

function Card(title, children, extra) {
    return h('div', { class: 'card' }, [
        h('div', { class: 'card-header' }, [
            h('div', { class: 'card-title' }, title),
            extra || null,
        ]),
        h('div', { class: 'card-body' }, children),
    ]);
}

function pre(text) {
    return h('pre', {
        style: 'white-space:pre-wrap;word-break:break-word;margin:0;font:inherit;line-height:1.55;',
    }, text || '—');
}

export const CompanionPage = defineComponent({
    name: 'CompanionPage',
    setup() {
        const selectedAccount = ref('');
        const loading = ref(false);
        const triggering = ref(false);
        const state = ref(null);
        const diaries = ref([]);
        const dreams = ref({ latest: null, fragments: [] });
        const notes = ref([]);
        const projects = ref([]);
        const tab = ref('overview');
        let loadSeq = 0;

        async function loadAll() {
            if (!selectedAccount.value) return;
            const seq = ++loadSeq;
            loading.value = true;
            try {
                const [st, d, dr, n, b] = await Promise.all([
                    api.accounts.companion.state(selectedAccount.value),
                    api.accounts.companion.diaries(selectedAccount.value).catch(() => ({ items: [] })),
                    api.accounts.companion.dreams(selectedAccount.value).catch(() => ({ latest: null, fragments: [] })),
                    api.accounts.companion.notes(selectedAccount.value).catch(() => ({ items: [] })),
                    api.accounts.companion.bookshelf(selectedAccount.value).catch(() => ({ items: [] })),
                ]);
                if (seq !== loadSeq) return;
                state.value = st;
                diaries.value = d.items || [];
                dreams.value = dr || { latest: null, fragments: [] };
                notes.value = n.items || [];
                projects.value = b.items || [];
            } catch (e) {
                if (seq !== loadSeq) return;
                showToast('加载陪伴生活失败: ' + e.message, 'error');
                state.value = null;
            } finally {
                if (seq === loadSeq) loading.value = false;
            }
        }

        async function trigger(action) {
            if (!selectedAccount.value) return;
            triggering.value = true;
            try {
                await api.accounts.companion.trigger(selectedAccount.value, action);
                showToast(action + ' 已触发', 'success');
                await loadAll();
            } catch (e) {
                showToast('触发失败: ' + e.message, 'error');
            } finally {
                triggering.value = false;
            }
        }

        async function ensureAccountAndLoad() {
            if (!appState.accountsLoaded) await refreshAccounts();
            if (!selectedAccount.value && appState.accounts.length > 0) {
                const first = appState.accounts[0];
                selectedAccount.value = appState.currentAccountId || first.account_id || first.id;
            }
            if (selectedAccount.value) await loadAll();
        }

        onMounted(ensureAccountAndLoad);
        watch(() => appState.accountsLoaded, (loaded) => {
            if (loaded && !selectedAccount.value && appState.accounts.length > 0) {
                const first = appState.accounts[0];
                selectedAccount.value = appState.currentAccountId || first.account_id || first.id;
                loadAll();
            }
        });
        watch(() => appState.currentAccountId, (id) => {
            if (id && id !== selectedAccount.value) {
                selectedAccount.value = id;
                loadAll();
            }
        });
        watch(selectedAccount, () => { if (selectedAccount.value) loadAll(); });

        const enabled = computed(() => !!(state.value && state.value.enabled));
        const life = computed(() => (state.value && state.value.life_state) || {});
        const plan = computed(() => (state.value && state.value.daily_plan) || { items: [] });

        return () => {
            if (loading.value && !state.value && !selectedAccount.value) return h(Loading);

            const accountOptions = (appState.accounts || []).map(a => ({
                value: a.account_id || a.id,
                label: a.name || a.account_id || a.id,
            }));

            const tabs = [
                { id: 'overview', label: '总览' },
                { id: 'plan', label: '日程' },
                { id: 'diary', label: '日记' },
                { id: 'dream', label: '梦境' },
                { id: 'notes', label: '探索' },
                { id: 'book', label: '书柜' },
            ];

            return h('div', { class: 'view-frame' }, [
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);',
                }, [
                    h('div', { class: 'hero-panel' }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('span', { class: 'eyebrow' }, '陪伴生活'),
                            h(Badge, { type: enabled.value ? 'success' : 'warning' }, () => enabled.value ? '已启用' : '未启用'),
                        ]),
                        h('h2', { style: 'margin: .4rem 0 .6rem;' }, '拟人日程与内心生活'),
                        h('p', { class: 'muted' }, '每账号独立的生活状态、日程、梦境日记、探索笔记与私下创作。默认关闭，需在 config 中设置 companion.enabled=true。'),
                        h('div', { class: 'flex gap-2 flex-wrap', style: 'margin-top: .8rem;' }, [
                            h(Button, { type: 'secondary', disabled: triggering.value, onClick: () => trigger('tick') }, () => '运行 tick'),
                            h(Button, { type: 'secondary', disabled: triggering.value, onClick: () => trigger('plan') }, () => '生成日程'),
                            h(Button, { type: 'secondary', disabled: triggering.value, onClick: () => trigger('diary') }, () => '写日记'),
                            h(Button, { type: 'secondary', disabled: triggering.value, onClick: () => trigger('dream') }, () => '做梦'),
                            h(Button, { type: 'secondary', disabled: triggering.value, onClick: () => trigger('explore') }, () => '探索'),
                            h(Button, { type: 'secondary', disabled: triggering.value, onClick: () => trigger('creative') }, () => '创作'),
                        ]),
                    ]),
                    h('div', { class: 'card' }, [
                        h('div', { class: 'card-header' }, [h('div', { class: 'card-title' }, '账号')]),
                        h('div', { class: 'card-body' }, [
                            h(FormSelect, {
                                modelValue: selectedAccount.value,
                                'onUpdate:modelValue': (v) => { selectedAccount.value = v; },
                                options: accountOptions,
                                placeholder: '选择账号',
                            }),
                            life.value.energy != null
                                ? h('div', { style: 'margin-top:1rem;display:grid;gap:.4rem;' }, [
                                    h('div', {}, `精力：${life.value.energy}/100`),
                                    h('div', {}, `心情：${life.value.mood_bias || '—'}`),
                                    h('div', {}, `当前：${life.value.activity || '—'}`),
                                    h('div', { class: 'muted' }, life.value.dream_afterglow || ''),
                                ])
                                : h('p', { class: 'muted', style: 'margin-top:1rem;' }, '暂无生活状态'),
                        ]),
                    ]),
                ]),

                h('div', { class: 'flex gap-2 flex-wrap', style: 'margin: .5rem 0 1rem;' },
                    tabs.map(t => h('button', {
                        class: 'btn ghost btn-sm',
                        type: 'button',
                        style: tab.value === t.id ? 'border-color: hsl(var(--accent)); background: hsl(var(--accent)/.2);' : '',
                        onClick: () => { tab.value = t.id; },
                    }, t.label))
                ),

                loading.value && !state.value
                    ? h(Loading)
                    : !state.value
                        ? h(EmptyState, { title: '暂无数据', description: '请选择账号，或确认陪伴层已初始化' })
                        : renderTab(tab.value, {
                            state: state.value,
                            life: life.value,
                            plan: plan.value,
                            diaries: diaries.value,
                            dreams: dreams.value,
                            notes: notes.value,
                            projects: projects.value,
                        }),
            ]);
        };

        function renderTab(id, ctx) {
            if (id === 'overview') {
                return h('div', { class: 'grid gap-3', style: 'grid-template-columns:1fr;' }, [
                    Card('提示词注入预览', [pre(ctx.state.prompt_surface || '（未注入：检查 companion.enabled 与 inject_into_replies）')]),
                    Card('话题种子', [
                        (ctx.state.topic_seeds || []).length
                            ? h('ul', {}, (ctx.state.topic_seeds || []).map(s => h('li', {}, s)))
                            : h('span', { class: 'muted' }, '暂无'),
                    ]),
                    Card('配置摘要', [pre(JSON.stringify(ctx.state.config || {}, null, 2))]),
                ]);
            }
            if (id === 'plan') {
                const items = (ctx.plan && ctx.plan.items) || [];
                return Card(`今日日程（${ctx.plan.source || '—'} · 质量 ${ctx.plan.quality_score ?? '—'}）`, [
                    items.length
                        ? h('div', { style: 'display:grid;gap:.6rem;' }, items.map(it =>
                            h('div', {
                                style: 'display:grid;grid-template-columns:7rem 1fr;gap:.6rem;padding:.55rem .4rem;border-bottom:1px solid hsl(var(--border));',
                            }, [
                                h('strong', {}, `${it.time || ''}-${it.end || ''}`),
                                h('div', {}, [
                                    h('div', {}, it.activity || ''),
                                    it.message_seed ? h('div', { class: 'muted' }, '念头：' + it.message_seed) : null,
                                ]),
                            ])
                        ))
                        : h('span', { class: 'muted' }, '尚无日程，可点击「生成日程」'),
                    ctx.state.story_detail && ctx.state.story_detail.summary
                        ? h('div', { style: 'margin-top:1rem;' }, [
                            h('div', { class: 'eyebrow' }, '当前细化'),
                            pre(ctx.state.story_detail.summary + '\n' + ((ctx.state.story_detail.events || []).join('\n'))),
                        ])
                        : null,
                ]);
            }
            if (id === 'diary') {
                return Card('日记', [
                    ctx.diaries.length
                        ? h('div', { style: 'display:grid;gap:1rem;' }, ctx.diaries.map(d =>
                            h('article', {
                                style: 'border:1px solid hsl(var(--border));border-radius:calc(var(--radius)*.72);padding:1rem;',
                            }, [
                                h('div', { class: 'flex justify-between gap-2' }, [
                                    h('strong', {}, d.date || ''),
                                    h('span', { class: 'muted' }, d.summary || ''),
                                ]),
                                pre(d.body || ''),
                                d.share_seed ? h('div', { class: 'muted', style: 'margin-top:.5rem;' }, '可分享：' + d.share_seed) : null,
                            ])
                        ))
                        : h('span', { class: 'muted' }, '尚无日记'),
                ]);
            }
            if (id === 'dream') {
                const latest = ctx.dreams.latest;
                return h('div', { class: 'grid gap-3' }, [
                    Card('最近梦境', [
                        latest
                            ? h('div', {}, [
                                h('div', {}, `${latest.label || ''} · ${latest.mood || ''} · Δ精力 ${latest.energy_delta ?? 0}`),
                                pre(latest.content || ''),
                                h('div', { class: 'muted', style: 'margin-top:.5rem;' }, latest.afterglow || ''),
                            ])
                            : h('span', { class: 'muted' }, '尚无梦境'),
                    ]),
                    Card('梦境碎片', [
                        (ctx.dreams.fragments || []).length
                            ? h('ul', {}, (ctx.dreams.fragments || []).map(f => h('li', {}, `${f.text} (w=${(f.weight || 0).toFixed?.(2) || f.weight})`)))
                            : h('span', { class: 'muted' }, '无碎片'),
                    ]),
                ]);
            }
            if (id === 'notes') {
                return Card('探索笔记', [
                    ctx.notes.length
                        ? h('div', { style: 'display:grid;gap:1rem;' }, ctx.notes.map(n =>
                            h('article', {
                                style: 'border:1px solid hsl(var(--border));border-radius:calc(var(--radius)*.72);padding:1rem;',
                            }, [
                                h('strong', {}, n.query || ''),
                                h('div', { class: 'muted' }, n.motive || ''),
                                pre(n.impression || ''),
                                n.self_link ? h('div', { class: 'muted' }, '关联：' + n.self_link) : null,
                            ])
                        ))
                        : h('span', { class: 'muted' }, '尚无探索笔记（需启用 exploration + web_search）'),
                ]);
            }
            if (id === 'book') {
                return Card('书柜', [
                    ctx.projects.length
                        ? h('div', { style: 'display:grid;gap:1rem;' }, ctx.projects.map(p =>
                            h('article', {
                                style: 'border:1px solid hsl(var(--border));border-radius:calc(var(--radius)*.72);padding:1rem;',
                            }, [
                                h('div', { class: 'flex justify-between' }, [
                                    h('strong', {}, `《${p.title || '未命名'}》`),
                                    h(Badge, { type: p.status === 'finished' ? 'success' : 'info' }, () => p.status || 'drafting'),
                                ]),
                                h('div', { class: 'muted' }, `${p.work_type || ''} · ${p.current_chars || 0}/${p.target_chars || 0} 字`),
                                pre(p.premise || ''),
                                (p.draft_chunks || []).length
                                    ? pre('最新片段：\n' + (p.draft_chunks[p.draft_chunks.length - 1].text || '').slice(0, 400))
                                    : null,
                            ])
                        ))
                        : h('span', { class: 'muted' }, '书柜为空（需启用 creative）'),
                ]);
            }
            return null;
        }
    },
});
