// components/memory/recall-page.js - 召回测试页
const { defineComponent, h, ref } = window.Vue;
import { api } from '../../api.js';
import { appState, showToast } from '../../state.js';
import { Card, Button, Badge, FormInput, Loading, EmptyState } from '../common.js';
import { formatTime } from '../../utils.js';

export const MemoryRecallPage = defineComponent({
    name: 'MemoryRecallPage',
    setup() {
        const query = ref('');
        const results = ref([]);
        const loading = ref(false);
        const searched = ref(false);

        async function search() {
            if (!query.value || !appState.currentAccountId) return;
            loading.value = true;
            searched.value = true;
            try {
                const data = await api.memory.search(appState.currentAccountId, {
                    keyword: query.value,
                    limit: 10,
                });
                results.value = data.items || data || [];
            } catch (e) {
                showToast('搜索失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        return () => h('div', [
            h(Card, { title: '召回测试' }, () => [
                h('p', { class: 'text-muted mb-4' },
                    '输入关键词测试记忆召回效果，验证语义搜索是否正常工作。'),
                h('div', { class: 'flex gap-2 mb-4' }, [
                    h(FormInput, {
                        modelValue: query.value,
                        'onUpdate:modelValue': (v) => query.value = v,
                        placeholder: '输入测试关键词...',
                    }),
                    h(Button, { type: 'primary', loading: loading.value, onClick: search }, () => '召回'),
                ]),
                loading.value
                    ? h(Loading)
                    : searched.value && results.value.length === 0
                        ? h(EmptyState, { icon: 'search', title: '无召回结果', desc: '尝试其他关键词' })
                        : h('div', { class: 'flex-col gap-3' },
                            results.value.map((item, idx) => h(Card, { key: idx, bordered: true }, () => h('div', [
                                h('div', { class: 'flex items-center justify-between mb-2' }, [
                                    h(Badge, { type: 'info', size: 'sm' }, () => item.category || 'unknown'),
                                    h('span', { class: 'text-muted', style: 'font-size:12px' },
                                        formatTime(item.created_at)),
                                ]),
                                h('div', { style: 'font-size:14px;line-height:1.6' }, item.content),
                                item.importance_score != null
                                    ? h('div', { class: 'mt-2' }, [
                                        h('span', { class: 'text-muted', style: 'font-size:12px' }, '重要性: '),
                                        h(Badge, {
                                            type: item.importance_score > 0.7 ? 'success'
                                                : item.importance_score > 0.4 ? 'warning' : 'danger',
                                            size: 'sm',
                                        }, () => (item.importance_score * 10).toFixed(1)),
                                    ])
                                    : null,
                            ])))
                        ),
            ]),
        ]);
    },
});
