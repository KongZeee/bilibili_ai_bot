// bilibot/web/static/js/pages/drafts.js - 动态草稿页（Golden Time 设计稿）
const { h, ref, reactive, onMounted, computed } = window.Vue;
import { appState, refreshAccounts } from '../state.js';
import { api } from '../api.js';
import { Button, Badge, FormSelect, FormTextarea, Modal, EmptyState, Pagination, Loading } from '../components/common.js';
import { formatTime } from '../utils.js';

export const DraftsPage = {
    name: 'DraftsPage',
    setup() {
        const loading = ref(false);
        const drafts = ref([]);
        const selectedAccount = ref(appState.currentAccountId || '');
        const filterStatus = ref('pending');
        const page = ref(1);
        const pageSize = 20;
        const total = ref(0);

        const editModal = reactive({
            visible: false,
            draft: null,
            content: '',
            version: 0,
            saving: false,
        });

        const previewModal = reactive({
            visible: false,
            draft: null,
        });

        const filteredDrafts = computed(() => {
            if (filterStatus.value === 'all') return drafts.value;
            return drafts.value.filter(d => d.status === filterStatus.value);
        });

        const statusFilters = [
            { value: 'pending', label: '待审核' },
            { value: 'approved', label: '已通过' },
            { value: 'rejected', label: '已拒绝' },
            { value: 'published', label: '已发布' },
            { value: 'all', label: '全部' },
        ];

        async function refresh() {
            if (!selectedAccount.value) return;
            loading.value = true;
            try {
                const data = await api.dynamicDrafts.list(selectedAccount.value);
                drafts.value = data.items || data || [];
                total.value = data.total || drafts.value.length || 0;
            } catch (e) {
                appState.notify('加载草稿失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function approve(draft) {
            if (!confirm(`确认通过草稿 #${draft.id}？将通过审核并加入发布队列。`)) return;
            try {
                await api.dynamicDrafts.approve(selectedAccount.value, draft.id);
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
                await api.dynamicDrafts.reject(selectedAccount.value, draft.id, reason || '');
                appState.notify('草稿已拒绝', 'success');
                refresh();
            } catch (e) {
                appState.notify('操作失败：' + (e.message || e), 'danger');
            }
        }

        async function retry(draft) {
            if (!confirm(`重新生成草稿 #${draft.id}？将调用 LLM 重新生成内容。`)) return;
            try {
                await api.dynamicDrafts.retry(selectedAccount.value, draft.id);
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
                await api.dynamicDrafts.update(selectedAccount.value, editModal.draft.id, {
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

        onMounted(() => {
            if (!appState.accountsLoaded) {
                refreshAccounts().then(() => {
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

        const tableGrid = 'minmax(8rem, 0.8fr) 7rem minmax(0, 1.8fr) 7rem 11rem';

        const accName = (id) => {
            const acc = appState.accounts.find(a => a.id === id);
            return acc?.name || id || '-';
        };

        return () => loading.value && drafts.value.length === 0
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                // ═══ hero-band：左侧草稿统计 + 右侧账号选择 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左侧：hero-panel 草稿统计
                    h('div', { class: 'hero-panel' }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('span', { class: 'eyebrow' }, '动态草稿'),
                            h(Badge, { type: 'warning' }, () => '待审核'),
                        ]),
                        h('h2', {
                            style: 'margin:0; font-size:1.65rem; line-height:1.1; text-wrap:balance; word-break:keep-all;',
                        }, '草稿审核'),
                        h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                            h('span', {
                                style: 'font-size:2.4rem; font-weight:500; line-height:1; font-variant-numeric:tabular-nums;',
                            }, String(total.value || 0)),
                            h('span', { class: 'muted m-0', style: 'font-size:0.9rem;' }, '条草稿记录'),
                        ]),
                        h('p', { class: 'muted m-0' }, '审核 AI 生成的动态内容，支持编辑、通过、拒绝与重新生成'),
                    ]),
                    // 右侧：账号选择 Card
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '账号'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '选择账号'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-2' }, [
                            h(FormSelect, {
                                modelValue: selectedAccount.value,
                                'onUpdate:modelValue': (v) => {
                                    selectedAccount.value = v;
                                    appState.currentAccountId = v;
                                    page.value = 1;
                                    refresh();
                                },
                                options: appState.accounts.map(a => ({ value: a.id, label: a.name || a.id })),
                            }),
                            h(Button, {
                                type: 'primary',
                                onClick: refresh,
                                loading: loading.value,
                            }, () => '刷新'),
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
                            h('span', { class: 'eyebrow' }, '草稿队列'),
                            h('h2', {
                                style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;',
                            }, '草稿列表'),
                        ]),
                        // 状态过滤按钮组
                        h('div', { class: 'flex items-center gap-1 flex-wrap' },
                            statusFilters.map(f => h('button', {
                                key: f.value,
                                class: ['btn', 'btn-sm', filterStatus.value === f.value ? 'primary' : 'ghost'].join(' '),
                                onClick: () => {
                                    filterStatus.value = f.value;
                                    page.value = 1;
                                },
                            }, f.label)),
                        ),
                    ]),
                    filteredDrafts.value.length === 0
                        ? h(EmptyState, { icon: 'folder', title: '暂无草稿', desc: '当前账号没有待审核的动态草稿' })
                        : h('div', { class: 'grid', style: 'gap:0; min-width:0;' }, [
                            // 表头
                            h('div', {
                                class: 'grid items-center',
                                style: `grid-template-columns: ${tableGrid}; column-gap: calc(var(--spacing) * 2); padding-bottom: calc(var(--spacing) * 2); border-bottom: 1px solid hsl(var(--border)); color: hsl(var(--muted-foreground)); font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.14em;`,
                            }, [
                                h('span', { class: 'whitespace-nowrap' }, '时间'),
                                h('span', { class: 'whitespace-nowrap' }, '账号'),
                                h('span', { class: 'whitespace-nowrap' }, '内容'),
                                h('span', { class: 'whitespace-nowrap' }, '状态'),
                                h('span', { class: 'whitespace-nowrap' }, '操作'),
                            ]),
                            // 数据行
                            ...filteredDrafts.value.map(d => h('div', {
                                key: d.id,
                                class: 'grid items-center',
                                style: `grid-template-columns: ${tableGrid}; column-gap: calc(var(--spacing) * 2); padding: calc(var(--spacing) * 2.3) 0; border-top: 1px solid hsl(var(--border)); font-size: 0.95rem;`,
                            }, [
                                h('span', {
                                    class: 'whitespace-nowrap',
                                    style: 'color: hsl(var(--muted-foreground)); font-variant-numeric: tabular-nums; font-size:0.85rem;',
                                }, formatTime(d.created_at)),
                                h('span', { class: 'truncate' }, accName(selectedAccount.value)),
                                h('div', {
                                    class: 'truncate',
                                    style: 'cursor:pointer; min-width:0;',
                                    title: d.content || '',
                                    onClick: () => openPreview(d),
                                }, d.content || '(空)'),
                                h('span', {
                                    class: ['badge',
                                        d.status === 'approved' ? 'badge-success'
                                        : d.status === 'rejected' ? 'badge-danger'
                                        : d.status === 'published' ? 'badge-info'
                                        : 'badge-warning'].join(' '),
                                }, d.status || 'pending'),
                                h('div', { class: 'flex items-center gap-1 flex-wrap' }, [
                                    d.status === 'pending' && h('button', {
                                        class: 'btn btn-sm primary',
                                        onClick: () => approve(d),
                                    }, '通过'),
                                    d.status === 'pending' && h('button', {
                                        class: 'btn btn-sm btn-danger',
                                        onClick: () => reject(d),
                                    }, '拒绝'),
                                    d.status === 'pending' && h('button', {
                                        class: 'btn btn-sm ghost',
                                        onClick: () => openEdit(d),
                                    }, '编辑'),
                                    (d.status === 'rejected' || d.status === 'pending') && h('button', {
                                        class: 'btn btn-sm ghost',
                                        onClick: () => retry(d),
                                    }, '重新生成'),
                                ].filter(Boolean)),
                            ])),
                        ]),
                    // 底部分页
                    total.value > pageSize && h('div', { class: 'flex justify-end' }, [
                        h(Pagination, {
                            page: page.value,
                            pageSize,
                            total: total.value,
                            'onUpdate:page': (p) => { page.value = p; refresh(); },
                        }),
                    ]),
                ]),

                // ═══ 编辑弹窗 ═══
                h(Modal, {
                    modelValue: editModal.visible,
                    title: `编辑草稿 #${editModal.draft?.id || ''}`,
                    'onUpdate:modelValue': (v) => editModal.visible = v,
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

                // ═══ 预览弹窗 ═══
                h(Modal, {
                    modelValue: previewModal.visible,
                    title: `草稿预览 #${previewModal.draft?.id || ''}`,
                    'onUpdate:modelValue': (v) => previewModal.visible = v,
                }, {
                    default: () => h('div', { class: 'preview-content' }, [
                        h('pre', { style: 'white-space:pre-wrap; word-break:break-word; font-family:inherit; line-height:1.6' },
                            previewModal.draft?.content || '(空内容)'),
                        previewModal.draft?.pictures && h('div', { class: 'flex gap-2 mt-3 flex-wrap' },
                            (previewModal.draft.pictures || []).map(pic => h('img', {
                                src: pic.src || pic.url,
                                style: 'max-width:120px; max-height:120px; border-radius:8px; border:1px solid hsl(var(--border));',
                            })),
                        ),
                    ]),
                    footer: () => h(Button, { onClick: () => previewModal.visible = false }, () => '关闭'),
                }),
            ]);
    },
};
