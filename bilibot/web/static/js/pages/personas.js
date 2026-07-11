// pages/personas.js - 人格管理（带关联账号视图）
const { defineComponent, h, ref, computed, onMounted } = window.Vue;
import { api } from '../api.js';
import { appState, refreshPersonas, refreshAccounts, showToast } from '../state.js';
import { Card, Button, Badge, Modal, FormInput, Toggle, EmptyState, Loading } from '../components/common.js';

export const PersonaListPage = defineComponent({
    name: 'PersonaListPage',
    setup() {
        const showEditor = ref(false);
        const editingId = ref(null);
        const form = ref({});

        // 计算每个人格被哪些账号使用
        const personaUsage = computed(() => {
            const map = {};
            for (const acc of appState.accounts) {
                const pid = acc.persona_id || acc.available_personas?.[0]?.id;
                if (pid) {
                    if (!map[pid]) map[pid] = [];
                    map[pid].push(acc);
                }
            }
            return map;
        });

        function startEdit(persona) {
            editingId.value = persona?.id || null;
            form.value = persona ? { ...persona } : {
                name: '', description: '', base_prompt: '', speaking_style: '',
                boundaries: '', relationship_rules: '', reply_rules: '',
                proactive_comment_rules: '', dynamic_rules: '', weekly_rules: '',
                examples: '', enabled: true,
            };
            showEditor.value = true;
        }

        async function save() {
            try {
                if (editingId.value) {
                    await api.personas.update(editingId.value, form.value);
                    showToast('人格已更新', 'success');
                } else {
                    await api.personas.create(form.value);
                    showToast('人格已创建', 'success');
                }
                showEditor.value = false;
                await refreshPersonas();
            } catch (e) {
                showToast('保存失败: ' + e.message, 'error');
            }
        }

        async function del(id) {
            if (!confirm('确定删除此人格？')) return;
            try {
                await api.personas.delete(id);
                showToast('已删除', 'success');
                await refreshPersonas();
            } catch (e) {
                showToast('删除失败: ' + e.message, 'error');
            }
        }

        async function activate(id) {
            try {
                await api.personas.activate(id);
                showToast('已激活', 'success');
                await refreshPersonas();
            } catch (e) {
                showToast('激活失败: ' + e.message, 'error');
            }
        }

        onMounted(() => {
            if (!appState.personasLoaded) refreshPersonas();
            if (!appState.accountsLoaded) refreshAccounts();
        });

        const formFields = [
            { key: 'name', label: '名称', type: 'text' },
            { key: 'description', label: '描述', type: 'text' },
            { key: 'base_prompt', label: '基础提示词', type: 'textarea' },
            { key: 'speaking_style', label: '说话风格', type: 'textarea' },
            { key: 'boundaries', label: '边界', type: 'textarea' },
            { key: 'relationship_rules', label: '关系规则', type: 'textarea' },
            { key: 'reply_rules', label: '回复规则', type: 'textarea' },
            { key: 'proactive_comment_rules', label: '主动评论规则', type: 'textarea' },
            { key: 'dynamic_rules', label: '动态规则', type: 'textarea' },
            { key: 'weekly_rules', label: '周报规则', type: 'textarea' },
            { key: 'examples', label: '示例', type: 'textarea' },
        ];

        return () => h('div', [
            h(Card, { title: '人格列表' }, {
                action: () => h(Button, { type: 'primary', onClick: () => startEdit(null) }, () => '+ 创建人格'),
                default: () => !appState.personasLoaded
                    ? h(Loading)
                    : appState.personas.length === 0
                        ? h(EmptyState, { icon: 'personas', title: '暂无人格', desc: '点击右上角创建' })
                        : appState.personas.map(p => h(Card, { key: p.id, bordered: true }, {
                            default: () => h('div', [
                                h('div', { class: 'flex items-center justify-between mb-2' }, [
                                    h('div', { class: 'flex items-center gap-2' }, [
                                        h('span', { style: 'font-size:16px;font-weight:600' }, p.name),
                                        p.is_current ? h(Badge, { type: 'success' }, () => '当前') : null,
                                        !p.enabled ? h(Badge, { type: 'danger' }, () => '已禁用') : null,
                                    ]),
                                    h('div', { class: 'flex gap-2' }, [
                                        !p.is_current ? h(Button, { size: 'sm', type: 'primary', onClick: () => activate(p.id) }, () => '激活') : null,
                                        h(Button, { size: 'sm', onClick: () => startEdit(p) }, () => '编辑'),
                                        h(Button, { size: 'sm', type: 'danger', onClick: () => del(p.id) }, () => '删除'),
                                    ]),
                                ]),
                                h('div', { class: 'text-muted', style: 'font-size:13px' }, p.description || '无描述'),
                                // 关联账号
                                (personaUsage.value[p.id] || []).length > 0
                                    ? h('div', { class: 'mt-2', style: 'padding-top:8px;border-top:1px solid var(--outline-variant)' }, [
                                        h('div', { class: 'text-muted', style: 'font-size:12px;margin-bottom:4px' }, '被以下账号使用:'),
                                        h('div', { class: 'flex gap-2' },
                                            personaUsage.value[p.id].map(acc =>
                                                h(Badge, { type: 'info', size: 'sm' }, () => acc.name || acc.id)
                                            )
                                        ),
                                    ])
                                    : null,
                            ]),
                        })),
            }),
            h(Modal, {
                modelValue: showEditor.value,
                'onUpdate:modelValue': (v) => showEditor.value = v,
                title: editingId.value ? '编辑人格' : '创建人格',
                width: '800px',
            }, {
                default: () => h('div', [
                    ...formFields.map(f => h(FormInput, {
                        label: f.label,
                        type: f.type === 'textarea' ? 'text' : f.type,
                        modelValue: form.value[f.key],
                        'onUpdate:modelValue': (v) => form.value[f.key] = v,
                    })),
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '启用'),
                        h(Toggle, {
                            modelValue: form.value.enabled,
                            'onUpdate:modelValue': (v) => form.value.enabled = v,
                        }),
                    ]),
                ]),
                footer: () => [
                    h(Button, { onClick: () => showEditor.value = false }, () => '取消'),
                    h(Button, { type: 'primary', onClick: save }, () => '保存'),
                ],
            }),
        ]);
    },
});
