// components/accounts.js - 账号列表和详情页
const { defineComponent, h, ref, computed, onMounted } = window.Vue;
import { api } from '../api.js';
import { appState, refreshAccounts, showToast } from '../state.js';
import { Card, Button, Badge, Modal, FormInput, FormSelect, Toggle, EmptyState, Loading, Icon } from './common.js';

export const AccountListPage = defineComponent({
    name: 'AccountListPage',
    setup() {
        const showAdd = ref(false);
        const adding = ref(false);
        const form = ref({ id: '', name: '', sessdata: '', bili_jct: '', dede_user_id: '', buvid3: '', llm_id: '', profile_id: '' });

        async function addAccount() {
            adding.value = true;
            try {
                await api.accounts.create(form.value);
                showToast('账号添加成功', 'success');
                showAdd.value = false;
                form.value = { id: '', name: '', sessdata: '', bili_jct: '', dede_user_id: '', buvid3: '', llm_id: '', profile_id: '' };
                await refreshAccounts();
            } catch (e) {
                showToast('添加失败: ' + e.message, 'error');
            } finally {
                adding.value = false;
            }
        }

        onMounted(() => { if (!appState.accountsLoaded) refreshAccounts(); });

        const llmOptions = computed(() => [
            { value: '', label: '默认 LLM' },
            ...appState.llmProviders.map(p => ({ value: p.id, label: `${p.name} (${p.model})` })),
        ]);

        const profileOptions = computed(() => [
            { value: '', label: '不绑定' },
            // 从 accounts.profiles API 获取
        ]);

        return () => h('div', [
            h(Card, { title: '账号列表' }, {
                action: () => h(Button, { type: 'primary', onClick: () => showAdd.value = true }, () => '+ 添加账号'),
                default: () => !appState.accountsLoaded
                    ? h(Loading)
                    : appState.accounts.length === 0
                        ? h(EmptyState, { icon: 'accounts', title: '暂无账号', desc: '点击右上角添加账号' })
                        : h('div', { class: 'status-grid' },
                            appState.accounts.map(acc => h('div', {
                                class: 'status-card',
                                onClick: () => window.location.hash = `#/accounts/${acc.id}`,
                            }, [
                                h('div', { class: 'flex items-center justify-between' }, [
                                    h('div', { class: 'flex items-center gap-2' }, [
                                        h(Icon, { name: 'proactive', size: 20, style: acc.running ? 'color:var(--success)' : 'color:var(--danger)' }),
                                        h('span', { style: 'font-size:16px;font-weight:600' }, acc.name || acc.id),
                                    ]),
                                    acc.is_default ? h(Badge, { type: 'info' }, () => '默认') : null,
                                ]),
                                h('div', { class: 'mt-2', style: 'font-size:13px;color:var(--text-secondary)' }, [
                                    h('div', `ID: ${acc.id}`),
                                    h('div', `LLM: ${acc.effective_llm_id || acc.llm_id || '默认'}`),
                                    h('div', `人格: ${acc.persona_id || '未绑定'}`),
                                    acc.authenticated ? h(Badge, { type: 'success', size: 'sm' }, () => '已认证')
                                                      : h(Badge, { type: 'warning', size: 'sm' }, () => '未认证'),
                                ]),
                            ])
                        ),
            }),
            // 添加账号弹窗
            h(Modal, {
                modelValue: showAdd.value,
                'onUpdate:modelValue': (v) => showAdd.value = v,
                title: '添加账号',
                width: '600px',
            }, {
                default: () => h('div', [
                    h(FormInput, { label: '账号 ID', modelValue: form.value.id,
                        'onUpdate:modelValue': (v) => form.value.id = v,
                        placeholder: '如: main, sub1' }),
                    h(FormInput, { label: '名称', modelValue: form.value.name,
                        'onUpdate:modelValue': (v) => form.value.name = v,
                        placeholder: '显示名称' }),
                    h(FormInput, { label: 'SESSDATA', modelValue: form.value.sessdata,
                        'onUpdate:modelValue': (v) => form.value.sessdata = v,
                        type: 'password' }),
                    h(FormInput, { label: 'bili_jct', modelValue: form.value.bili_jct,
                        'onUpdate:modelValue': (v) => form.value.bili_jct = v,
                        type: 'password' }),
                    h(FormInput, { label: 'UID', modelValue: form.value.dede_user_id,
                        'onUpdate:modelValue': (v) => form.value.dede_user_id = v }),
                    h(FormInput, { label: 'buvid3', modelValue: form.value.buvid3,
                        'onUpdate:modelValue': (v) => form.value.buvid3 = v,
                        type: 'password' }),
                    h(FormSelect, { label: 'LLM', modelValue: form.value.llm_id,
                        'onUpdate:modelValue': (v) => form.value.llm_id = v,
                        options: llmOptions.value }),
                ]),
                footer: () => [
                    h(Button, { onClick: () => showAdd.value = false }, () => '取消'),
                    h(Button, { type: 'primary', loading: adding.value, onClick: addAccount }, () => '添加'),
                ],
            }),
        ]);
    },
});
