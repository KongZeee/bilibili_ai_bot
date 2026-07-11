// components/llm.js - LLM Provider 管理（带关联账号视图，Golden Time 设计稿）
const { defineComponent, h, ref, computed, onMounted } = window.Vue;
import { api } from '../api.js';
import { appState, refreshLlm, refreshAccounts, showToast } from '../state.js';
import { Button, Badge, Modal, FormInput, EmptyState, Loading, Icon } from './common.js';

export const LlmListPage = defineComponent({
    name: 'LlmListPage',
    setup() {
        const showAdd = ref(false);
        const adding = ref(false);
        const form = ref({ id: '', name: '', api_key: '', base_url: '', model: '', max_tokens: 1024, temperature: 0.8, enabled: true });

        // 计算每个 LLM 被哪些账号引用
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

        async function addProvider() {
            adding.value = true;
            try {
                await api.llm.create(form.value);
                showToast('LLM Provider 添加成功', 'success');
                showAdd.value = false;
                form.value = { id: '', name: '', api_key: '', base_url: '', model: '', max_tokens: 1024, temperature: 0.8, enabled: true };
                await refreshLlm();
            } catch (e) {
                showToast('添加失败: ' + e.message, 'error');
            } finally { adding.value = false; }
        }

        async function deleteProvider(id) {
            const usage = llmUsage.value[id] || [];
            const force = usage.length > 0;
            if (force) {
                if (!confirm(`此 LLM 被 ${usage.length} 个账号使用，删除将清除引用并回退到默认。继续？`)) return;
            } else {
                if (!confirm('确定删除此 LLM Provider？')) return;
            }
            try {
                await api.llm.delete(id, force);
                showToast('已删除', 'success');
                await Promise.all([refreshLlm(), refreshAccounts()]);
            } catch (e) {
                showToast('删除失败: ' + e.message, 'error');
            }
        }

        async function testProvider(id) {
            try {
                showToast('测试中...', 'info');
                await api.llm.test(id);
                showToast('连接成功', 'success');
            } catch (e) {
                showToast('连接失败: ' + e.message, 'error');
            }
        }

        async function setDefault(id) {
            try {
                await api.llm.setDefault(id);
                showToast('已设为默认', 'success');
                await refreshLlm();
            } catch (e) {
                showToast('设置失败: ' + e.message, 'error');
            }
        }

        onMounted(() => {
            if (!appState.llmLoaded) refreshLlm();
            if (!appState.accountsLoaded) refreshAccounts();
        });

        const tableGrid = 'minmax(0, 1.2fr) minmax(0, 1fr) 8rem 7rem 12rem';

        return () => h('div', { class: 'view-frame' }, [
            // ═══ hero-band：LLM 统计面板 + 添加按钮 ═══
            h('section', {
                class: 'grid gap-3',
                style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
            }, [
                // 左侧：hero-panel LLM 统计
                h('div', { class: 'hero-panel' }, [
                    h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                        h('span', { class: 'eyebrow' }, 'LLM 管理'),
                        h(Badge, { type: 'info' }, () => 'Provider'),
                    ]),
                    h('h2', {
                        style: 'margin:0; font-size:1.65rem; line-height:1.1; text-wrap:balance; word-break:keep-all;',
                    }, 'LLM Provider'),
                    h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                        h('span', {
                            style: 'font-size:2.4rem; font-weight:500; line-height:1; font-variant-numeric:tabular-nums;',
                        }, String(appState.llmProviders.length || 0)),
                        h('span', { class: 'muted m-0', style: 'font-size:0.9rem;' }, '个 Provider'),
                    ]),
                    h('p', { class: 'muted m-0' }, '管理大语言模型服务商、API 密钥与默认 Provider'),
                ]),
                // 右侧：添加操作 Card
                h('article', {
                    class: 'grid gap-3',
                    style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                }, [
                    h('div', { class: 'card-header' }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '快速操作'),
                            h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '新增 Provider'),
                        ]),
                    ]),
                    h('div', { class: 'card-body grid gap-2' }, [
                        h('div', { class: 'flex items-center justify-between' }, [
                            h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '默认 Provider'),
                            h(Badge, { type: 'success' }, () => {
                                const def = appState.llmProviders.find(p => p.is_default);
                                return def ? def.name : '未设置';
                            }),
                        ]),
                        h('div', { class: 'flex items-center justify-between' }, [
                            h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '已启用'),
                            h('span', {
                                style: 'font-size:0.88rem; font-variant-numeric: tabular-nums;',
                            }, String(appState.llmProviders.filter(p => p.enabled).length || 0)),
                        ]),
                        h(Button, {
                            type: 'primary',
                            onClick: () => showAdd.value = true,
                        }, () => '+ 添加 Provider'),
                    ]),
                ]),
            ]),

            // ═══ LLM Provider 列表表格 ═══
            h('article', {
                class: 'grid gap-3',
                style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
            }, [
                h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                    h('div', { class: 'grid gap-1' }, [
                        h('span', { class: 'eyebrow' }, 'Provider 列表'),
                        h('h2', {
                            style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;',
                        }, 'LLM Provider 管理'),
                    ]),
                ]),
                !appState.llmLoaded
                    ? h(Loading)
                    : appState.llmProviders.length === 0
                        ? h(EmptyState, { icon: 'folder', title: '暂无 LLM Provider', desc: '点击右上角添加第一个 Provider' })
                        : h('div', { class: 'grid', style: 'gap:0; min-width:0;' }, [
                            // 表头
                            h('div', {
                                class: 'grid items-center',
                                style: `grid-template-columns: ${tableGrid}; column-gap: calc(var(--spacing) * 2); padding-bottom: calc(var(--spacing) * 2); border-bottom: 1px solid hsl(var(--border)); color: hsl(var(--muted-foreground)); font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.14em;`,
                            }, [
                                h('span', { class: 'whitespace-nowrap' }, '名称'),
                                h('span', { class: 'whitespace-nowrap' }, '模型'),
                                h('span', { class: 'whitespace-nowrap' }, '状态'),
                                h('span', { class: 'whitespace-nowrap' }, '默认'),
                                h('span', { class: 'whitespace-nowrap' }, '操作'),
                            ]),
                            // 数据行
                            ...appState.llmProviders.map(p => h('div', {
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
                                    // 关联账号
                                    (llmUsage.value[p.id] || []).length > 0
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
                                        p.vision_enabled && h(Badge, { type: 'success', size: 'sm' }, () => 'Vision'),
                                        p.embedding_enabled && h(Badge, { type: 'info', size: 'sm' }, () => 'Embedding'),
                                        !p.has_api_key && h(Badge, { type: 'warning', size: 'sm' }, () => '无密钥'),
                                    ].filter(Boolean)),
                                ]),
                                h('span', {
                                    class: ['badge',
                                        p.enabled ? 'badge-success' : 'badge-danger'].join(' '),
                                }, p.enabled ? '已启用' : '已禁用'),
                                h('span', {
                                    class: ['badge', p.is_default ? 'badge-success' : 'badge-muted'].join(' '),
                                }, p.is_default ? '默认' : '-'),
                                h('div', { class: 'flex items-center gap-1 flex-wrap' }, [
                                    h('button', {
                                        class: 'btn btn-sm ghost',
                                        onClick: () => testProvider(p.id),
                                    }, '测试'),
                                    !p.is_default && h('button', {
                                        class: 'btn btn-sm primary',
                                        onClick: () => setDefault(p.id),
                                    }, '设默认'),
                                    h('button', {
                                        class: 'btn btn-sm btn-danger',
                                        onClick: () => deleteProvider(p.id),
                                    }, '删除'),
                                ].filter(Boolean)),
                            ])),
                        ]),
            ]),

            // ═══ 添加 Modal ═══
            h(Modal, {
                modelValue: showAdd.value,
                'onUpdate:modelValue': (v) => showAdd.value = v,
                title: '添加 LLM Provider',
                width: '600px',
            }, {
                default: () => h('div', [
                    h(FormInput, { label: 'ID', modelValue: form.value.id,
                        'onUpdate:modelValue': (v) => form.value.id = v, placeholder: '如: siliconflow' }),
                    h(FormInput, { label: '名称', modelValue: form.value.name,
                        'onUpdate:modelValue': (v) => form.value.name = v }),
                    h(FormInput, { label: 'API Key', modelValue: form.value.api_key,
                        'onUpdate:modelValue': (v) => form.value.api_key = v, type: 'password' }),
                    h(FormInput, { label: 'Base URL', modelValue: form.value.base_url,
                        'onUpdate:modelValue': (v) => form.value.base_url = v }),
                    h(FormInput, { label: '模型', modelValue: form.value.model,
                        'onUpdate:modelValue': (v) => form.value.model = v }),
                ]),
                footer: () => [
                    h(Button, { onClick: () => showAdd.value = false }, () => '取消'),
                    h(Button, { type: 'primary', loading: adding.value, onClick: addProvider }, () => '添加'),
                ],
            }),
        ]);
    },
});
