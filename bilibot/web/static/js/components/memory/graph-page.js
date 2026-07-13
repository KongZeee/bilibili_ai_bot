// components/memory/graph-page.js - 2D 记忆图谱页（Golden Time 设计稿）
const { defineComponent, h, ref, reactive, computed, onMounted, watch } = window.Vue;
import { api } from '../../api.js';
import { Card, Button, Badge, Loading, EmptyState, Icon, HeroPanel } from '../common.js';
import { appState, showToast } from '../../state.js';
import { formatTime } from '../../utils.js';

// 节点类型 → chart 色号映射
const TYPE_COLOR_INDEX = { summary: 1, person: 5, topic: 3 };
const TYPE_LABELS = { summary: '记忆节点', person: '用户节点', topic: '分类节点' };

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

// 节点位置计算：force / tree / radial
function computeLayout(nodes, edges, layoutType) {
    if (nodes.length === 0) return nodes;
    const cx = 500, cy = 250;

    if (layoutType === 'radial') {
        const groups = {};
        nodes.forEach(n => {
            const t = n.type || 'summary';
            if (!groups[t]) groups[t] = [];
            groups[t].push(n);
        });
        const types = Object.keys(groups);
        const sectorAngle = (2 * Math.PI) / Math.max(1, types.length);
        types.forEach((type, ti) => {
            const groupNodes = groups[type];
            const baseAngle = ti * sectorAngle;
            groupNodes.forEach((n, i) => {
                const angle = baseAngle + (i / Math.max(1, groupNodes.length)) * sectorAngle;
                const radius = Math.min(200, 110 + (n.degree || 1) * 12);
                n.x = cx + Math.cos(angle) * radius;
                n.y = cy + Math.sin(angle) * radius;
            });
        });
    } else if (layoutType === 'tree') {
        const sorted = [...nodes].sort((a, b) => (b.degree || 0) - (a.degree || 0));
        const hubCount = Math.min(Math.ceil(nodes.length / 4), 5);
        sorted.forEach((n, i) => {
            if (i < hubCount) {
                n.x = 200 + (hubCount > 1 ? (i / (hubCount - 1)) * 600 : 300);
                n.y = cy;
            } else {
                const idx = i - hubCount;
                const rest = nodes.length - hubCount;
                const cols = Math.max(1, Math.ceil(Math.sqrt(rest)));
                const col = idx % cols;
                const row = Math.floor(idx / cols);
                n.x = 100 + (cols > 1 ? (col / (cols - 1)) * 800 : 400);
                n.y = 90 + row * 110;
            }
        });
    } else {
        // force: 圆形分布 + 度数影响半径
        nodes.forEach((n, i) => {
            const angle = (i / nodes.length) * 2 * Math.PI;
            const radius = Math.min(200, 120 + (n.degree || 1) * 10);
            n.x = cx + Math.cos(angle) * radius;
            n.y = cy + Math.sin(angle) * radius;
        });
    }
    return nodes;
}

function nodeRadius(node) {
    const deg = node.degree || 1;
    return Math.max(10, Math.min(30, 10 + deg * 2));
}

