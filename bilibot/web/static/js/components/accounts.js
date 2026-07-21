// components/accounts.js - 本机唯一 B站连接（列表 + 详情）
const { defineComponent, h, ref, reactive, computed, onMounted, watch, onUnmounted } = window.Vue;
import { api } from '../api.js';
import { appState, refreshAccounts, showToast } from '../state.js';
import { Card, Button, Badge, Modal, ConfirmModal, createConfirmHelper, FormInput, FormSelect, Toggle, EmptyState, Loading, Icon, KpiCard, HeroPanel, ActionList, ProgressBar, StatusDot } from './common.js';
import { navigate } from '../router.js';
import { formatRelative } from '../utils.js';

export const AccountListPage = defineComponent({
    name: 'AccountListPage',
    setup() {
        // ── 数据加载 ──
        const accounts = computed(() => appState.accounts);
        const total = computed(() => accounts.value.length);
        const onlineCount = computed(() => accounts.value.filter(a => a.is_running || a.running).length);
        const firstAccountId = computed(() => accounts.value[0]?.id || null);

        // ── 添加账号 Modal ──
        const showAddModal = ref(false);
        const adding = ref(false);
        const addForm = reactive({ uid: '', nickname: '', note: '' });

        async function submitAdd() {
            adding.value = true;
            try {
                await api.accounts.create({
                    id: addForm.uid,
                    dede_user_id: addForm.uid,
                    name: addForm.nickname,
                    note: addForm.note,
                });
                showToast('账号添加成功', 'success');
                showAddModal.value = false;
                addForm.uid = '';
                addForm.nickname = '';
                addForm.note = '';
                await refreshAccounts();
            } catch (e) {
                showToast('添加失败: ' + e.message, 'error');
            } finally {
                adding.value = false;
            }
        }

        // ── 删除账号（单账号模式：唯一账号不可删）──
        const { state: confirmState, showConfirm, handleConfirm } = createConfirmHelper();
        function deleteAccount(id) {
            if (accounts.value.length <= 1) {
                showToast('不能删除唯一连接', 'warning');
                return;
            }
            showConfirm({
                title: '删除账号',
                message: '确定删除该账号？此操作不可撤销。',
                confirmText: '删除',
                danger: true,
                action: async () => {
                    try {
                        await api.accounts.delete(id);
                        showToast('账号已删除', 'success');
                        await refreshAccounts();
                    } catch (e) {
                        showToast('删除失败: ' + e.message, 'error');
                    }
                },
            });
        }

        function openAddModal() {
            if (accounts.value.length >= 1) {
                showToast('本机仅支持一个 B站连接', 'warning');
                return;
            }
            showAddModal.value = true;
        }

        // ── 编辑账号（跳转详情页）──
        function editAccount(id) {
            navigate('/accounts/' + id);
        }

        // ── QR 扫码登录 ──
        const qrUrl = ref('');
        const qrSessionId = ref('');
        const qrPolling = ref(false);
        const qrStatus = ref('');
        let qrTimer = null;
        const qrAccountId = computed(() => appState.currentAccountId || firstAccountId.value);

        async function startQrLogin() {
            if (!qrAccountId.value) {
                showToast('请先添加账号后再扫码登录', 'warning');
                return;
            }
            try {
                const resp = await api.accounts.qrLogin(qrAccountId.value);
                qrUrl.value = resp.qrcode_data_url || resp.qrcode_url || resp.qr_url || '';
                qrSessionId.value = resp.qr_session_id;
                qrPolling.value = true;
                qrStatus.value = '等待扫码...';
                startQrPolling();
            } catch (e) {
                showToast('启动扫码失败: ' + e.message, 'error');
            }
        }

        let qrPollCount = 0;
        const QR_MAX_POLL = 90;

        function startQrPolling() {
            if (qrTimer) clearInterval(qrTimer);
            qrPollCount = 0;
            qrTimer = setInterval(async () => {
                if (!qrPolling.value || !qrSessionId.value) return;
                qrPollCount++;
                if (qrPollCount > QR_MAX_POLL) {
                    qrPolling.value = false;
                    if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
                    qrStatus.value = '二维码已过期';
                    showToast('二维码超时，请重新生成', 'warning');
                    return;
                }
                try {
                    const resp = await api.accounts.qrPoll(qrAccountId.value, qrSessionId.value);
                    if (resp.status === 'confirmed') {
                        qrStatus.value = '登录成功';
                        showToast('B站登录成功', 'success');
                        qrPolling.value = false;
                        if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
                        setTimeout(() => {
                            qrUrl.value = '';
                            qrSessionId.value = '';
                            qrStatus.value = '';
                            refreshAccounts();
                        }, 1500);
                    } else if (resp.status === 'scanned') {
                        qrStatus.value = '已扫码，等待确认...';
                    } else if (resp.status === 'expired') {
                        qrStatus.value = '二维码已过期';
                        showToast('二维码过期，请重新生成', 'warning');
                        qrPolling.value = false;
                        if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
                    }
                } catch (e) {
                    qrPolling.value = false;
                    if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
                    showToast('轮询失败: ' + e.message, 'error');
                }
            }, 2000);
        }

        onUnmounted(() => {
            if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
        });

        // ── 生命周期 ──
        onMounted(() => { if (!appState.accountsLoaded) refreshAccounts(); });

        // ── 表格列宽 ──
        const tableCols = '6rem 8rem 5rem 8rem 8rem 7rem 6rem';
        const headerLabelStyle = 'font-size:0.75rem;text-transform:uppercase;white-space:nowrap;color:hsl(var(--muted-foreground));letter-spacing:0.14em;';
        const cellTruncateStyle = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:hsl(var(--foreground));';
        const iconBtnStyle = 'display:inline-flex;align-items:center;justify-content:center;width:2rem;height:2rem;flex-shrink:0;background:hsl(var(--card));border:1px solid hsl(var(--border));border-radius:calc(var(--radius) * 0.5);cursor:pointer;transition:transform .18s ease,border-color .18s ease;color:hsl(var(--foreground));';

        return () => h('div', { style: 'display:grid;gap:calc(var(--spacing) * 4);' }, [
            // ═══ Section 1: hero band — 账号摘要 + KPI ═══
            h('section', {
                style: 'display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:calc(var(--spacing) * 4);',
            }, [
                // 左侧：账号摘要面板（accent 背景）
                h('article', {
                    style: 'display:grid;gap:calc(var(--spacing) * 3);padding:calc(var(--spacing) * 5);border:1px solid hsl(var(--accent));background:hsl(var(--accent) / 0.22);border-radius:calc(var(--radius) * 0.82);box-shadow:var(--shadow-xs);align-content:start;',
                }, [
                    h('div', { style: 'display:grid;gap:calc(var(--spacing) * 1);' }, [
                        h('span', { class: 'eyebrow' }, 'B站登录'),
                        h('h2', {
                            style: 'font-size:1.65rem;font-weight:600;line-height:1.18;text-wrap:balance;word-break:keep-all;overflow-wrap:break-word;margin:0;',
                        }, total.value === 0
                            ? '尚未登录 B站'
                            : '本机 Bot 已连接'),
                    ]),
                    // 单 bot：仅无连接时显示「接入 B站」（onboarding）
                    total.value === 0
                        ? h('div', { style: 'display:flex;align-items:center;gap:calc(var(--spacing) * 3);' }, [
                            h(Button, { type: 'primary', onClick: openAddModal }, () => [
                                h(Icon, { name: 'circle-plus', size: '1rem' }),
                                h('span', '接入 B站'),
                            ]),
                        ])
                        : h('p', {
                            style: 'margin:0;font-size:0.875rem;color:hsl(var(--muted-foreground));',
                        }, '单 Bot 模式 · 可切换人格与模型'),
                ]),
                // 右侧：KPI 面板（Card 样式）
                h('article', {
                    style: 'display:grid;gap:calc(var(--spacing) * 3);padding:calc(var(--spacing) * 5);border:1px solid hsl(var(--border));background:hsl(var(--card));border-radius:calc(var(--radius) * 0.82);box-shadow:var(--shadow-xs);align-content:start;',
                }, [
                    h('div', { style: 'display:grid;gap:calc(var(--spacing) * 1);' }, [
                        h('span', { class: 'eyebrow' }, '运行状态'),
                        h('h2', { style: 'font-size:1.25rem;font-weight:600;line-height:1.2;margin:0;' }, '运行状态'),
                    ]),
                    h('div', {
                        style: 'font-size:2.25rem;font-weight:600;white-space:nowrap;line-height:1;color:hsl(var(--foreground));font-variant-numeric:tabular-nums;',
                    }, total.value === 0
                        ? '—'
                        : (onlineCount.value > 0 ? '在线' : '离线')),
                    h(ProgressBar, {
                        value: onlineCount.value,
                        max: total.value || 1,
                        colorToken: 'chart-4',
                    }),
                    h('div', { style: 'display:flex;align-items:center;gap:calc(var(--spacing) * 4);' }, [
                        h('span', {
                            style: 'display:inline-flex;align-items:center;gap:0.375rem;font-size:0.75rem;white-space:nowrap;color:hsl(var(--accent-foreground));',
                        }, [
                            h('span', { style: 'display:inline-block;width:0.375rem;height:0.375rem;border-radius:999px;background:hsl(var(--chart-4));' }),
                            onlineCount.value > 0 ? 'Bot 运行中' : 'Bot 未运行',
                        ]),
                        h('span', {
                            style: 'display:inline-flex;align-items:center;gap:0.375rem;font-size:0.75rem;white-space:nowrap;color:hsl(var(--muted-foreground));',
                        }, [
                            h('span', { style: 'display:inline-block;width:0.375rem;height:0.375rem;border-radius:999px;background:hsl(var(--muted-foreground));' }),
                            total.value === 0 ? '未接入' : '单账号',
                        ]),
                    ]),
                ]),
            ]),

            // ═══ Section 2: 数据表格（7 列 CSS Grid）═══
            h('section', {
                style: 'display:grid;gap:calc(var(--spacing) * 4);padding:calc(var(--spacing) * 5);border:1px solid hsl(var(--border));background:hsl(var(--card));border-radius:calc(var(--radius) * 0.82);box-shadow:var(--shadow-xs);',
            }, [
                // 表格 header
                h('div', {
                    style: 'display:flex;align-items:flex-start;justify-content:space-between;gap:calc(var(--spacing) * 4);',
                }, [
                    h('div', { style: 'display:grid;gap:calc(var(--spacing) * 1);' }, [
                        h('span', { class: 'eyebrow' }, '连接明细'),
                        h('h2', { style: 'font-size:1.25rem;font-weight:600;line-height:1.2;margin:0;' }, '本机连接'),
                    ]),
                    h('span', {
                        style: 'display:inline-flex;align-items:center;gap:0.375rem;padding:0.25rem 0.625rem;font-size:0.75rem;white-space:nowrap;background:hsl(var(--muted));color:hsl(var(--accent-foreground));border-radius:999px;',
                    }, total.value === 0 ? '未接入' : '已接入'),
                ]),
                // 表格内容
                !appState.accountsLoaded
                    ? h(Loading)
                    : total.value === 0
                        ? h(EmptyState, {
                            icon: 'user',
                            title: '暂无账号',
                            desc: '点击上方「接入 B站」完成配置（仅支持一个 B站账号）',
                        })
                        : h('div', { style: 'overflow-x:auto;' }, [
                            h('div', { style: 'min-width:780px;display:grid;gap:0;' }, [
                                // 表头行
                                h('div', {
                                    style: `display:grid;align-items:center;gap:calc(var(--spacing) * 3);padding-bottom:calc(var(--spacing) * 3);border-bottom:1px solid hsl(var(--border));grid-template-columns:${tableCols};`,
                                }, [
                                    h('span', { style: headerLabelStyle }, '账号 UID'),
                                    h('span', { style: headerLabelStyle }, '昵称'),
                                    h('span', { style: headerLabelStyle }, '状态'),
                                    h('span', { style: headerLabelStyle }, '人格'),
                                    h('span', { style: headerLabelStyle }, '对话模型'),
                                    h('span', { style: headerLabelStyle }, '最近活跃'),
                                    h('span', { style: headerLabelStyle + 'text-align:right;' }, '操作'),
                                ]),
                                // 数据行
                                ...accounts.value.map(acc => {
                                    const isOnline = acc.is_running || acc.running;
                                    return h('div', {
                                        key: acc.id,
                                        style: `display:grid;align-items:center;gap:calc(var(--spacing) * 3);padding:calc(var(--spacing) * 3) 0;border-bottom:1px solid hsl(var(--border));grid-template-columns:${tableCols};`,
                                    }, [
                                        h('span', {
                                            style: cellTruncateStyle + 'font-weight:500;font-variant-numeric:tabular-nums;',
                                        }, acc.dede_user_id || acc.uid || '-'),
                                        h('span', {
                                            style: cellTruncateStyle,
                                        }, acc.nickname || acc.name || '-'),
                                        h(StatusDot, {
                                            status: isOnline ? 'online' : 'offline',
                                            label: isOnline ? '在线' : '离线',
                                        }),
                                        h('span', {
                                            style: cellTruncateStyle,
                                        }, acc.persona_name || acc.persona_id || '-'),
                                        h('span', {
                                            style: cellTruncateStyle,
                                        }, acc.llm_name || acc.llm_id || '-'),
                                        h('span', {
                                            style: cellTruncateStyle + 'font-size:0.875rem;color:hsl(var(--muted-foreground));',
                                        }, formatRelative(acc.last_active_at)),
                                        h('div', {
                                            style: 'display:flex;align-items:center;justify-content:flex-end;gap:0.375rem;',
                                        }, [
                                            h('button', {
                                                type: 'button',
                                                'aria-label': '编辑',
                                                onClick: () => editAccount(acc.id),
                                                style: iconBtnStyle,
                                            }, [h(Icon, { name: 'pen-line', size: '1rem' })]),
                                            // 单账号模式：唯一账号不提供删除入口
                                            total.value > 1
                                                ? h('button', {
                                                    type: 'button',
                                                    'aria-label': '删除',
                                                    onClick: () => deleteAccount(acc.id),
                                                    style: iconBtnStyle + 'color:hsl(var(--destructive));',
                                                }, [h(Icon, { name: 'trash-2', size: '1rem' })])
                                                : null,
                                        ]),
                                    ]);
                                }),
                            ]),
                        ]),
            ]),

            // ═══ Section 3: split grid — QR 登录 + 凭证管理 ═══
            h('section', {
                style: 'display:grid;grid-template-columns:minmax(0,1.15fr) minmax(18rem,0.85fr);gap:calc(var(--spacing) * 4);',
            }, [
                // 左侧：QR 登录面板
                h('article', {
                    style: 'display:grid;gap:calc(var(--spacing) * 4);padding:calc(var(--spacing) * 5);border:1px solid hsl(var(--border));background:hsl(var(--card));border-radius:calc(var(--radius) * 0.82);box-shadow:var(--shadow-xs);',
                }, [
                    h('div', { style: 'display:grid;gap:calc(var(--spacing) * 1);' }, [
                        h('span', { class: 'eyebrow' }, '快速接入'),
                        h('h2', { style: 'font-size:1.25rem;font-weight:600;line-height:1.2;margin:0;' }, '扫码登录'),
                    ]),
                    // 11rem 占位区
                    h('div', { style: 'display:flex;justify-content:center;' }, [
                        h('div', {
                            style: 'position:relative;display:grid;place-items:center;width:11rem;height:11rem;background:hsl(var(--card));border:1px solid hsl(var(--border));border-radius:calc(var(--radius) * 0.72);',
                        }, [
                            qrUrl.value
                                ? h('img', {
                                    src: qrUrl.value,
                                    style: 'width:10rem;height:10rem;border-radius:calc(var(--radius) * 0.5);object-fit:contain;',
                                    alt: '登录二维码',
                                })
                                : h('button', {
                                    type: 'button',
                                    onClick: startQrLogin,
                                    style: 'display:grid;place-items:center;gap:0.5rem;width:100%;height:100%;background:transparent;border:none;cursor:pointer;color:hsl(var(--muted-foreground));font-size:0.875rem;',
                                }, [
                                    h(Icon, { name: 'mouse-pointer-click', size: '2rem' }),
                                    h('span', '点击生成二维码'),
                                ]),
                        ]),
                    ]),
                    // 状态文字
                    qrStatus.value
                        ? h('div', {
                            style: 'display:flex;align-items:center;justify-content:center;gap:0.5rem;',
                        }, [
                            qrPolling.value
                                ? h('span', {
                                    style: 'display:inline-block;width:0.5rem;height:0.5rem;border-radius:999px;background:hsl(var(--secondary));',
                                })
                                : null,
                            h('span', {
                                style: 'font-size:0.875rem;font-weight:500;color:hsl(var(--foreground));',
                            }, qrStatus.value),
                        ])
                        : null,
                    h('p', {
                        style: 'font-size:0.75rem;text-align:center;color:hsl(var(--muted-foreground));margin:0;',
                    }, '二维码有效期 3 分钟 · 可刷新'),
                ]),
                // 右侧：凭证管理 ActionList
                h('article', {
                    style: 'display:grid;gap:calc(var(--spacing) * 4);padding:calc(var(--spacing) * 5);border:1px solid hsl(var(--border));background:hsl(var(--card));border-radius:calc(var(--radius) * 0.82);box-shadow:var(--shadow-xs);',
                }, [
                    h('div', { style: 'display:grid;gap:calc(var(--spacing) * 1);' }, [
                        h('span', { class: 'eyebrow' }, '凭证管理'),
                        h('h2', { style: 'font-size:1.25rem;font-weight:600;line-height:1.2;margin:0;' }, '常用操作'),
                    ]),
                    h(ActionList, {
                        items: [
                            ...(total.value === 0
                                ? [{ iconName: 'user', label: '添加账号', onClick: openAddModal }]
                                : []),
                            {
                                iconName: 'pen-line',
                                label: '编辑账号',
                                onClick: () => {
                                    if (!firstAccountId.value) {
                                        showToast('暂无账号可编辑', 'warning');
                                        return;
                                    }
                                    navigate('/accounts/' + firstAccountId.value);
                                },
                            },
                            { iconName: 'star', label: '配置人格', onClick: () => navigate('/personas') },
                            { iconName: 'box', label: '配置模型', onClick: () => navigate('/llm') },
                        ],
                    }),
                ]),
            ]),

            // ═══ 添加账号 Modal ═══
            h(Modal, {
                modelValue: showAddModal.value,
                'onUpdate:modelValue': (v) => showAddModal.value = v,
                title: '添加账号',
                width: '480px',
            }, {
                default: () => h('div', { style: 'display:grid;gap:calc(var(--spacing) * 3);' }, [
                    h(FormInput, {
                        label: '账号 UID',
                        modelValue: addForm.uid,
                        'onUpdate:modelValue': (v) => addForm.uid = v,
                        id: 'add-uid', name: 'add-uid',
                        autocomplete: 'off', spellcheck: false,
                        placeholder: 'B站用户 UID',
                    }),
                    h(FormInput, {
                        label: '昵称',
                        modelValue: addForm.nickname,
                        'onUpdate:modelValue': (v) => addForm.nickname = v,
                        id: 'add-nickname', name: 'add-nickname',
                        autocomplete: 'off', spellcheck: false,
                        placeholder: '账号显示名称',
                    }),
                    h(FormInput, {
                        label: '备注',
                        modelValue: addForm.note,
                        'onUpdate:modelValue': (v) => addForm.note = v,
                        id: 'add-note', name: 'add-note',
                        autocomplete: 'off', spellcheck: false,
                        placeholder: '可选备注',
                    }),
                ]),
                footer: () => [
                    h(Button, { type: 'ghost', onClick: () => showAddModal.value = false }, () => '取消'),
                    h(Button, { type: 'primary', loading: adding.value, onClick: submitAdd }, () => '添加'),
                ],
            }),

            // ═══ 统一确认对话框 ═══
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
});

// ═══ 账号详情页 Tab 组件 ═══

// ── Card 样式辅助 ──
const detailCardStyle = 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start; box-shadow: var(--shadow-xs);';
const detailCardBodyStyle = 'display:grid; gap:calc(var(--spacing) * 3);';
const detailHeadingStyle = 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;';

// ── 原生按钮辅助（CSS 使用 .btn.primary / .btn.ghost 复合选择器）──
function primaryBtn(label, opts = {}) {
    const { loading = false, disabled = false, onClick } = opts;
    return h('button', {
        type: 'button',
        class: ['btn', 'primary', loading ? 'btn-loading' : ''].filter(Boolean).join(' '),
        disabled: disabled || loading,
        onClick,
    }, [
        loading ? h('span', { class: 'spinner spinner-sm', 'aria-hidden': 'true' }) : null,
        h('span', label),
    ].filter(Boolean));
}

function ghostBtn(label, opts = {}) {
    const { loading = false, disabled = false, onClick } = opts;
    return h('button', {
        type: 'button',
        class: ['btn', 'ghost', loading ? 'btn-loading' : ''].filter(Boolean).join(' '),
        disabled: disabled || loading,
        onClick,
    }, [
        loading ? h('span', { class: 'spinner spinner-sm', 'aria-hidden': 'true' }) : null,
        h('span', label),
    ].filter(Boolean));
}

// ── 1. AccountInfoTab（单账号：不展示删除入口）──
	const AccountInfoTab = defineComponent({
	    name: 'AccountInfoTab',
	    props: { account: Object },
	    emits: ['save'],
	    setup(props, { emit }) {
	        // 兼容后端 uid / name 与表单 dede_user_id / nickname 字段
	        const form = ref({
	            ...props.account,
	            dede_user_id: props.account?.dede_user_id || props.account?.uid || '',
	            nickname: props.account?.nickname || props.account?.name || '',
	            note: props.account?.note || '',
	        });
	        const saving = ref(false);

	        async function handleSave() {
	            saving.value = true;
	            try {
	                emit('save', {
	                    ...form.value,
	                    // 写回后端识别的字段
	                    name: form.value.nickname || form.value.name,
	                    dede_user_id: form.value.dede_user_id,
	                    note: form.value.note,
	                });
	            } finally {
	                saving.value = false;
	            }
	        }

	        return () => h('article', {
	            class: 'grid gap-3',
	            style: detailCardStyle,
	        }, [
	            h('div', { class: 'card-header' }, [
	                h('div', { class: 'grid gap-1' }, [
	                    h('span', { class: 'eyebrow' }, '账号信息'),
	                    h('h2', { style: detailHeadingStyle }, '基本资料'),
	                ]),
	            ]),
	            h('div', { class: 'card-body', style: detailCardBodyStyle }, [
	                h(FormInput, {
	                    label: '账号 UID',
	                    modelValue: form.value.dede_user_id,
	                    'onUpdate:modelValue': (v) => form.value.dede_user_id = v,
	                    id: 'info-uid', name: 'info-uid',
	                    autocomplete: 'off', spellcheck: false,
	                    placeholder: 'B站用户 UID',
	                }),
	                h(FormInput, {
	                    label: '昵称',
	                    modelValue: form.value.nickname || form.value.name || '',
	                    'onUpdate:modelValue': (v) => form.value.nickname = v,
	                    id: 'info-nickname', name: 'info-nickname',
	                    autocomplete: 'off', spellcheck: false,
	                    placeholder: '账号显示名称',
	                }),
	                h(FormInput, {
	                    label: '备注',
	                    modelValue: form.value.note || '',
	                    'onUpdate:modelValue': (v) => form.value.note = v,
	                    id: 'info-note', name: 'info-note',
	                    autocomplete: 'off', spellcheck: false,
	                    placeholder: '可选备注',
	                }),
	                h('div', {
	                    style: 'display:flex; align-items:center; gap:calc(var(--spacing) * 2); flex-wrap:wrap;',
	                }, [
	                    primaryBtn('保存', { loading: saving.value, onClick: handleSave }),
	                ]),
	            ]),
	        ]);
	    },
	});

// ── 2. AccountLoginTab ──
const AccountLoginTab = defineComponent({
    name: 'AccountLoginTab',
    props: { account: Object },
    setup(props) {
        const qrUrl = ref('');
        const sessionId = ref('');
        const polling = ref(false);
        const status = ref('');
        let qrTimer = null;

        async function startQrLogin() {
            try {
                const resp = await api.accounts.qrLogin(props.account.id);
                qrUrl.value = resp.qrcode_data_url || resp.qrcode_url || resp.qr_url || '';
                sessionId.value = resp.qr_session_id;
                polling.value = true;
                status.value = '等待扫描';
                startPolling();
            } catch (e) {
                showToast('启动扫码失败: ' + e.message, 'error');
            }
        }

        let pollCount = 0;
        const MAX_POLL = 90;

        function startPolling() {
            if (qrTimer) clearInterval(qrTimer);
            pollCount = 0;
            qrTimer = setInterval(async () => {
                if (!polling.value || !sessionId.value) return;
                pollCount++;
                if (pollCount > MAX_POLL) {
                    polling.value = false;
                    if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
                    status.value = '登录失败';
                    showToast('二维码超时，请重新生成', 'warning');
                    return;
                }
                try {
                    const resp = await api.accounts.qrPoll(props.account.id, sessionId.value);
                    if (resp.status === 'confirmed') {
                        status.value = '登录成功';
                        showToast('B站登录成功', 'success');
                        polling.value = false;
                        if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
                        setTimeout(() => {
                            qrUrl.value = '';
                            sessionId.value = '';
                            status.value = '';
                            refreshAccounts();
                        }, 1500);
                    } else if (resp.status === 'scanned') {
                        status.value = '已扫描，等待确认';
                    } else if (resp.status === 'expired') {
                        status.value = '登录失败';
                        showToast('二维码过期，请重新生成', 'warning');
                        polling.value = false;
                        if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
                    }
                } catch (e) {
                    polling.value = false;
                    status.value = '登录失败';
                    if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
                    showToast('轮询失败: ' + e.message, 'error');
                }
            }, 2000);
        }

        onUnmounted(() => {
            if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
        });

        return () => h('article', {
            class: 'grid gap-3',
            style: detailCardStyle,
        }, [
            h('div', { class: 'card-header' }, [
                h('div', { class: 'grid gap-1' }, [
                    h('span', { class: 'eyebrow' }, 'B站登录'),
                    h('h2', { style: detailHeadingStyle }, '扫码登录'),
                ]),
            ]),
            h('div', { class: 'card-body', style: 'display:grid; gap:calc(var(--spacing) * 4);' }, [
                // 当前认证状态
                h('div', {
                    style: 'display:flex; align-items:center; gap:calc(var(--spacing) * 2);',
                }, [
                    props.account.authenticated
                        ? h(Badge, { type: 'success' }, () => '已认证')
                        : h(Badge, { type: 'warning' }, () => '未认证'),
                    h('span', {
                        style: 'font-size:0.88rem; color:hsl(var(--muted-foreground));',
                    }, '扫码登录会自动获取 Cookie'),
                ]),
                // QR 扫码区 11rem 占位
                h('div', { style: 'display:flex; justify-content:center;' }, [
                    h('div', {
                        style: 'position:relative; display:grid; place-items:center; width:11rem; height:11rem; background:hsl(var(--card)); border:1px solid hsl(var(--border)); border-radius:calc(var(--radius) * 0.72);',
                    }, [
                        qrUrl.value
                            ? h('img', {
                                src: qrUrl.value,
                                style: 'width:10rem; height:10rem; border-radius:calc(var(--radius) * 0.5); object-fit:contain;',
                                alt: '登录二维码',
                            })
                            : h('button', {
                                type: 'button',
                                onClick: startQrLogin,
                                style: 'display:grid; place-items:center; gap:0.5rem; width:100%; height:100%; background:transparent; border:none; cursor:pointer; color:hsl(var(--muted-foreground)); font-size:0.875rem;',
                            }, [
                                h(Icon, { name: 'mouse-pointer-click', size: '2rem' }),
                                h('span', '点击生成二维码'),
                            ]),
                    ]),
                ]),
                // 状态文字
                status.value
                    ? h('div', {
                        style: 'display:flex; align-items:center; justify-content:center; gap:0.5rem;',
                    }, [
                        polling.value
                            ? h('span', {
                                style: 'display:inline-block; width:0.5rem; height:0.5rem; border-radius:999px; background:hsl(var(--secondary));',
                            })
                            : null,
                        h('span', {
                            style: 'font-size:0.875rem; font-weight:500; color:hsl(var(--foreground));',
                        }, status.value),
                    ])
                    : null,
                h('p', {
                    style: 'font-size:0.75rem; text-align:center; color:hsl(var(--muted-foreground)); margin:0;',
                }, '二维码有效期 3 分钟 · 可刷新'),
            ]),
        ]);
    },
});

// ── 3. AccountLlmTab ──
const AccountLlmTab = defineComponent({
    name: 'AccountLlmTab',
    props: { account: Object },
    setup(props) {
        const selected = ref(props.account.llm_id || '');
        const saving = ref(false);

        const llmOptions = computed(() => [
            { value: '', label: '使用默认模型' },
            ...appState.llmProviders.map(p => ({
                value: p.id,
                label: `${p.name} (${p.model})${p.enabled ? '' : ' [已禁用]'}`,
            })),
        ]);

        async function save() {
            saving.value = true;
            try {
                await api.accounts.bindLlm(props.account.id, selected.value);
                showToast('对话模型绑定已更新', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('绑定失败: ' + e.message, 'error');
            } finally {
                saving.value = false;
            }
        }

        return () => h('article', {
            class: 'grid gap-3',
            style: detailCardStyle,
        }, [
            h('div', { class: 'card-header' }, [
                h('div', { class: 'grid gap-1' }, [
                    h('span', { class: 'eyebrow' }, '模型配置'),
                    h('h2', { style: detailHeadingStyle }, '模型绑定'),
                ]),
            ]),
            h('div', { class: 'card-body', style: detailCardBodyStyle }, [
                h('p', {
                    style: 'margin:0; font-size:0.88rem; color:hsl(var(--muted-foreground));',
                }, '选择此账号使用的对话模型服务商。留空则使用默认模型。'),
                h(FormSelect, {
                    label: '对话模型服务商',
                    modelValue: selected.value,
                    'onUpdate:modelValue': (v) => selected.value = v,
                    options: llmOptions.value,
                    id: 'llm-provider', name: 'llm-provider',
                }),
                props.account.fallback_reason
                    ? h('div', { class: 'form-error' }, '回退原因：' + props.account.fallback_reason)
                    : null,
                h('div', {}, [
                    primaryBtn('保存', { loading: saving.value, onClick: save }),
                ]),
            ]),
        ]);
    },
});

// ── 4. AccountPersonaTab ──
const AccountPersonaTab = defineComponent({
    name: 'AccountPersonaTab',
    props: { account: Object },
    setup(props) {
        const selectedProfile = ref(props.account.profile_id || '');
        const saving = ref(false);
        const switching = ref(false);
        const profiles = ref([]);

        async function loadProfiles() {
            try { profiles.value = await api.accounts.profiles(); }
            catch (e) { showToast('加载人格组失败: ' + e.message, 'error'); }
        }

        async function loadPersonas() {
            if (!appState.personasLoaded) {
                try {
                    appState.personas = await api.personas.list();
                    appState.personasLoaded = true;
                } catch (e) { /* 静默失败 */ }
            }
        }

        onMounted(() => {
            loadProfiles();
            loadPersonas();
        });

        const profileOptions = computed(() => [
            { value: '', label: '不绑定' },
            ...profiles.value.map(p => ({ value: p.id, label: p.name })),
        ]);

        async function bindProfile() {
            saving.value = true;
            try {
                await api.accounts.bindPersona(props.account.id, { profile_id: selectedProfile.value });
                showToast('人格组绑定已更新', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('绑定失败: ' + e.message, 'error');
            } finally { saving.value = false; }
        }

        async function activatePersona(personaId) {
            switching.value = true;
            try {
                await api.accounts.switchPersona(props.account.id, personaId);
                showToast('人格切换成功', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('切换失败: ' + e.message, 'error');
            } finally { switching.value = false; }
        }

        return () => h('article', {
            class: 'grid gap-3',
            style: detailCardStyle,
        }, [
            h('div', { class: 'card-header' }, [
                h('div', { class: 'grid gap-1' }, [
                    h('span', { class: 'eyebrow' }, '人格配置'),
                    h('h2', { style: detailHeadingStyle }, '角色绑定'),
                ]),
            ]),
            h('div', { class: 'card-body', style: 'display:grid; gap:calc(var(--spacing) * 4);' }, [
                // 人格组绑定
                h('div', { style: 'display:grid; gap:calc(var(--spacing) * 3);' }, [
                    h(FormSelect, {
                        label: '人格组',
                        modelValue: selectedProfile.value,
                        'onUpdate:modelValue': (v) => selectedProfile.value = v,
                        options: profileOptions.value,
                        id: 'persona-profile', name: 'persona-profile',
                        hint: '绑定人格组后，账号可在该组内的人格间切换',
                    }),
                    h('div', {}, [
                        primaryBtn('保存', { loading: saving.value, onClick: bindProfile }),
                    ]),
                ]),
                // 人格列表 — 每项一张小 Card
                appState.personas.length > 0
                    ? h('div', { style: 'display:grid; gap:calc(var(--spacing) * 2);' }, [
                        ...appState.personas.map(p => {
                            const isActive = props.account.persona_id === p.id;
                            return h('article', {
                                key: p.id,
                                style: `display:grid; gap:calc(var(--spacing) * 1); padding:calc(var(--spacing) * 3); background:hsl(var(--card)); border:1px solid ${isActive ? 'hsl(var(--accent))' : 'hsl(var(--border))'}; border-radius:calc(var(--radius) * 0.72);${isActive ? ' box-shadow: 0 0 0 1px hsl(var(--accent) / 0.3);' : ''}`,
                            }, [
                                h('div', {
                                    style: 'display:flex; align-items:flex-start; justify-content:space-between; gap:calc(var(--spacing) * 2);',
                                }, [
                                    h('div', { style: 'display:grid; gap:calc(var(--spacing) * 1); min-width:0;' }, [
                                        h('span', {
                                            style: 'font-size:0.97rem; font-weight:500; color:hsl(var(--foreground));',
                                        }, p.name || p.id),
                                        p.description
                                            ? h('span', {
                                                style: 'font-size:0.82rem; color:hsl(var(--muted-foreground)); line-height:1.5;',
                                            }, p.description)
                                            : null,
                                    ]),
                                    isActive
                                        ? h(Badge, { type: 'success' }, () => '当前')
                                        : h('button', {
                                            type: 'button',
                                            class: ['btn', 'ghost', 'btn-sm', switching.value ? 'btn-loading' : ''].filter(Boolean).join(' '),
                                            disabled: switching.value,
                                            onClick: () => activatePersona(p.id),
                                        }, '激活'),
                                ]),
                            ]);
                        }),
                    ])
                    : h(EmptyState, {
                        icon: 'star',
                        title: '暂无人格',
                        desc: '请先在人格管理页面创建人格',
                    }),
            ]),
        ]);
    },
});

// ── 5. AccountStatusTab（单账号：无「设为默认」）──
const AccountStatusTab = defineComponent({
    name: 'AccountStatusTab',
    props: { account: Object },
    setup(props) {
        const operating = ref(false);

        async function start() {
            operating.value = true;
            try {
                await api.accounts.start(props.account.id);
                showToast('账号已启动', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('启动失败: ' + e.message, 'error');
            } finally { operating.value = false; }
        }

        async function stop() {
            operating.value = true;
            try {
                await api.accounts.stop(props.account.id);
                showToast('账号已停止', 'info');
                await refreshAccounts();
            } catch (e) {
                showToast('停止失败: ' + e.message, 'error');
            } finally { operating.value = false; }
        }

        return () => h('div', { style: 'display:grid; gap:calc(var(--spacing) * 4);' }, [
            // 运行状态 Card
            h('article', {
                class: 'grid gap-3',
                style: detailCardStyle,
            }, [
                h('div', { class: 'card-header' }, [
                    h('div', { class: 'grid gap-1' }, [
                        h('span', { class: 'eyebrow' }, '运行状态'),
                        h('h2', { style: detailHeadingStyle }, '账号运行'),
                    ]),
                ]),
                h('div', { class: 'card-body', style: detailCardBodyStyle }, [
                    h('div', {
                        style: 'display:flex; align-items:center; gap:calc(var(--spacing) * 2);',
                    }, [
                        h(StatusDot, {
                            status: (props.account.running || props.account.is_running) ? 'online' : 'offline',
                            label: (props.account.running || props.account.is_running) ? '运行中' : '已停止',
                        }),
                    ]),
                    props.account.last_error
                        ? h('div', { class: 'form-error' }, '最后错误: ' + props.account.last_error)
                        : null,
                ]),
            ]),
            // 底部按钮区：启动 / 停止（单账号无「设为默认」）
            h('div', {
                style: 'display:flex; align-items:center; gap:calc(var(--spacing) * 2); flex-wrap:wrap;',
            }, [
                primaryBtn('启动', {
                    loading: operating.value,
                    disabled: !!(props.account.running || props.account.is_running),
                    onClick: start,
                }),
                ghostBtn('停止', {
                    loading: operating.value,
                    disabled: !(props.account.running || props.account.is_running),
                    onClick: stop,
                }),
            ]),
        ]);
    },
});

// ═══ 账号详情页主组件 ═══
export const AccountDetailPage = defineComponent({
    name: 'AccountDetailPage',
    props: { route: Object },
    setup(props) {
        const accountId = computed(() => props.route.params.id);
        const account = computed(() =>
            appState.accounts.find(a => a.id === accountId.value) || null
        );
        const activeTab = ref('info');
        const tabs = [
            { id: 'info', label: '信息' },
            { id: 'login', label: '登录' },
            { id: 'llm', label: '模型' },
            { id: 'persona', label: '人格' },
            { id: 'status', label: '状态' },
        ];

        async function saveInfo(data) {
            try {
                await api.accounts.update(accountId.value, data);
                showToast('账号信息已保存', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('保存失败: ' + e.message, 'error');
            }
        }

        onMounted(() => { if (!appState.accountsLoaded) refreshAccounts(); });

        return () => {
            // 账号列表尚未加载完：显示 Loading
            if (!appState.accountsLoaded) {
                return h(Loading);
            }
            // 已加载但仍找不到：空状态，避免永久转圈
            if (!account.value) {
                return h('div', { style: 'display:grid; gap:calc(var(--spacing) * 4);' }, [
                    h('div', {
                        style: 'display:flex; align-items:center; gap:calc(var(--spacing) * 3); flex-wrap:wrap;',
                    }, [
                        h('button', {
                            type: 'button',
                            class: 'btn ghost',
                            onClick: () => navigate('/accounts'),
                        }, [
                            h(Icon, { name: 'chevron-right', size: '1rem', style: 'transform: scaleX(-1);' }),
                            h('span', '返回'),
                        ]),
                    ]),
                    h(EmptyState, {
                        icon: 'user',
                        title: '账号不存在',
                        desc: `未找到账号 ${accountId.value || ''}，可能已被删除`,
                    }),
                ]);
            }
            return h('div', { style: 'display:grid; gap:calc(var(--spacing) * 4);' }, [
                // 顶部：返回按钮 + 账号标题
                h('div', {
                    style: 'display:flex; align-items:center; gap:calc(var(--spacing) * 3); flex-wrap:wrap;',
                }, [
                    h('button', {
                        type: 'button',
                        class: 'btn ghost',
                        onClick: () => navigate('/accounts'),
                    }, [
                        h(Icon, { name: 'chevron-right', size: '1rem', style: 'transform: scaleX(-1);' }),
                        h('span', '返回'),
                    ]),
                    h('div', {
                        style: 'display:flex; align-items:center; gap:calc(var(--spacing) * 2); min-width:0;',
                    }, [
                        h(StatusDot, {
                            status: (account.value.running || account.value.is_running) ? 'online' : 'offline',
                        }),
                        h('h2', {
                            style: 'margin:0; font-size:1.5rem; font-weight:600; line-height:1.2; text-wrap:balance; word-break:keep-all; overflow-wrap:break-word;',
                        }, account.value.nickname || account.value.name || account.value.id),
                    ]),
                ]),
                // Tab 切换
                h('div', { class: 'tabs', role: 'tablist' },
                    tabs.map(t => h('button', {
                        type: 'button',
                        class: ['tab', activeTab.value === t.id ? 'active' : ''].filter(Boolean).join(' '),
                        role: 'tab',
                        'aria-selected': activeTab.value === t.id,
                        onClick: () => activeTab.value = t.id,
                    }, t.label))
                ),
                // Tab 内容（单账号：无删除入口）
                activeTab.value === 'info' ? h(AccountInfoTab, { account: account.value, onSave: saveInfo }) : null,
                activeTab.value === 'login' ? h(AccountLoginTab, { account: account.value }) : null,
                activeTab.value === 'llm' ? h(AccountLlmTab, { account: account.value }) : null,
                activeTab.value === 'persona' ? h(AccountPersonaTab, { account: account.value }) : null,
                activeTab.value === 'status' ? h(AccountStatusTab, { account: account.value }) : null,
            ]);
        };
    },
});
