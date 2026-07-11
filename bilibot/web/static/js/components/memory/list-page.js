// components/memory/list-page.js - 记忆列表页（Golden Time 设计稿）
const { defineComponent, h, ref, reactive, computed, onMounted, watch } = window.Vue;
import { api } from '../../api.js';
import { Card, Button, Badge, FormInput, Loading, EmptyState, Icon, HeroPanel, ActionList, ProgressBar, Pagination } from '../common.js';
import { appState, showToast } from '../../state.js';
import { navigate } from '../../router.js';

// 分类标签映射
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

// 时间格式化
function formatTime(ts) {
    if (!ts) return '-';
    const d = new Date(typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts);
    return d.toLocaleString('zh-CN', { hour12: false });
}

export const MemoryListPage = defineComponent({
    name: 'MemoryListPage',
    setup() {
        const accountId = ref(null);
        const memories = ref([]);
        const stats = ref({ total: 0, categories: {}, weekly_new: 0 });
        const loading = ref(true);
        const searchQuery = ref('');
        const filterCategory = ref('');
        const filterStatus = ref('');
        const page = ref(1);
        const pageSize = ref(20);
        const total = ref(0);
        const recallQuery = ref('');
        const recallResult = ref(null);
        const recalling = ref(false);

        // 分类统计列表（按数量降序）
        const categoryStats = computed(() => {
            const cats = stats.value.categories || {};
            const totalSum = Object.values(cats).reduce((a, b) => a + (b || 0), 0) || 1;
            return Object.entries(cats)
                .map(([k, v]) => ({
                    key: k,
                    label: categoryLabel(k),
                    count: v || 0,
                    percent: Math.round(((v || 0) / totalSum) * 100),
                }))
                .sort((a, b) => b.count - a.count);
        });

        const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value) || 1));

        async function loadData() {
            accountId.value = appState.currentAccountId || appState.accounts[0]?.id;
            if (!accountId.value) {
                loading.value = false;
                return;
            }
            loading.value = true;
            try {
                const [statsData, listData] = await Promise.all([
                    api.memory.stats(accountId.value).catch(() => ({ total: 0, categories: {} })),
                    api.memory.list(accountId.value, {
                        page: page.value,
                        page_size: pageSize.value,
                        ...(filterCategory.value ? { category: filterCategory.value } : {}),
                        ...(filterStatus.value ? { active: filterStatus.value } : {}),
                    }).catch(() => ({ items: [], total: 0 })),
                ]);
                stats.value = statsData || { total: 0, categories: {} };
                memories.value = listData?.items || [];
                total.value = listData?.total || 0;
            } catch (e) {
                showToast('加载失败: ' + e.message, 'error');
            } finally {
                loading.value = false;
            }
        }

        async function search() {
            if (!accountId.value) return;
            if (!searchQuery.value.trim()) {
                page.value = 1;
                await loadData();
                return;
            }
            loading.value = true;
            try {
                const data = await api.memory.search(accountId.value, { query: searchQuery.value });
                memories.value = data?.items || data || [];
                total.value = memories.value.length;
            } catch (e) {
                showToast('搜索失败: ' + e.message, 'error');
            } finally {
                loading.value = false;
            }
        }

        async function deleteMemory(id) {
            if (!confirm('确定删除此记忆？')) return;
            try {
                await api.memory.delete(accountId.value, id);
                showToast('已删除', 'success');
                await loadData();
            } catch (e) {
                showToast('删除失败: ' + e.message, 'error');
            }
        }

        async function runRecall() {
            if (!recallQuery.value.trim() || !accountId.value) return;
            recalling.value = true;
            try {
                const data = await api.memory.search(accountId.value, { query: recallQuery.value });
                const items = data?.items || data || [];
                recallResult.value = {
                    count: items.length,
                    time: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
                };
            } catch (e) {
                showToast('召回测试失败: ' + e.message, 'error');
            } finally {
                recalling.value = false;
            }
        }

        function onPageChange({ page: p }) {
            page.value = p;
            loadData();
        }

        function clearCategory() {
            filterCategory.value = '';
            page.value = 1;
            loadData();
        }

        function clearStatus() {
            filterStatus.value = '';
            page.value = 1;
            loadData();
        }

        function cycleCategory() {
            const cats = Object.keys(stats.value.categories || {});
            if (cats.length === 0) {
                showToast('暂无分类数据', 'info');
                return;
            }
            const idx = cats.indexOf(filterCategory.value);
            filterCategory.value = cats[(idx + 1) % cats.length];
            page.value = 1;
            loadData();
        }

        function cycleStatus() {
            const opts = ['', '1', '0'];
            const idx = opts.indexOf(filterStatus.value);
            filterStatus.value = opts[(idx + 1) % opts.length];
            page.value = 1;
            loadData();
        }

        onMounted(loadData);

        watch(() => appState.currentAccountId, (newId) => {
            if (newId) {
                page.value = 1;
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
                        desc: '请先在账号管理中添加 B站 账号后再管理记忆。',
                    }),
                ]);
            }

            if (loading.value && memories.value.length === 0) {
                return h(Loading);
            }

            // 表格网格列模板
            const gridCols = '6rem minmax(0, 1fr) 6rem 7rem 5rem 5rem';
            const headerStyle = 'font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.14em; color: hsl(var(--muted-foreground)); white-space: nowrap;';

            // 周新增数
            const weeklyNew = stats.value.weekly_new ?? stats.value.weeklyNew ?? 0;
            const totalCount = stats.value.total ?? total.value ?? 0;

            return h('div', { class: 'view-frame' }, [
                // ═══ Section 1: Hero band — 记忆统计 + 搜索 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左：记忆统计面板（accent 背景）
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--accent) / 0.22); border: 1px solid hsl(var(--accent)); color: hsl(var(--card-foreground)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'flex items-start justify-between gap-2' }, [
                            h('div', { class: 'grid gap-1 min-w-0' }, [
                                h('span', { class: 'eyebrow' }, '记忆库'),
                                h('h2', {
                                    class: 'm-0',
                                    style: 'font-size: 1.65rem; line-height: 1.08; text-wrap: balance; word-break: keep-all; overflow-wrap: break-word;',
                                }, `${totalCount.toLocaleString()} 条记忆`),
                            ]),
                            h('span', {
                                class: 'inline-flex items-center gap-1 whitespace-nowrap',
                                style: 'padding: calc(var(--spacing) * 0.8) calc(var(--spacing) * 1.6); border-radius: 999px; background: hsl(var(--accent) / 0.34); color: hsl(var(--accent-foreground)); font-size: 0.82rem;',
                            }, [
                                h(Icon, { name: 'circle-check', size: '0.9rem' }),
                                '已就绪',
                            ]),
                        ]),
                        h('div', {
                            class: 'inline-flex items-center gap-2 w-fit',
                            style: 'padding: calc(var(--spacing) * 1.1) calc(var(--spacing) * 2); border-radius: 999px; background: hsl(var(--muted)); color: hsl(var(--accent-foreground)); font-size: 0.86rem; white-space: nowrap;',
                        }, [
                            h(Icon, { name: 'arrow-up', size: '0.9rem' }),
                            `较上周新增 ${weeklyNew} 条`,
                        ]),
                    ]),

                    // 右：搜索 Card
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '检索'),
                            h('h2', {
                                class: 'm-0',
                                style: 'font-size: 1.35rem; line-height: 1.08;',
                            }, '搜索记忆'),
                        ]),
                        // field-wrap 搜索框
                        h('div', { class: 'flex gap-2', style: 'flex-wrap: wrap;' }, [
                            h('label', {
                                class: 'field-wrap',
                                style: 'flex: 1 1 auto; min-width: 14rem;',
                                'aria-label': '搜索记忆内容',
                            }, [
                                h(Icon, { name: 'funnel', size: '1.05rem' }),
                                h('input', {
                                    class: 'field',
                                    type: 'text',
                                    placeholder: '搜索记忆内容...',
                                    value: searchQuery.value,
                                    onInput: (e) => searchQuery.value = e.target.value,
                                    onKeyup: (e) => { if (e.key === 'Enter') search(); },
                                }),
                            ]),
                            h(Button, { type: 'primary', onClick: search }, () => '搜索'),
                        ]),
                        // 筛选按钮组
                        h('div', { class: 'flex items-center gap-2', style: 'flex-wrap: wrap;' }, [
                            h(Button, { type: 'ghost', onClick: cycleCategory }, () => [
                                h(Icon, { name: 'tag', size: '0.9rem' }),
                                filterCategory.value ? categoryLabel(filterCategory.value) : '全部分类',
                                h(Icon, { name: 'chevron-down', size: '0.75rem' }),
                            ]),
                            h(Button, { type: 'ghost', onClick: cycleStatus }, () => [
                                filterStatus.value === '1' ? '活跃' : filterStatus.value === '0' ? '归档' : '全部状态',
                                h(Icon, { name: 'chevron-down', size: '0.75rem' }),
                            ]),
                            h(Button, { type: 'ghost', onClick: () => { page.value = 1; loadData(); } }, () => [
                                '最近优先',
                                h(Icon, { name: 'chevron-down', size: '0.75rem' }),
                            ]),
                        ]),
                    ]),
                ]),

                // ═══ Section 2: 数据表格 ═══
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
                                h('span', { class: 'eyebrow' }, '记忆条目'),
                                h('h2', {
                                    class: 'm-0',
                                    style: 'font-size: 1.35rem; line-height: 1.08;',
                                }, '记忆列表'),
                            ]),
                            h('div', { class: 'flex items-center gap-2', style: 'flex-wrap: wrap;' }, [
                                h('span', {
                                    style: 'font-size: 0.88rem; color: hsl(var(--muted-foreground));',
                                }, `共 ${total.value} 条`),
                                ...(filterCategory.value
                                    ? [h(Button, { size: 'sm', type: 'ghost', onClick: clearCategory }, () => `清除分类`)]
                                    : []),
                                ...(filterStatus.value
                                    ? [h(Button, { size: 'sm', type: 'ghost', onClick: clearStatus }, () => `清除状态`)]
                                    : []),
                            ]),
                        ]),

                        // 表格容器
                        h('div', { style: 'overflow-x: auto;' }, [
                            h('div', {
                                class: 'grid gap-0',
                                style: `min-width: 860px;`,
                            }, [
                                // 表头
                                h('div', {
                                    class: 'grid items-center gap-2',
                                    style: `grid-template-columns: ${gridCols}; padding-bottom: calc(var(--spacing) * 2); border-bottom: 1px solid hsl(var(--border));`,
                                }, [
                                    h('span', { style: headerStyle }, '分类'),
                                    h('span', { style: headerStyle }, '内容预览'),
                                    h('span', { style: headerStyle }, '来源'),
                                    h('span', { style: headerStyle }, '时间'),
                                    h('span', { style: headerStyle }, '状态'),
                                    h('span', { style: headerStyle }, '操作'),
                                ]),

                                // 数据行 / 空状态
                                memories.value.length === 0
                                    ? h('div', {
                                        class: 'grid',
                                        style: 'padding: calc(var(--spacing) * 5) 0; justify-items: center; border-top: 1px solid hsl(var(--border));',
                                    }, [h(EmptyState, { icon: 'folder', title: '暂无记忆', desc: '记忆库为空或未匹配到任何条目。' })])
                                    : memories.value.map((mem) => h('div', {
                                        key: mem.id,
                                        class: 'grid items-center gap-2',
                                        style: `grid-template-columns: ${gridCols}; padding-top: calc(var(--spacing) * 2.3); padding-bottom: calc(var(--spacing) * 2.3); border-top: 1px solid hsl(var(--border));`,
                                    }, [
                                        // 分类
                                        h('span', {
                                            class: 'inline-flex items-center gap-1 whitespace-nowrap w-fit',
                                            style: 'padding: calc(var(--spacing) * 0.4) calc(var(--spacing) * 1); border-radius: 999px; background: hsl(var(--muted)); color: hsl(var(--accent-foreground)); font-size: 0.78rem;',
                                        }, [
                                            h(Icon, { name: 'tag', size: '0.75rem' }),
                                            categoryLabel(mem.category),
                                        ]),
                                        // 内容预览
                                        h('span', {
                                            style: 'font-size: 0.96rem; color: hsl(var(--foreground)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;',
                                            title: mem.content || '',
                                        }, (mem.content && mem.content.length > 60)
                                            ? mem.content.slice(0, 60) + '...'
                                            : (mem.content || '-')),
                                        // 来源
                                        h('span', {
                                            style: 'font-size: 0.92rem; color: hsl(var(--muted-foreground)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;',
                                        }, mem.source || '-'),
                                        // 时间
                                        h('span', {
                                            style: 'font-size: 0.92rem; color: hsl(var(--muted-foreground)); white-space: nowrap;',
                                        }, formatTime(mem.created_at)),
                                        // 状态
                                        h('span', {
                                            class: 'inline-flex items-center whitespace-nowrap w-fit',
                                            style: `padding: calc(var(--spacing) * 0.4) calc(var(--spacing) * 1); border-radius: 999px; font-size: 0.78rem; ${(mem.active === 0 || mem.status === 'archived')
                                                ? 'background: hsl(var(--muted)); color: hsl(var(--muted-foreground));'
                                                : 'background: hsl(var(--accent) / 0.34); color: hsl(var(--accent-foreground));'}`,
                                        }, (mem.active === 0 || mem.status === 'archived') ? '归档' : '活跃'),
                                        // 操作
                                        h('div', { class: 'flex items-center gap-1' }, [
                                            h('button', {
                                                type: 'button',
                                                'aria-label': '删除记忆',
                                                class: 'icon-btn',
                                                onClick: () => deleteMemory(mem.id),
                                            }, [h(Icon, { name: 'trash-2', size: '1.05rem' })]),
                                        ]),
                                    ])),
                            ]),
                        ]),

                        // 分页
                        h('div', {
                            class: 'flex items-center justify-between',
                            style: 'margin-top: calc(var(--spacing) * 2); flex-wrap: wrap; gap: calc(var(--spacing) * 2);',
                        }, [
                            h('span', {
                                style: 'font-size: 0.88rem; color: hsl(var(--muted-foreground));',
                            }, `共 ${total.value} 条，第 ${page.value}/${totalPages.value} 页`),
                            h(Pagination, {
                                page: page.value,
                                pageSize: pageSize.value,
                                total: total.value,
                                'onUpdate:page': (p) => { page.value = p; },
                                onChange: onPageChange,
                            }),
                        ]),
                    ]),
                ]),

                // ═══ Section 3: Split grid — 分类统计 + 召回测试 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1.15fr) minmax(18rem, 0.85fr);',
                }, [
                    // 左：分类统计 Card
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '分类统计'),
                            h('h2', {
                                class: 'm-0',
                                style: 'font-size: 1.35rem; line-height: 1.08;',
                            }, '记忆分布'),
                        ]),
                        // ProgressBar 列表
                        h('div', { class: 'grid gap-3' },
                            (categoryStats.value.length === 0
                                ? [h('p', {
                                    class: 'muted m-0',
                                    style: 'font-size: 0.92rem;',
                                }, '暂无分类数据')]
                                : categoryStats.value.slice(0, 6).map(cat => h('div', { class: 'grid gap-1' }, [
                                    h('div', { class: 'flex items-center justify-between gap-2' }, [
                                        h('span', {
                                            style: 'font-size: 0.96rem; color: hsl(var(--foreground)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;',
                                        }, cat.label),
                                        h('span', {
                                            style: 'font-size: 0.92rem; color: hsl(var(--muted-foreground)); white-space: nowrap; font-variant-numeric: tabular-nums;',
                                        }, `${cat.count} 条`),
                                    ]),
                                    h(ProgressBar, {
                                        value: cat.count,
                                        max: categoryStats.value[0]?.count || cat.count || 1,
                                        showValue: false,
                                    }),
                                ]))
                            ),
                        ),
                    ]),

                    // 右：召回测试 Card
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '快速测试'),
                            h('h2', {
                                class: 'm-0',
                                style: 'font-size: 1.35rem; line-height: 1.08;',
                            }, '召回测试'),
                        ]),
                        // 查询输入 + 测试按钮
                        h('div', { class: 'flex gap-2', style: 'flex-wrap: wrap;' }, [
                            h('label', {
                                class: 'field-wrap',
                                style: 'flex: 1 1 auto; min-width: 12rem;',
                                'aria-label': '输入查询关键词',
                            }, [
                                h(Icon, { name: 'funnel', size: '1.05rem' }),
                                h('input', {
                                    class: 'field',
                                    type: 'text',
                                    placeholder: '输入关键词...',
                                    value: recallQuery.value,
                                    onInput: (e) => recallQuery.value = e.target.value,
                                    onKeyup: (e) => { if (e.key === 'Enter') runRecall(); },
                                }),
                            ]),
                            h(Button, {
                                type: 'primary',
                                loading: recalling.value,
                                onClick: runRecall,
                            }, () => '测试'),
                        ]),
                        // 最近测试结果
                        recallResult.value
                            ? h('div', {
                                class: 'inline-flex items-center gap-2 w-fit',
                                style: 'padding: calc(var(--spacing) * 1.1) calc(var(--spacing) * 2); border-radius: 999px; background: hsl(var(--muted)); color: hsl(var(--accent-foreground)); font-size: 0.86rem; white-space: nowrap;',
                            }, [
                                h(Icon, { name: 'circle-check', size: '0.9rem' }),
                                `命中 ${recallResult.value.count} 条 · ${recallResult.value.time}`,
                            ])
                            : h('p', {
                                class: 'muted m-0',
                                style: 'font-size: 0.92rem;',
                            }, '输入关键词后点击"测试"以验证召回效果。'),
                        // 底部 CTA: 系统配置
                        h('div', {
                            class: 'flex items-center justify-between gap-2',
                            style: 'margin-top: auto; padding-top: calc(var(--spacing) * 2); border-top: 1px solid hsl(var(--border)); flex-wrap: wrap;',
                        }, [
                            h('div', { class: 'grid gap-1 min-w-0' }, [
                                h('span', { class: 'eyebrow' }, '后续步骤'),
                                h('p', {
                                    class: 'muted m-0',
                                    style: 'font-size: 0.92rem;',
                                }, '调整系统参数以优化记忆召回。'),
                            ]),
                            h(Button, { type: 'ghost', onClick: () => navigate('/config') }, () => [
                                '系统配置',
                                h(Icon, { name: 'arrow-right', size: '0.95rem' }),
                            ]),
                        ]),
                    ]),
                ]),
            ]);
        };
    },
});
