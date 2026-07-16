// components/memory/list-page.js - 记忆列表页（Golden Time 设计稿）
const { defineComponent, h, ref, reactive, computed, onMounted, watch } = window.Vue;
import { api } from '../../api.js';
import { Button, Loading, EmptyState, Icon, ProgressBar, Pagination, Modal, ConfirmModal, createConfirmHelper } from '../common.js';
import { appState, showToast } from '../../state.js';
import { navigate } from '../../router.js';
import { formatTime } from '../../utils.js';

// 分类标签映射（V6 event_type + 兼容旧 key）
const CATEGORY_LABELS = {
    conversation_message: '对话消息',
    conversation_context: '对话上下文',
    conversation: '对话',
    video_observation: '视频观察',
    video_metadata_observation: '视频元数据',
    bangumi_episode: '番剧剧集',
    bot_experience: '观看评价',
    bot_action: 'Bot 行为',
    action_intent: '行为意图',
    action_outcome: '行为结果',
    web_observation: '联网参考',
    reflection: '反思总结',
    observation: '观察',
    // 旧 V5 兼容
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

// 来源 source_type 中文（列表「来源」列）
const SOURCE_LABELS = {
    video: '视频观看',
    video_metadata: '视频元数据',
    video_experience: '观看评价',
    bot_action: 'Bot 行为',
    comment: '评论',
    comment_thread: '评论上下文',
    web_reference: '联网参考',
    dynamic: '动态',
    private_message: '私信',
    summary: '总结',
    weekly_summary: '周总结',
    asr: '语音转写',
    subtitle: '字幕',
    visual_description: '画面描述',
    behavior_log: '行为日志',
};

function categoryLabel(cat) {
    return CATEGORY_LABELS[cat] || cat || '未分类';
}

function sourceLabel(src) {
    return SOURCE_LABELS[src] || src || '-';
}

/** 列表「内容」列预览：视频类优先露出《标题》，再跟摘要。 */
function contentPreview(mem) {
    const title = (mem.title || '').trim();
    const summary = (mem.summary || '').trim();
    const content = (mem.content || '').trim();
    const cat = mem.category || mem.event_type || '';
    const videoLike = [
        'video_observation',
        'video_metadata_observation',
        'bangumi_episode',
    ].includes(cat) || ['video', 'video_metadata', 'bangumi'].includes(mem.source_type || mem.source || '');

    let text = content;
    if (videoLike && title) {
        const head = `《${title}》`;
        if (summary && !summary.includes(title)) {
            text = `${head} ${summary}`;
        } else if (content.startsWith(head) || content.includes(title)) {
            text = content;
        } else {
            text = summary ? `${head} ${summary}` : head;
        }
        if (mem.owner) {
            // 标题旁附带 UP，方便区分同主题视频
            text = text.replace(head, `${head}（UP：${mem.owner}）`);
        }
    }
    if (!text) text = '-';
    return text.length > 72 ? text.slice(0, 72) + '...' : text;
}

function contentTitleAttr(mem) {
    const title = (mem.title || '').trim();
    const summary = (mem.summary || '').trim();
    const content = (mem.content || '').trim();
    if (title && summary && !summary.includes(title)) {
        return `《${title}》\n${summary}`;
    }
    return content || summary || title || '';
}

const STATUS_LABELS = {
    ready: '索引就绪',
    pending: '处理中',
    processing: '执行中',
    retry: '等待重试',
    fts_only: '仅全文索引',
    enrichment_blocked: '增强待配置',
    degraded: '索引降级',
    blocked: '已阻塞',
    completed: '已完成',
    dead: '死信',
};

const JOB_TYPE_LABELS = {
    summarize_event: '事件摘要',
    extract_entities: '实体提取',
    link_associations: '关联构建',
    embed_event: '事件向量',
    embed_chunks: '分块向量',
};

function statusLabel(status) {
    return STATUS_LABELS[status] || status || '处理中';
}

function jobBadgeClass(status) {
    if (status === 'completed') return 'badge-success';
    if (status === 'dead') return 'badge-danger';
    if (status === 'blocked' || status === 'retry') return 'badge-warning';
    return 'badge-info';
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
        const selectedMemory = ref(null);
        const detailVisible = ref(false);
        const detailLoading = ref(false);
        const jobs = ref([]);
        const jobFilter = ref('');
        const jobsLoading = ref(false);
        const reindexing = ref(false);
        const retryingJobId = ref('');

        const { state: confirmState, showConfirm, handleConfirm } = createConfirmHelper();

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

        let loadSeq = 0;

        async function loadData() {
            accountId.value = appState.currentAccountId || appState.accounts[0]?.id;
            if (!accountId.value) {
                loading.value = false;
                return;
            }
            const seq = ++loadSeq;
            loading.value = true;
            try {
                const [statsData, listData, jobsData] = await Promise.all([
                    api.memory.stats(accountId.value).catch(() => ({ total: 0, categories: {}, health: {} })),
                    api.memory.list(accountId.value, {
                        page: page.value,
                        page_size: pageSize.value,
                        ...(filterCategory.value ? { category: filterCategory.value } : {}),
                        ...(filterStatus.value ? { status: filterStatus.value } : {}),
                    }).catch(() => ({ items: [], total: 0 })),
                    api.memory.jobs(accountId.value, {
                        limit: 50,
                        ...(jobFilter.value ? { status: jobFilter.value } : {}),
                    }).catch(() => ({ items: [] })),
                ]);
                if (seq !== loadSeq) return;
                stats.value = statsData || { total: 0, categories: {} };
                memories.value = listData?.items || [];
                total.value = listData?.total || 0;
                jobs.value = jobsData?.items || [];
            } catch (e) {
                if (seq !== loadSeq) return;
                showToast('加载失败: ' + e.message, 'error');
            } finally {
                if (seq === loadSeq) loading.value = false;
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

        function deleteMemory(id) {
            showConfirm({
                title: '确认删除',
                message: '确定删除此记忆？',
                confirmText: '删除',
                danger: true,
                action: async () => {
                    try {
                        await api.memory.delete(accountId.value, id);
                        showToast('已删除', 'success');
                        await loadData();
                    } catch (e) {
                        showToast('删除失败: ' + e.message, 'error');
                    }
                },
            });
        }

        async function openDetail(id) {
            detailVisible.value = true;
            detailLoading.value = true;
            selectedMemory.value = null;
            try {
                selectedMemory.value = await api.memory.detail(accountId.value, id);
            } catch (e) {
                detailVisible.value = false;
                showToast('读取详情失败: ' + e.message, 'error');
            } finally {
                detailLoading.value = false;
            }
        }

        async function loadJobs() {
            if (!accountId.value) return;
            jobsLoading.value = true;
            try {
                const data = await api.memory.jobs(accountId.value, {
                    limit: 50,
                    ...(jobFilter.value ? { status: jobFilter.value } : {}),
                });
                jobs.value = data?.items || [];
            } catch (e) {
                showToast('读取索引任务失败: ' + e.message, 'error');
            } finally {
                jobsLoading.value = false;
            }
        }

        function requestReindex(eventId = '') {
            showConfirm({
                title: eventId ? '重建此记忆索引' : '重建全部记忆索引',
                message: eventId
                    ? '将重新生成该记忆的派生索引与增强任务，原始来源不会改变。'
                    : '将重建全文索引，并重新排队全部派生索引与增强任务。原始来源不会改变。',
                confirmText: '开始重建',
                action: async () => {
                    reindexing.value = true;
                    try {
                        const report = await api.memory.reindex(accountId.value, eventId
                            ? { event_id: eventId }
                            : { clear_enrichment: true });
                        const count = report?.events_requeued ?? report?.events ?? 0;
                        showToast(`索引重建已排队，共 ${count} 个事件`, 'success');
                        await loadData();
                        if (eventId && detailVisible.value) await openDetail(eventId);
                    } catch (e) {
                        showToast('索引重建失败: ' + e.message, 'error');
                    } finally {
                        reindexing.value = false;
                    }
                },
            });
        }

        async function retryJob(jobId) {
            if (!jobId) return;
            retryingJobId.value = jobId;
            try {
                await api.memory.retryJob(accountId.value, jobId);
                showToast('死信任务已重新排队', 'success');
                await loadData();
            } catch (e) {
                showToast('任务重试失败: ' + e.message, 'error');
            } finally {
                retryingJobId.value = '';
            }
        }

        async function runRecall() {
            if (!recallQuery.value.trim() || !accountId.value) return;
            recalling.value = true;
            try {
                const data = await api.memory.recall(accountId.value, { query: recallQuery.value, scene: 'memory_list_quick_test' });
                const items = data?.events || data?.memories || [];
                recallResult.value = {
                    count: items.length,
                    mode: data?.trace?.mode || 'empty',
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
            // 首位 '' = 全部分类，保证能循环回到「全部」
            const opts = ['', ...cats];
            if (cats.length === 0) {
                if (filterCategory.value) {
                    filterCategory.value = '';
                    page.value = 1;
                    loadData();
                } else {
                    showToast('暂无分类数据', 'info');
                }
                return;
            }
            const idx = opts.indexOf(filterCategory.value);
            filterCategory.value = opts[(idx + 1) % opts.length];
            page.value = 1;
            loadData();
        }

        function cycleStatus() {
            const opts = ['', 'ready', 'pending', 'fts_only', 'enrichment_blocked', 'degraded'];
            const idx = opts.indexOf(filterStatus.value);
            filterStatus.value = opts[(idx + 1) % opts.length];
            page.value = 1;
            loadData();
        }

        function setCategory(cat) {
            filterCategory.value = filterCategory.value === cat ? '' : cat;
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
                    style: 'grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr));',
                }, [
                    // 左：记忆统计面板（accent 背景）
                    h('article', {
                        class: 'grid gap-3 memory-panel',
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
                                stats.value.health?.ok === false ? '健康检查异常' : '脑库健康',
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
                                filterStatus.value ? statusLabel(filterStatus.value) : '全部索引状态',
                                h(Icon, { name: 'chevron-down', size: '0.75rem' }),
                            ]),
                            h(Button, { type: 'ghost', onClick: () => { page.value = 1; loadData(); } }, () => [
                                h(Icon, { name: 'arrow-up', size: '0.9rem' }),
                                '刷新列表',
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
                        h('div', { class: 'memory-table-scroll' }, [
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
                                        h('button', {
                                            type: 'button',
                                            class: 'btn btn-ghost',
                                            style: 'justify-content: flex-start; min-width: 0; padding: 0; font-size: 0.96rem; color: hsl(var(--foreground)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border: 0; box-shadow: none;',
                                            title: contentTitleAttr(mem),
                                            onClick: () => openDetail(mem.id),
                                        }, contentPreview(mem)),
                                        // 来源
                                        h('span', {
                                            style: 'font-size: 0.92rem; color: hsl(var(--muted-foreground)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;',
                                            title: mem.source || mem.source_type || '',
                                        }, sourceLabel(mem.source || mem.source_type)),
                                        // 时间
                                        h('span', {
                                            style: 'font-size: 0.92rem; color: hsl(var(--muted-foreground)); white-space: nowrap;',
                                        }, formatTime(mem.created_at)),
                                        // 状态
                                        h('span', {
                                            class: 'inline-flex items-center whitespace-nowrap w-fit',
                                            style: `padding: calc(var(--spacing) * 0.4) calc(var(--spacing) * 1); border-radius: 999px; font-size: 0.78rem; ${mem.index_health?.healthy
                                                ? 'background: hsl(var(--accent) / 0.34); color: hsl(var(--accent-foreground));'
                                                : 'background: hsl(var(--muted)); color: hsl(var(--muted-foreground));'}`,
                                            title: `全文索引: ${mem.index_health?.fts || '-'} / 向量: ${mem.index_health?.embedding || '-'}`,
                                        }, statusLabel(mem.index_status || mem.status)),
                                        // 操作
                                        h('div', { class: 'flex items-center gap-1' }, [
                                            h('button', {
                                                type: 'button',
                                                'aria-label': '查看完整记忆详情',
                                                title: '查看详情',
                                                class: 'icon-btn',
                                                onClick: () => openDetail(mem.id),
                                            }, [h(Icon, { name: 'external-link', size: '1.05rem' })]),
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

                // ═══ Section 3: 持久索引任务 ═══
                h('section', {}, [
                    h('article', {
                        class: 'grid gap-3 memory-panel',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                    }, [
                        h('div', { class: 'flex items-end justify-between gap-3', style: 'flex-wrap: wrap;' }, [
                            h('div', { class: 'grid gap-1 min-w-0' }, [
                                h('span', { class: 'eyebrow' }, '持久任务'),
                                h('h2', { class: 'm-0', style: 'font-size: 1.35rem; line-height: 1.08;' }, '索引与增强队列'),
                            ]),
                            h('div', { class: 'flex items-center gap-2', style: 'flex-wrap: wrap;' }, [
                                h('select', {
                                    class: 'form-input',
                                    value: jobFilter.value,
                                    'aria-label': '筛选任务状态',
                                    style: 'width: auto; min-width: 9rem;',
                                    onChange: (event) => {
                                        jobFilter.value = event.target.value;
                                        loadJobs();
                                    },
                                }, [
                                    h('option', { value: '' }, '全部任务'),
                                    h('option', { value: 'pending' }, '等待执行'),
                                    h('option', { value: 'processing' }, '执行中'),
                                    h('option', { value: 'retry' }, '等待重试'),
                                    h('option', { value: 'blocked' }, '已阻塞'),
                                    h('option', { value: 'dead' }, '死信'),
                                    h('option', { value: 'completed' }, '已完成'),
                                ]),
                                h(Button, { type: 'ghost', size: 'sm', loading: jobsLoading.value, onClick: loadJobs }, () => '刷新'),
                                h(Button, { type: 'primary', size: 'sm', loading: reindexing.value, onClick: () => requestReindex() }, () => '重建全部索引'),
                            ]),
                        ]),
                        h('div', { class: 'flex gap-2', style: 'flex-wrap: wrap;' },
                            Object.entries(stats.value.jobs || {}).map(([status, count]) =>
                                h('span', { key: status, class: `badge ${jobBadgeClass(status)}` }, `${statusLabel(status)} ${count}`)
                            )
                        ),
                        jobsLoading.value && jobs.value.length === 0
                            ? h(Loading)
                            : jobs.value.length === 0
                                ? h(EmptyState, { icon: 'circle-check', title: '当前没有任务', desc: jobFilter.value ? '该状态下没有索引任务。' : '索引任务队列为空。' })
                                : h('div', { class: 'memory-table-scroll' }, [
                                    h('div', { class: 'grid gap-0', style: 'min-width: 860px;' }, [
                                        h('div', {
                                            class: 'grid items-center gap-2',
                                            style: 'grid-template-columns: minmax(10rem, .9fr) minmax(13rem, 1.25fr) 7rem 6rem 10rem 6rem; padding-bottom: calc(var(--spacing) * 2); border-bottom: 1px solid hsl(var(--border));',
                                        }, [
                                            h('span', { style: headerStyle }, '任务'),
                                            h('span', { style: headerStyle }, '事件'),
                                            h('span', { style: headerStyle }, '状态'),
                                            h('span', { style: headerStyle }, '尝试'),
                                            h('span', { style: headerStyle }, '更新时间'),
                                            h('span', { style: headerStyle }, '操作'),
                                        ]),
                                        ...jobs.value.map(job => h('div', {
                                            key: job.id,
                                            class: 'grid items-center gap-2',
                                            style: 'grid-template-columns: minmax(10rem, .9fr) minmax(13rem, 1.25fr) 7rem 6rem 10rem 6rem; padding: calc(var(--spacing) * 2.2) 0; border-bottom: 1px solid hsl(var(--border));',
                                        }, [
                                            h('div', { class: 'grid gap-1 min-w-0' }, [
                                                h('span', { style: 'font-size: .9rem;' }, JOB_TYPE_LABELS[job.job_type] || job.job_type || '-'),
                                                job.last_error
                                                    ? h('span', { class: 'muted memory-break', style: 'font-size: .74rem;', title: job.last_error }, job.last_error)
                                                    : null,
                                            ]),
                                            job.event_id
                                                ? h('button', {
                                                    type: 'button',
                                                    class: 'btn btn-ghost',
                                                    title: job.event_id,
                                                    style: 'min-width: 0; justify-content: flex-start; padding: 0; border: 0; box-shadow: none; overflow: hidden; text-overflow: ellipsis;',
                                                    onClick: () => openDetail(job.event_id),
                                                }, job.event_id)
                                                : h('span', { class: 'muted' }, '-'),
                                            h('span', { class: `badge ${jobBadgeClass(job.status)} w-fit` }, statusLabel(job.status)),
                                            h('code', `${job.attempts || 0}/${job.max_attempts || 8}`),
                                            h('span', { class: 'muted', style: 'font-size: .84rem; white-space: nowrap;' }, formatTime(job.updated_at)),
                                            job.status === 'dead'
                                                ? h(Button, {
                                                    type: 'ghost',
                                                    size: 'sm',
                                                    loading: retryingJobId.value === job.id,
                                                    onClick: () => retryJob(job.id),
                                                }, () => '重试')
                                                : h('span', { class: 'muted' }, '-'),
                                        ])),
                                    ]),
                                ]),
                    ]),
                ]),

                // ═══ Section 4: Split grid — 分类统计 + 召回测试 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr));',
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
                                : categoryStats.value.slice(0, 6).map(cat => h('button', {
                                    type: 'button',
                                    key: cat.key,
                                    class: 'grid gap-1',
                                    style: `text-align: left; border: 0; background: ${filterCategory.value === cat.key ? 'hsl(var(--accent) / 0.12)' : 'transparent'}; border-radius: calc(var(--radius) * 0.4); padding: calc(var(--spacing) * 1); cursor: pointer;`,
                                    title: `筛选：${cat.label}`,
                                    onClick: () => setCategory(cat.key),
                                }, [
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
                                class: 'inline-flex items-center gap-2 w-fit memory-break',
                                style: 'padding: calc(var(--spacing) * 1.1) calc(var(--spacing) * 2); border-radius: 999px; background: hsl(var(--muted)); color: hsl(var(--accent-foreground)); font-size: 0.86rem;',
                            }, [
                                h(Icon, { name: 'circle-check', size: '0.9rem' }),
                                `命中 ${recallResult.value.count} 条 · ${recallResult.value.mode} · ${recallResult.value.time}`,
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

                h(Modal, {
                    modelValue: detailVisible.value,
                    title: selectedMemory.value?.title
                        || selectedMemory.value?.summary
                        || '记忆详情',
                    width: 'min(920px, 94vw)',
                    'onUpdate:modelValue': (value) => detailVisible.value = value,
                }, {
                    default: () => detailLoading.value
                        ? h(Loading)
                        : selectedMemory.value
                            ? h('div', { class: 'grid gap-4 memory-detail-scroll' }, [
                                h('section', { class: 'grid gap-2' }, [
                                    h('div', { class: 'flex gap-2', style: 'flex-wrap: wrap;' }, [
                                        h('span', { class: 'badge badge-info' }, selectedMemory.value.source_type || '未知'),
                                        h('span', { class: `badge ${selectedMemory.value.index_health?.healthy ? 'badge-success' : 'badge-warning'}` }, statusLabel(selectedMemory.value.index_status)),
                                        h('span', { class: 'badge badge-info' }, `${selectedMemory.value.chunk_count || 0} 分块`),
                                        h('span', { class: 'badge badge-info' }, `${selectedMemory.value.entity_count || 0} 实体`),
                                        selectedMemory.value.bvid
                                            ? h('span', { class: 'badge badge-info' }, selectedMemory.value.bvid)
                                            : null,
                                    ]),
                                    // 视频类：标题单独一行，摘要再展示
                                    selectedMemory.value.title
                                        ? h('h3', {
                                            class: 'm-0',
                                            style: 'font-size: 1.1rem; line-height: 1.4; font-weight: 600;',
                                        }, `《${selectedMemory.value.title}》`)
                                        : null,
                                    (selectedMemory.value.owner || selectedMemory.value.bvid)
                                        ? h('p', {
                                            class: 'm-0 muted',
                                            style: 'font-size: 0.88rem;',
                                        }, [
                                            selectedMemory.value.owner
                                                ? `UP主：${selectedMemory.value.owner}`
                                                : '',
                                            selectedMemory.value.owner && selectedMemory.value.bvid
                                                ? ' · '
                                                : '',
                                            selectedMemory.value.bvid || '',
                                        ].join(''))
                                        : null,
                                    h('p', {
                                        class: 'm-0',
                                        style: 'line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere;',
                                    }, selectedMemory.value.summary || selectedMemory.value.content || '-'),
                                    h('div', { class: 'grid gap-1', style: 'grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); font-size: 0.84rem; color: hsl(var(--muted-foreground));' }, [
                                        h('span', `事件 ID: ${selectedMemory.value.id}`),
                                        h('span', `全文索引: ${selectedMemory.value.index_health?.fts || '-'}`),
                                        h('span', `向量: ${selectedMemory.value.index_health?.embedding || '-'}`),
                                        h('span', `召回次数: ${selectedMemory.value.recall_count || 0}`),
                                    ]),
                                    h('div', { class: 'flex gap-2', style: 'flex-wrap: wrap;' },
                                        Object.entries(selectedMemory.value.index_health?.jobs || {}).map(([jobType, jobStatus]) =>
                                            h('span', { key: jobType, class: `badge ${jobBadgeClass(jobStatus)}` }, `${JOB_TYPE_LABELS[jobType] || jobType}: ${statusLabel(jobStatus)}`)
                                        )
                                    ),
                                    h('div', { class: 'flex justify-end' }, [
                                        h(Button, {
                                            type: 'ghost',
                                            size: 'sm',
                                            loading: reindexing.value,
                                            onClick: () => requestReindex(selectedMemory.value.id),
                                        }, () => '重建此记忆索引'),
                                    ]),
                                ]),
                                h('section', { class: 'grid gap-2', style: 'padding-top: calc(var(--spacing) * 3); border-top: 1px solid hsl(var(--border));' }, [
                                    h('h3', { class: 'm-0', style: 'font-size: 1rem;' }, '完整脱敏来源'),
                                    ...(selectedMemory.value.sources || []).length
                                        ? selectedMemory.value.sources.map((source, index) => h('article', {
                                            key: source.id || index,
                                            class: 'grid gap-2',
                                            style: 'padding: calc(var(--spacing) * 3); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * .6); min-width: 0;',
                                        }, [
                                            h('div', { class: 'flex justify-between gap-2', style: 'font-size: .82rem; color: hsl(var(--muted-foreground)); flex-wrap: wrap;' }, [
                                                h('span', `${source.source_type || 'source'} · ${source.external_id || '无外部 ID'}`),
                                                h('span', `来源 ${index + 1}`),
                                            ]),
                                            h('pre', { class: 'memory-source-text' }, source.full_text || '-'),
                                            source.structured_data && Object.keys(source.structured_data).length
                                                ? h('details', { class: 'grid gap-1' }, [
                                                    h('summary', { style: 'cursor: pointer; font-size: .82rem; color: hsl(var(--muted-foreground));' }, '结构化数据'),
                                                    h('pre', { class: 'memory-source-json' }, JSON.stringify(source.structured_data, null, 2)),
                                                ])
                                                : null,
                                        ]))
                                        : [h('p', { class: 'muted m-0' }, '该事件没有可显示的来源。')],
                                ]),
                                h('section', { class: 'grid gap-2', style: 'padding-top: calc(var(--spacing) * 3); border-top: 1px solid hsl(var(--border));' }, [
                                    h('h3', { class: 'm-0', style: 'font-size: 1rem;' }, '证据分块'),
                                    ...(selectedMemory.value.chunks || []).length
                                        ? selectedMemory.value.chunks.map((chunk, index) => h('article', {
                                            key: chunk.id || index,
                                            class: 'grid gap-1',
                                            style: 'padding: calc(var(--spacing) * 3); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * .6); min-width: 0;',
                                        }, [
                                            h('span', { class: 'memory-break', style: 'font-size: .8rem; color: hsl(var(--muted-foreground));' }, `#${index + 1} · ${chunk.id || '-'} · ${chunk.char_count || 0} 字 · ${chunk.token_count || 0} tokens · overlap ${chunk.overlap_chars || 0}`),
                                            h('p', { class: 'm-0 memory-break', style: 'white-space: pre-wrap; line-height: 1.65;' }, chunk.text || '-'),
                                        ]))
                                        : [h('p', { class: 'muted m-0' }, '该事件尚无证据分块。')],
                                ]),
                                h('section', { class: 'grid gap-2', style: 'padding-top: calc(var(--spacing) * 3); border-top: 1px solid hsl(var(--border));' }, [
                                    h('h3', { class: 'm-0', style: 'font-size: 1rem;' }, '实体与关联'),
                                    h('div', { class: 'flex gap-2', style: 'flex-wrap: wrap;' }, (selectedMemory.value.entities || []).map((entity, index) =>
                                        h('span', { key: entity.entity_id || index, class: 'badge badge-info' }, `${entity.canonical_name || entity.surface_text} · ${entity.entity_type || 'topic'}`)
                                    )),
                                    (selectedMemory.value.links || []).length
                                        ? h('div', { class: 'grid gap-1' }, selectedMemory.value.links.map((link, index) => h('code', { key: link.id || index, style: 'font-size: .82rem; overflow-wrap: anywhere;' }, `${link.source_event_id} --${link.relation_type}--> ${link.target_event_id}`)))
                                        : h('p', { class: 'muted m-0' }, '暂无事件关系'),
                                ]),
                            ])
                            : h(EmptyState, { title: '详情不可用', desc: '该记忆可能已被删除。' }),
                }),

                // ═══ 确认对话框 ═══
                h(ConfirmModal, {
                    modelValue: confirmState.visible,
                    title: confirmState.title,
                    message: confirmState.message,
                    confirmText: confirmState.confirmText,
                    cancelText: confirmState.cancelText,
                    danger: confirmState.danger,
                    prompt: confirmState.prompt,
                    promptPlaceholder: confirmState.promptPlaceholder,
                    'onUpdate:modelValue': (v) => confirmState.visible = v,
                    onConfirm: handleConfirm,
                }),
            ]);
        };
    },
});
