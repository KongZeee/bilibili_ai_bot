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

// 账号详情页 Tab 组件
const AccountInfoTab = defineComponent({
    props: { account: Object },
    emits: ['save', 'delete'],
    setup(props, { emit }) {
        const form = ref({ ...props.account });
        const saving = ref(false);

        return () => h('div', [
            h(FormInput, { label: '账号 ID', modelValue: form.value.id, disabled: true }),
            h(FormInput, { label: '名称', modelValue: form.value.name,
                'onUpdate:modelValue': (v) => form.value.name = v }),
            h(FormInput, { label: 'UID', modelValue: form.value.dede_user_id,
                'onUpdate:modelValue': (v) => form.value.dede_user_id = v }),
            h(FormInput, { label: 'buvid3', modelValue: form.value.buvid3,
                'onUpdate:modelValue': (v) => form.value.buvid3 = v, type: 'password' }),
            h('div', { class: 'flex gap-3 mt-4' }, [
                h(Button, { type: 'primary', loading: saving.value,
                    onClick: async () => { saving.value = true; try { emit('save', form.value); } finally { saving.value = false; } },
                }, () => '保存'),
                h(Button, { type: 'danger', onClick: () => emit('delete') }, () => '删除账号'),
            ]),
        ]);
    },
});

const AccountLoginTab = defineComponent({
    props: { account: Object },
    setup(props) {
        const qrModal = ref(false);
        const qrUrl = ref('');
        const sessionId = ref('');
        const polling = ref(false);
        const status = ref('');

        async function startQrLogin() {
            try {
                const resp = await api.accounts.qrLogin(props.account.id);
                qrUrl.value = resp.qrcode_url;
                sessionId.value = resp.session_id;
                qrModal.value = true;
                polling.value = true;
                pollStatus();
            } catch (e) {
                showToast('启动扫码失败: ' + e.message, 'error');
            }
        }

        async function pollStatus() {
            if (!polling.value || !sessionId.value) return;
            try {
                const resp = await api.accounts.qrPoll(props.account.id, sessionId.value);
                if (resp.code === 0) {
                    status.value = '登录成功';
                    showToast('B站登录成功', 'success');
                    polling.value = false;
                    setTimeout(() => { qrModal.value = false; refreshAccounts(); }, 1500);
                } else if (resp.code === 86090) {
                    status.value = '等待扫码...';
                    setTimeout(pollStatus, 2000);
                } else if (resp.code === 86038) {
                    status.value = '二维码已过期';
                    showToast('二维码过期，请重新生成', 'warning');
                    polling.value = false;
                } else {
                    setTimeout(pollStatus, 2000);
                }
            } catch (e) {
                polling.value = false;
                showToast('轮询失败: ' + e.message, 'error');
            }
        }

        function cancelQr() {
            if (sessionId.value) api.accounts.qrCancel(props.account.id, sessionId.value);
            polling.value = false;
            qrModal.value = false;
        }

        return () => h('div', [
            h(Card, { title: 'B站登录状态' }, () => [
                h('div', { class: 'flex items-center gap-4 mb-4' }, [
                    props.account.authenticated
                        ? h(Badge, { type: 'success' }, () => '已认证')
                        : h(Badge, { type: 'warning' }, () => '未认证'),
                    h(Button, { type: 'primary', onClick: startQrLogin }, () => '扫码登录'),
                ]),
                h('div', { class: 'text-muted', style: 'font-size:13px' },
                    '说明：扫码登录会自动获取 cookie，无需手动填写 SESSDATA/bili_jct'),
            ]),
            h(Modal, {
                modelValue: qrModal.value,
                'onUpdate:modelValue': (v) => { qrModal.value = v; if (!v) cancelQr(); },
                title: '扫码登录',
            }, {
                default: () => h('div', { style: 'text-align:center' }, [
                    qrUrl.value
                        ? h('img', { src: qrUrl.value, style: 'width:240px;height:240px;border-radius:12px' })
                        : h(Loading),
                    h('p', { class: 'mt-4' }, status.value || '请使用B站 App 扫描二维码'),
                ]),
                footer: () => h(Button, { onClick: cancelQr }, () => '关闭'),
            }),
        ]);
    },
});

