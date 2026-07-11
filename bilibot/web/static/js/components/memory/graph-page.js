// components/memory/graph-page.js - 记忆图谱页
const { defineComponent, h, ref, onMounted, onUnmounted, watch } = window.Vue;
import { api } from '../../api.js';
import { appState, showToast } from '../../state.js';
import { Card, Button, Badge, FormInput, Loading, EmptyState } from '../common.js';
import { MemoryGraphRenderer } from './graph-canvas.js';

export const MemoryGraphPage = defineComponent({
    name: 'MemoryGraphPage',
    setup() {
        const canvasRef = ref(null);
        const renderer = ref(null);
        const loading = ref(false);
        const selectedNode = ref(null);
        const queryKeyword = ref('');
        const stats = ref(null);

        async function loadGraph() {
            if (!appState.currentAccountId) return;
            loading.value = true;
            try {
                const data = await api.memory.graph(appState.currentAccountId);
                if (renderer.value) {
                    renderer.value.setData(data.snapshot || { nodes: [], edges: [], memories: [] });
                }
                stats.value = {
                    nodes: data.graph_nodes || 0,
                    edges: data.graph_edges || 0,
                    memories: data.total_memories || 0,
                };
            } catch (e) {
                showToast('加载图谱失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        async function searchGraph() {
            if (!queryKeyword.value || !appState.currentAccountId) return;
            loading.value = true;
            try {
                const data = await api.memory.graphQuery(appState.currentAccountId, { query: queryKeyword.value });
                if (renderer.value) {
                    renderer.value.setData(data.snapshot || { nodes: [], edges: [], memories: [] });
                }
            } catch (e) {
                showToast('搜索失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        onMounted(async () => {
            if (!appState.currentAccountId && appState.accounts.length > 0) {
                appState.currentAccountId = appState.accounts[0].id;
            }
            await loadGraph();
            if (canvasRef.value) {
                renderer.value = new MemoryGraphRenderer(canvasRef.value, {
                    onNodeClick: (node) => { selectedNode.value = node; },
                    onNodeHover: (node) => { /* 可扩展 tooltip */ },
                });
                renderer.value.resize();
                window.addEventListener('resize', () => renderer.value?.resize());
            }
        });

        onUnmounted(() => { renderer.value?.destroy(); });

        return () => h('div', [
            h(Card, { title: '记忆图谱' }, {
                action: () => h('div', { class: 'flex gap-2' }, [
                    h(FormInput, {
                        modelValue: queryKeyword.value,
                        'onUpdate:modelValue': (v) => queryKeyword.value = v,
                        placeholder: '关键词搜索子图...',
                    }),
                    h(Button, { type: 'primary', onClick: searchGraph }, () => '搜索'),
                    h(Button, { onClick: loadGraph }, () => '重置'),
                ]),
                default: () => [
                    stats.value ? h('div', { class: 'flex gap-4 mb-4' }, [
                        h(Badge, { type: 'info' }, () => `节点: ${stats.value.nodes}`),
                        h(Badge, { type: 'success' }, () => `边: ${stats.value.edges}`),
                        h(Badge, { type: 'warning' }, () => `记忆: ${stats.value.memories}`),
                    ]) : null,
                    loading.value
                        ? h(Loading)
                        : h('canvas', {
                            ref: canvasRef,
                            style: 'width:100%;height:calc(100vh - 280px);border:1px solid var(--outline-variant);border-radius:12px;cursor:grab;',
                        }),
                ],
            }),
            selectedNode.value
                ? h(Card, { title: '选中节点' }, () => h('div', [
                    h('div', { class: 'flex gap-2 mb-2' }, [
                        h(Badge, { type: 'info' }, () => selectedNode.value.type),
                        h('span', { style: 'font-weight:600' }, selectedNode.value.label || selectedNode.value.id),
                    ]),
                    h('div', { class: 'text-muted', style: 'font-size:13px' },
                        JSON.stringify(selectedNode.value, null, 2)),
                ]))
                : null,
        ]);
    },
});
