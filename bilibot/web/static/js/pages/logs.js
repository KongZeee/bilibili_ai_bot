// pages/logs.js - 日志查看页
const { defineComponent, h, ref, onMounted } = window.Vue;
import { api } from '../api.js';
import { showToast } from '../state.js';
import { Card, Button, Badge, FormInput, FormSelect, DataTable, Loading } from '../components/common.js';
import { downloadFile } from '../utils.js';

export const LogsPage = defineComponent({
    name: 'LogsPage',
    setup() {
        const logs = ref([]);
        const loading = ref(false);
        const level = ref('');
        const keyword = ref('');
        const limit = ref(100);

        const columns = [
            { key: 'timestamp', label: '时间', width: '180px' },
            { key: 'level', label: '级别', width: '80px' },
            { key: 'message', label: '内容' },
        ];

        async function load() {
            loading.value = true;
            try {
                const data = await api.logs({
                    level: level.value || undefined,
                    keyword: keyword.value || undefined,
                    limit: limit.value,
                });
                logs.value = data.items || data || [];
            } catch (e) {
                showToast('加载失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        async function download() {
            try {
                const resp = await fetch('/api/logs/download', { credentials: 'same-origin' });
                const blob = await resp.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url; a.download = 'bililog.txt'; a.click();
                URL.revokeObjectURL(url);
            } catch (e) { showToast('下载失败', 'error'); }
        }

        onMounted(load);

        return () => h('div', [
            h(Card, { title: '系统日志' }, {
                action: () => h('div', { class: 'flex gap-2' }, [
                    h(Button, { onClick: download }, () => '下载'),
                    h(Button, { type: 'primary', onClick: load }, () => '查询'),
                ]),
                default: () => [
                    h('div', { class: 'flex gap-3 mb-4', style: 'flex-wrap:wrap' }, [
                        h(FormSelect, {
                            modelValue: level.value,
                            'onUpdate:modelValue': (v) => level.value = v,
                            options: [
                                { value: '', label: '全部级别' },
                                { value: 'DEBUG', label: 'DEBUG' },
                                { value: 'INFO', label: 'INFO' },
                                { value: 'WARNING', label: 'WARNING' },
                                { value: 'ERROR', label: 'ERROR' },
                            ],
                        }),
                        h(FormInput, {
                            modelValue: keyword.value,
                            'onUpdate:modelValue': (v) => keyword.value = v,
                            placeholder: '关键词...',
                        }),
                    ]),
                    h(DataTable, { columns, rows: logs.value, loading: loading.value }, {
                        level: ({ value }) => {
                            const type = value === 'ERROR' ? 'danger'
                                       : value === 'WARNING' ? 'warning'
                                       : value === 'INFO' ? 'info' : 'success';
                            return h(Badge, { type, size: 'sm' }, () => value);
                        },
                        message: ({ value }) => h('div', {
                            style: 'max-width:500px;white-space:pre-wrap;font-family:monospace;font-size:12px',
                        }, value),
                    }),
                ],
            }),
        ]);
    },
});