export const MemoryGraphPage = defineComponent({
    name: 'MemoryGraphPage',
    setup() {
        const accountId = ref(null);
        const graphData = ref({ nodes: [], edges: [], memories: [], summary: {} });
        const totalCounts = ref({ nodes: 0, edges: 0, memories: 0 });
        const loading = ref(true);
        const selectedNode = ref(null);
        const hoveredNode = ref(null);
        const filterCategory = ref('');
        const searchQuery = ref('');
        const layout = ref('force'); // force / tree / radial

        // ── 过滤后的节点/边 ──
        const filteredNodes = computed(() => {
            let nodes = graphData.value.nodes || [];
            if (filterCategory.value) {
                nodes = nodes.filter(n => n.type === filterCategory.value);
            }
            if (searchQuery.value.trim()) {
                const q = searchQuery.value.trim().toLowerCase();
                nodes = nodes.filter(n => (n.label || '').toLowerCase().includes(q));
            }
            // 节点过多时按度数截断，保持 SVG 可读
            if (nodes.length > 60) {
                nodes = [...nodes].sort((a, b) => (b.degree || 0) - (a.degree || 0)).slice(0, 60);
            }
            return nodes;
        });

        const filteredNodeIds = computed(() => new Set(filteredNodes.value.map(n => n.id)));

        const filteredEdges = computed(() => {
            const ids = filteredNodeIds.value;
            return (graphData.value.edges || []).filter(e =>
                ids.has(e.source) && ids.has(e.target)
            );
        });

        // ── 带坐标的节点（不修改原数据） ──
        const positionedNodes = computed(() => {
            const nodes = filteredNodes.value.map(n => ({ ...n }));
            computeLayout(nodes, filteredEdges.value, layout.value);
            return nodes;
        });

        const nodeMap = computed(() => new Map(positionedNodes.value.map(n => [n.id, n])));

        const positionedEdges = computed(() => {
            const map = nodeMap.value;
            return filteredEdges.value
                .filter(e => map.has(e.source) && map.has(e.target))
                .map(e => {
                    const sn = map.get(e.source);
                    const tn = map.get(e.target);
                    return {
                        ...e,
                        sx: sn.x, sy: sn.y,
                        tx: tn.x, ty: tn.y,
                        strong: (sn.degree > 5 || tn.degree > 5),
                    };
                });
        });

        // ── 统计 ──
        const nodeCount = computed(() => totalCounts.value.nodes || (graphData.value.nodes || []).length);
        const edgeCount = computed(() => totalCounts.value.edges || (graphData.value.edges || []).length);
        const memoryCount = computed(() => totalCounts.value.memories || (graphData.value.memories || []).length);

        const categoryStats = computed(() => {
            const breakdown = graphData.value.summary?.node_type_breakdown || {};
            const total = Object.values(breakdown).reduce((a, b) => a + (b || 0), 0) || nodeCount.value || 1;
            return Object.entries(breakdown)
                .map(([key, count]) => ({
                    key,
                    label: TYPE_LABELS[key] || key,
                    count: count || 0,
                    percent: Math.round(((count || 0) / total) * 100),
                    colorIndex: TYPE_COLOR_INDEX[key] || 2,
                }))
                .sort((a, b) => b.count - a.count);
        });

        const avgDegree = computed(() => {
            const n = nodeCount.value;
            return n > 0 ? ((edgeCount.value * 2) / n).toFixed(1) : '0';
        });

        const maxDegree = computed(() => {
            const nodes = graphData.value.nodes || [];
            if (nodes.length === 0) return 0;
            return Math.max(...nodes.map(n => n.degree || 0));
        });

        // ── 选中节点的关联节点 ──
        const connectedNodes = computed(() => {
            if (!selectedNode.value) return [];
            const sid = selectedNode.value.id;
            const allNodes = graphData.value.nodes || [];
            const result = [];
            const seen = new Set();
            for (const e of (graphData.value.edges || [])) {
                if (e.source === sid || e.target === sid) {
                    const otherId = e.source === sid ? e.target : e.source;
                    if (!seen.has(otherId)) {
                        seen.add(otherId);
                        const node = allNodes.find(n => n.id === otherId);
                        if (node) result.push(node);
                    }
                }
            }
            return result;
        });

        // ── 选中节点对应的记忆详情 ──
        const selectedMemory = computed(() => {
            if (!selectedNode.value) return null;
            if (selectedNode.value.type !== 'summary') return null;
            const mid = selectedNode.value.id;
            return (graphData.value.memories || []).find(m => m.memory_id === mid) || null;
        });

        // ── 数据加载 ──
        async function loadData() {
            accountId.value = appState.currentAccountId || appState.accounts[0]?.id;
            if (!accountId.value) {
                loading.value = false;
                return;
            }
            loading.value = true;
            selectedNode.value = null;
            hoveredNode.value = null;
            try {
                const data = await api.memory.graph(accountId.value);
                const snap = data.snapshot || { nodes: [], edges: [], memories: [] };
                graphData.value = {
                    nodes: snap.nodes || [],
                    edges: snap.edges || [],
                    memories: snap.memories || [],
                    summary: data.summary || {},
                };
                totalCounts.value = {
                    nodes: data.graph_nodes || (snap.nodes || []).length,
                    edges: data.graph_edges || (snap.edges || []).length,
                    memories: data.total_memories || (snap.memories || []).length,
                };
            } catch (e) {
                showToast('加载图谱失败: ' + e.message, 'error');
                graphData.value = { nodes: [], edges: [], memories: [], summary: {} };
                totalCounts.value = { nodes: 0, edges: 0, memories: 0 };
            } finally {
                loading.value = false;
            }
        }

        async function searchGraph() {
            if (!accountId.value) return;
            if (!searchQuery.value.trim()) {
                await loadData();
                return;
            }
            loading.value = true;
            selectedNode.value = null;
            try {
                const data = await api.memory.graphQuery(accountId.value, { keyword: searchQuery.value.trim() });
                const snap = data.snapshot || { nodes: [], edges: [], memories: [] };
                graphData.value = {
                    nodes: snap.nodes || [],
                    edges: snap.edges || [],
                    memories: snap.memories || [],
                    summary: data.summary || {},
                };
                totalCounts.value = {
                    nodes: data.graph_nodes || (snap.nodes || []).length,
                    edges: data.graph_edges || (snap.edges || []).length,
                    memories: data.total_memories || (snap.memories || []).length,
                };
            } catch (e) {
                showToast('搜索失败: ' + e.message, 'error');
            } finally {
                loading.value = false;
            }
        }

        function selectNode(node) {
            selectedNode.value = node;
        }

        function filterByCategory(cat) {
            filterCategory.value = filterCategory.value === cat ? '' : cat;
        }

        function clearFilters() {
            filterCategory.value = '';
            searchQuery.value = '';
            loadData();
        }

        function setLayout(l) {
            layout.value = l;
        }

        const layoutOptions = [
            { key: 'force', label: '力导向' },
            { key: 'tree', label: '树形' },
            { key: 'radial', label: '环形' },
        ];

        onMounted(loadData);
        watch(() => appState.currentAccountId, (newId) => {
            if (newId) loadData();
        });

        // ── 边是否高亮（连接到悬停/选中节点） ──
        function isEdgeActive(edge) {
            const hid = hoveredNode.value;
            const sid = selectedNode.value?.id;
            return (hid && (edge.source === hid || edge.target === hid))
                || (sid && (edge.source === sid || edge.target === sid));
        }

        function isNodeActive(node) {
            return hoveredNode.value === node.id || selectedNode.value?.id === node.id;
        }

        return () => {
            // 无账号
            if (!loading.value && !accountId.value) {
                return h('div', { class: 'view-frame' }, [
                    h(EmptyState, {
                        icon: 'folder',
                        title: '暂无账号',
                        desc: '请先在账号管理中添加 B站 账号后查看记忆图谱。',
                    }),
                ]);
            }

            if (loading.value && (graphData.value.nodes || []).length === 0) {
                return h(Loading);
            }

            const hasGraph = (positionedNodes.value.length > 0);
            const heroTitle = `${nodeCount.value.toLocaleString()} 条记忆 · ${categoryStats.value.length} 个分类 · ${edgeCount.value.toLocaleString()} 条关联`;

            return h('div', { class: 'view-frame' }, [
                // ═══════ Section 1: hero-band — 统计面板 + 图谱控制 ═══════
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左：统计面板（hero-panel accent 背景）
                    h(HeroPanel, {
                        eyebrow: '记忆图谱',
                        title: heroTitle,
                        badge: '已就绪',
                        badgeType: 'success',
                    }, () => h('div', {
                        class: 'flex items-baseline gap-2 flex-wrap',
                    }, [
                        h('span', {
                            style: 'font-size: 2.4rem; line-height: 1; font-weight: 700; color: hsl(var(--accent-foreground));',
                        }, nodeCount.value.toLocaleString()),
                        h('span', {
                            class: 'muted',
                            style: 'font-size: 0.92rem;',
                        }, `条记忆节点 · ${edgeCount.value.toLocaleString()} 条关联`),
                    ])),

                    // 右：图谱控制面板
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '图谱控制'),
                            h('h2', {
                                class: 'm-0',
                                style: 'font-size: 1.35rem; line-height: 1.08;',
                            }, '视图设置'),
                        ]),
                        // 分类筛选
                        h('div', { class: 'flex items-center gap-2', style: 'flex-wrap: wrap;' }, [
                            h(Button, {
                                type: 'ghost',
                                onClick: () => filterCategory.value = '',
                            }, () => [
                                h(Icon, { name: 'tag', size: '0.9rem' }),
                                filterCategory.value ? (TYPE_LABELS[filterCategory.value] || filterCategory.value) : '全部分类',
                                h(Icon, { name: 'chevron-down', size: '0.75rem' }),
                            ]),
                        ]),
                        // 布局切换
                        h('div', {
                            class: 'inline-flex',
                            style: 'border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.76); overflow: hidden;',
                        }, layoutOptions.map(opt => h('button', {
                            key: opt.key,
                            type: 'button',
                            class: 'btn',
                            style: `min-height: 2.5rem; padding: calc(var(--spacing) * 1.5) calc(var(--spacing) * 3); font-size: 0.88rem; border: 0; border-radius: 0; box-shadow: none; ${
                                layout.value === opt.key
                                    ? 'background: hsl(var(--primary)); color: hsl(var(--primary-foreground));'
                                    : 'background: transparent; color: hsl(var(--accent-foreground));'
                            }${opt.key !== 'force' ? ' border-left: 1px solid hsl(var(--border));' : ''}`,
                            onClick: () => setLayout(opt.key),
                        }, opt.label))),
                        // 搜索框
                        h('label', {
                            class: 'field-wrap',
                            style: 'width: 100%; min-width: 0;',
                            'aria-label': '搜索节点',
                        }, [
                            h(Icon, { name: 'funnel', size: '1.05rem' }),
                            h('input', {
                                class: 'field',
                                type: 'text',
                                placeholder: '搜索节点名称...',
                                value: searchQuery.value,
                                onInput: (e) => searchQuery.value = e.target.value,
                                onKeyup: (e) => { if (e.key === 'Enter') searchGraph(); },
                            }),
                        ]),
                    ]),
                ]),

                // ═══════ Section 2: SVG 画布 ═══════
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
                                h('span', { class: 'eyebrow' }, '关系图谱'),
                                h('h2', {
                                    class: 'm-0',
                                    style: 'font-size: 1.35rem; line-height: 1.08;',
                                }, '节点与连接'),
                            ]),
                            h('div', { class: 'flex items-center gap-2', style: 'flex-wrap: wrap;' }, [
                                h('span', {
                                    style: 'font-size: 0.88rem; color: hsl(var(--muted-foreground));',
                                }, `显示 ${positionedNodes.value.length} / ${nodeCount.value} 节点`),
                                ...(filterCategory.value || searchQuery.value
                                    ? [h(Button, { size: 'sm', type: 'ghost', onClick: clearFilters }, () => '清除筛选')]
                                    : []),
                            ]),
                        ]),

                        // SVG 画布 / 空状态
                        hasGraph
                            ? h('div', {
                                style: 'position: relative; border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); overflow: hidden;',
                            }, [
                                h('svg', {
                                    viewBox: '0 0 1000 500',
                                    style: 'width:100%; height:65vh; display:block; background: hsl(var(--card));',
                                    role: 'img',
                                    'aria-label': '记忆关系图谱',
                                }, [
                                    // dotGrid pattern
                                    h('defs', [
                                        h('pattern', {
                                            id: 'dotGrid',
                                            width: '20',
                                            height: '20',
                                            patternUnits: 'userSpaceOnUse',
                                        }, [
                                            h('circle', { cx: 10, cy: 10, r: 1, fill: 'hsl(var(--border))' }),
                                        ]),
                                    ]),
                                    h('rect', { width: 1000, height: 500, fill: 'url(#dotGrid)' }),

                                    // 边
                                    ...positionedEdges.value.map(e => h('line', {
                                        key: `e-${e.id}`,
                                        x1: e.sx, y1: e.sy,
                                        x2: e.tx, y2: e.ty,
                                        stroke: isEdgeActive(e)
                                            ? 'hsl(var(--accent) / 0.9)'
                                            : (e.strong ? 'hsl(var(--accent) / 0.8)' : 'hsl(var(--border))'),
                                        'stroke-width': isEdgeActive(e) ? 2.5 : (e.strong ? 2 : 1),
                                        style: 'transition: stroke 0.18s ease, stroke-width 0.18s ease;',
                                    })),

                                    // 节点
                                    ...positionedNodes.value.map(n => {
                                        const r = nodeRadius(n);
                                        const colorIdx = TYPE_COLOR_INDEX[n.type] || 2;
                                        const active = isNodeActive(n);
                                        const isHub = (n.degree || 0) > 5;
                                        const label = (n.label || String(n.id)).slice(0, 8);
                                        return h('g', {
                                            key: `n-${n.id}`,
                                            onClick: () => selectNode(n),
                                            onMouseenter: () => { hoveredNode.value = n.id; },
                                            onMouseleave: () => { hoveredNode.value = null; },
                                            style: 'cursor: pointer;',
                                        }, [
                                            // hub 节点 halo 动画
                                            isHub ? h('circle', {
                                                cx: n.x, cy: n.y, r: r + 8,
                                                fill: 'none',
                                                stroke: `hsl(var(--chart-${colorIdx}))`,
                                                'stroke-width': 1,
                                                opacity: 0.4,
                                            }, [
                                                h('animate', {
                                                    attributeName: 'r',
                                                    values: `${r + 6};${r + 12};${r + 6}`,
                                                    dur: '2s',
                                                    repeatCount: 'indefinite',
                                                }),
                                                h('animate', {
                                                    attributeName: 'opacity',
                                                    values: '0.3;0.6;0.3',
                                                    dur: '2s',
                                                    repeatCount: 'indefinite',
                                                }),
                                            ]) : null,
                                            // 节点圆
                                            h('circle', {
                                                cx: n.x, cy: n.y,
                                                r: active ? r + 3 : r,
                                                fill: `hsl(var(--chart-${colorIdx}))`,
                                                stroke: active ? 'hsl(var(--accent))' : 'hsl(var(--card))',
                                                'stroke-width': active ? 3 : 2,
                                                style: 'transition: r 0.18s ease, stroke 0.18s ease, stroke-width 0.18s ease;',
                                            }),
                                            // 标签
                                            h('text', {
                                                x: n.x,
                                                y: n.y + r + 14,
                                                'text-anchor': 'middle',
                                                fill: 'hsl(var(--foreground))',
                                                'font-size': 11,
                                                style: 'pointer-events: none; user-select: none;',
                                            }, label),
                                        ]);
                                    }),
                                ]),
                                h('span', {
                                    style: 'position: absolute; bottom: calc(var(--spacing) * 2); right: calc(var(--spacing) * 3); color: hsl(var(--muted-foreground)); font-size: 0.78rem; pointer-events: none;',
                                }, '悬停高亮 · 点击查看详情'),
                            ])
                            : h('div', {
                                class: 'grid',
                                style: 'padding: calc(var(--spacing) * 6) 0; justify-items: center; border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82);',
                            }, [h(EmptyState, { icon: 'folder', title: '暂无图谱数据', desc: '记忆库为空，或当前筛选条件下无匹配节点。' })]),
                    ]),
                ]),

                // ═══════ Section 3: split grid — 分类图例/统计 + 选中节点详情 ═══════
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1.15fr) minmax(18rem, 0.85fr);',
                }, [
                    // 左：分类图例 + 图谱统计
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '图谱分析'),
                            h('h2', {
                                class: 'm-0',
                                style: 'font-size: 1.35rem; line-height: 1.08;',
                            }, '统计信息'),
                        ]),
                        // 分类图例
                        h('div', { class: 'grid gap-0' },
                            (categoryStats.value.length === 0
                                ? [h('p', { class: 'muted m-0', style: 'font-size: 0.92rem;' }, '暂无分类数据')]
                                : categoryStats.value.map(cat => h('div', {
                                    key: cat.key,
                                    class: 'flex items-center justify-between gap-2',
                                    style: `padding: calc(var(--spacing) * 2.2) calc(var(--spacing) * 1.5); border-bottom: 1px solid hsl(var(--border)); cursor: pointer; border-radius: calc(var(--radius) * 0.3);${filterCategory.value === cat.key ? ' background: hsl(var(--accent) / 0.12);' : ''}`,
                                    onClick: () => filterByCategory(cat.key),
                                }, [
                                    h('div', { class: 'flex items-center gap-2' }, [
                                        h('span', {
                                            style: `width: 0.85rem; height: 0.85rem; border-radius: 999px; background: hsl(var(--chart-${cat.colorIndex})); flex: 0 0 auto;`,
                                        }),
                                        h('span', { style: 'font-size: 0.98rem;' }, cat.label),
                                    ]),
                                    h('div', { class: 'flex items-baseline gap-1' }, [
                                        h('span', { style: 'font-size: 1.1rem; font-weight: 600;' }, String(cat.count)),
                                        h('span', { class: 'muted', style: 'font-size: 0.85rem;' }, '节点'),
                                        h('span', { style: 'color: hsl(var(--muted-foreground)); font-size: 0.85rem;' }, '·'),
                                        h('span', {
                                            style: 'color: hsl(var(--accent-foreground)); font-size: 0.88rem; font-weight: 500;',
                                        }, `${cat.percent}%`),
                                    ]),
                                ]))
                            ),
                        ),
                        // 图谱统计
                        h('div', {
                            class: 'grid gap-2',
                            style: 'padding: calc(var(--spacing) * 3); background: hsl(var(--muted) / 0.3); border-radius: calc(var(--radius) * 0.76); border: 1px solid hsl(var(--border));',
                        }, [
                            h('span', { class: 'eyebrow' }, '图谱统计'),
                            h('div', {
                                class: 'grid gap-2',
                                style: 'grid-template-columns: 1fr 1fr;',
                            }, [
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', { class: 'muted', style: 'font-size: 0.82rem;' }, '总节点数'),
                                    h('span', { style: 'font-size: 1.4rem; font-weight: 600;' }, String(nodeCount.value)),
                                ]),
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', { class: 'muted', style: 'font-size: 0.82rem;' }, '总边数'),
                                    h('span', { style: 'font-size: 1.4rem; font-weight: 600;' }, String(edgeCount.value)),
                                ]),
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', { class: 'muted', style: 'font-size: 0.82rem;' }, '平均度数'),
                                    h('span', { style: 'font-size: 1.4rem; font-weight: 600;' }, avgDegree.value),
                                ]),
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', { class: 'muted', style: 'font-size: 0.82rem;' }, '最大度数'),
                                    h('span', { style: 'font-size: 1.4rem; font-weight: 600;' }, String(maxDegree.value)),
                                ]),
                            ]),
                        ]),
                    ]),

                    // 右：选中节点详情
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '节点详情'),
                            h('h2', {
                                class: 'm-0',
                                style: 'font-size: 1.35rem; line-height: 1.08;',
                            }, '选中节点'),
                        ]),
                        selectedNode.value
                            ? h('div', { class: 'grid gap-3' }, [
                                // 分类标签
                                h('div', {
                                    class: 'inline-flex items-center gap-2 w-fit',
                                    style: `padding: calc(var(--spacing) * 1.2) calc(var(--spacing) * 2.5); border-radius: 999px; background: hsl(var(--chart-${TYPE_COLOR_INDEX[selectedNode.value.type] || 2}) / 0.18); border: 1px solid hsl(var(--chart-${TYPE_COLOR_INDEX[selectedNode.value.type] || 2}) / 0.35);`,
                                }, [
                                    h('span', {
                                        style: `width: 0.7rem; height: 0.7rem; border-radius: 999px; background: hsl(var(--chart-${TYPE_COLOR_INDEX[selectedNode.value.type] || 2}));`,
                                    }),
                                    h('span', { style: 'font-size: 0.88rem; font-weight: 500;' },
                                        TYPE_LABELS[selectedNode.value.type] || selectedNode.value.type || '未分类'),
                                ]),
                                // 节点名称
                                h('h3', {
                                    class: 'm-0',
                                    style: 'font-size: 1.15rem; line-height: 1.3; word-break: break-all;',
                                }, selectedNode.value.label || String(selectedNode.value.id)),
                                // 记忆详情（仅 summary 节点）
                                selectedMemory.value
                                    ? h('p', {
                                        class: 'm-0',
                                        style: 'line-height: 1.6; font-size: 0.95rem; color: hsl(var(--foreground));',
                                    }, selectedMemory.value.content || selectedMemory.value.summary || '-')
                                    : null,
                                // 数据行
                                h('div', {
                                    class: 'grid gap-2',
                                    style: 'grid-template-columns: 1fr 1fr 1fr; padding: calc(var(--spacing) * 3) 0; border-top: 1px solid hsl(var(--border)); border-bottom: 1px solid hsl(var(--border));',
                                }, [
                                    h('div', { class: 'grid gap-1' }, [
                                        h('span', { class: 'muted', style: 'font-size: 0.78rem;' }, '节点类型'),
                                        h('span', { style: 'font-size: 0.95rem; font-weight: 500;' },
                                            TYPE_LABELS[selectedNode.value.type] || selectedNode.value.type || '-'),
                                    ]),
                                    h('div', { class: 'grid gap-1' }, [
                                        h('span', { class: 'muted', style: 'font-size: 0.78rem;' }, '关联数'),
                                        h('span', { style: 'font-size: 0.95rem; font-weight: 500;' },
                                            String(selectedNode.value.degree || 0)),
                                    ]),
                                    h('div', { class: 'grid gap-1' }, [
                                        h('span', { class: 'muted', style: 'font-size: 0.78rem;' }, '权重'),
                                        h('span', { style: 'font-size: 0.95rem; font-weight: 500;' },
                                            (selectedNode.value.weight || 0).toFixed(2)),
                                    ]),
                                ]),
                                // 关联节点
                                h('div', { class: 'grid gap-2' }, [
                                    h('span', { class: 'eyebrow' }, '关联节点'),
                                    connectedNodes.value.length === 0
                                        ? h('p', { class: 'muted m-0', style: 'font-size: 0.92rem;' }, '无关联节点')
                                        : h('div', { class: 'flex gap-2', style: 'flex-wrap: wrap;' },
                                            connectedNodes.value.slice(0, 12).map(n => h(Button, {
                                                key: `cn-${n.id}`,
                                                type: 'ghost',
                                                size: 'sm',
                                                onClick: () => selectNode(n),
                                            }, () => [
                                                h('span', {
                                                    style: `display:inline-block; width:0.6rem; height:0.6rem; border-radius:999px; background: hsl(var(--chart-${TYPE_COLOR_INDEX[n.type] || 2}));`,
                                                }),
                                                (n.label || String(n.id)).slice(0, 12),
                                            ]))
                                        ),
                                ]),
                            ])
                            : h(EmptyState, {
                                icon: 'circle-question-mark',
                                title: '未选中节点',
                                desc: '点击图谱中的节点以查看详情。',
                            }),
                    ]),
                ]),
            ]);
        };
    },
});