const AccountLlmTab = defineComponent({
    props: { account: Object },
    setup(props) {
        const selected = ref(props.account.llm_id || '');
        const saving = ref(false);

        const llmOptions = computed(() => [
            { value: '', label: '默认 LLM' },
            ...appState.llmProviders.map(p => ({
                value: p.id,
                label: `${p.name} (${p.model})${p.enabled ? '' : ' [已禁用]'}`,
            })),
        ]);

        async function save() {
            saving.value = true;
            try {
                await api.accounts.bindLlm(props.account.id, selected.value);
                showToast('LLM 绑定已更新', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('绑定失败: ' + e.message, 'error');
            } finally {
                saving.value = false;
            }
        }

        return () => h(Card, { title: 'LLM 绑定' }, () => [
            h('p', { class: 'text-muted mb-4' },
                '选择此账号使用的 LLM Provider。留空则使用默认 LLM。'),
            h(FormSelect, {
                label: 'LLM Provider',
                modelValue: selected.value,
                'onUpdate:modelValue': (v) => selected.value = v,
                options: llmOptions.value,
            }),
            props.account.fallback_reason
                ? h('div', { class: 'form-error mt-2' }, '回退原因: ' + props.account.fallback_reason)
                : null,
            h('div', { class: 'mt-4' }, [
                h(Button, { type: 'primary', loading: saving.value, onClick: save }, () => '保存绑定'),
            ]),
        ]);
    },
});

const AccountPersonaTab = defineComponent({
    props: { account: Object },
    setup(props) {
        const selectedProfile = ref(props.account.profile_id || '');
        const selectedPersona = ref(props.account.persona_id || '');
        const saving = ref(false);
        const profiles = ref([]);

        async function loadProfiles() {
            try { profiles.value = await api.accounts.profiles(); }
            catch (e) { showToast('加载人格组失败: ' + e.message, 'error'); }
        }
        onMounted(loadProfiles);

        const profileOptions = computed(() => [
            { value: '', label: '不绑定' },
            ...profiles.value.map(p => ({ value: p.id, label: p.name })),
        ]);

        const availablePersonas = computed(() =>
            appState.personas.filter(p =>
                !selectedProfile.value ||
                profiles.value.find(pf => pf.id === selectedProfile.value)?.personas?.includes(p.id)
            )
        );

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

        async function switchPersona() {
            if (!selectedPersona.value) return;
            try {
                await api.accounts.switchPersona(props.account.id, selectedPersona.value);
                showToast('人格切换成功', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('切换失败: ' + e.message, 'error');
            }
        }

        return () => h('div', [
            h(Card, { title: '人格组绑定' }, () => [
                h('p', { class: 'text-muted mb-4' },
                    '绑定人格组后，账号可在该组内的人格间切换。'),
                h(FormSelect, {
                    label: '人格组',
                    modelValue: selectedProfile.value,
                    'onUpdate:modelValue': (v) => selectedProfile.value = v,
                    options: profileOptions.value,
                }),
                h('div', { class: 'mt-4' }, [
                    h(Button, { type: 'primary', loading: saving.value, onClick: bindProfile }, () => '保存绑定'),
                ]),
            ]),
            selectedProfile.value && availablePersonas.value.length > 1
                ? h(Card, { title: '切换当前人格' }, () => [
                    h(FormSelect, {
                        label: '当前人格',
                        modelValue: selectedPersona.value,
                        'onUpdate:modelValue': (v) => selectedPersona.value = v,
                        options: availablePersonas.value.map(p => ({ value: p.id, label: p.name })),
                    }),
                    h('div', { class: 'mt-4' }, [
                        h(Button, { type: 'primary', onClick: switchPersona }, () => '切换'),
                    ]),
                ])
                : null,
        ]);
    },
});

const AccountStatusTab = defineComponent({
    props: { account: Object },
    setup(props) {
        const operating = ref(false);

        async function toggle() {
            operating.value = true;
            try {
                if (props.account.running) {
                    await api.accounts.stop(props.account.id);
                    showToast('账号已停止', 'info');
                } else {
                    await api.accounts.start(props.account.id);
                    showToast('账号已启动', 'success');
                }
                await refreshAccounts();
            } catch (e) {
                showToast('操作失败: ' + e.message, 'error');
            } finally { operating.value = false; }
        }

        async function setDefault() {
            try {
                await api.accounts.setDefault(props.account.id);
                showToast('已设为默认账号', 'success');
                await refreshAccounts();
            } catch (e) {
                showToast('设置失败: ' + e.message, 'error');
            }
        }

        return () => h(Card, { title: '运行状态' }, () => [
            h('div', { class: 'status-grid' }, [
                h('div', { class: 'status-card' }, [
                    h('div', { class: 'status-icon ' + (props.account.running ? 'success' : 'warning') },
                        [h(Icon, { name: 'proactive', size: 26 })]),
                    h('div', { style: 'font-size:20px;font-weight:600' },
                        props.account.running ? '运行中' : '已停止'),
                ]),
                h('div', { class: 'status-card' }, [
                    h('div', { class: 'status-icon ' + (props.account.authenticated ? 'success' : 'danger') },
                        [h(Icon, { name: props.account.authenticated ? 'system' : 'logout', size: 26 })]),
                    h('div', { style: 'font-size:20px;font-weight:600' },
                        props.account.authenticated ? '已认证' : '未认证'),
                ]),
            ]),
            h('div', { class: 'flex gap-3 mt-4' }, [
                h(Button, {
                    type: props.account.running ? 'danger' : 'primary',
                    loading: operating.value,
                    onClick: toggle,
                }, () => props.account.running ? '停止' : '启动'),
                !props.account.is_default
                    ? h(Button, { onClick: setDefault }, () => '设为默认')
                    : null,
            ]),
            props.account.last_error
                ? h('div', { class: 'form-error mt-4' }, '最后错误: ' + props.account.last_error)
                : null,
        ]);
    },
});

// 账号详情页主组件
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
            { id: 'info', label: '基本信息' },
            { id: 'login', label: 'B站登录' },
            { id: 'llm', label: 'LLM 绑定' },
            { id: 'persona', label: '人格绑定' },
            { id: 'status', label: '运行状态' },
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

        async function deleteAccount() {
            if (!confirm(`确定删除账号 ${accountId.value}？`)) return;
            try {
                await api.accounts.delete(accountId.value);
                showToast('账号已删除', 'success');
                window.location.hash = '#/accounts';
                await refreshAccounts();
            } catch (e) {
                showToast('删除失败: ' + e.message, 'error');
            }
        }

        onMounted(() => { if (!appState.accountsLoaded) refreshAccounts(); });

        return () => !account.value
            ? h(Loading)
            : h('div', [
                h('div', { class: 'flex items-center gap-3 mb-4' }, [
                    h(Icon, { name: 'proactive', size: 24, style: account.value.running ? 'color:var(--success)' : 'color:var(--danger)' }),
                    h('h2', { style: 'font-size:20px;font-weight:600' }, account.value.name || account.value.id),
                    account.value.is_default ? h(Badge, { type: 'info' }, () => '默认') : null,
                ]),
                h('div', { class: 'tabs' },
                    tabs.map(t => h('div', {
                        class: ['tab', activeTab.value === t.id ? 'active' : ''],
                        onClick: () => activeTab.value = t.id,
                    }, t.label))
                ),
                activeTab.value === 'info' ? h(AccountInfoTab, { account: account.value, onSave: saveInfo, onDelete: deleteAccount }) : null,
                activeTab.value === 'login' ? h(AccountLoginTab, { account: account.value }) : null,
                activeTab.value === 'llm' ? h(AccountLlmTab, { account: account.value }) : null,
                activeTab.value === 'persona' ? h(AccountPersonaTab, { account: account.value }) : null,
                activeTab.value === 'status' ? h(AccountStatusTab, { account: account.value }) : null,
            ]);
    },
});
