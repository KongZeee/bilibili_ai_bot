// pages/companion.js - 陪伴生活页（状态 / 日程 / 日记梦境 / 探索 / 书柜）
const { defineComponent, h, ref, onMounted, watch, computed } = window.Vue;
import { api } from '../api.js';
import { appState, showToast, refreshAccounts } from '../state.js';
import { Button, Badge, FormSelect, Loading, EmptyState } from '../components/common.js';

function panel(eyebrow, title, body, extra) {
    return h('article', {
        class: 'grid gap-3',
        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start; min-width: 0;',
    }, [
        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
            h('div', { class: 'grid gap-1', style: 'min-width:0;' }, [
                eyebrow ? h('span', { class: 'eyebrow' }, eyebrow) : null,
                h('h2', { style: 'margin:0; font-size:1.2rem; line-height:1.15; font-weight:500;' }, title),
            ]),
            extra || null,
        ]),
        h('div', { class: 'grid gap-2', style: 'min-width:0;' }, body),
    ]);
}

function prose(text, opts = {}) {
    const max = opts.max || 0;
    let t = text || '—';
    if (max && t.length > max) t = t.slice(0, max) + '…';
    return h('div', {
        style: 'white-space:pre-wrap;word-break:break-word;line-height:1.65;font-size:0.95rem;',
    }, t);
}

function metaRow(label, value) {
    return h('div', {
        style: 'display:grid;grid-template-columns:5.5rem 1fr;gap:.5rem;padding:.35rem 0;border-bottom:1px solid hsl(var(--border)/.55);',
    }, [
        h('span', { class: 'muted', style: 'font-size:.82rem;' }, label),
        h('span', { style: 'min-width:0;word-break:break-word;' }, value || '—'),
    ]);
}

function energyBar(n) {
    const v = Math.max(0, Math.min(100, Number(n) || 0));
    return h('div', { style: 'display:grid;gap:.35rem;' }, [
        h('div', { class: 'flex justify-between', style: 'font-size:.85rem;' }, [
            h('span', {}, '精力'),
            h('strong', {}, `${v}/100`),
        ]),
        h('div', {
            style: 'height:.55rem;border-radius:999px;background:hsl(var(--muted));overflow:hidden;',
        }, [
            h('div', {
                style: `height:100%;width:${v}%;border-radius:999px;background:linear-gradient(90deg,hsl(var(--primary)),hsl(var(--accent)));transition:width .25s ease;`,
            }),
        ]),
    ]);
}

function chip(text, type = 'info') {
    return h(Badge, { type }, () => text);
}

function linkList(items) {
    const list = (items || []).filter(Boolean);
    if (!list.length) return h('span', { class: 'muted' }, '无链接结果');
    return h('div', { style: 'display:grid;gap:.55rem;' }, list.map((it, i) => {
        const title = it.title || it.name || `结果 ${i + 1}`;
        const url = it.url || it.link || it.href || '';
        const snip = it.snippet || it.content || it.description || '';
        return h('div', {
            style: 'padding:.65rem .75rem;border:1px solid hsl(var(--border));border-radius:calc(var(--radius)*.7);background:hsl(var(--background)/.5);',
        }, [
            url
                ? h('a', {
                    href: url,
                    target: '_blank',
                    rel: 'noopener noreferrer',
                    style: 'color:hsl(var(--primary));font-weight:500;text-decoration:none;word-break:break-all;',
                }, title)
                : h('strong', {}, title),
            snip ? h('div', { class: 'muted', style: 'margin-top:.35rem;font-size:.88rem;line-height:1.5;' }, String(snip).slice(0, 180)) : null,
            url ? h('div', { class: 'muted', style: 'margin-top:.25rem;font-size:.75rem;word-break:break-all;' }, url) : null,
        ]);
    }));
}

