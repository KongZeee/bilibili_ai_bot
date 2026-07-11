// components/memory/list-page.js - 记忆列表页（筛选+分页+搜索）
const { defineComponent, h, ref, computed, onMounted } = window.Vue;
import { api } from '../../api.js';
import { appState, showToast } from '../../state.js';
import { Card, Button, Badge, FormInput, FormSelect, DataTable, Loading, EmptyState } from '../common.js';
import { formatTime } from '../../utils.js';

export const MemoryListPage = defineComponent({
    name: 'MemoryListPage',
    setup() {
        const memories = ref([]);
        const loading = ref(false);
        const page = ref(1);
        const pageSize = ref(20);
        const total = ref(0);
        const keyword = ref('');
        const category = ref('');
        const active = ref('');

        const columns = [
            { key: 'id', label: 'ID', width: '60px' },
            { key: 'category', label: '类型', width: '80px' },
            { key: 'content', label: '内容' },
            { key: 'created_at', label: '时间', width: '160px' },
            { key: 'actions', label: '操作', width: '80px' },
        ];

        async function loadList() {
            if (!appState.currentAccountId) return;
            loading.value = true;
            try {
                const params = {
                    page: page.value,
                    page_size: pageSize.value,
                    ...(keyword.value ? { keyword: keyword.value } : {}),
                    ...(category.value ? { category: category.value } : {}),
                    ...(active.value ? { active: active.value } : {}),
                };
                const data = await api.memory.list(appState.currentAccountId, params);
                memories.value = data.items || [];
                total.value = data.total || 0;
            } catch (e) {
                showToast('加载失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        async function deleteMemory(id) {
            if (!confirm('确定删除此记忆？')) return;
            try {
                await api.memory.delete(appState.currentAccountId, id);
                showToast('已删除', 'success');
                await loadList();
            } catch (e) {
                showToast('删除失败: ' + e.message, 'error');
            }
        }

        async function migrate() {
            if (!confirm('从 JSON 迁移记忆到 SQLite？')) return;
            try {
                await api.memory.migrate(appState.currentAccountId);
                showToast('迁移完成', 'success');
                await loadList();
            } catch (e) {
                showToast('迁移失败: ' + e.message, 'error');
            }
        }

        const totalPages = computed(() => Math.ceil(total.value / pageSize.value) || 1);

        onMounted(loadList);

        return () => h('div', [
            h(Card, { title: '记忆列表' }, {
                action: () => h('div', { class: 'flex gap-2' }, [
                    h(Button, { onClick: migrate }, () => '从 JSON 迁移'),
                    h(Button, { type: 'primary', onClick: loadList }, () => '刷新'),
                ]),
                default: () => [
                    h('div', { class: 'flex gap-3 mb-4', style: 'flex-wrap:wrap' }, [
                        h(FormInput, {
                            modelValue: keyword.value,
                            'onUpdate:modelValue': (v) => keyword.value = v,
                            placeholder: '关键词搜索...',
                        }),
                        h(FormSelect, {
                            modelValue: category.value,
                            'onUpdate:modelValue': (v) => category.value = v,
                            options: [
                                { value: '', label: '全部分类' },
                                { value: 'episodic', label: '情景记忆' },
                                { value: 'factual', label: '事实记忆' },
                                { value: 'procedural', label: '程序记忆' },
                            ],
                        }),
                        h(FormSelect, {
                            modelValue: active.value,
                            'onUpdate:modelValue': (v) => active.value = v,
                            options: [
                                { value: '', label: '全部' },
                                { value: '1', label: '活跃' },
                                { value: '0', label: '已删除' },
                            ],
                        }),
                        h(Button, { type: 'primary', onClick: () => { page.value = 1; loadList(); } }, () => '搜索'),
                    ]),
                    h(DataTable, {
                        columns,
                        rows: memories.value,
                        loading: loading.value,
                    }, {
                        category: ({ value }) => h(Badge, { type: 'info', size: 'sm' }, () => value),
                        content: ({ value }) => h('div', {
                            style: 'max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap',
                            title: value,
                        }, value),
                        created_at: ({ value }) => h('span', { class: 'text-muted', style: 'font-size:12px' },
                            formatTime(value)),
                        actions: ({ row }) => h(Button, {
                            size: 'sm', type: 'danger',
                            onClick: () => deleteMemory(row.id),
                        }, () => '删除'),
                    }),
                    // 分页
                    h('div', { class: 'flex items-center justify-between mt-4' }, [
                        h('span', { class: 'text-muted', style: 'font-size:13px' },
                            `共 ${total.value} 条，第 ${page.value}/${totalPages.value} 页`),
                        h('div', { class: 'flex gap-2' }, [
                            h(Button, { size: 'sm', disabled: page.value <= 1,
                                onClick: () => { page.value--; loadList(); } }, () => '上一页'),
                            h(Button, { size: 'sm', disabled: page.value >= totalPages.value,
                                onClick: () => { page.value++; loadList(); } }, () => '下一页'),
                        ]),
                    ]),
                ],
            }),
        ]);
    },
});
