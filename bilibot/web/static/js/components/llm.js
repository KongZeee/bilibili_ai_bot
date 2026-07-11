// components/llm.js - LLM Provider 管理（带关联账号视图）
const { defineComponent, h, ref, computed, onMounted } = window.Vue;
import { api } from '../api.js';
import { appState, refreshLlm, refreshAccounts, showToast } from '../state.js';
import { Card, Button, Badge, Modal, FormInput, FormSelect, Toggle, EmptyState, Loading, Icon } from './common.js';

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
                const resp = await api.llm.test(id);
                showToast('连接成功', 'success');
            } catch (e) {
                showToast('连接失败: ' + e.message, 'error');
            }
        }

        onMounted(() => {
            if (!appState.llmLoaded) refreshLlm();
            if (!appState.accountsLoaded) refreshAccounts();
        });

        return () => h('div', [
            h(Card, { title: 'LLM Provider 列表' }, {
                action: () => h(Button, { type: 'primary', onClick: () => showAdd.value = true }, () => '+ 添加 Provider'),
                default: () => !appState.llmLoaded
                    ? h(Loading)
                    : appState.llmProviders.length === 0
                        ? h(EmptyState, { icon: 'llm', title: '暂无 LLM Provider', desc: '点击右上角添加' })
                        : appState.llmProviders.map(p => h(Card, { key: p.id, bordered: true }, {
                            default: () => h('div', [
                                h('div', { class: 'flex items-center justify-between mb-2' }, [
                                    h('div', { class: 'flex items-center gap-2' }, [
                                        h('span', { style: 'font-size:16px;font-weight:600' }, p.name),
                                        p.is_default ? h(Badge, { type: 'info' }, () => '默认') : null,
                                        !p.enabled ? h(Badge, { type: 'danger' }, () => '已禁用') : null,
                                        !p.has_api_key ? h(Badge, { type: 'warning' }, () => '无密钥') : null,
                                    ]),
                                    h('div', { class: 'flex gap-2' }, [
                                        h(Button, { size: 'sm', onClick: () => testProvider(p.id) }, () => '测试'),
                                        h(Button, { size: 'sm', type: 'danger', onClick: () => deleteProvider(p.id) }, () => '删除'),
                                    ]),
                                ]),
                                h('div', { class: 'text-muted', style: 'font-size:13px' }, [
                                    h('div', `ID: ${p.id}`),
                                    h('div', `模型: ${p.model}`),
                                    h('div', `Base URL: ${p.base_url}`),
                                    p.vision_enabled ? h(Badge, { type: 'success', size: 'sm' }, () => 'Vision') : null,
                                    p.embedding_enabled ? h(Badge, { type: 'info', size: 'sm' }, () => 'Embedding') : null,
                                ]),
                                // 关联账号视图
                                (llmUsage.value[p.id] || []).length > 0
                                    ? h('div', { class: 'mt-4', style: 'padding-top:12px;border-top:1px solid var(--outline-variant)' }, [
                                        h('div', { class: 'text-muted', style: 'font-size:12px;margin-bottom:8px' }, '被以下账号使用:'),
                                        h('div', { class: 'flex gap-2', style: 'flex-wrap:wrap' },
                                            llmUsage.value[p.id].map(acc =>
                                                h(Badge, { type: 'info', size: 'sm' }, () => `${acc.name || acc.id}`)
                                            )
                                        ),
                                    ])
                                    : null,
                            ]),
                        })),
            }),
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
