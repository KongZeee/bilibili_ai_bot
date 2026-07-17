// components/memory/recall-page.js - V6 recall debugger
const { defineComponent, h, ref, computed, onMounted, watch } = window.Vue;
import { api } from '../../api.js';
import { Button, Badge, EmptyState, Icon, Loading } from '../common.js';
import { appState, showToast } from '../../state.js';
import { formatTime } from '../../utils.js';

function score(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
    return Number(value).toFixed(3);
}

function modeLabel(mode) {
    return mode === 'llm' ? '模型重排' : mode === 'fallback' ? '确定性降级' : '空召回';
}

function normalizeCandidate(candidate, mode = 'llm') {
    const kind = candidate.kind || candidate.decision || 'direct';
    const fallback = mode === 'fallback';
    return {
        ...candidate,
        candidate_id: candidate.candidate_id || candidate.event_id || '',
        channels: candidate.channels || [],
        kind,
        evidence_ids: candidate.evidence_ids || [],
        D: candidate.D ?? candidate.deterministic_score ?? candidate.d ?? 0,
        L: candidate.L ?? candidate.llm_score ?? candidate.l ?? null,
        F: candidate.F ?? candidate.final_score ?? candidate.f ?? 0,
        injected: Boolean(candidate.injected ?? candidate.accepted),
        threshold: candidate.threshold ?? (
            kind === 'association'
                ? (fallback ? 0.88 : 0.80)
                : (fallback ? 0.80 : 0.72)
        ),
    };
}

