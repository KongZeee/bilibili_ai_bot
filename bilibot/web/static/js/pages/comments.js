// pages/comments.js - 评论回复审计页
const { defineComponent, h, ref, onMounted } = window.Vue;
import { api } from '../api.js';
import { showToast } from '../state.js';
import { Card, Button, Badge, FormSelect, DataTable, Loading, EmptyState } from '../components/common.js';
import { formatTime } from '../utils.js';

export const CommentsPage = defineComponent({
    name: 'CommentsPage',
    setup() {
        const replies = ref([]);
        const loading = ref(false);
        const filter = ref('all');
        const page = ref(1);
        const total = ref(0);

        const columns = [
            { key: 'source_content', label: '原评论' },
            { key: 'reply_content', label: '回复内容' },
            { key: 'status', label: '状态', width: '80px' },
            { key: 'created_at', label: '时间', width: '160px' },
        ];

        async function load() {
            loading.value = true;
            try {
                const data = await api.replies({
                    page: page.value,
                    page_size: 20,
                    status: filter.value === 'all' ? undefined : filter.value,
                });
                replies.value = data.items || [];
                total.value = data.total || 0;
            } catch (e) {
                showToast('加载失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        onMounted(load);

        return () => h('div', [
            h(Card, { title: '评论回复记录' }, {
                action: () => h(FormSelect, {
                    modelValue: filter.value,
                    'onUpdate:modelValue': (v) => { filter.value = v; page.value = 1; load(); },
                    options: [
                        { value: 'all', label: '全部' },
                        { value: 'pending', label: '待处理' },
                        { value: 'replied', label: '已回复' },
                        { value: 'failed', label: '失败' },
                    ],
                }),
                default: () => h(DataTable, {
                    columns, rows: replies.value, loading: loading.value,
                }, {
                    status: ({ value }) => {
                        const type = value === 'published' ? 'success'
                                   : value === 'failed' ? 'danger' : 'warning';
                        return h(Badge, { type, size: 'sm' }, () => value);
                    },
                    created_at: ({ value }) => h('span', { class: 'text-muted', style: 'font-size:12px' },
                        formatTime(value)),
                    source_content: ({ value }) => h('div', {
                        style: 'max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap',
                        title: value,
                    }, value),
                    reply_content: ({ value }) => h('div', {
                        style: 'max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap',
                        title: value,
                    }, value),
                }),
            }),
        ]);
    },
});
