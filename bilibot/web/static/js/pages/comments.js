// pages/comments.js - 评论回复审计页（Golden Time 设计稿）
const { defineComponent, h, ref, onMounted } = window.Vue;
import { api } from '../api.js';
import { showToast } from '../state.js';
import { Button, Badge, FormInput, Loading, EmptyState, Icon, Pagination } from '../components/common.js';
import { formatTime, auditStatusLabel, auditStatusBadgeType } from '../utils.js';

function parseTarget(raw) {
    if (!raw) return {};
    if (typeof raw === 'object') return raw;
    try { return JSON.parse(raw); } catch { return {}; }
}

function isProactiveComment(r) {
    return r.scene === 'proactive_comment' || parseTarget(r.target).kind === 'proactive_comment';
}

function replyStatusLabel(r) {
    const target = parseTarget(r.target);
    if (r.status === 'published' || r.published) {
        return isProactiveComment(r) ? '已发布' : '已回复';
    }
    if (target.failure_reason || r.status === 'failed') return '失败';
    return auditStatusLabel(r.status, { published: r.published });
}

function replyBadgeType(r) {
    const target = parseTarget(r.target);
    if (r.status === 'published' || r.published) return 'success';
    if (target.failure_reason || r.status === 'failed') return 'danger';
    return auditStatusBadgeType(r.status, { published: r.published });
}

