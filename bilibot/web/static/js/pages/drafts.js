// bilibot/web/static/js/pages/drafts.js - 动态草稿页（Golden Time 设计稿）
const { h, ref, reactive, onMounted, onUnmounted, computed, watch } = window.Vue;
import { appState, refreshAccounts } from '../state.js';
import { api } from '../api.js';
import { Button, Badge, FormSelect, FormTextarea, Modal, ConfirmModal, createConfirmHelper, EmptyState, Pagination, Loading } from '../components/common.js';
import { formatTime } from '../utils.js';

function draftId(d) {
    return d?.draft_id || d?.id || '';
}

const STATUS_LABELS = {
    generating: '生成中',
    awaiting_review: '待审核',
    approved: '已通过',
    rejected: '已拒绝',
    publishing: '发布中',
    published: '已发布',
    retry_wait: '等待重试',
    result_unknown: '结果未知',
    failed: '失败',
    expired: '已过期',
};

const RETRYABLE = new Set(['approved', 'retry_wait', 'failed', 'result_unknown']);
const REVIEWABLE = new Set(['awaiting_review']);

export const DraftsPage = {
    name: 'DraftsPage',
    setup() {
        const loading = ref(false);
        const drafts = ref([]);
        const selectedAccount = ref(appState.currentAccountId || '');
        const filterStatus = ref('awaiting_review');
        const page = ref(1);
        const pageSize = 20;
        const total = ref(0);
        let refreshTimer = null;
        let loadSeq = 0;

        const editModal = reactive({
            visible: false,
            draft: null,
            content: '',
            revision: 0,
            saving: false,
        });

        const previewModal = reactive({
            visible: false,
            draft: null,
        });

        const { state: confirmState, showConfirm, handleConfirm } = createConfirmHelper();

        const statusFilters = [
            { value: 'awaiting_review', label: '待审核' },
            { value: 'approved', label: '已通过' },
            { value: 'publishing', label: '发布中' },
            { value: 'retry_wait', label: '等待重试' },
            { value: 'published', label: '已发布' },
            { value: 'rejected', label: '已拒绝' },
            { value: 'failed', label: '失败' },
            { value: 'all', label: '全部' },
        ];

        async function refresh() {
            if (!selectedAccount.value) return;
            const seq = ++loadSeq;
            loading.value = true;
            try {
                const data = await api.dynamicDrafts.list(selectedAccount.value, {
                    status: filterStatus.value === 'all' ? undefined : filterStatus.value,
                    page: page.value,
                    page_size: pageSize,
                });
                if (seq !== loadSeq) return;
                drafts.value = data.items || (Array.isArray(data) ? data : []);
                total.value = data.total || drafts.value.length || 0;
            } catch (e) {
                if (seq !== loadSeq) return;
                appState.notify('加载草稿失败：' + (e.message || e), 'danger');
            } finally {
                if (seq === loadSeq) loading.value = false;
            }
        }

        function approve(draft) {
            const id = draftId(draft);
            showConfirm({
                title: '确认通过',
                message: `确认通过草稿 #${id}？将通过审核并加入发布队列。`,
                confirmText: '通过',
                action: async () => {
                    try {
                        const res = await api.dynamicDrafts.approve(selectedAccount.value, id, {
                            expected_revision: draft.revision,
                        });
                        const taskId = res?.publish_task_id || res?.task_id || '';
                        if (!taskId) {
                            appState.notify(
                                '审核已通过但未返回发布任务 ID，请刷新草稿/任务列表确认',
                                'warning',
                            );
                        } else {
                            appState.notify(
                                `草稿已通过，发布任务已创建（${String(taskId).slice(0, 10)}…）`,
                                'success',
                            );
                        }
                        refresh();
                    } catch (e) {
                        appState.notify('操作失败：' + (e.message || e), 'danger');
                    }
                },
            });
        }

        function reject(draft) {
            const id = draftId(draft);
            showConfirm({
                title: '拒绝草稿',
                message: `拒绝草稿 #${id} 的原因（可选）：`,
                confirmText: '拒绝',
                danger: true,
                prompt: true,
                promptPlaceholder: '输入拒绝原因…',
                action: async (reason) => {
                    try {
                        await api.dynamicDrafts.reject(selectedAccount.value, id, reason || '');
                        appState.notify('草稿已拒绝', 'success');
                        refresh();
                    } catch (e) {
                        appState.notify('操作失败：' + (e.message || e), 'danger');
                    }
                },
            });
        }

        function retry(draft) {
            const id = draftId(draft);
            showConfirm({
                title: '重新发布',
                message: `将草稿 #${id} 重新加入发布队列？`,
                confirmText: '重新发布',
                action: async () => {
                    try {
                        const res = await api.dynamicDrafts.retry(selectedAccount.value, id);
                        const taskId = res?.publish_task_id || res?.task_id || '';
                        if (!taskId) {
                            appState.notify(
                                '已请求重发但未返回任务 ID，请刷新确认',
                                'warning',
                            );
                        } else {
                            appState.notify(
                                `已触发重新发布（${String(taskId).slice(0, 10)}…）`,
                                'info',
                            );
                        }
                        if (refreshTimer) clearTimeout(refreshTimer);
                        refreshTimer = setTimeout(refresh, 1500);
                    } catch (e) {
                        appState.notify('操作失败：' + (e.message || e), 'danger');
                    }
                },
            });
        }

        function openEdit(draft) {
            editModal.draft = draft;
            editModal.content = draft.content || '';
            editModal.revision = draft.revision;
            editModal.saving = false;
            editModal.visible = true;
        }

        async function saveEdit() {
            if (!editModal.draft) return;
            editModal.saving = true;
            const id = draftId(editModal.draft);
            try {
                await api.dynamicDrafts.update(selectedAccount.value, id, {
                    content: editModal.content,
                    expected_revision: editModal.revision,
                });
                appState.notify('草稿已保存', 'success');
                editModal.visible = false;
                refresh();
            } catch (e) {
                const msg = e.message || String(e);
                if (msg.includes('version') || msg.includes('409') || msg.includes('revision')) {
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

        function statusBadgeType(status) {
            if (status === 'approved' || status === 'published') return 'badge-success';
            if (status === 'rejected' || status === 'failed' || status === 'expired') return 'badge-danger';
            if (status === 'publishing' || status === 'retry_wait' || status === 'result_unknown') return 'badge-info';
            return 'badge-warning';
        }

        function imageRefs(draft) {
            const refs = draft?.image_refs || draft?.pictures || [];
            return Array.isArray(refs) ? refs : [];
        }

        async function ensureAccountAndLoad() {
            if (!appState.accountsLoaded) {
                await refreshAccounts();
            }
            if (!selectedAccount.value && appState.accounts.length > 0) {
                const first = appState.accounts[0];
                selectedAccount.value = appState.currentAccountId || first.account_id || first.id;
                // selectedAccount watch 统一 refresh，避免双请求
                return;
            }
            if (selectedAccount.value) refresh();
        }

        onMounted(ensureAccountAndLoad);

        watch(() => appState.currentAccountId, (id) => {
            if (id && id !== selectedAccount.value) {
                selectedAccount.value = id;
                page.value = 1;
                // selectedAccount watch 统一 refresh
            }
        });

        watch(() => appState.accountsLoaded, (loaded) => {
            if (loaded && !selectedAccount.value && appState.accounts.length > 0) {
                const first = appState.accounts[0];
                selectedAccount.value = appState.currentAccountId || first.account_id || first.id;
                // selectedAccount watch 统一 refresh
            }
        });

        watch(selectedAccount, (id, prev) => {
            if (id && id !== prev) {
                page.value = 1;
                drafts.value = [];
                total.value = 0;
                refresh();
            } else if (id && !drafts.value.length) {
                refresh();
            }
        });

        onUnmounted(() => {
            if (refreshTimer) {
                clearTimeout(refreshTimer);
                refreshTimer = null;
            }
        });

        const tableGrid = 'minmax(8rem, 0.8fr) 7rem minmax(0, 1.8fr) 7rem 11rem';

        const accName = (id) => {
            const acc = appState.accounts.find(a => (a.account_id || a.id) === id);
            return acc?.name || id || '-';
        };

        return () => loading.value && drafts.value.length === 0
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
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
                        h('p', { class: 'muted m-0' }, '审核 AI 生成的动态内容，支持编辑、通过、拒绝与重新发布'),
                    ]),
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
                                    // selectedAccount watch 会 refresh
                                },
                                options: appState.accounts.map(a => ({
                                    value: a.account_id || a.id,
                                    label: a.name || a.account_id || a.id,
                                })),
                            }),
                            h(Button, {
                                type: 'primary',
                                onClick: refresh,
                                loading: loading.value,
                            }, () => '刷新'),
                        ]),
                    ]),
                ]),

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
                        h('div', { class: 'flex items-center gap-1 flex-wrap' },
                            statusFilters.map(f => h('button', {
                                key: f.value,
                                class: ['btn', 'btn-sm', filterStatus.value === f.value ? 'primary' : 'ghost'].join(' '),
                                onClick: () => {
                                    filterStatus.value = f.value;
                                    page.value = 1;
                                    refresh();
                                },
                            }, f.label)),
                        ),
                    ]),
                    drafts.value.length === 0
                        ? h(EmptyState, { icon: 'folder', title: '暂无草稿', desc: '当前筛选条件下没有动态草稿' })
                        : h('div', { class: 'grid', style: 'gap:0; min-width:0;' }, [
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
                            ...drafts.value.map(d => {
                                const id = draftId(d);
                                return h('div', {
                                    key: id,
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
                                        role: 'button',
                                        tabindex: '0',
                                        'aria-label': '预览草稿内容',
                                        onClick: () => openPreview(d),
                                        onKeydown: (e) => {
                                            if (e.key === 'Enter' || e.key === ' ') {
                                                e.preventDefault();
                                                openPreview(d);
                                            }
                                        },
                                    }, d.content || '(空)'),
                                    h('span', {
                                        class: ['badge', statusBadgeType(d.status)].join(' '),
                                    }, STATUS_LABELS[d.status] || d.status || '-'),
                                    h('div', { class: 'flex items-center gap-1 flex-wrap' }, [
                                        REVIEWABLE.has(d.status) && h('button', {
                                            class: 'btn btn-sm primary',
                                            onClick: () => approve(d),
                                        }, '通过'),
                                        REVIEWABLE.has(d.status) && h('button', {
                                            class: 'btn btn-sm btn-danger',
                                            onClick: () => reject(d),
                                        }, '拒绝'),
                                        REVIEWABLE.has(d.status) && h('button', {
                                            class: 'btn btn-sm ghost',
                                            onClick: () => openEdit(d),
                                        }, '编辑'),
                                        RETRYABLE.has(d.status) && h('button', {
                                            class: 'btn btn-sm ghost',
                                            onClick: () => retry(d),
                                        }, '重新发布'),
                                    ].filter(Boolean)),
                                ]);
                            }),
                        ]),
                    total.value > pageSize && h('div', { class: 'flex justify-end' }, [
                        h(Pagination, {
                            page: page.value,
                            pageSize,
                            total: total.value,
                            'onUpdate:page': (p) => { page.value = p; refresh(); },
                        }),
                    ]),
                ]),

                h(Modal, {
                    modelValue: editModal.visible,
                    title: `编辑草稿 #${draftId(editModal.draft)}`,
                    'onUpdate:modelValue': (v) => editModal.visible = v,
                }, {
                    default: () => h('div', [
                        h(FormTextarea, {
                            modelValue: editModal.content,
                            'onUpdate:modelValue': (v) => editModal.content = v,
                            rows: 8,
                            placeholder: '输入草稿内容...',
                        }),
                        h('p', { class: 'form-hint' }, `当前版本：v${editModal.revision ?? '-'}（乐观锁，保存时若版本不匹配将拒绝）`),
                    ]),
                    footer: () => h('div', { class: 'flex gap-2 justify-end' }, [
                        h(Button, { onClick: () => editModal.visible = false }, () => '取消'),
                        h(Button, { type: 'primary', onClick: saveEdit, loading: editModal.saving }, () => '保存'),
                    ]),
                }),

                h(Modal, {
                    modelValue: previewModal.visible,
                    title: `草稿预览 #${draftId(previewModal.draft)}`,
                    'onUpdate:modelValue': (v) => previewModal.visible = v,
                }, {
                    default: () => h('div', { class: 'preview-content' }, [
                        h('pre', { style: 'white-space:pre-wrap; word-break:break-word; font-family:inherit; line-height:1.6' },
                            previewModal.draft?.content || '(空内容)'),
                        imageRefs(previewModal.draft).length > 0 && h('div', { class: 'flex gap-2 mt-3 flex-wrap' },
                            imageRefs(previewModal.draft).map((pic, i) => {
                                const src = typeof pic === 'string' ? pic : (pic.src || pic.url || pic.path || '');
                                return src ? h('img', {
                                    key: i,
                                    src,
                                    style: 'max-width:120px; max-height:120px; border-radius:8px; border:1px solid hsl(var(--border));',
                                }) : null;
                            }).filter(Boolean),
                        ),
                    ]),
                    footer: () => h(Button, { onClick: () => previewModal.visible = false }, () => '关闭'),
                }),

                h(ConfirmModal, {
                    modelValue: confirmState.visible,
                    title: confirmState.title,
                    message: confirmState.message,
                    confirmText: confirmState.confirmText,
                    cancelText: confirmState.cancelText,
                    danger: confirmState.danger,
                    prompt: confirmState.prompt,
                    promptPlaceholder: confirmState.promptPlaceholder,
                    'onUpdate:modelValue': (v) => confirmState.visible = v,
                    onConfirm: handleConfirm,
                }),
            ]);
    },
};