export const MemoryRecallPage = defineComponent({
    name: 'MemoryRecallPage',
    setup() {
        const accountId = ref(null);
        const query = ref('');
        const title = ref('');
        const bvid = ref('');
        const oid = ref('');
        const scene = ref('memory_debug');
        const recentTurns = ref('');
        const loading = ref(false);
        const historyLoading = ref(false);
        const result = ref(null);
        const traces = ref([]);
        const activeTraceId = ref('');

        const trace = computed(() => result.value?.trace || {});
        const candidates = computed(() => (trace.value.candidates || result.value?.candidates || [])
            .map(candidate => normalizeCandidate(candidate, trace.value.mode)));
        const injected = computed(() => candidates.value.filter(item => item.injected));
        const finalPrompt = computed(() => result.value?.final_prompt || result.value?.prompt_evidence || '');
        const channelErrors = computed(() => Object.entries(trace.value.channel_errors || {}));

        async function loadTraces() {
            const nextId = appState.currentAccountId
                || appState.accounts[0]?.account_id
                || appState.accounts[0]?.id
                || '';
            if (!nextId) {
                accountId.value = '';
                traces.value = [];
                return;
            }
            if (accountId.value && accountId.value !== nextId) {
                result.value = null;
                traces.value = [];
            }
            accountId.value = nextId;
            historyLoading.value = true;
            try {
                const data = await api.memory.recallTraces(accountId.value, { limit: 30 });
                if (accountId.value !== nextId) return;
                traces.value = data?.items || [];
            } catch (e) {
                showToast('读取召回记录失败: ' + e.message, 'error');
            } finally {
                historyLoading.value = false;
            }
        }

        async function runRecall() {
            if (!accountId.value || !query.value.trim()) return;
            loading.value = true;
            activeTraceId.value = '';
            const boundAccount = accountId.value;
            try {
                const turns = recentTurns.value
                    .split('\n')
                    .map(line => line.trim())
                    .filter(Boolean)
                    .slice(-6);
                // 仅账号级 API；body 带 account_id 便于后端日志/审计对齐
                result.value = await api.memory.recall(accountId.value, {
                    query: query.value.trim(),
                    recent_turns: turns,
                    title: title.value.trim(),
                    bvid: bvid.value.trim(),
                    oid: oid.value.trim(),
                    scene: scene.value || 'memory_debug',
                    account_id: boundAccount,
                });
                if (accountId.value !== boundAccount) return;
                activeTraceId.value = result.value?.trace_id || '';
                await loadTraces();
            } catch (e) {
                showToast('召回失败: ' + e.message, 'error');
            } finally {
                loading.value = false;
            }
        }

        async function openTrace(traceId) {
            if (!accountId.value || !traceId) return;
            loading.value = true;
            try {
                const data = await api.memory.recallTrace(accountId.value, traceId);
                result.value = {
                    trace_id: traceId,
                    final_prompt: data.final_prompt || '',
                    events: data.events || [],
                    trace: {
                        ...data,
                        mode: data.mode || (data.used_fallback ? 'fallback' : ((data.candidates || []).length ? 'llm' : 'empty')),
                        candidates: data.candidates || [],
                    },
                };
                query.value = data.query_text || query.value;
                activeTraceId.value = traceId;
            } catch (e) {
                showToast('读取 trace 失败: ' + e.message, 'error');
            } finally {
                loading.value = false;
            }
        }

        function clearResult() {
            result.value = null;
            activeTraceId.value = '';
        }

        onMounted(loadTraces);
        watch(() => appState.currentAccountId, (newId, prevId) => {
            if (!newId || newId === prevId) return;
            result.value = null;
            traces.value = [];
            activeTraceId.value = '';
            loadTraces();
        });

        return () => {
            if (!accountId.value && !historyLoading.value) {
                return h('div', { class: 'view-frame' }, [
                    h(EmptyState, { icon: 'folder', title: '暂无账号', desc: '请先选择账号。' }),
                ]);
            }

            const candidateColumns = 'minmax(16rem, 1.6fr) minmax(11rem, 1fr) 4.5rem 4.5rem 4.5rem 5rem 8rem';
            const headerStyle = 'font-size: .74rem; color: hsl(var(--muted-foreground)); text-transform: uppercase; letter-spacing: .08em;';

            return h('div', { class: 'view-frame' }, [
                h('section', { class: 'grid gap-3', style: 'grid-template-columns: repeat(auto-fit, minmax(min(100%, 24rem), 1fr));' }, [
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * .82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'flex items-end justify-between gap-3', style: 'flex-wrap: wrap;' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, 'V6 召回调试'),
                                h('h2', { class: 'm-0', style: 'font-size: 1.35rem;' }, '查询上下文'),
                            ]),
                            result.value ? h(Button, { type: 'ghost', size: 'sm', onClick: clearResult }, () => '清空结果') : null,
                        ]),
                        h('label', { class: 'grid gap-1' }, [
                            h('span', { class: 'form-label' }, '当前消息'),
                            h('textarea', {
                                class: 'form-input',
                                rows: 4,
                                id: 'recall-query',
                                value: query.value,
                                placeholder: '输入要验证的消息',
                                onInput: event => query.value = event.target.value,
                                onKeydown: event => {
                                    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') runRecall();
                                },
                            }),
                        ]),
                        h('div', { class: 'grid gap-2', style: 'grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));' }, [
                            h('label', { class: 'grid gap-1' }, [
                                h('span', { class: 'form-label' }, '标题'),
                                h('input', { class: 'form-input', value: title.value, onInput: event => title.value = event.target.value }),
                            ]),
                            h('label', { class: 'grid gap-1' }, [
                                h('span', { class: 'form-label' }, '视频 BV 号'),
                                h('input', { class: 'form-input', value: bvid.value, onInput: event => bvid.value = event.target.value, placeholder: '可选' }),
                            ]),
                            h('label', { class: 'grid gap-1' }, [
                                h('span', { class: 'form-label' }, '对象 ID'),
                                h('input', { class: 'form-input', value: oid.value, onInput: event => oid.value = event.target.value, placeholder: '可选' }),
                            ]),
                            h('label', { class: 'grid gap-1' }, [
                                h('span', { class: 'form-label' }, '场景'),
                                h('select', { class: 'form-input', value: scene.value, onChange: event => scene.value = event.target.value }, [
                                    h('option', { value: 'memory_debug' }, '调试'),
                                    h('option', { value: 'reply_comment' }, '评论回复'),
                                    h('option', { value: 'private_message' }, '私信'),
                                    h('option', { value: 'proactive' }, '主动行为'),
                                ]),
                            ]),
                        ]),
                        h('label', { class: 'grid gap-1' }, [
                            h('span', { class: 'form-label' }, '最近对话'),
                            h('textarea', {
                                class: 'form-input',
                                rows: 3,
                                maxlength: 1200,
                                placeholder: '每行一个 turn，最多采用最近 6 条',
                                value: recentTurns.value,
                                onInput: event => recentTurns.value = event.target.value,
                            }),
                        ]),
                        h('div', { class: 'flex justify-end' }, [
                            h(Button, { type: 'primary', loading: loading.value, disabled: !query.value.trim(), onClick: runRecall }, () => [
                                h(Icon, { name: 'circle-check', size: '.95rem' }),
                                '执行召回',
                            ]),
                        ]),
                    ]),
                    h('aside', {
                        class: 'grid gap-2',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * .82); padding: calc(var(--spacing) * 4); align-content: start; max-height: 34rem; overflow: auto;',
                    }, [
                        h('div', { class: 'flex items-center justify-between gap-2' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '历史'),
                                h('h2', { class: 'm-0', style: 'font-size: 1.15rem;' }, '最近召回'),
                            ]),
                            h(Button, { type: 'ghost', size: 'sm', loading: historyLoading.value, onClick: loadTraces }, () => '刷新'),
                        ]),
                        historyLoading.value && traces.value.length === 0
                            ? h(Loading)
                            : traces.value.length === 0
                                ? h('p', { class: 'muted m-0' }, '暂无记录')
                                : traces.value.map(item => h('button', {
                                    key: item.id,
                                    type: 'button',
                                    class: 'grid gap-1',
                                    style: `width: 100%; text-align: left; padding: calc(var(--spacing) * 2.5); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * .55); background: ${activeTraceId.value === item.id ? 'hsl(var(--accent) / .18)' : 'transparent'}; color: inherit; cursor: pointer;`,
                                    onClick: () => openTrace(item.id),
                                }, [
                                    h('span', { style: 'font-size: .9rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;' }, item.query_text || item.query_hash || item.id),
                                    h('span', { class: 'muted', style: 'font-size: .78rem;' }, `${formatTime(item.created_at)} · ${item.candidate_count || 0} 候选 · ${item.injected_count || 0} 注入`),
                                ])),
                    ]),
                ]),

                result.value
                    ? h('section', { class: 'grid gap-3' }, [
                        h('article', {
                            class: 'grid gap-3 memory-panel',
                            style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * .82); padding: calc(var(--spacing) * 4);',
                        }, [
                            h('div', { class: 'flex items-end justify-between gap-3', style: 'flex-wrap: wrap;' }, [
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', { class: 'eyebrow' }, '候选决策'),
                                    h('h2', { class: 'm-0', style: 'font-size: 1.25rem;' }, `${candidates.value.length} 个候选 · ${injected.value.length} 个注入`),
                                ]),
                                h('div', { class: 'flex gap-2', style: 'flex-wrap: wrap;' }, [
                                    h(Badge, { type: trace.value.mode === 'fallback' ? 'warning' : 'info' }, () => modeLabel(trace.value.mode)),
                                    h(Badge, {
                                        type: (candidates.value.length ? trace.value.rerank_calls === 1 : (trace.value.rerank_calls ?? 0) === 0) ? 'success' : 'warning',
                                    }, () => `${trace.value.rerank_calls ?? 0} 次重排`),
                                    trace.value.rerank_status ? h(Badge, { type: trace.value.rerank_status === 'ok' ? 'success' : 'warning' }, () => trace.value.rerank_status) : null,
                                    h(Badge, { type: 'info' }, () => `${trace.value.latency_ms ?? 0} ms`),
                                ]),
                            ]),
                            channelErrors.value.length
                                ? h('div', {
                                    role: 'alert',
                                    class: 'grid gap-1',
                                    style: 'padding: calc(var(--spacing) * 2.5); border-left: 3px solid hsl(var(--chart-5)); background: hsl(var(--chart-5) / .08);',
                                }, [
                                    h('strong', { style: 'font-size: .84rem;' }, '候选通道发生降级'),
                                    ...channelErrors.value.map(([channel, message]) =>
                                        h('span', { key: channel, class: 'memory-break muted', style: 'font-size: .78rem;' }, `${channel}: ${message}`)
                                    ),
                                ])
                                : null,
                            candidates.value.length === 0
                                ? h(EmptyState, { icon: 'folder', title: '无候选', desc: '本次没有可注入的记忆。' })
                                : h('div', { class: 'memory-table-scroll' }, [
                                    h('div', { class: 'grid gap-0', style: 'min-width: 900px;' }, [
                                        h('div', { class: 'grid gap-2', style: `grid-template-columns: ${candidateColumns}; padding: 0 0 calc(var(--spacing) * 2); border-bottom: 1px solid hsl(var(--border));` }, [
                                            h('span', { style: headerStyle }, '候选事件'),
                                            h('span', { style: headerStyle }, '命中通道'),
                                            h('span', { style: headerStyle }, '确定分'),
                                            h('span', { style: headerStyle }, '模型分'),
                                            h('span', { style: headerStyle }, '最终分'),
                                            h('span', { style: headerStyle }, '阈值'),
                                            h('span', { style: headerStyle }, '类型 / 决策'),
                                        ]),
                                        ...candidates.value.map(item => h('div', {
                                            key: item.candidate_id,
                                            class: 'grid items-center gap-2',
                                            style: `grid-template-columns: ${candidateColumns}; padding: calc(var(--spacing) * 2.4) 0; border-bottom: 1px solid hsl(var(--border)); ${item.injected ? 'background: hsl(var(--accent) / .08);' : ''}`,
                                        }, [
                                            h('div', { class: 'grid gap-1 min-w-0' }, [
                                                h('code', { class: 'memory-break', style: 'font-size: .8rem;' }, item.candidate_id),
                                                item.reason ? h('span', { class: 'muted memory-break', style: 'font-size: .78rem;' }, item.reason) : null,
                                                item.evidence_ids.length
                                                    ? h('span', { class: 'muted memory-break', style: 'font-size: .72rem;' }, `证据: ${item.evidence_ids.join(', ')}`)
                                                    : null,
                                            ]),
                                            h('div', { class: 'flex gap-1', style: 'flex-wrap: wrap;' }, item.channels.map(channel => h('span', { key: channel, class: 'badge badge-info', style: 'font-size: .7rem;' }, channel))),
                                            h('code', score(item.D)),
                                            h('code', score(item.L)),
                                            h('code', score(item.F)),
                                            h('code', score(item.threshold)),
                                            h('div', { class: 'flex gap-1', style: 'flex-wrap: wrap;' }, [
                                                h(Badge, { type: item.kind === 'association' ? 'warning' : 'info' }, () => item.kind === 'association' ? '联想' : '直接'),
                                                h(Badge, { type: item.injected ? 'success' : 'warning' }, () => item.injected ? '注入' : '拒绝'),
                                            ]),
                                        ])),
                                    ]),
                                ]),
                        ]),

                        h('div', { class: 'grid gap-3', style: 'grid-template-columns: repeat(auto-fit, minmax(min(100%, 24rem), 1fr));' }, [
                            h('article', {
                                class: 'grid gap-2',
                                style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * .82); padding: calc(var(--spacing) * 4); align-content: start;',
                            }, [
                                h('span', { class: 'eyebrow' }, '最终注入'),
                                h('h2', { class: 'm-0', style: 'font-size: 1.15rem;' }, `${(result.value.events || []).length} 个事件`),
                                ...(result.value.events || []).map(event => h('div', {
                                    key: event.id,
                                    class: 'grid gap-1',
                                    style: 'padding: calc(var(--spacing) * 2.5) 0; border-top: 1px solid hsl(var(--border));',
                                }, [
                                    h('div', { class: 'flex gap-2', style: 'flex-wrap: wrap;' }, [
                                        h('span', { class: 'badge badge-info' }, event.source_type || event.source || '未知'),
                                        h('code', { style: 'font-size: .76rem;' }, event.id),
                                    ]),
                                    h('p', { class: 'm-0 memory-break', style: 'line-height: 1.55;' }, event.summary || event.content || '-'),
                                    ...(event.evidence_chunks || event.chunks || []).map((chunk, index) => h('div', {
                                        key: chunk.id || index,
                                        class: 'grid gap-1',
                                        style: 'padding: calc(var(--spacing) * 2); background: hsl(var(--muted) / .28); border-left: 2px solid hsl(var(--accent));',
                                    }, [
                                        h('code', { class: 'memory-break', style: 'font-size: .72rem;' }, chunk.id || `chunk-${index + 1}`),
                                        h('p', { class: 'm-0 memory-break', style: 'font-size: .82rem; line-height: 1.5; white-space: pre-wrap;' }, chunk.text || '-'),
                                    ])),
                                ])),
                            ]),
                            h('article', {
                                class: 'grid gap-2',
                                style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * .82); padding: calc(var(--spacing) * 4); align-content: start; min-width: 0;',
                            }, [
                                h('div', { class: 'flex justify-between gap-2', style: 'flex-wrap: wrap;' }, [
                                    h('div', { class: 'grid gap-1' }, [
                                        h('span', { class: 'eyebrow' }, '提示词'),
                                        h('h2', { class: 'm-0', style: 'font-size: 1.15rem;' }, '最终记忆证据'),
                                    ]),
                                    h('span', { class: 'muted', style: 'font-size: .82rem;' }, `${finalPrompt.value.length} / 5000 字`),
                                ]),
                                h('pre', {
                                    style: 'margin: 0; max-height: 30rem; overflow: auto; padding: calc(var(--spacing) * 3); background: hsl(var(--muted) / .38); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * .55); white-space: pre-wrap; overflow-wrap: anywhere; font-size: .82rem; line-height: 1.55;',
                                }, finalPrompt.value || '<memory_evidence />'),
                            ]),
                        ]),
                    ])
                    : h('section', { style: 'padding: calc(var(--spacing) * 5) 0;' }, [
                        h(EmptyState, { icon: 'circle-question-mark', title: '暂无召回结果', desc: '尚未运行召回。' }),
                    ]),
            ]);
        };
    },
});
