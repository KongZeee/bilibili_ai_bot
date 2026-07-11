// bilibot/web/static/js/pages/drafts.js
import { h, ref, reactive, onMounted, computed } from '../vendor/vue.esm-browser.prod.js';
import { appState } from '../state.js';
import { api } from '../api.js';
import { Card, Button, Badge, DataTable, Modal, FormTextarea, FormInput, FormSelect, EmptyState, Pagination } from '../components.js';
import { formatTime, formatDateTime, getStatusType } from '../utils.js';

export const DraftsPage = {
    setup() {
        const loading = ref(false);
        const drafts = ref([]);
        const selectedAccount = ref(appState.currentAccountId || '');
        const filterStatus = ref('pending'); // pending / approved / rejected / published
        const page = ref(1);
        const pageSize = 20;
        const total = ref(0);

        // 编辑弹窗
        const editModal = reactive({
            visible: false,
            draft: null,
            content: '',
            version: 0,
            saving: false,
        });

        // 预览弹窗
        const previewModal = reactive({
            visible: false,
            draft: null,
        });

        const filteredDrafts = computed(() => {
            if (filterStatus.value === 'all') return drafts.value;
            return drafts.value.filter(d => d.status === filterStatus.value);
        });

        async function refresh() {
            if (!selectedAccount.value) return;
            loading.value = true;
            try {
                const res = await api.dynamicDrafts.list({
                    account_id: selectedAccount.value,
                    status: filterStatus.value === 'all' ? undefined : filterStatus.value,
                    page: page.value,
                    page_size: pageSize,
                });
                drafts.value = res.data.items || [];
                total.value = res.data.total || 0;
            } catch (e) {
                appState.notify('加载草稿失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function approve(draft) {
            if (!confirm(`确认通过草稿 #${draft.id}？将通过审核并加入发布队列。`)) return;
            try {
                await api.dynamicDrafts.approve(draft.id, { version: draft.version });
                appState.notify('草稿已通过', 'success');
                refresh();
            } catch (e) {
                appState.notify('操作失败：' + (e.message || e), 'danger');
            }
        }

        async function reject(draft) {
            const reason = prompt(`拒绝草稿 #${draft.id} 的原因（可选）：`);
            if (reason === null) return;
            try {
                await api.dynamicDrafts.reject(draft.id, {
                    version: draft.version,
                    reason: reason || '',
                });
                appState.notify('草稿已拒绝', 'success');
                refresh();
            } catch (e) {
                appState.notify('操作失败：' + (e.message || e), 'danger');
            }
        }

        async function retry(draft) {
            if (!confirm(`重新生成草稿 #${draft.id}？将调用 LLM 重新生成内容。`)) return;
            try {
                await api.dynamicDrafts.retry(draft.id);
                appState.notify('已触发重新生成', 'info');
                setTimeout(refresh, 1500);
            } catch (e) {
                appState.notify('操作失败：' + (e.message || e), 'danger');
            }
        }

        function openEdit(draft) {
            editModal.draft = draft;
            editModal.content = draft.content || '';
            editModal.version = draft.version;
            editModal.saving = false;
            editModal.visible = true;
        }

        async function saveEdit() {
            if (!editModal.draft) return;
            editModal.saving = true;
            try {
                await api.dynamicDrafts.update(editModal.draft.id, {
                    content: editModal.content,
                    version: editModal.version,
                });
                appState.notify('草稿已保存', 'success');
                editModal.visible = false;
                refresh();
            } catch (e) {
                const msg = e.message || String(e);
                if (msg.includes('version') || msg.includes('409')) {
                    appState.notify('草稿已被其他人修改，请刷新后重试', 'warning');
                } else {
                    appState.notify('保存失败：' + msg, 'danger');
                }
            } finally {
                editModal.saving = false;
            }
        }

        function openPreview(draft) {
            previewModal.draft = draft;
            previewModal.visible = true;
        }

        const statusFilters = [
            { value: 'pending', label: '待审核' },
            { value: 'approved', label: '已通过' },
            { value: 'rejected', label: '已拒绝' },
            { value: 'published', label: '已发布' },
            { value: 'all', label: '全部' },
        ];

        const columns = [
            { key: 'id', label: 'ID', width: '60px' },
            { key: 'type', label: '类型' },
            { key: 'content', label: '内容预览' },
            { key: 'status', label: '状态', width: '100px' },
            { key: 'created_at', label: '创建时间', width: '160px' },
            { key: 'actions', label: '操作', width: '260px' },
        ];

        onMounted(() => {
            if (!appState.accountsLoaded) {
                appState.refreshAccounts().then(() => {
                    if (appState.accounts.length > 0 && !selectedAccount.value) {
                        selectedAccount.value = appState.currentAccountId || appState.accounts[0].id;
                        refresh();
                    }
                });
            } else if (appState.accounts.length > 0 && !selectedAccount.value) {
                selectedAccount.value = appState.currentAccountId || appState.accounts[0].id;
                refresh();
            } else if (selectedAccount.value) {
                refresh();
            }
        });

        return () => h('div', [
            h(Card, { title: '动态草稿审核' }, {
                action: () => h('div', { class: 'flex gap-3 items-center' }, [
                    h(FormSelect, {
                        modelValue: selectedAccount.value,
                        'onUpdate:modelValue': (v) => {
                            selectedAccount.value = v;
                            appState.currentAccountId = v;
                            page.value = 1;
                            refresh();
                        },
                        options: appState.accounts.map(a => ({ value: a.id, label: a.name || a.id })),
                        style: 'width:160px',
                    }),
                    h(Button, { type: 'primary', onClick: refresh, loading: loading.value }, () => '刷新'),
                ]),
                default: () => [
                    // 状态过滤标签
                    h('div', { class: 'flex gap-2 mb-4' },
                        statusFilters.map(f => h(Button, {
                            type: filterStatus.value === f.value ? 'primary' : 'secondary',
                            size: 'sm',
                            onClick: () => {
                                filterStatus.value = f.value;
                                page.value = 1;
                                refresh();
                            },
                        }, () => f.label)),
                    ),
                    h(DataTable, {
                        columns,
                        rows: filteredDrafts.value,
                        loading: loading.value,
                        empty: '暂无草稿',
                    }, {
                        content: ({ row }) => h('div', {
                            class: 'text-ellipsis',
                            style: 'max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; cursor:pointer',
                            title: row.content,
                            onClick: () => openPreview(row),
                        }, row.content || '(空)'),
                        type: ({ value }) => {
                            const map = { dynamic: '动态', video: '视频' };
                            return h(Badge, { type: 'info', size: 'sm' }, () => map[value] || value);
                        },
                        status: ({ value }) => {
                            const typeMap = {
                                pending: 'warning',
                                approved: 'success',
                                rejected: 'danger',
                                published: 'info',
                            };
                            return h(Badge, { type: typeMap[value] || 'info', size: 'sm' }, () => value);
                        },
                        created_at: ({ value }) => h('span', { class: 'text-muted', style: 'font-size:12px' }, formatTime(value)),
                        actions: ({ row }) => h('div', { class: 'flex gap-2' }, [
                            row.status === 'pending' && h(Button, { size: 'sm', type: 'primary', onClick: () => approve(row) }, () => '通过'),
                            row.status === 'pending' && h(Button, { size: 'sm', type: 'danger', onClick: () => reject(row) }, () => '拒绝'),
                            row.status === 'pending' && h(Button, { size: 'sm', onClick: () => openEdit(row) }, () => '编辑'),
                            h(Button, { size: 'sm', onClick: () => openPreview(row) }, () => '预览'),
                            (row.status === 'rejected' || row.status === 'pending') && h(Button, { size: 'sm', onClick: () => retry(row) }, () => '重新生成'),
                        ].filter(Boolean)),
                    }),
                    // 分页
                    total.value > pageSize && h(Pagination, {
                        page: page.value,
                        pageSize,
                        total: total.value,
                        'onUpdate:page': (p) => { page.value = p; refresh(); },
                    }),
                ],
            }),

            // 编辑弹窗
            h(Modal, {
                visible: editModal.visible,
                title: `编辑草稿 #${editModal.draft?.id || ''}`,
                'onUpdate:visible': (v) => editModal.visible = v,
            }, {
                default: () => h('div', [
                    h(FormTextarea, {
                        modelValue: editModal.content,
                        'onUpdate:modelValue': (v) => editModal.content = v,
                        rows: 8,
                        placeholder: '输入草稿内容...',
                    }),
                    h('p', { class: 'form-hint' }, `当前版本：v${editModal.version}（乐观锁，保存时若版本不匹配将拒绝）`),
                ]),
                footer: () => h('div', { class: 'flex gap-2 justify-end' }, [
                    h(Button, { onClick: () => editModal.visible = false }, () => '取消'),
                    h(Button, { type: 'primary', onClick: saveEdit, loading: editModal.saving }, () => '保存'),
                ]),
            }),

            // 预览弹窗
            h(Modal, {
                visible: previewModal.visible,
                title: `草稿预览 #${previewModal.draft?.id || ''}`,
                'onUpdate:visible': (v) => previewModal.visible = v,
            }, {
                default: () => h('div', { class: 'preview-content' }, [
                    h('pre', { style: 'white-space:pre-wrap; word-break:break-word; font-family:inherit; line-height:1.6' },
                        previewModal.draft?.content || '(空内容)'),
                    previewModal.draft?.pictures && h('div', { class: 'flex gap-2 mt-3 flex-wrap' },
                        (previewModal.draft.pictures || []).map(pic => h('img', {
                            src: pic.src || pic.url,
                            style: 'max-width:120px; max-height:120px; border-radius:8px; border:1px solid var(--outline-variant)',
                        })),
                    ),
                ]),
                footer: () => h(Button, { onClick: () => previewModal.visible = false }, () => '关闭'),
            }),
        ]);
    },
};
