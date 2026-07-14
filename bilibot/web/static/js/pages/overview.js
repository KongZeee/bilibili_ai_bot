// pages/overview.js - 总览页（Golden Time 设计稿）
const { defineComponent, h, ref, onMounted } = window.Vue;
import { api } from '../api.js';
import { Card, Button, Badge, Loading, EmptyState, Icon, KpiCard, ActionList } from '../components/common.js';
import { showToast } from '../state.js';
import { navigate } from '../router.js';
import { formatTime, auditSceneLabel, auditStatusLabel, auditStatusBadgeType } from '../utils.js';

export const OverviewPage = defineComponent({
    name: 'OverviewPage',
    setup() {
        const status = ref(null);
        const audits = ref([]);
        const auditsTotal = ref(0);
        const commentAuditsTotal = ref(0);
        const memoryTotal = ref(0);
        const loading = ref(true);

        async function loadData() {
            loading.value = true;
            try {
                const [statusData, auditsData, auditStats] = await Promise.all([
                    api.status(),
                    api.audits({ page: 1, page_size: 5 }).catch(() => ({ items: [] })),
                    api.get('/api/audit/stats').catch(() => null),
                ]);
                status.value = statusData;
                audits.value = auditsData?.items || (Array.isArray(auditsData) ? auditsData : []);
                // list 端点无 total；KPI 用 /api/audit/stats（扁平字段 + total）
                const published = Number(auditStats?.published || 0);
                const totalAll = Number(auditStats?.total || 0);
                auditsTotal.value = totalAll || audits.value.length || 0;
                commentAuditsTotal.value = published || totalAll || 0;

                try {
                    const accounts = await api.accounts.list();
                    const accList = Array.isArray(accounts) ? accounts : (accounts?.items || []);
                    if (accList.length > 0) {
                        const firstAcc = accList[0].id || accList[0].account_id;
                        if (firstAcc) {
                            const memStats = await api.memory.stats(firstAcc);
                            memoryTotal.value = memStats?.total || 0;
                        }
                    }
                } catch (_) { /* 无账号或记忆统计失败，保持 0 */ }
            } catch (e) {
                showToast('加载失败: ' + e.message, 'error');
            } finally {
                loading.value = false;
            }
        }

        onMounted(loadData);

        return () => loading.value
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                // ═══ Section 1: System status hero band (2-col) ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // Card A: B站连接状态
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'flex items-start justify-between gap-2' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '系统状态'),
                                h('h2', {
                                    class: 'm-0',
                                    style: 'font-size: 1.35rem; line-height: 1.1; text-wrap: balance; word-break: keep-all;',
                                }, 'B站连接'),
                            ]),
                            h('span', {
                                class: 'inline-flex items-center gap-1 whitespace-nowrap',
                                style: `padding: calc(var(--spacing) * 0.8) calc(var(--spacing) * 1.6); border-radius: 999px; background: hsl(${status.value?.bilibili?.authenticated ? 'var(--accent)' : 'var(--destructive)'} / 0.34); color: hsl(${status.value?.bilibili?.authenticated ? 'var(--accent-foreground)' : 'var(--destructive-foreground)'}); font-size: 0.82rem;`,
                            }, [
                                h(Icon, { name: status.value?.bilibili?.authenticated ? 'circle-check' : 'triangle-alert', size: '0.9rem' }),
                                status.value?.bilibili?.authenticated ? '正常' : '异常',
                            ]),
                        ]),
                        h('p', { class: 'muted m-0' },
                            status.value?.bilibili?.authenticated ? '已登录' : '未登录 · 请前往账号管理'),
                    ]),
                    // Card B: LLM 服务状态
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'flex items-start justify-between gap-2' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '系统状态'),
                                h('h2', {
                                    class: 'm-0',
                                    style: 'font-size: 1.35rem; line-height: 1.1; text-wrap: balance; word-break: keep-all;',
                                }, '对话模型'),
                            ]),
                            h('span', {
                                class: 'inline-flex items-center gap-1 whitespace-nowrap',
                                style: `padding: calc(var(--spacing) * 0.8) calc(var(--spacing) * 1.6); border-radius: 999px; background: hsl(${status.value?.llm?.connected ? 'var(--accent)' : 'var(--destructive)'} / 0.34); color: hsl(${status.value?.llm?.connected ? 'var(--accent-foreground)' : 'var(--destructive-foreground)'}); font-size: 0.82rem;`,
                            }, [
                                h(Icon, { name: status.value?.llm?.connected ? 'circle-check' : 'triangle-alert', size: '0.9rem' }),
                                status.value?.llm?.connected ? '正常' : '异常',
                            ]),
                        ]),
                        h('p', { class: 'muted m-0' },
                            (status.value?.llm?.model || '未配置') + (status.value?.llm?.connected ? ' · 服务可用' : ' · 不可用')),
                    ]),
                ]),

                // ═══ Section 2: KPI grid (3-col) ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: repeat(3, minmax(0, 1fr));',
                }, [
                    h(KpiCard, {
                        eyebrow: '指标',
                        iconName: 'message-circle-more',
                        value: commentAuditsTotal.value ? String(commentAuditsTotal.value) : '-',
                        valueLabel: '今日评论数',
                        label: '暂无趋势',
                    }),
                    h(KpiCard, {
                        eyebrow: '指标',
                        iconName: 'pen-line',
                        value: auditsTotal.value ? String(auditsTotal.value) : '-',
                        valueLabel: '生成审计数',
                        label: '暂无趋势',
                    }),
                    h(KpiCard, {
                        eyebrow: '指标',
                        iconName: 'folder-open',
                        value: memoryTotal.value ? String(memoryTotal.value) : '-',
                        valueLabel: '记忆条数',
                        label: '暂无趋势',
                    }),
                ]),

                // ═══ Section 3: Split grid (1.15fr : 0.85fr) ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1.15fr) minmax(18rem, 0.85fr);',
                }, [
                    // Left: 最近审计表格
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                    }, [
                        h('div', { class: 'flex items-start justify-between gap-2' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '审计队列'),
                                h('h2', {
                                    class: 'm-0',
                                    style: 'font-size: 1.35rem; line-height: 1.1; text-wrap: balance; word-break: keep-all;',
                                }, '最近审计'),
                            ]),
                            h('span', {
                                class: 'inline-flex items-center gap-1 whitespace-nowrap',
                                style: 'padding: calc(var(--spacing) * 0.8) calc(var(--spacing) * 1.6); border-radius: 999px; background: hsl(var(--muted)); color: hsl(var(--accent-foreground)); font-size: 0.78rem;',
                            }, `${audits.value.length} 条记录`),
                        ]),
                        // Table (CSS Grid)
                        audits.value.length === 0
                            ? h(EmptyState, { icon: 'folder', title: '暂无审计记录', desc: '最近没有生成审计数据' })
                            : h('div', { class: 'grid', style: 'gap: 0; min-width: 0;' }, [
                                // 表头
                                h('div', {
                                    class: 'grid items-center',
                                    style: 'grid-template-columns: 8.5rem 3.5rem 4.5rem minmax(0, 1fr) 5.5rem; column-gap: calc(var(--spacing) * 2); padding-bottom: calc(var(--spacing) * 2); border-bottom: 1px solid hsl(var(--border)); color: hsl(var(--muted-foreground)); font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.14em;',
                                }, [
                                    h('span', { class: 'whitespace-nowrap' }, '时间'),
                                    h('span', { class: 'whitespace-nowrap' }, '人格'),
                                    h('span', { class: 'whitespace-nowrap' }, '场景'),
                                    h('span', { class: 'whitespace-nowrap' }, '内容预览'),
                                    h('span', { class: 'whitespace-nowrap' }, '状态'),
                                ]),
                                // 数据行
                                ...audits.value.map(a => {
                                    const badge = auditStatusBadgeType(a.status, { published: a.published });
                                    const preview = (a.output || a.input_summary || '').replace(/\s+/g, ' ').trim();
                                    return h('div', {
                                        class: 'grid items-center',
                                        style: 'grid-template-columns: 8.5rem 3.5rem 4.5rem minmax(0, 1fr) 5.5rem; column-gap: calc(var(--spacing) * 2); padding: calc(var(--spacing) * 2.3) 0; border-top: 1px solid hsl(var(--border)); font-size: 0.95rem;',
                                    }, [
                                        h('span', {
                                            class: 'whitespace-nowrap',
                                            style: 'color: hsl(var(--muted-foreground)); font-variant-numeric: tabular-nums;',
                                        }, formatTime(a.created_at)),
                                        h('span', {
                                            class: 'truncate',
                                            title: a.persona_id || '',
                                        }, a.persona_id || '默认'),
                                        h('span', {
                                            class: 'truncate',
                                            title: a.scene || '',
                                        }, auditSceneLabel(a.scene)),
                                        h('span', {
                                            class: 'truncate',
                                            title: preview,
                                        }, preview ? (preview.length > 36 ? preview.slice(0, 36) + '…' : preview) : '（无内容）'),
                                        h('span', {
                                            class: `badge badge-${badge}`,
                                        }, a.status === 'published' || a.published
                                            ? '已发布'
                                            : auditStatusLabel(a.status, { published: a.published })),
                                    ]);
                                }),
                            ]),
                    ]),

                    // Right: 快捷操作
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '快捷操作'),
                            h('h2', {
                                class: 'm-0',
                                style: 'font-size: 1.35rem; line-height: 1.1; text-wrap: balance; word-break: keep-all;',
                            }, '常用入口'),
                        ]),
                        h(ActionList, {
                            items: [
                                { iconName: 'user', label: '添加账号', onClick: () => navigate('/accounts') },
                                { iconName: 'star', label: '创建人格', onClick: () => navigate('/personas') },
                                { iconName: 'circle-check', label: '测试召回', onClick: () => navigate('/memory/recall') },
                                { iconName: 'tag', label: '保存配置', onClick: () => navigate('/config') },
                            ],
                        }),
                        // CTA
                        h(Button, {
                            type: 'primary',
                            onClick: () => navigate('/accounts'),
                        }, () => ['管理账号']),
                    ]),
                ]),
            ]);
    },
});
