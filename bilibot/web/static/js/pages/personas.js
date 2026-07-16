// pages/personas.js - 人格管理（Golden Time 设计稿）
const { defineComponent, h, ref, reactive, computed, onMounted } = window.Vue;
import { api } from '../api.js';
import { Card, Button, Badge, Modal, ConfirmModal, createConfirmHelper, FormInput, FormTextarea, FormSelect, Loading, EmptyState, Icon, HeroPanel, ActionList, ProgressBar } from '../components/common.js';
import { appState, showToast, refreshPersonas, refreshLlm } from '../state.js';
import { navigate } from '../router.js';

export const PersonaListPage = defineComponent({
    name: 'PersonaListPage',
    setup() {
        const personas = ref([]);
        const loading = ref(true);
        const activePersonaId = ref(null);
        const showCreateModal = ref(false);
        const editingId = ref(null);
        const createForm = reactive({ name: '', description: '', system_prompt: '', personality: '', appearance: '' });
        const testInput = ref('');
        const testReply = ref('');
        const testing = ref(false);
        const selectedLlm = ref('');

        // 激活人格查找
        const activePersona = computed(() =>
            personas.value.find(p => p.is_active || p.is_current || p.id === activePersonaId.value)
        );

        // ── 辅助函数 ──
        function getTags(persona) {
            if (!persona) return [];
            if (Array.isArray(persona.tags)) return persona.tags;
            if (typeof persona.tags === 'string' && persona.tags) {
                try {
                    const parsed = JSON.parse(persona.tags);
                    return Array.isArray(parsed) ? parsed : [persona.tags];
                } catch {
                    return persona.tags.split(/[,，]/).map(s => s.trim()).filter(Boolean);
                }
            }
            return [];
        }

        function getPersonaIcon(persona) {
            const tags = getTags(persona);
            const text = [...tags, persona.type || '', persona.name || ''].join(' ');
            if (text.includes('助手')) return 'star';
            if (text.includes('创作')) return 'pen-line';
            if (text.includes('服务')) return 'heart';
            return 'circle-question-mark';
        }

        function isActive(p) {
            return !!(p.is_active || p.is_current);
        }

        // 绑定账号统计
        const personaUsage = computed(() => {
            const map = {};
            for (const acc of appState.accounts || []) {
                const pid = acc.persona_id || acc.available_personas?.[0]?.id;
                if (pid) {
                    if (!map[pid]) map[pid] = [];
                    map[pid].push(acc);
                }
            }
            return map;
        });

        // 分类统计
        const categoryStats = computed(() => {
            const total = personas.value.length;
            let assistant = 0, creative = 0, service = 0;
            for (const p of personas.value) {
                const icon = getPersonaIcon(p);
                if (icon === 'star') assistant++;
                else if (icon === 'pen-line') creative++;
                else if (icon === 'heart') service++;
            }
            return [
                { label: '助手类', value: assistant, max: total },
                { label: '创作类', value: creative, max: total },
                { label: '服务类', value: service, max: total },
            ];
        });

        // LLM 选项（为空时显示"默认模型"）
        const llmOptions = computed(() => {
            const list = appState.llmProviders || [];
            if (list.length === 0) {
                return [{ value: '', label: '默认模型' }];
            }
            return list.map(p => ({ value: String(p.id), label: p.name || p.provider || `模型 ${p.id}` }));
        });

        // ── 数据加载 ──
        async function loadData() {
            loading.value = true;
            try {
                const list = await api.personas.list();
                personas.value = list || [];
                appState.personas = list || [];
                appState.personasLoaded = true;

                if (!appState.accountsLoaded) {
                    try {
                        appState.accounts = await api.accounts.list();
                        appState.accountsLoaded = true;
                    } catch { /* ignore */ }
                }

                const active = personas.value.find(p => isActive(p));
                if (active) activePersonaId.value = active.id;
            } catch (e) {
                showToast('加载人格列表失败: ' + e.message, 'error');
            } finally {
                loading.value = false;
            }
        }

        // ── 人格操作 ──
        async function activate(id) {
            try {
                await api.personas.activate(id);
                showToast('已激活', 'success');
                await loadData();
            } catch (e) {
                showToast('激活失败: ' + e.message, 'error');
            }
        }

        async function copyPersona(id) {
            try {
                await api.personas.copy(id);
                showToast('已复制人格', 'success');
                await loadData();
            } catch (e) {
                showToast('复制失败: ' + e.message, 'error');
            }
        }

        async function exportPersona(id) {
            try {
                const data = await api.personas.export(id);
                const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `persona-${id}.json`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                showToast('已导出人格配置', 'success');
            } catch (e) {
                showToast('导出失败: ' + e.message, 'error');
            }
        }

        const { state: confirmState, showConfirm, handleConfirm } = createConfirmHelper();
        function deletePersona(id) {
            showConfirm({
                title: '删除人格',
                message: '确定删除此人格？此操作不可撤销。',
                confirmText: '删除',
                danger: true,
                action: async () => {
                    try {
                        await api.personas.delete(id);
                        showToast('已删除', 'success');
                        await loadData();
                    } catch (e) {
                        showToast('删除失败: ' + e.message, 'error');
                    }
                },
            });
        }

        // ── 创建/编辑 Modal ──
        function openCreateModal() {
            editingId.value = null;
            createForm.name = '';
            createForm.description = '';
            createForm.system_prompt = '';
            createForm.personality = '';
            createForm.appearance = '';
            showCreateModal.value = true;
        }

        function openEditModal(persona) {
            editingId.value = persona.id;
            createForm.name = persona.name || '';
            createForm.description = persona.description || '';
            createForm.system_prompt = persona.system_prompt || persona.base_prompt || '';
            const tags = getTags(persona);
            createForm.personality = persona.personality || (tags.length > 0 ? tags.join('，') : '');
            createForm.appearance = persona.appearance || '';
            showCreateModal.value = true;
        }

        async function submitCreate() {
            if (!createForm.name.trim()) {
                showToast('请输入人格名称', 'warning');
                return;
            }
            try {
                // 后端 persona_store 只持久化 base_prompt，不认 system_prompt
                const payload = {
                    name: createForm.name,
                    description: createForm.description,
                    base_prompt: createForm.system_prompt,
                    personality: createForm.personality,
                    appearance: createForm.appearance,
                };
                if (editingId.value) {
                    await api.personas.update(editingId.value, payload);
                    showToast('人格已更新', 'success');
                } else {
                    await api.personas.create(payload);
                    showToast('人格已创建', 'success');
                }
                showCreateModal.value = false;
                await loadData();
            } catch (e) {
                showToast('保存失败: ' + e.message, 'error');
            }
        }

        // ── 试一试 ──
        async function sendTest() {
            if (!activePersonaId.value) {
                showToast('请先激活一个人格', 'warning');
                return;
            }
            if (!testInput.value.trim()) {
                showToast('请输入测试消息', 'warning');
                return;
            }
            testing.value = true;
            testReply.value = '';
            try {
                const payload = {
                    input: testInput.value,
                    use_llm: true,
                };
                // 空字符串不要传给后端（会走 resolve_chat("") 可能误报 LLM_NOT_CONFIGURED）
                if (selectedLlm.value) {
                    payload.llm_provider_id = selectedLlm.value;
                }
                const result = await api.personas.test(activePersonaId.value, payload);
                testReply.value = result?.output || result?.reply || result?.response || result?.message || result?.content ||
                    (typeof result === 'string' ? result : (result ? JSON.stringify(result) : '（无回复）'));
            } catch (e) {
                if (e.code === 'LLM_NOT_FOUND' || e.code === 'LLM_NOT_CONFIGURED') {
                    testReply.value = '请先在模型管理页面配置并启用一个对话模型服务商';
                    showToast('请先在模型管理页面配置并启用一个对话模型服务商', 'warning');
                } else {
                    testReply.value = '测试失败: ' + e.message;
                    showToast('测试失败: ' + e.message, 'error');
                }
            } finally {
                testing.value = false;
            }
        }

        // ── 激活人格快捷操作 ──
        function copyActivePersona() {
            if (activePersonaId.value) copyPersona(activePersonaId.value);
            else showToast('当前无激活人格', 'warning');
        }
        function exportActivePersona() {
            if (activePersonaId.value) exportPersona(activePersonaId.value);
            else showToast('当前无激活人格', 'warning');
        }
        function deleteActivePersona() {
            if (activePersonaId.value) deletePersona(activePersonaId.value);
            else showToast('当前无激活人格', 'warning');
        }

        onMounted(() => {
            loadData();
            refreshLlm();
        });

        // ── 样式常量 ──
        const CARD_BASE = 'display:grid; gap: calc(var(--spacing) * 3); background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;';
        const cardStyle = (active = false) =>
            CARD_BASE + (active ? ' border-color: hsl(var(--accent));' : '');
        const EYEBROW = 'color:hsl(var(--muted-foreground)); font-size:0.73rem; text-transform:uppercase; letter-spacing:0.14em;';
        const H2 = 'margin:0; font-size:1.25rem; line-height:1.15; text-wrap:balance;';

        return () => loading.value
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                // ═══ SECTION 1: hero-band（2 列网格）═══
                h('section', {
                    style: 'display:grid; gap: calc(var(--spacing) * 4); grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左侧：激活人格高亮面板（HeroPanel，accent 背景）
                    h(HeroPanel, {
                        eyebrow: '人格管理',
                        title: activePersona.value?.name || '未激活',
                        badge: activePersona.value ? '激活中' : '',
                        badgeType: 'success',
                        ctaText: '创建人格',
                        ctaIcon: 'circle-plus',
                        onCta: openCreateModal,
                    }, {
                        default: () => activePersona.value
                            ? [
                                activePersona.value.description
                                    ? h('p', {
                                        class: 'muted',
                                        style: 'margin:0; max-width:34rem; line-height:1.6; font-size:0.95rem;',
                                    }, activePersona.value.description)
                                    : null,
                                getTags(activePersona.value).length > 0
                                    ? h('div', {
                                        style: 'display:flex; flex-wrap:wrap; gap: calc(var(--spacing) * 1.5);',
                                    }, getTags(activePersona.value).map(tag =>
                                        h(Badge, { type: 'info', size: 'sm' }, () => tag)
                                    ))
                                    : null,
                            ]
                            : null,
                    }),

                    // 右侧：人格统计 Card
                    h('article', {
                        class: 'gap-3',
                        style: cardStyle(),
                    }, [
                        h('div', {
                            style: 'display:grid; gap: calc(var(--spacing) * 0.8);',
                        }, [
                            h('span', { style: EYEBROW }, '人格概览'),
                            h('h2', { style: H2 }, '角色配置'),
                        ]),
                        h('div', {
                            style: 'display:flex; align-items:baseline; gap: calc(var(--spacing) * 1.5);',
                        }, [
                            h('span', {
                                style: 'font-size:2.5rem; font-weight:700; line-height:1; color:hsl(var(--foreground)); font-variant-numeric:tabular-nums;',
                            }, String(personas.value.length)),
                            h('span', {
                                style: 'color:hsl(var(--muted-foreground)); font-size:0.95rem;',
                            }, '个人格'),
                        ]),
                        // 3 个 ProgressBar 显示分类统计
                        ...categoryStats.value.map((stat, i) =>
                            h(ProgressBar, {
                                key: stat.label,
                                label: stat.label,
                                value: stat.value,
                                max: stat.max || 1,
                                showValue: true,
                                colorToken: `chart-${i + 1}`,
                            })
                        ),
                    ]),
                ]),

                // ═══ SECTION 2: 人格卡片网格（3 列）═══
                personas.value.length === 0
                    ? h(EmptyState, {
                        icon: 'user',
                        title: '暂无人格',
                        desc: '点击"创建人格"添加第一个角色',
                    })
                    : h('section', {
                        style: 'display:grid; gap: calc(var(--spacing) * 4); grid-template-columns: repeat(3, minmax(0, 1fr));',
                    }, personas.value.map(p => {
                        const tags = getTags(p);
                        const usage = personaUsage.value[p.id] || [];
                        const active = isActive(p);
                        return h('article', {
                            key: p.id,
                            class: 'gap-3',
                            style: cardStyle(active),
                        }, [
                            // 图标 + 名称
                            h('div', {
                                style: 'display:flex; align-items:center; gap: calc(var(--spacing) * 2);',
                            }, [
                                h('span', {
                                    style: `width:2.5rem; height:2.5rem; border-radius:999px; background: ${active ? 'hsl(var(--accent) / 0.4)' : 'hsl(var(--accent) / 0.3)'}; display:inline-flex; align-items:center; justify-content:center; flex:0 0 auto; color: hsl(var(--accent-foreground));`,
                                }, [h(Icon, { name: getPersonaIcon(p), size: '1.25rem' })]),
                                h('h3', {
                                    style: 'margin:0; font-size:1.25rem; line-height:1.15; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;',
                                }, p.name),
                            ]),
                            // 描述
                            p.description
                                ? h('p', {
                                    style: 'margin:0; color:hsl(var(--muted-foreground)); font-size:0.92rem; line-height:1.55; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;',
                                }, p.description)
                                : null,
                            // 标签组（pill 徽章）
                            tags.length > 0
                                ? h('div', {
                                    style: 'display:flex; flex-wrap:wrap; gap: calc(var(--spacing) * 0.8);',
                                }, tags.map(tag =>
                                    h(Badge, { type: 'info', size: 'sm' }, () => tag)
                                ))
                                : null,
                            // 状态 + 绑定账号数
                            h('div', {
                                style: 'display:flex; align-items:center; justify-content:space-between; gap: calc(var(--spacing) * 1); padding-top: calc(var(--spacing) * 2); border-top: 1px solid hsl(var(--border));',
                            }, [
                                active
                                    ? h('span', {
                                        style: 'display:inline-flex; align-items:center; gap: calc(var(--spacing) * 1); font-size:0.82rem; color:hsl(var(--primary)); white-space:nowrap;',
                                    }, [
                                        h('span', { style: 'width:0.375rem; height:0.375rem; border-radius:999px; background:hsl(var(--primary));' }),
                                        '激活中',
                                    ])
                                    : h('span', {
                                        style: 'display:inline-flex; align-items:center; gap: calc(var(--spacing) * 1); font-size:0.82rem; color:hsl(var(--muted-foreground)); white-space:nowrap;',
                                    }, [
                                        h('span', { style: 'width:0.375rem; height:0.375rem; border-radius:999px; background:hsl(var(--muted-foreground));' }),
                                        '待激活',
                                    ]),
                                h('span', {
                                    style: 'font-size:0.82rem; color:hsl(var(--muted-foreground)); white-space:nowrap;',
                                }, usage.length > 0 ? `绑定 ${usage.length} 个账号` : '未绑定'),
                            ]),
                            // 底部按钮：激活/复制/编辑
                            h('div', {
                                style: 'display:flex; gap: calc(var(--spacing) * 1.5);',
                            }, [
                                !active
                                    ? h('button', {
                                        class: 'btn primary btn-sm',
                                        onClick: () => activate(p.id),
                                    }, [h('span', '激活')])
                                    : null,
                                h(Button, { size: 'sm', onClick: () => copyPersona(p.id) }, () => '复制'),
                                h(Button, { size: 'sm', onClick: () => openEditModal(p) }, () => '编辑'),
                            ]),
                        ]);
                    })),

                // ═══ SECTION 3: split grid（1.15fr : 0.85fr）═══
                h('section', {
                    style: 'display:grid; gap: calc(var(--spacing) * 4); grid-template-columns: minmax(0, 1.15fr) minmax(18rem, 0.85fr);',
                }, [
                    // 左侧：试一试面板
                    h('article', {
                        class: 'gap-3',
                        style: cardStyle(),
                    }, [
                        h('div', {
                            style: 'display:grid; gap: calc(var(--spacing) * 0.8);',
                        }, [
                            h('span', { style: EYEBROW }, '交互测试'),
                            h('h2', { style: H2 }, '试一试'),
                            h('p', {
                                style: 'margin:0; color:hsl(var(--muted-foreground)); font-size:0.92rem; line-height:1.55;',
                            }, '输入一段文字，查看当前人格的回复风格'),
                        ]),
                        // LLM 模型选择
                        h(FormSelect, {
                            label: '对话模型',
                            modelValue: selectedLlm.value,
                            'onUpdate:modelValue': (v) => selectedLlm.value = v,
                            options: llmOptions.value,
                        }),
                        // FormTextarea（输入测试消息）
                        h(FormTextarea, {
                            modelValue: testInput.value,
                            'onUpdate:modelValue': (v) => testInput.value = v,
                            placeholder: '输入测试消息...',
                            rows: 4,
                        }),
                        // Button primary "发送测试"
                        h('div', {
                            style: 'display:flex; justify-content:flex-end;',
                        }, [
                            h('button', {
                                class: 'btn primary',
                                onClick: sendTest,
                                disabled: testing.value,
                                'aria-busy': testing.value || undefined,
                            }, [
                                testing.value ? h('span', { class: 'spinner spinner-sm', 'aria-hidden': 'true' }) : null,
                                h(Icon, { name: 'send-horizontal', size: '1rem' }),
                                h('span', '发送测试'),
                            ]),
                        ]),
                        // 回复区：AI 回复占位
                        h('div', {
                            style: 'background: hsl(var(--muted) / 0.5); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.68); padding: calc(var(--spacing) * 3); min-height:4rem; display:flex; align-items:center; justify-content:center;',
                        }, [
                            testReply.value
                                ? h('p', {
                                    style: 'margin:0; color:hsl(var(--foreground)); font-size:0.92rem; line-height:1.55; width:100%; white-space:pre-wrap;',
                                }, testReply.value)
                                : h('span', {
                                    style: 'color:hsl(var(--muted-foreground)); font-size:0.88rem;',
                                }, 'AI 回复将显示在这里'),
                        ]),
                    ]),

                    // 右侧：管理工具 ActionList
                    h('article', {
                        class: 'gap-3',
                        style: cardStyle(),
                    }, [
                        h('div', {
                            style: 'display:grid; gap: calc(var(--spacing) * 0.8);',
                        }, [
                            h('span', { style: EYEBROW }, '人格工具'),
                            h('h2', { style: H2 }, '管理操作'),
                        ]),
                        h(ActionList, {
                            items: [
                                { iconName: 'circle-plus', label: '创建人格', onClick: openCreateModal },
                                { iconName: 'pen-line', label: '复制当前', onClick: copyActivePersona },
                                { iconName: 'folder-open', label: '导出人格', onClick: exportActivePersona },
                                { iconName: 'trash-2', label: '删除人格', onClick: deleteActivePersona },
                            ],
                        }),
                        // 底部 CTA: Button ghost "查看记忆"
                        h('div', {
                            style: 'display:flex; justify-content:flex-end; padding-top: calc(var(--spacing) * 2);',
                        }, [
                            h('button', {
                                class: 'btn ghost',
                                onClick: () => navigate('/memory/list'),
                            }, [
                                h('span', '查看记忆'),
                                h(Icon, { name: 'arrow-right', size: '1rem' }),
                            ]),
                        ]),
                    ]),
                ]),

                // ═══ 创建/编辑人格 Modal ═══
                h(Modal, {
                    modelValue: showCreateModal.value,
                    'onUpdate:modelValue': (v) => showCreateModal.value = v,
                    title: editingId.value ? '编辑人格' : '创建人格',
                    width: '640px',
                }, {
                    default: () => h('div', {
                        style: 'display:grid; gap: calc(var(--spacing) * 3);',
                    }, [
                        // FormInput（名称）— 使用正确的 type
                        h(FormInput, {
                            label: '名称',
                            type: 'text',
                            placeholder: '输入人格名称',
                            modelValue: createForm.name,
                            'onUpdate:modelValue': (v) => createForm.name = v,
                        }),
                        // FormTextarea（描述）— 修复 bug：直接使用 FormTextarea
                        h(FormTextarea, {
                            label: '描述',
                            placeholder: '简要描述人格特点',
                            rows: 2,
                            modelValue: createForm.description,
                            'onUpdate:modelValue': (v) => createForm.description = v,
                        }),
                        // FormTextarea（系统提示词）
                        h(FormTextarea, {
                            label: '系统提示词',
                            placeholder: '定义人格的系统提示词',
                            rows: 4,
                            modelValue: createForm.system_prompt,
                            'onUpdate:modelValue': (v) => createForm.system_prompt = v,
                        }),
                        // FormTextarea（性格特征）
                        h(FormTextarea, {
                            label: '性格特征',
                            placeholder: '如：温柔、耐心、专业（逗号分隔）',
                            rows: 2,
                            modelValue: createForm.personality,
                            'onUpdate:modelValue': (v) => createForm.personality = v,
                        }),
                        // FormTextarea（外貌描述）— 用于动态配图时作为主角外貌注入图片生成 prompt
                        h(FormTextarea, {
                            label: '外貌描述（用于动态配图）',
                            placeholder: '描述主角外貌，如：粉色长发少女，绿色大眼睛，穿白色连衣裙，温柔气质。留空则配图不固定主角形象。',
                            rows: 3,
                            modelValue: createForm.appearance,
                            'onUpdate:modelValue': (v) => createForm.appearance = v,
                        }),
                    ]),
                    footer: () => [
                        h(Button, { onClick: () => showCreateModal.value = false }, () => '取消'),
                        h('button', {
                            class: 'btn primary',
                            onClick: submitCreate,
                        }, [h('span', editingId.value ? '保存' : '创建')]),
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
