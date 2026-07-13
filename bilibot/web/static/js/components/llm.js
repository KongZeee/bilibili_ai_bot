// components/llm.js - 模型 Provider 管理（V3 多 Tab 布局，Golden Time 设计稿）
const { defineComponent, h, ref, computed, onMounted } = window.Vue;
import { api } from '../api.js';
import { appState, refreshAccounts, showToast } from '../state.js';
import { Button, Badge, Modal, FormInput, Toggle, EmptyState, Loading, ConfirmModal, createConfirmHelper } from './common.js';

// Tab 配置：type 与后端 PROVIDER_TYPES 对齐
const TABS = [
    { type: 'chat',      label: '对话模型',  eyebrow: 'Chat',       desc: '主动回复 / 动态 / 记忆提取' },
    { type: 'vision',    label: '视觉模型',  eyebrow: 'Vision',     desc: '视频视觉轨理解' },
    { type: 'embedding', label: 'Embedding', eyebrow: 'Embedding',  desc: '记忆向量检索' },
    { type: 'asr',       label: 'ASR',       eyebrow: 'ASR',        desc: '视频音频轨转写' },
    { type: 'image',     label: '文生图',    eyebrow: 'Image',      desc: '动态配图生成' },
];

// 默认表单值（按类型）
function defaultForm(type) {
    const base = {
        id: '', name: '', api_key: '', base_url: '', model: '', enabled: true,
    };
    if (type === 'chat') {
        base.max_tokens = 1024;
        base.temperature = 0.8;
    }
    if (type === 'image') {
        base.default_size = '1024x768';
        base.timeout = 120;
    }
    return base;
}