export const CommentsPage = defineComponent({
    name: 'CommentsPage',
    setup() {
        const replies = ref([]);
        const loading = ref(false);
        const filter = ref('all');
        const keyword = ref('');
        const page = ref(1);
        const pageSize = 20;
        const total = ref(0);

        const filters = [
            { value: 'all', label: '全部' },
            { value: 'pending', label: '待处理' },
            { value: 'replied', label: '已回复' },
            { value: 'failed', label: '失败' },
        ];

        async function load() {
            loading.value = true;
            try {
                const data = await api.replies({
                    page: page.value,
                    page_size: pageSize,
                    status: filter.value === 'all' ? undefined : filter.value,
                    keyword: keyword.value || undefined,
                });
                replies.value = data.items || [];
                total.value = data.total || 0;
            } catch (e) {
                showToast('加载失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        function setFilter(v) {
            filter.value = v;
            page.value = 1;
            load();
        }

        onMounted(load);

        const tableGrid = 'minmax(8rem, 0.9fr) minmax(7rem, 0.7fr) minmax(8rem, 0.8fr) minmax(0, 1.6fr) 7rem';

        return () => loading.value && replies.value.length === 0
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                // ═══ hero-band：左侧统计 + 右侧搜索 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左侧：hero-panel 统计面板
                    h('div', { class: 'hero-panel' }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('span', { class: 'eyebrow' }, '评论审计'),
                            h(Badge, { type: 'success' }, () => '已回复'),
                        ]),
                        h('h2', {
                            style: 'margin:0; font-size:1.65rem; line-height:1.1; text-wrap:balance; word-break:keep-all;',
                        }, '回复记录'),
                        h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                            h('span', {
                                style: 'font-size:2.4rem; font-weight:500; line-height:1; font-variant-numeric:tabular-nums;',
                            }, String(total.value || 0)),
                            h('span', { class: 'muted m-0', style: 'font-size:0.9rem;' }, '条回复记录'),
                        ]),
                        h('p', { class: 'muted m-0' }, '查看评论回复与主动看视频后发表评论的审计轨迹'),
                    ]),
                    // 右侧：搜索 Card
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '检索'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '快速筛选'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-2' }, [
                            h(FormInput, {
                                modelValue: keyword.value,
                                'onUpdate:modelValue': (v) => keyword.value = v,
                                placeholder: '搜索评论或回复内容…',
                                id: 'comments-search',
                            }),
                            h('div', { class: 'flex items-center gap-2 flex-wrap' }, [
                                h(Button, {
                                    type: 'primary',
                                    onClick: () => { page.value = 1; load(); },
                                }, () => '查询'),
                                h(Button, {
                                    type: 'ghost',
                                    onClick: () => {
                                        keyword.value = '';
                                        filter.value = 'all';
                                        page.value = 1;
                                        load();
                                    },
                                }, () => '重置'),
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
                            h('span', { class: 'eyebrow' }, '审计队列'),
                            h('h2', {
                                style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;',
                            }, '评论记录'),
                        ]),
                        // 状态筛选按钮组
                        h('div', { class: 'flex items-center gap-1 flex-wrap' },
                            filters.map(f => h('button', {
                                key: f.value,
                                class: ['btn', 'btn-sm', filter.value === f.value ? 'primary' : 'ghost'].join(' '),
                                onClick: () => setFilter(f.value),
                            }, f.label)),
                        ),
                    ]),
                    replies.value.length === 0
                        ? h(EmptyState, { icon: 'folder', title: '暂无评论记录', desc: '当前筛选条件下没有回复数据' })
                        : h('div', { class: 'grid', style: 'gap:0; min-width:0;' }, [
                            // 表头
                            h('div', {
                                class: 'grid items-center',
                                style: `grid-template-columns: ${tableGrid}; column-gap: calc(var(--spacing) * 2); padding-bottom: calc(var(--spacing) * 2); border-bottom: 1px solid hsl(var(--border)); color: hsl(var(--muted-foreground)); font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.14em;`,
                            }, [
                                h('span', { class: 'whitespace-nowrap' }, '时间'),
                                h('span', { class: 'whitespace-nowrap' }, '人格'),
                                h('span', { class: 'whitespace-nowrap' }, '视频'),
                                h('span', { class: 'whitespace-nowrap' }, '内容'),
                                h('span', { class: 'whitespace-nowrap' }, '状态'),
                            ]),
                            // 数据行
                            ...replies.value.map(r => {
                                const target = parseTarget(r.target);
                                const badge = replyBadgeType(r);
                                const proactive = isProactiveComment(r);
                                return h('div', {
                                    key: r.id,
                                    class: 'grid items-center',
                                    style: `grid-template-columns: ${tableGrid}; column-gap: calc(var(--spacing) * 2); padding: calc(var(--spacing) * 2.3) 0; border-top: 1px solid hsl(var(--border)); font-size: 0.95rem;`,
                                }, [
                                    h('span', {
                                        class: 'whitespace-nowrap',
                                        style: 'color: hsl(var(--muted-foreground)); font-variant-numeric: tabular-nums; font-size:0.85rem;',
                                    }, formatTime(r.created_at)),
                                    h('div', { class: 'grid gap-1', style: 'min-width:0;' }, [
                                        h('span', {
                                            class: 'truncate',
                                            title: r.persona_id || '',
                                        }, r.persona_id || '默认人格'),
                                        h('span', {
                                            class: 'badge badge-info',
                                            style: 'width:fit-content; font-size:0.72rem;',
                                        }, proactive ? '主动评论' : '回复评论'),
                                    ]),
                                    h('span', {
                                        class: 'truncate',
                                        title: target.video_title || target.bvid || target.oid || '',
                                    }, target.video_title || target.bvid || (target.oid ? `视频 ${target.oid}` : '-')),
                                    h('div', { class: 'grid gap-1', style: 'min-width:0;' }, [
                                        h('div', {
                                            class: 'truncate',
                                            style: 'color: hsl(var(--muted-foreground)); font-size:0.82rem;',
                                            title: r.input_summary || r.prompt_preview || '',
                                        }, r.input_summary || r.prompt_preview || (proactive ? '（主动评论）' : '（无原评论）')),
                                        h('div', {
                                            class: 'truncate',
                                            title: r.output || '',
                                        }, r.output || '（无内容）'),
                                    ]),
                                    h('span', {
                                        class: `badge badge-${badge}`,
                                        title: target.failure_reason || r.status || '',
                                    }, replyStatusLabel(r)),
                                ]);
                            }),
                        ]),
                    // 底部分页
                    total.value > pageSize && h('div', { class: 'flex justify-end' }, [
                        h(Pagination, {
                            page: page.value,
                            pageSize,
                            total: total.value,
                            'onUpdate:page': (p) => { page.value = p; load(); },
                        }),
                    ]),
                ]),
            ]);
    },
});
