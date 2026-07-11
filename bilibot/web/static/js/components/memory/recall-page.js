// components/memory/recall-page.js - 召回测试页（Golden Time 设计稿）
const { defineComponent, h, ref, computed, onMounted, watch } = window.Vue;
import { api } from '../../api.js';
import { Card, Button, Badge, FormTextarea, Loading, EmptyState, Icon, HeroPanel, ProgressBar } from '../common.js';
import { appState, showToast } from '../../state.js';

// 分类标签映射（与 list-page 保持一致）
const CATEGORY_LABELS = {
    episodic: '情景记忆',
    factual: '事实记忆',
    procedural: '程序记忆',
    preference: '用户偏好',
    interaction: '互动历史',
    tag: '内容标签',
    emotion: '情感记忆',
    behavior: '行为模式',
    fact: '事实',
    event: '事件',
    other: '其他',
};

function categoryLabel(cat) {
    return CATEGORY_LABELS[cat] || cat || '未分类';
}

// 时间格式化（兼容 unix 秒 / 毫秒 / ISO 字符串）
function formatTime(ts) {
    if (!ts) return '-';
    const d = new Date(typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts);
    return d.toLocaleString('zh-CN', { hour12: false });
}

export const MemoryRecallPage = defineComponent({
    name: 'MemoryRecallPage',
    setup() {
        const accountId = ref(null);
        const stats = ref({ total: 0, categories: {} });
        const loading = ref(true);
        const query = ref('');
        const results = ref([]);
        const searching = ref(false);
        const hasSearched = ref(false);

        // 分类数
        const categoryCount = computed(() => Object.keys(stats.value.categories || {}).length);

        // 平均分数：优先使用 stats.avg_score，否则从结果集计算
        const avgScore = computed(() => {
            const s = stats.value;
            if (s && typeof s.avg_score === 'number') return s.avg_score;
            const items = results.value;
            if (!Array.isArray(items) || items.length === 0) return null;
            let sum = 0, n = 0;
            for (const it of items) {
                if (it && typeof it.importance_score === 'number') {
                    sum += it.importance_score;
                    n++;
                }
            }
            return n > 0 ? sum / n : null;
        });

        // 顶部分类占比（用于 ProgressBar 可视化）
        const topCategory = computed(() => {
            const cats = stats.value.categories || {};
            const entries = Object.entries(cats);
            if (entries.length === 0) return null;
            const total = entries.reduce((a, [, v]) => a + (v || 0), 0) || 1;
            const [key, count] = entries.reduce((a, b) => (b[1] > a[1] ? b : a), entries[0]);
            return {
                key,
                label: categoryLabel(key),
                count: count || 0,
                percent: Math.round(((count || 0) / total) * 100),
            };
        });

        async function loadData() {
            accountId.value = appState.currentAccountId || appState.accounts[0]?.id;
            if (!accountId.value) {
                loading.value = false;
                return;
            }
            loading.value = true;
            try {
                const data = await api.memory.stats(accountId.value).catch(() => ({ total: 0, categories: {} }));
                stats.value = data || { total: 0, categories: {} };
            } catch (e) {
                showToast('加载统计失败: ' + e.message, 'error');
                stats.value = { total: 0, categories: {} };
            } finally {
                loading.value = false;
            }
        }

        async function runRecall() {
            if (!accountId.value) {
                showToast('请先选择账号', 'warning');
                return;
            }
            const q = query.value.trim();
            if (!q) {
                showToast('请输入查询内容', 'warning');
                return;
            }
            searching.value = true;
            hasSearched.value = true;
            try {
                const data = await api.memory.search(accountId.value, {
                    query: q,
                    keyword: q,
                    limit: 10,
                });
                const items = data?.items || data || [];
                results.value = Array.isArray(items) ? items : [];
                if (results.value.length === 0) {
                    showToast('未召回相关记忆', 'info');
                } else {
                    showToast(`命中 ${results.value.length} 条记忆`, 'success');
                }
            } catch (e) {
                showToast('召回失败: ' + e.message, 'error');
                results.value = [];
            } finally {
                searching.value = false;
            }
        }

        function clearResults() {
            query.value = '';
            results.value = [];
            hasSearched.value = false;
        }

        onMounted(loadData);
        watch(() => appState.currentAccountId, (newId) => {
            if (newId) {
                results.value = [];
                hasSearched.value = false;
                query.value = '';
                loadData();
            }
        });

        return () => {
            // 无账号
            if (!loading.value && !accountId.value) {
                return h('div', { class: 'view-frame' }, [
                    h(EmptyState, {
                        icon: 'folder',
                        title: '暂无账号',
                        desc: '请先在账号管理中添加 B站 账号后再进行召回测试。',
                    }),
                ]);
            }

            if (loading.value) {
                return h(Loading);
            }

            const totalCount = stats.value.total ?? 0;
            const avgScoreText = avgScore.value != null
                ? avgScore.value.toFixed(2)
                : '0.00';

            return h('div', { class: 'view-frame' }, [
                // ═══════ Section 1: hero-band — 召回测试面板 + 统计 Card ═══════
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左：召回测试面板（hero-panel accent 背景）
                    h(HeroPanel, {
                        eyebrow: '召回测试',
                        title: '测试记忆库检索能力',
                        badge: '已就绪',
                        badgeType: 'success',
                    }, () => h('p', {
                        class: 'muted m-0',
                        style: 'font-size: 0.95rem; line-height: 1.6; max-width: 36rem;',
                    }, '输入一段测试消息，验证语义检索与召回效果。系统将基于向量相似度返回最相关的记忆条目，帮助确认记忆库的可用性。')),

                    // 右：统计 Card
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '记忆概览'),
                            h('h2', {
                                class: 'm-0',
                                style: 'font-size: 1.35rem; line-height: 1.08;',
                            }, '数据统计'),
                        ]),
                        // 3 个数据行
                        h('div', { class: 'grid gap-2' }, [
                            h('div', {
                                class: 'flex items-center justify-between gap-2',
                                style: 'padding: calc(var(--spacing) * 2) 0; border-bottom: 1px solid hsl(var(--border));',
                            }, [
                                h('span', { class: 'muted', style: 'font-size: 0.92rem;' }, '总记忆数'),
                                h('span', {
                                    style: 'font-size: 1.15rem; font-weight: 600; font-variant-numeric: tabular-nums;',
                                }, totalCount.toLocaleString()),
                            ]),
                            h('div', {
                                class: 'flex items-center justify-between gap-2',
                                style: 'padding: calc(var(--spacing) * 2) 0; border-bottom: 1px solid hsl(var(--border));',
                            }, [
                                h('span', { class: 'muted', style: 'font-size: 0.92rem;' }, '分类数'),
                                h('span', {
                                    style: 'font-size: 1.15rem; font-weight: 600; font-variant-numeric: tabular-nums;',
                                }, String(categoryCount.value)),
                            ]),
                            h('div', {
                                class: 'flex items-center justify-between gap-2',
                                style: 'padding: calc(var(--spacing) * 2) 0;',
                            }, [
                                h('span', { class: 'muted', style: 'font-size: 0.92rem;' }, '平均分数'),
                                h('span', {
                                    style: 'font-size: 1.15rem; font-weight: 600; font-variant-numeric: tabular-nums;',
                                }, avgScoreText),
                            ]),
                        ]),
                        // ProgressBar
                        h('div', { class: 'grid gap-1' }, [
                            h('div', {
                                class: 'flex items-center justify-between gap-2',
                            }, [
                                h('span', { class: 'muted', style: 'font-size: 0.88rem;' },
                                    topCategory.value ? `Top 分类 · ${topCategory.value.label}` : '记忆分布'),
                                h('span', {
                                    style: 'font-size: 0.82rem; color: hsl(var(--muted-foreground)); font-variant-numeric: tabular-nums;',
                                }, topCategory.value ? `${topCategory.value.percent}%` : '-'),
                            ]),
                            h(ProgressBar, {
                                value: topCategory.value ? topCategory.value.percent : 0,
                                max: 100,
                                showValue: false,
                            }),
                        ]),
                    ]),
                ]),

                // ═══════ Section 2: 测试区 ═══════
                h('section', {}, [
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                    }, [
                        // 面板头
                        h('div', {
                            class: 'flex items-end justify-between gap-3',
                            style: 'flex-wrap: wrap;',
                        }, [
                            h('div', { class: 'grid gap-1 min-w-0' }, [
                                h('span', { class: 'eyebrow' }, '召回测试'),
                                h('h2', {
                                    class: 'm-0',
                                    style: 'font-size: 1.35rem; line-height: 1.08;',
                                }, '输入查询'),
                            ]),
                            h('div', { class: 'flex items-center gap-2' }, [
                                hasSearched.value
                                    ? h(Button, { size: 'sm', type: 'ghost', onClick: clearResults }, () => '清空')
                                    : null,
                            ]),
                        ]),

                        // 输入区
                        h(FormTextarea, {
                            modelValue: query.value,
                            'onUpdate:modelValue': (v) => query.value = v,
                            placeholder: '输入测试消息，例如："你还记得我们上次聊的剧情吗？"',
                            rows: 3,
                            id: 'recall-query',
                        }),
                        h('div', { class: 'flex items-center gap-2', style: 'flex-wrap: wrap;' }, [
                            h(Button, {
                                type: 'primary',
                                loading: searching.value,
                                onClick: runRecall,
                            }, () => [
                                h(Icon, { name: 'funnel', size: '0.95rem' }),
                                '召回测试',
                            ]),
                            searching.value
                                ? h('span', {
                                    class: 'inline-flex items-center gap-1',
                                    style: 'font-size: 0.88rem; color: hsl(var(--muted-foreground));',
                                }, [
                                    h('span', { class: 'spinner spinner-sm', 'aria-hidden': 'true' }),
                                    '正在检索记忆库...',
                                ])
                                : null,
                        ]),

                        // 结果区
                        searching.value
                            ? h('div', {
                                class: 'grid',
                                style: 'padding: calc(var(--spacing) * 5) 0; justify-items: center;',
                            }, [h(Loading)])
                            : hasSearched.value && results.value.length === 0
                                ? h('div', {
                                    class: 'grid',
                                    style: 'padding: calc(var(--spacing) * 4) 0; justify-items: center;',
                                }, [h(EmptyState, {
                                    icon: 'folder',
                                    title: '无召回结果',
                                    desc: '未找到与查询相关的记忆，尝试调整关键词后重试。',
                                })])
                                : results.value.length > 0
                                    ? h('div', { class: 'grid gap-3' }, [
                                        // 召回数量 Badge
                                        h('div', {
                                            class: 'flex items-center gap-2',
                                            style: 'flex-wrap: wrap;',
                                        }, [
                                            h(Badge, { type: 'success' }, () => `命中 ${results.value.length} 条`),
                                            h('span', {
                                                class: 'muted',
                                                style: 'font-size: 0.85rem;',
                                            }, '按相关性排序'),
                                        ]),
                                        // 每项结果一张小 Card
                                        ...results.value.map((item, idx) => {
                                            const score = typeof item.importance_score === 'number'
                                                ? item.importance_score
                                                : null;
                                            const scoreText = score != null ? score.toFixed(2) : null;
                                            return h('article', {
                                                key: item.id ?? idx,
                                                class: 'grid gap-2',
                                                style: 'background: hsl(var(--muted) / 0.3); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.76); padding: calc(var(--spacing) * 3);',
                                            }, [
                                                // 顶栏：分类 + 时间 + 分数
                                                h('div', {
                                                    class: 'flex items-center justify-between gap-2',
                                                    style: 'flex-wrap: wrap;',
                                                }, [
                                                    h('div', { class: 'flex items-center gap-2', style: 'flex-wrap: wrap;' }, [
                                                        h(Badge, { type: 'info', size: 'sm' }, () =>
                                                            categoryLabel(item.category)),
                                                        h('span', {
                                                            class: 'muted',
                                                            style: 'font-size: 0.82rem; font-variant-numeric: tabular-nums;',
                                                        }, formatTime(item.created_at)),
                                                    ]),
                                                    scoreText != null
                                                        ? h(Badge, { type: 'warning', size: 'sm' }, () => `分数: ${scoreText}`)
                                                        : null,
                                                ]),
                                                // 内容
                                                h('p', {
                                                    class: 'm-0',
                                                    style: 'font-size: 0.96rem; line-height: 1.65; color: hsl(var(--foreground)); word-break: break-word;',
                                                }, item.content || item.summary || '-'),
                                                // 来源（可选）
                                                item.source
                                                    ? h('div', {
                                                        class: 'flex items-center gap-1',
                                                        style: 'font-size: 0.82rem; color: hsl(var(--muted-foreground));',
                                                    }, [
                                                        h(Icon, { name: 'tag', size: '0.75rem' }),
                                                        h('span', { class: 'truncate' }, item.source),
                                                    ])
                                                    : null,
                                            ]);
                                        }),
                                    ])
                                    : null,
                    ]),
                ]),

                // ═══════ Section 3: 使用提示 ═══════
                h('section', {}, [
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                    }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '使用指南'),
                            h('h2', {
                                class: 'm-0',
                                style: 'font-size: 1.35rem; line-height: 1.08;',
                            }, '如何测试'),
                        ]),
                        h('div', { class: 'grid gap-3' }, [
                            h('div', {
                                class: 'flex items-start gap-2',
                            }, [
                                h('span', {
                                    style: 'flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; width: 1.5rem; height: 1.5rem; border-radius: 999px; background: hsl(var(--accent) / 0.18); color: hsl(var(--accent-foreground));',
                                }, [h(Icon, { name: 'circle-check', size: '0.95rem' })]),
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', {
                                        style: 'font-size: 0.98rem; font-weight: 500; color: hsl(var(--foreground));',
                                    }, '输入自然语言'),
                                    h('span', {
                                        class: 'muted',
                                        style: 'font-size: 0.88rem; line-height: 1.6;',
                                    }, '使用完整的句子或问题进行测试，更接近真实对话场景，能更好反映语义召回效果。'),
                                ]),
                            ]),
                            h('div', {
                                class: 'flex items-start gap-2',
                            }, [
                                h('span', {
                                    style: 'flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; width: 1.5rem; height: 1.5rem; border-radius: 999px; background: hsl(var(--accent) / 0.18); color: hsl(var(--accent-foreground));',
                                }, [h(Icon, { name: 'circle-check', size: '0.95rem' })]),
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', {
                                        style: 'font-size: 0.98rem; font-weight: 500; color: hsl(var(--foreground));',
                                    }, '关注分数与分类'),
                                    h('span', {
                                        class: 'muted',
                                        style: 'font-size: 0.88rem; line-height: 1.6;',
                                    }, '重要性分数（0-1）反映记忆的权重，分类标签标识记忆类型，二者结合可判断召回质量。'),
                                ]),
                            ]),
                            h('div', {
                                class: 'flex items-start gap-2',
                            }, [
                                h('span', {
                                    style: 'flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; width: 1.5rem; height: 1.5rem; border-radius: 999px; background: hsl(var(--accent) / 0.18); color: hsl(var(--accent-foreground));',
                                }, [h(Icon, { name: 'circle-check', size: '0.95rem' })]),
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', {
                                        style: 'font-size: 0.98rem; font-weight: 500; color: hsl(var(--foreground));',
                                    }, '多次对比验证'),
                                    h('span', {
                                        class: 'muted',
                                        style: 'font-size: 0.88rem; line-height: 1.6;',
                                    }, '尝试不同的表达方式查询同一意图，对比召回结果一致性，评估记忆库的稳定性与鲁棒性。'),
                                ]),
                            ]),
                            h('div', {
                                class: 'flex items-start gap-2',
                            }, [
                                h('span', {
                                    style: 'flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center; width: 1.5rem; height: 1.5rem; border-radius: 999px; background: hsl(var(--accent) / 0.18); color: hsl(var(--accent-foreground));',
                                }, [h(Icon, { name: 'circle-check', size: '0.95rem' })]),
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', {
                                        style: 'font-size: 0.98rem; font-weight: 500; color: hsl(var(--foreground));',
                                    }, '结合列表页管理'),
                                    h('span', {
                                        class: 'muted',
                                        style: 'font-size: 0.88rem; line-height: 1.6;',
                                    }, '若召回结果异常或缺失，可前往记忆列表页检查数据完整性，必要时清理无效条目。'),
                                ]),
                            ]),
                        ]),
                    ]),
                ]),
            ]);
        };
    },
});