export const LlmListPage = defineComponent({
    name: 'LlmListPage',
    setup() {
        const activeTab = ref('chat');
        const loading = ref(false);
        const overview = ref({ routing: {}, features: {}, local_whisper: {} });

        // 添加 Modal
        const showAdd = ref(false);
        const adding = ref(false);
        const form = ref(defaultForm(activeTab.value));

        // 编辑 Modal
        const showEdit = ref(false);
        const saving = ref(false);
        const editingId = ref('');
        const editForm = ref(defaultForm(activeTab.value));

        const { state: confirmState, showConfirm, handleConfirm } = createConfirmHelper();

        // 计算每个 chat Provider 被哪些账号引用（仅 chat tab 显示）
        const llmUsage = computed(() => {
            const map = {};
            for (const acc of appState.accounts) {
                const llmId = acc.llm_id || acc.configured_llm_id;
                if (llmId) {
                    if (!map[llmId]) map[llmId] = [];
                    map[llmId].push(acc);
                }
            }
            return map;
        });

        const currentFeature = computed(() => overview.value.features?.[activeTab.value] || null);
        const currentProviders = computed(() => currentFeature.value?.providers || []);
        const currentRoutedId = computed(() => currentFeature.value?.routed_provider_id || '');

        async function loadOverview() {
            loading.value = true;
            try {
                overview.value = await api.modelRouting.getOverview();
            } catch (e) {
                showToast('加载模型路由失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        function switchTab(type) {
            if (activeTab.value === type) return;
            activeTab.value = type;
            form.value = defaultForm(type);
        }

        function openAdd() {
            form.value = defaultForm(activeTab.value);
            showAdd.value = true;
        }

        async function addProvider() {
            adding.value = true;
            try {
                const payload = { ...form.value };
                if (!payload.id) delete payload.id;
                await api.modelRouting.addProvider(activeTab.value, payload);
                showToast('Provider 添加成功', 'success');
                showAdd.value = false;
                await loadOverview();
            } catch (e) {
                showToast('添加失败: ' + e.message, 'error');
            } finally { adding.value = false; }
        }

        function openEdit(p) {
            editingId.value = p.id;
            const f = defaultForm(activeTab.value);
            f.name = p.name || '';
            f.model = p.model || '';
            f.base_url = p.base_url || '';
            f.api_key = ''; // 不回显，留空不修改
            f.enabled = p.enabled ?? true;
            if (activeTab.value === 'chat') {
                f.max_tokens = p.max_tokens ?? 1024;
                f.temperature = p.temperature ?? 0.8;
            }
            if (activeTab.value === 'image') {
                f.default_size = p.default_size || '1024x768';
                f.timeout = p.timeout ?? 120;
            }
            editForm.value = f;
            showEdit.value = true;
        }

        async function saveEdit() {
            saving.value = true;
            try {
                const payload = {
                    name: editForm.value.name,
                    model: editForm.value.model,
                    base_url: editForm.value.base_url,
                    enabled: editForm.value.enabled,
                };
                if (activeTab.value === 'chat') {
                    payload.max_tokens = Number(editForm.value.max_tokens) || 1024;
                    payload.temperature = Number(editForm.value.temperature) || 0.8;
                }
                if (activeTab.value === 'image') {
                    payload.default_size = editForm.value.default_size || '1024x768';
                    payload.timeout = Number(editForm.value.timeout) || 120;
                }
                // 仅当用户输入了新 key 时才发送，避免空串覆盖
                if (editForm.value.api_key) payload.api_key = editForm.value.api_key;

                await api.modelRouting.updateProvider(activeTab.value, editingId.value, payload);
                showToast('Provider 已更新', 'success');
                showEdit.value = false;
                await loadOverview();
            } catch (e) {
                showToast('更新失败: ' + e.message, 'error');
            } finally { saving.value = false; }
        }

        function deleteProvider(p) {
            let message = '确定删除此 Provider？';
            // chat 类型：检查账号引用
            if (activeTab.value === 'chat') {
                const usage = llmUsage.value[p.id] || [];
                if (usage.length > 0) {
                    message = `此 Provider 被 ${usage.length} 个账号引用，删除后引用将失效。继续？`;
                }
            }
            showConfirm({
                title: '确认删除',
                message: message,
                confirmText: '删除',
                danger: true,
                action: async () => {
                    try {
                        await api.modelRouting.deleteProvider(activeTab.value, p.id);
                        showToast('已删除', 'success');
                        await loadOverview();
                        if (activeTab.value === 'chat') await refreshAccounts();
                    } catch (e) {
                        showToast('删除失败: ' + e.message, 'error');
                    }
                },
            });
        }

        async function testProvider(p) {
            try {
                showToast('测试中...', 'info');
                await api.modelRouting.testProvider(activeTab.value, p.id);
                showToast('连接成功', 'success');
            } catch (e) {
                showToast('连接失败: ' + e.message, 'error');
            }
        }

        async function setRouting(p) {
            try {
                await api.modelRouting.updateRouting({ [activeTab.value]: p.id });
                showToast(`已将该功能路由到 ${p.name || p.id}`, 'success');
                await loadOverview();
            } catch (e) {
                showToast('路由设置失败: ' + e.message, 'error');
            }
        }

        onMounted(() => {
            loadOverview();
            if (!appState.accountsLoaded) refreshAccounts();
        });

        const tableGrid = 'minmax(0, 1.2fr) minmax(0, 1fr) 7rem 8rem 12rem';
        const cardStyle = 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);';

        // 渲染添加/编辑 Modal 中按类型差异化的字段
        function renderExtraFields(formVal) {
            const extra = [];
            if (activeTab.value === 'chat') {
                extra.push(
                    h('div', { class: 'grid', style: 'grid-template-columns: 1fr 1fr; gap: calc(var(--spacing) * 2);' }, [
                        h(FormInput, {
                            label: 'Max Tokens', type: 'number',
                            modelValue: formVal.max_tokens,
                            'onUpdate:modelValue': (v) => formVal.max_tokens = v,
                            placeholder: '1024',
                        }),
                        h(FormInput, {
                            label: 'Temperature', type: 'number',
                            modelValue: formVal.temperature,
                            'onUpdate:modelValue': (v) => formVal.temperature = v,
                            placeholder: '0.8',
                        }),
                    ]),
                );
            }
            if (activeTab.value === 'image') {
                extra.push(
                    h('div', { class: 'grid', style: 'grid-template-columns: 1fr 1fr; gap: calc(var(--spacing) * 2);' }, [
                        h(FormInput, {
                            label: '默认尺寸',
                            modelValue: formVal.default_size,
                            'onUpdate:modelValue': (v) => formVal.default_size = v,
                            placeholder: '1024x768',
                        }),
                        h(FormInput, {
                            label: '超时（秒）', type: 'number',
                            modelValue: formVal.timeout,
                            'onUpdate:modelValue': (v) => formVal.timeout = v,
                            placeholder: '120',
                        }),
                    ]),
                );
            }
            return extra;
        }

        // 渲染 Provider 列表表格
        function renderProviderTable() {
            if (loading.value) return h(Loading);
            if (currentProviders.value.length === 0) {
                return h(EmptyState, {
                    icon: 'folder',
                    title: `暂无 ${TABS.find(t => t.type === activeTab.value)?.label || ''} Provider`,
                    desc: '点击右上角添加第一个 Provider',
                });
            }
            return h('div', { class: 'grid', style: 'gap:0; min-width:0;' }, [
                // 表头
                h('div', {
                    class: 'grid items-center',
                    style: `grid-template-columns: ${tableGrid}; column-gap: calc(var(--spacing) * 2); padding-bottom: calc(var(--spacing) * 2); border-bottom: 1px solid hsl(var(--border)); color: hsl(var(--muted-foreground)); font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.14em;`,
                }, [
                    h('span', { class: 'whitespace-nowrap' }, '名称'),
                    h('span', { class: 'whitespace-nowrap' }, '模型'),
                    h('span', { class: 'whitespace-nowrap' }, '状态'),
                    h('span', { class: 'whitespace-nowrap' }, '路由'),
                    h('span', { class: 'whitespace-nowrap' }, '操作'),
                ]),
                // 数据行
                ...currentProviders.value.map(p => {
                    const routed = currentRoutedId.value === p.id;
                    return h('div', {
                        key: p.id,
                        class: 'grid items-center',
                        style: `grid-template-columns: ${tableGrid}; column-gap: calc(var(--spacing) * 2); padding: calc(var(--spacing) * 2.3) 0; border-top: 1px solid hsl(var(--border)); font-size: 0.95rem;`,
                    }, [
                        h('div', { class: 'grid gap-1', style: 'min-width:0;' }, [
                            h('span', { class: 'truncate', style: 'font-weight:500;' }, p.name || p.id),
                            h('span', {
                                class: 'truncate muted',
                                style: 'font-size:0.78rem;',
                            }, `ID: ${p.id}`),
                            // chat 类型：显示关联账号
                            (activeTab.value === 'chat' && (llmUsage.value[p.id] || []).length > 0)
                                ? h('div', { class: 'flex items-center gap-1 flex-wrap', style: 'margin-top:calc(var(--spacing) * 0.5);' }, [
                                    h('span', {
                                        class: 'muted',
                                        style: 'font-size:0.72rem;',
                                    }, '关联:'),
                                    ...(llmUsage.value[p.id] || []).map(acc =>
                                        h(Badge, { type: 'info', size: 'sm' }, () => acc.name || acc.id),
                                    ),
                                ])
                                : null,
                        ]),
                        h('div', { class: 'grid gap-1', style: 'min-width:0;' }, [
                            h('span', { class: 'truncate' }, p.model || '-'),
                            h('span', {
                                class: 'truncate muted',
                                style: 'font-size:0.78rem;',
                            }, p.base_url || '-'),
                            h('div', { class: 'flex items-center gap-1 flex-wrap', style: 'margin-top:calc(var(--spacing) * 0.5);' }, [
                                !p.has_api_key && h(Badge, { type: 'warning', size: 'sm' }, () => '无密钥'),
                            ].filter(Boolean)),
                        ]),
                        h('span', {
                            class: ['badge', p.enabled ? 'badge-success' : 'badge-danger'].join(' '),
                        }, p.enabled ? '已启用' : '已禁用'),
                        h('span', {
                            class: ['badge', routed ? 'badge-success' : 'badge-muted'].join(' '),
                        }, routed ? '已选中' : '-'),
                        h('div', { class: 'flex items-center gap-1 flex-wrap' }, [
                            h('button', {
                                class: 'btn btn-sm primary',
                                onClick: () => openEdit(p),
                            }, '编辑'),
                            h('button', {
                                class: 'btn btn-sm ghost',
                                onClick: () => testProvider(p),
                            }, '测试'),
                            !routed && h('button', {
                                class: 'btn btn-sm ghost',
                                onClick: () => setRouting(p),
                            }, '设为路由'),
                            h('button', {
                                class: 'btn btn-sm btn-danger',
                                onClick: () => deleteProvider(p),
                            }, '删除'),
                        ].filter(Boolean)),
                    ]);
                }),
            ]);
        }

        return () => {
            const tab = TABS.find(t => t.type === activeTab.value) || TABS[0];
            const routedProvider = currentFeature.value?.routed_provider;
            return h('div', { class: 'view-frame' }, [
                // ═══ hero-band：模型管理概览 + 添加按钮 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左侧：hero-panel
                    h('div', { class: 'hero-panel' }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('span', { class: 'eyebrow' }, '模型管理'),
                            h(Badge, { type: 'info' }, () => 'V3 路由'),
                        ]),
                        h('h2', {
                            style: 'margin:0; font-size:1.65rem; line-height:1.1; text-wrap:balance; word-break:keep-all;',
                        }, '模型 Provider'),
                        h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                            h('span', {
                                style: 'font-size:2.4rem; font-weight:500; line-height:1; font-variant-numeric:tabular-nums;',
                            }, String(currentProviders.value.length || 0)),
                            h('span', { class: 'muted m-0', style: 'font-size:0.9rem;' }, `个 ${tab.label} Provider`),
                        ]),
                        h('p', { class: 'muted m-0' }, tab.desc),
                    ]),
                    // 右侧：当前路由 + 添加操作
                    h('article', {
                        class: 'grid gap-3',
                        style: cardStyle + ' align-content: start;',
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '当前路由'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, tab.label + ' 路由'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-2' }, [
                            h('div', { class: 'flex items-center justify-between' }, [
                                h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '已路由 Provider'),
                                h(Badge, { type: routedProvider ? 'success' : 'muted' }, () => routedProvider ? (routedProvider.name || routedProvider.id) : '未设置'),
                            ]),
                            h('div', { class: 'flex items-center justify-between' }, [
                                h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '已启用'),
                                h('span', {
                                    style: 'font-size:0.88rem; font-variant-numeric: tabular-nums;',
                                }, String(currentProviders.value.filter(p => p.enabled).length || 0)),
                            ]),
                            h(Button, {
                                type: 'primary',
                                onClick: openAdd,
                            }, () => `+ 添加 ${tab.label} Provider`),
                        ]),
                    ]),
                ]),

                // ═══ Tab 导航 ═══
                h('div', {
                    class: 'flex items-center gap-1 flex-wrap',
                    style: 'border-bottom: 1px solid hsl(var(--border));',
                    role: 'tablist',
                }, TABS.map(t => {
                    const active = activeTab.value === t.type;
                    return h('button', {
                        key: t.type,
                        role: 'tab',
                        'aria-selected': active ? 'true' : 'false',
                        class: ['btn', 'btn-sm', active ? 'primary' : 'ghost'].join(' '),
                        style: active
                            ? 'border-bottom: 2px solid hsl(var(--accent)); border-radius: calc(var(--radius) * 0.6) calc(var(--radius) * 0.6) 0 0;'
                            : 'opacity: 0.72;',
                        onClick: () => switchTab(t.type),
                    }, t.label);
                })),

                // ═══ Provider 列表 ═══
                h('article', {
                    class: 'grid gap-3',
                    style: cardStyle,
                }, [
                    h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, tab.eyebrow),
                            h('h2', {
                                style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;',
                            }, `${tab.label} Provider 管理`),
                        ]),
                        h(Button, { type: 'primary', size: 'sm', onClick: openAdd }, () => '+ 新增'),
                    ]),
                    renderProviderTable(),
                ]),

                // ═══ 添加 Modal ═══
                h(Modal, {
                    modelValue: showAdd.value,
                    'onUpdate:modelValue': (v) => showAdd.value = v,
                    title: `添加 ${tab.label} Provider`,
                    width: '600px',
                }, {
                    default: () => h('div', { class: 'grid gap-3' }, [
                        h(FormInput, { label: 'ID（可选）', modelValue: form.value.id,
                            'onUpdate:modelValue': (v) => form.value.id = v, placeholder: '留空自动生成，如: siliconflow' }),
                        h(FormInput, { label: '名称', modelValue: form.value.name,
                            'onUpdate:modelValue': (v) => form.value.name = v }),
                        h(FormInput, { label: '模型', modelValue: form.value.model,
                            'onUpdate:modelValue': (v) => form.value.model = v,
                            placeholder: '如 Qwen/Qwen2.5-72B-Instruct' }),
                        h(FormInput, { label: 'Base URL', modelValue: form.value.base_url,
                            'onUpdate:modelValue': (v) => form.value.base_url = v }),
                        h(FormInput, { label: 'API Key', modelValue: form.value.api_key,
                            'onUpdate:modelValue': (v) => form.value.api_key = v, type: 'password' }),
                        ...renderExtraFields(form.value),
                        h('div', { class: 'flex items-center justify-between' }, [
                            h('span', { class: 'eyebrow' }, '启用'),
                            h(Toggle, {
                                modelValue: form.value.enabled,
                                'onUpdate:modelValue': (v) => form.value.enabled = v,
                            }),
                        ]),
                    ]),
                    footer: () => [
                        h(Button, { onClick: () => showAdd.value = false }, () => '取消'),
                        h(Button, { type: 'primary', loading: adding.value, onClick: addProvider }, () => '添加'),
                    ],
                }),

                // ═══ 编辑 Modal ═══
                h(Modal, {
                    modelValue: showEdit.value,
                    'onUpdate:modelValue': (v) => showEdit.value = v,
                    title: `编辑 ${tab.label} Provider — ${editingId.value}`,
                    width: '640px',
                }, {
                    default: () => h('div', { class: 'grid gap-3' }, [
                        h('div', { class: 'grid gap-2' }, [
                            h('span', { class: 'eyebrow' }, '基础配置'),
                            h(FormInput, { label: '名称', modelValue: editForm.value.name,
                                'onUpdate:modelValue': (v) => editForm.value.name = v }),
                            h(FormInput, { label: '模型', modelValue: editForm.value.model,
                                'onUpdate:modelValue': (v) => editForm.value.model = v,
                                placeholder: '如 Qwen/Qwen2.5-72B-Instruct' }),
                            h(FormInput, { label: 'Base URL', modelValue: editForm.value.base_url,
                                'onUpdate:modelValue': (v) => editForm.value.base_url = v }),
                            h(FormInput, { label: 'API Key（留空不修改）', modelValue: editForm.value.api_key,
                                'onUpdate:modelValue': (v) => editForm.value.api_key = v, type: 'password' }),
                        ]),
                        ...renderExtraFields(editForm.value),
                        h('div', { class: 'flex items-center justify-between' }, [
                            h('span', { class: 'eyebrow' }, '启用'),
                            h(Toggle, {
                                modelValue: editForm.value.enabled,
                                'onUpdate:modelValue': (v) => editForm.value.enabled = v,
                            }),
                        ]),
                    ]),
                    footer: () => [
                        h(Button, { onClick: () => showEdit.value = false }, () => '取消'),
                        h(Button, { type: 'primary', loading: saving.value, onClick: saveEdit }, () => '保存'),
                    ],
                }),

                // ═══ 确认对话框 ═══
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
        };
    },
});