function progressBar(cur, max) {
    const c = Number(cur) || 0;
    const m = Math.max(1, Number(max) || 1);
    const pct = Math.max(0, Math.min(100, Math.round((c / m) * 100)));
    return h('div', { style: 'display:grid;gap:.3rem;' }, [
        h('div', { class: 'flex justify-between muted', style: 'font-size:.82rem;' }, [
            h('span', {}, `${c} / ${m} 字`),
            h('span', {}, `${pct}%`),
        ]),
        h('div', { style: 'height:.45rem;border-radius:999px;background:hsl(var(--muted));overflow:hidden;' }, [
            h('div', { style: `height:100%;width:${pct}%;background:hsl(var(--primary));border-radius:999px;` }),
        ]),
    ]);
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
        const openBookId = ref('');
        const openNoteId = ref('');
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
                if (!openBookId.value && projects.value[0]) openBookId.value = projects.value[0].id;
                if (!openNoteId.value && notes.value[0]) openNoteId.value = notes.value[0].id;
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
        const cfg = computed(() => (state.value && state.value.config) || {});

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
                    style: 'grid-template-columns: minmax(0, 1.45fr) minmax(16rem, .9fr);',
                }, [
                    h('div', { class: 'hero-panel' }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('span', { class: 'eyebrow' }, '陪伴生活'),
                            h(Badge, { type: enabled.value ? 'success' : 'warning' }, () => enabled.value ? '已启用' : '未启用'),
                        ]),
                        h('h2', { style: 'margin: .4rem 0 .55rem;' }, '像真人一样过一天'),
                        h('p', { class: 'muted' }, '日程、生活状态、梦境余韵、探索笔记与书柜创作。探索只搜公开资料（百科/新闻/设定），不会搜「今天做了什么」这类假问题。'),
                        h('div', { class: 'flex gap-2 flex-wrap', style: 'margin-top: .85rem;' }, [
                            h(Button, { type: 'secondary', disabled: triggering.value, onClick: () => trigger('tick') }, () => '运行 tick'),
                            h(Button, { type: 'secondary', disabled: triggering.value, onClick: () => trigger('plan') }, () => '生成日程'),
                            h(Button, { type: 'secondary', disabled: triggering.value, onClick: () => trigger('diary') }, () => '写日记'),
                            h(Button, { type: 'secondary', disabled: triggering.value, onClick: () => trigger('dream') }, () => '做梦'),
                            h(Button, { type: 'primary', disabled: triggering.value, onClick: () => trigger('explore') }, () => '探索一次'),
                            h(Button, { type: 'secondary', disabled: triggering.value, onClick: () => trigger('creative') }, () => '续写创作'),
                        ]),
                    ]),
                    panel('账号与状态', '此刻', [
                        h(FormSelect, {
                            modelValue: selectedAccount.value,
                            'onUpdate:modelValue': (v) => { selectedAccount.value = v; },
                            options: accountOptions,
                            placeholder: '选择账号',
                        }),
                        life.value.energy != null ? energyBar(life.value.energy) : h('span', { class: 'muted' }, '暂无生活状态'),
                        metaRow('心情', life.value.mood_bias),
                        metaRow('当前', life.value.activity),
                        metaRow('念头', life.value.message_seed),
                        metaRow('梦境余韵', life.value.dream_afterglow),
                        metaRow('想刷站', state.value?.wants_browse_now ? '是' : '否'),
                    ]),
                ]),

                h('div', { class: 'flex gap-2 flex-wrap', style: 'margin: .15rem 0 .4rem;' },
                    tabs.map(t => h('button', {
                        class: 'btn ghost btn-sm',
                        type: 'button',
                        style: tab.value === t.id ? 'border-color: hsl(var(--accent)); background: hsl(var(--accent)/.22);' : '',
                        onClick: () => { tab.value = t.id; },
                    }, t.label))
                ),

                loading.value && !state.value
                    ? h(Loading)
                    : !state.value
                        ? h(EmptyState, { title: '暂无数据', desc: '请选择账号，并在全局配置中启用 companion' })
                        : renderTab(tab.value),
            ]);
        };

        function renderTab(id) {
            if (id === 'overview') return renderOverview();
            if (id === 'plan') return renderPlan();
            if (id === 'diary') return renderDiary();
            if (id === 'dream') return renderDream();
            if (id === 'notes') return renderNotes();
            if (id === 'book') return renderBook();
            return null;
        }

        function renderOverview() {
            const seeds = state.value?.topic_seeds || [];
            const interests = state.value?.interest_keywords || [];
            const rt = state.value?.runtime || {};
            return h('div', { class: 'grid gap-3', style: 'grid-template-columns:1fr;' }, [
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: repeat(2, minmax(0, 1fr));',
                }, [
                    panel('注入', '回复会看到的生活块', [
                        prose(state.value?.prompt_surface || '（未注入：检查 companion.enabled 与 inject_into_replies）'),
                    ]),
                    panel('种子', '话题 / 兴趣', [
                        h('div', { class: 'eyebrow' }, '话题种子'),
                        seeds.length
                            ? h('div', { class: 'flex gap-2 flex-wrap' }, seeds.map(s => chip(s, 'info')))
                            : h('span', { class: 'muted' }, '暂无'),
                        h('div', { class: 'eyebrow', style: 'margin-top:.6rem;' }, '兴趣关键词'),
                        interests.length
                            ? h('div', { class: 'flex gap-2 flex-wrap' }, interests.map(s => chip(s, 'success')))
                            : h('span', { class: 'muted' }, '暂无（可在人格 interests 或 companion.exploration.interests 配置）'),
                        h('div', { style: 'margin-top:.75rem;' }, [
                            metaRow('上次探索', rt.last_explore_query || '—'),
                            metaRow('探索成功', rt.last_explore_ok == null ? '—' : (rt.last_explore_ok ? '是' : '否')),
                            metaRow('上次视频', rt.last_proactive_video_bvid || '—'),
                        ]),
                    ]),
                ]),
                panel('模块开关', '当前配置摘要', [
                    h('div', { class: 'flex gap-2 flex-wrap' }, [
                        chip(cfg.value.enabled ? '总开关 ON' : '总开关 OFF', cfg.value.enabled ? 'success' : 'warning'),
                        chip(`日程 ${cfg.value.schedule?.enabled ? 'ON' : 'OFF'}`),
                        chip(`日记 ${cfg.value.diary?.enabled ? 'ON' : 'OFF'}`),
                        chip(`梦境 ${cfg.value.dream?.enabled ? 'ON' : 'OFF'}`),
                        chip(`探索 ${cfg.value.exploration?.enabled ? 'ON' : 'OFF'}`, cfg.value.exploration?.enabled ? 'info' : 'warning'),
                        chip(`创作 ${cfg.value.creative?.enabled ? 'ON' : 'OFF'}`),
                        chip(`探索草稿 ${cfg.value.exploration?.offer_dynamic_draft ? 'ON' : 'OFF'}`),
                    ]),
                    h('p', { class: 'muted', style: 'margin:0;font-size:.88rem;' },
                        '探索需同时开启「联网搜索」。无结果不会生成动态草稿。'),
                ]),
            ]);
        }

        function renderPlan() {
            const items = plan.value.items || [];
            const detail = state.value?.story_detail || {};
            return h('div', { class: 'grid gap-3' }, [
                panel(
                    '今日',
                    `日程 · ${plan.value.source || '—'} · 质量 ${plan.value.quality_score ?? '—'}`,
                    [
                        items.length
                            ? h('div', { style: 'display:grid;gap:0;' }, items.map(it =>
                                h('div', {
                                    style: 'display:grid;grid-template-columns:7.2rem 1fr;gap:.75rem;padding:.7rem .2rem;border-bottom:1px solid hsl(var(--border));',
                                }, [
                                    h('div', {}, [
                                        h('strong', {}, `${it.time || ''}-${it.end || ''}`),
                                        it.mood ? h('div', { class: 'muted', style: 'font-size:.8rem;' }, it.mood) : null,
                                    ]),
                                    h('div', {}, [
                                        h('div', {}, it.activity || ''),
                                        it.message_seed ? h('div', { class: 'muted', style: 'margin-top:.25rem;' }, '念头：' + it.message_seed) : null,
                                    ]),
                                ])
                            ))
                            : h('span', { class: 'muted' }, '尚无日程，点「生成日程」'),
                    ],
                    h(Button, { type: 'ghost', class: 'btn-sm', disabled: triggering.value, onClick: () => trigger('plan') }, () => '重新生成'),
                ),
                detail.summary
                    ? panel('细化', detail.window || '当前时段', [
                        prose(detail.summary),
                        (detail.events || []).length
                            ? h('ul', { style: 'margin:.4rem 0 0;padding-left:1.1rem;' },
                                detail.events.map(e => h('li', {}, e)))
                            : null,
                        (detail.proactive_hooks || []).length
                            ? h('div', { class: 'flex gap-2 flex-wrap', style: 'margin-top:.5rem;' },
                                detail.proactive_hooks.map(x => chip(x)))
                            : null,
                    ])
                    : null,
            ]);
        }

        function renderDiary() {
            return panel(
                '私密',
                `日记（${diaries.value.length}）`,
                [
                    diaries.value.length
                        ? h('div', { style: 'display:grid;gap:1rem;' }, diaries.value.map(d =>
                            h('article', {
                                style: 'border:1px solid hsl(var(--border));border-radius:calc(var(--radius)*.75);padding:1rem;background:hsl(var(--background)/.35);',
                            }, [
                                h('div', { class: 'flex justify-between gap-2 flex-wrap' }, [
                                    h('strong', {}, d.date || ''),
                                    h('span', { class: 'muted' }, d.summary || ''),
                                ]),
                                prose(d.body || ''),
                                (d.tags || []).length
                                    ? h('div', { class: 'flex gap-2 flex-wrap', style: 'margin-top:.5rem;' },
                                        d.tags.map(t => chip(t)))
                                    : null,
                                d.share_seed
                                    ? h('div', {
                                        style: 'margin-top:.65rem;padding:.55rem .7rem;border-left:3px solid hsl(var(--accent));background:hsl(var(--accent)/.12);',
                                    }, ['可分享：', d.share_seed])
                                    : null,
                            ])
                        ))
                        : h('span', { class: 'muted' }, '尚无日记'),
                ],
                h(Button, { type: 'ghost', class: 'btn-sm', disabled: triggering.value, onClick: () => trigger('diary') }, () => '写日记'),
            );
        }

        function renderDream() {
            const latest = dreams.value.latest;
            const frags = dreams.value.fragments || [];
            return h('div', {
                class: 'grid gap-3',
                style: 'grid-template-columns: minmax(0, 1.3fr) minmax(0, .9fr);',
            }, [
                panel(
                    '昨夜',
                    latest ? (latest.label || '梦境') : '梦境',
                    [
                        latest
                            ? h('div', { class: 'grid gap-2' }, [
                                h('div', { class: 'flex gap-2 flex-wrap' }, [
                                    chip(latest.mood || '—'),
                                    chip(`Δ精力 ${latest.energy_delta ?? 0}`, Number(latest.energy_delta) >= 0 ? 'success' : 'warning'),
                                    chip(latest.dream_type || '—'),
                                ]),
                                prose(latest.content || ''),
                                latest.afterglow
                                    ? h('div', { class: 'muted' }, '余韵：' + latest.afterglow)
                                    : null,
                                (latest.factors || []).length
                                    ? h('div', { class: 'flex gap-2 flex-wrap' },
                                        latest.factors.map(f => chip(f, 'info')))
                                    : null,
                            ])
                            : h('span', { class: 'muted' }, '尚无梦境'),
                    ],
                    h(Button, { type: 'ghost', class: 'btn-sm', disabled: triggering.value, onClick: () => trigger('dream') }, () => '生成梦境'),
                ),
                panel('碎片池', `共 ${frags.length} 条`, [
                    frags.length
                        ? h('div', { style: 'display:grid;gap:.45rem;' }, frags.map(f =>
                            h('div', {
                                style: 'display:flex;justify-content:space-between;gap:.5rem;padding:.45rem .2rem;border-bottom:1px solid hsl(var(--border)/.6);',
                            }, [
                                h('span', {}, f.text || ''),
                                h('span', { class: 'muted', style: 'font-size:.8rem;white-space:nowrap;' },
                                    `w=${Number(f.weight || 0).toFixed(2)} · ${f.source || ''}`),
                            ])
                        ))
                        : h('span', { class: 'muted' }, '无碎片'),
                ]),
            ]);
        }

        function renderNotes() {
            const list = notes.value || [];
            const active = list.find(n => n.id === openNoteId.value) || list[0] || null;
            if (active && active.id !== openNoteId.value) openNoteId.value = active.id;

            return h('div', {
                class: 'grid gap-3',
                style: 'grid-template-columns: minmax(14rem, .85fr) minmax(0, 1.4fr);',
            }, [
                panel(
                    '探索笔记',
                    `共 ${list.length} 条`,
                    [
                        list.length
                            ? h('div', { style: 'display:grid;gap:.4rem;max-height:28rem;overflow:auto;' }, list.map(n =>
                                h('button', {
                                    type: 'button',
                                    class: 'btn ghost',
                                    style: `justify-content:flex-start;text-align:left;padding:.65rem .7rem;${active && active.id === n.id ? 'border-color:hsl(var(--accent));background:hsl(var(--accent)/.15);' : ''}`,
                                    onClick: () => { openNoteId.value = n.id; },
                                }, [
                                    h('div', { style: 'font-weight:500;word-break:break-word;' }, n.query || '(无 query)'),
                                    h('div', { class: 'muted', style: 'font-size:.8rem;margin-top:.2rem;' },
                                        `${(n.created_at || '').replace('T', ' ').slice(0, 16)} · ${n.source || 'web'}`),
                                ])
                            ))
                            : h('span', { class: 'muted' }, '尚无笔记。点「探索一次」；需启用 exploration + 联网搜索。'),
                    ],
                    h(Button, { type: 'primary', class: 'btn-sm', disabled: triggering.value, onClick: () => trigger('explore') }, () => '探索一次'),
                ),
                active
                    ? panel(
                        '详情',
                        active.query || '探索',
                        [
                            metaRow('动机', active.motive),
                            metaRow('可分享', active.should_share ? '是' : '否'),
                            metaRow('时间', (active.created_at || '').replace('T', ' ')),
                            h('div', { class: 'eyebrow', style: 'margin-top:.4rem;' }, '观感'),
                            prose(active.impression || ''),
                            active.self_link
                                ? h('div', {}, [
                                    h('div', { class: 'eyebrow', style: 'margin-top:.5rem;' }, '与我的关联'),
                                    prose(active.self_link),
                                ])
                                : null,
                            h('div', { class: 'eyebrow', style: 'margin-top:.6rem;' }, `检索结果（${(active.items || []).length}）`),
                            linkList(active.items || []),
                        ],
                        active.should_share ? chip('曾标记可分享', 'success') : null,
                    )
                    : panel('详情', '选择一条笔记', [h('span', { class: 'muted' }, '左侧点选笔记查看检索结果与观感')]),
            ]);
        }

        function renderBook() {
            const list = projects.value || [];
            const active = list.find(p => p.id === openBookId.value) || list[0] || null;
            if (active && active.id !== openBookId.value) openBookId.value = active.id;
            const chunks = (active && active.draft_chunks) || [];
            const fullText = chunks.map(c => c.text || '').join('\n\n');

            return h('div', {
                class: 'grid gap-3',
                style: 'grid-template-columns: minmax(14rem, .85fr) minmax(0, 1.4fr);',
            }, [
                panel(
                    '书柜',
                    `共 ${list.length} 部`,
                    [
                        list.length
                            ? h('div', { style: 'display:grid;gap:.45rem;max-height:28rem;overflow:auto;' }, list.map(p =>
                                h('button', {
                                    type: 'button',
                                    class: 'btn ghost',
                                    style: `justify-content:flex-start;text-align:left;padding:.7rem .75rem;${active && active.id === p.id ? 'border-color:hsl(var(--accent));background:hsl(var(--accent)/.15);' : ''}`,
                                    onClick: () => { openBookId.value = p.id; },
                                }, [
                                    h('div', { class: 'flex justify-between gap-2', style: 'width:100%;' }, [
                                        h('strong', { style: 'word-break:break-word;' }, `《${p.title || '未命名'}》`),
                                        chip(p.status || 'drafting', p.status === 'finished' ? 'success' : 'info'),
                                    ]),
                                    h('div', { class: 'muted', style: 'font-size:.8rem;margin-top:.25rem;' },
                                        `${p.work_type || ''} · ${p.current_chars || 0}/${p.target_chars || 0}`),
                                ])
                            ))
                            : h('span', { class: 'muted' }, '书柜为空。启用 creative 后空闲时会开写；也可点「续写创作」。'),
                    ],
                    h(Button, { type: 'secondary', class: 'btn-sm', disabled: triggering.value, onClick: () => trigger('creative') }, () => '续写'),
                ),
                active
                    ? panel(
                        active.work_type || '作品',
                        `《${active.title || '未命名'}》`,
                        [
                            h('div', { class: 'flex gap-2 flex-wrap' }, [
                                chip(active.status || 'drafting', active.status === 'finished' ? 'success' : 'info'),
                                chip(active.tone || '—'),
                            ]),
                            progressBar(active.current_chars, active.target_chars),
                            metaRow('设定', active.premise),
                            metaRow('灵感', active.inspiration_source),
                            metaRow('下一段', active.next_hint),
                            (active.outline || []).length
                                ? h('div', {}, [
                                    h('div', { class: 'eyebrow', style: 'margin-top:.35rem;' }, '大纲'),
                                    h('ol', { style: 'margin:.3rem 0 0;padding-left:1.2rem;' },
                                        active.outline.map(x => h('li', {}, x))),
                                ])
                                : null,
                            h('div', { class: 'eyebrow', style: 'margin-top:.6rem;' }, `正文（${chunks.length} 节）`),
                            chunks.length
                                ? h('div', {
                                    style: 'display:grid;gap:1rem;max-height:26rem;overflow:auto;padding:.2rem;',
                                }, chunks.map((c, idx) =>
                                    h('section', {
                                        style: 'border:1px solid hsl(var(--border));border-radius:calc(var(--radius)*.7);padding:.75rem .85rem;background:hsl(var(--background)/.4);',
                                    }, [
                                        h('div', { class: 'flex justify-between muted', style: 'font-size:.78rem;margin-bottom:.4rem;' }, [
                                            h('span', {}, `第 ${idx + 1} 节`),
                                            h('span', {}, `${c.chars || (c.text || '').length} 字 · ${(c.at || '').replace('T', ' ').slice(0, 16)}`),
                                        ]),
                                        prose(c.text || ''),
                                    ])
                                ))
                                : h('span', { class: 'muted' }, '尚无正文，点「续写」推进'),
                            fullText
                                ? h(Button, {
                                    type: 'ghost',
                                    class: 'btn-sm',
                                    style: 'justify-self:start;',
                                    onClick: async () => {
                                        try {
                                            await navigator.clipboard.writeText(fullText);
                                            showToast('全文已复制', 'success');
                                        } catch (_) {
                                            showToast('复制失败', 'error');
                                        }
                                    },
                                }, () => '复制全文')
                                : null,
                        ],
                    )
                    : panel('作品', '选择一部作品', [h('span', { class: 'muted' }, '左侧点选查看大纲与正文')]),
            ]);
        }
    },
});
