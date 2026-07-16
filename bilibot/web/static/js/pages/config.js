// pages/config.js - 全局配置（Golden Time 设计稿）
const { defineComponent, h, ref, reactive, computed, onMounted, onBeforeUnmount, watch } = window.Vue;
import { api } from '../api.js';
import { Card, Button, Badge, Loading, EmptyState, Icon, HeroPanel, ConfirmModal, createConfirmHelper } from '../components/common.js';
import { SchemaForm } from '../components/SchemaForm.js';
import { showToast } from '../state.js';

// 后端 schema 可能是嵌套 dict，SchemaForm 需要扁平数组
function flattenSchema(nested, prefix = '', category = '', labelPrefix = '') {
    const result = [];
    if (!nested || typeof nested !== 'object') return result;
    // 已经是数组则直接返回
    if (Array.isArray(nested)) return nested;
    for (const [key, def] of Object.entries(nested)) {
        if (!def || typeof def !== 'object') continue;
        const dotKey = prefix ? `${prefix}.${key}` : key;
        const cat = category || key;
        const fieldLabel = def.label || key;
        const nextLabelPrefix = prefix
            ? [labelPrefix, fieldLabel].filter(Boolean).join(' / ')
            : labelPrefix;
        if (def.type === 'object' && def.fields) {
            result.push(...flattenSchema(def.fields, dotKey, cat, nextLabelPrefix));
        } else {
            result.push({
                key: dotKey,
                id: `cfg-${dotKey.replace(/\./g, '-')}`,
                label: [labelPrefix, fieldLabel].filter(Boolean).join(' / '),
                type: def.sensitive ? 'password' : (def.type || 'string'),
                category: cat,
                sensitive: !!def.sensitive,
                options: def.options,
                hint: def.description,
                deprecated: !!def.deprecated,
                itemType: def.itemType,
                default: def.default,
                min: def.min,
                max: def.max,
                placeholder: def.placeholder,
                rows: def.rows,
                immediate: !!def.immediate,
                autocomplete: def.sensitive ? 'off' : (def.autocomplete || undefined),
                spellcheck: def.sensitive ? false : (def.spellcheck ?? undefined),
            });
        }
    }
    return result;
}

// 嵌套 config → 扁平 dict（点分键）
function flattenConfig(obj, prefix = '') {
    const result = {};
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return result;
    for (const [key, value] of Object.entries(obj)) {
        const dotKey = prefix ? `${prefix}.${key}` : key;
        if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
            Object.assign(result, flattenConfig(value, dotKey));
        } else {
            result[dotKey] = value;
        }
    }
    return result;
}

// 扁平 dict（点分键）→ 嵌套 config（PATCH 需要）
function unflattenConfig(flat) {
    const result = {};
    for (const [dotKey, value] of Object.entries(flat)) {
        if (value === undefined || value === null) continue;
        const keys = dotKey.split('.');
        let cur = result;
        for (let i = 0; i < keys.length - 1; i++) {
            if (!cur[keys[i]] || typeof cur[keys[i]] !== 'object') cur[keys[i]] = {};
            cur = cur[keys[i]];
        }
        cur[keys[keys.length - 1]] = value;
    }
    return result;
}

// Task 28 / Task 30：本页渲染的 category 列表
// 不含 bilibili/llm（V2 遗留，已移除）、web/safety（在 system.js Tab 管理）、
// profiles（人格管理页）、video_analysis（视频理解页）、accounts（账号管理页）
const RENDERED_CATEGORIES = [
    'reply',
    'proactive',
    'web_search',
    'features',
    'companion',
    'dynamic_publish',
    'interactions',
    'memory',
    'global_defaults',
    'model_request_limits',
    'data_dir',
    'logging',
];

const FORM_DEFAULTS = {
    'web_search.retry.base_delay': 1,
    'web_search.retry.factor': 2,
    'web_search.retry.max_delay': 60,
    'web_search.retry.max_attempts': 3,
};

function materializeFormDefaults(flat, fields) {
    const model = { ...flat };
    for (const field of fields) {
        if (model[field.key] !== undefined) continue;
        const fallback = field.default ?? FORM_DEFAULTS[field.key];
        if (fallback !== undefined) model[field.key] = fallback;
    }
    return model;
}

export const ConfigPage = defineComponent({
    name: 'ConfigPage',
    setup() {
        const schema = ref([]);
        const config = ref({});
        const localModel = ref({});
        // Task 32.1：originalModel 用于 dirty 检测和"放弃修改"
        const originalModel = ref({});
        const loading = ref(true);
        const saving = ref(false);
        const version = ref(0);
        const lastSaved = ref(null);
        const personas = ref([]);
        const webSearchAdvanced = ref(false);

        const { state: confirmState, showConfirm, handleConfirm } = createConfirmHelper();

        async function loadData() {
            loading.value = true;
            try {
                const [schemaData, configResponse] = await Promise.all([
                    api.config.schema(),
                    api.config.fullWithMeta(),
                ]);
                schema.value = flattenSchema(schemaData || {});
                const configData = configResponse?.data || {};
                config.value = configData || {};
                const flat = materializeFormDefaults(
                    flattenConfig(configData || {}), schema.value
                );
                localModel.value = flat;
                // Task 32.1：记录加载时的原始状态
                originalModel.value = { ...flat };
                version.value = configResponse?.config_revision || 0;
                try {
                    personas.value = await api.personas.list();
                } catch (_) {
                    personas.value = [];
                }
            } catch (e) {
                showToast('加载配置失败: ' + e.message, 'error');
            } finally {
                loading.value = false;
            }
        }

        // Task 30：save() 只提取当前渲染的 category 字段提交
        async function save() {
            saving.value = true;
            try {
                // 只提取属于渲染 category 的字段，避免覆盖未渲染的配置段
                const renderedKeys = new Set(
                    schema.value
                        .filter(f => RENDERED_CATEGORIES.includes(f.category))
                        .map(f => f.key)
                );
                const filtered = {};
                for (const [key, value] of Object.entries(localModel.value)) {
                    if (renderedKeys.has(key)) {
                        filtered[key] = value;
                    }
                }
                const nested = unflattenConfig(filtered);
                // Always send optimistic lock, including revision 0 on fresh installs
                if (version.value !== null && version.value !== undefined) {
                    nested._expected_revision = version.value;
                }
                const result = await api.config.patch(nested);
                // 展示热重载契约摘要：哪些字段需重启 / 下个任务周期才生效
                const applied = result?.applied || result?.resp?.applied || {};
                const needsRestart = Object.entries(applied)
                    .filter(([, info]) => info?.status === 'requires_restart')
                    .map(([k]) => k);
                const pendingNext = Object.entries(applied)
                    .filter(([, info]) => info?.status === 'pending_next_task')
                    .map(([k]) => k);
                if (needsRestart.length) {
                    showToast(
                        `配置已保存；以下字段需重启后生效：${needsRestart.slice(0, 4).join(', ')}${needsRestart.length > 4 ? '…' : ''}`,
                        'warning',
                    );
                } else if (pendingNext.length) {
                    showToast('配置已保存；部分字段将在下个任务周期生效', 'success');
                } else {
                    showToast('配置保存成功', 'success');
                }
                lastSaved.value = new Date().toLocaleString();
                await loadData();
            } catch (e) {
                const msg = e.message || String(e);
                if (msg.includes('version') || msg.includes('409') || e.code === 'CONFIG_REVISION_CONFLICT') {
                    showToast('配置已被其他人修改，请刷新后重试', 'warning');
                    await loadData();
                } else {
                    showToast('保存失败: ' + msg, 'error');
                }
            } finally {
                saving.value = false;
            }
        }

        async function doReloadFromDisk() {
            try {
                await api.config.reload();
                showToast('配置已从磁盘重载', 'success');
                await loadData();
            } catch (e) {
                showToast('重载失败: ' + e.message, 'error');
            }
        }

        async function reload() {
            // 有未保存修改时先确认：重载会丢弃表单本地改动
            if (isDirty.value) {
                showConfirm({
                    title: '确认重载配置',
                    message: '当前有未保存的修改，从磁盘重载将丢弃这些改动。是否继续？',
                    confirmText: '丢弃并重载',
                    danger: true,
                    action: () => { doReloadFromDisk(); },
                });
                return;
            }
            await doReloadFromDisk();
        }

        // Task 32.2：放弃修改，恢复到加载时状态
        function discardChanges() {
            if (!isDirty.value) {
                showToast('没有未保存的修改', 'info');
                return;
            }
            showConfirm({
                title: '确认放弃修改',
                message: '确定放弃所有未保存的修改？',
                confirmText: '放弃修改',
                danger: true,
                action: () => {
                    localModel.value = { ...originalModel.value };
                    showToast('已放弃修改', 'info');
                },
            });
        }

        // 按 category 过滤 schema
        const schemaByCategory = (cat) => computed(() => schema.value.filter(
            f => (f.category || '通用') === cat && !f.deprecated
        ));

        const replySchema = schemaByCategory('reply');
        const proactiveSchema = schemaByCategory('proactive');
        const webSearchSchema = schemaByCategory('web_search');
        const featuresSchema = schemaByCategory('features');
        const companionSchema = schemaByCategory('companion');
        const dynamicPublishSchema = schemaByCategory('dynamic_publish');
        const interactionsSchema = schemaByCategory('interactions');
        const memorySchema = schemaByCategory('memory');
        const globalDefaultsSchema = schemaByCategory('global_defaults');
        const modelRequestLimitsSchema = schemaByCategory('model_request_limits');
        const dataDirSchema = schemaByCategory('data_dir');
        const loggingSchema = schemaByCategory('logging');
        const activePersona = computed(() => personas.value.find(
            persona => persona.is_active || persona.is_current
        ) || personas.value[0] || null);
        const webSearchBasicSchema = computed(() => {
            const keys = new Set([
                'web_search.enabled',
                'web_search.backend',
                'web_search.api_key',
                'web_search.max_results',
                'web_search.daily_budget_per_account',
            ]);
            return webSearchSchema.value.filter(field => keys.has(field.key));
        });
        const webSearchScenesSchema = computed(() => webSearchSchema.value.filter(
            field => field.key.startsWith('web_search.scenes.')
                && field.key.endsWith('.enabled')
        ));
        const webSearchAdvancedSchema = computed(() => {
            const basic = new Set(webSearchBasicSchema.value.map(field => field.key));
            const scenes = new Set(webSearchScenesSchema.value.map(field => field.key));
            return webSearchSchema.value.filter(field => !basic.has(field.key) && !scenes.has(field.key));
        });

        // Task 29.3：验证清单 — V3 结构
        const validations = computed(() => [
            { label: 'B站账号配置', passed: (config.value?.accounts?.length || 0) > 0 },
            { label: '对话模型连接', passed: (config.value?.chat_providers || []).some(p => p.api_key) },
            { label: '回复策略设置', passed: !!config.value?.reply },
            { label: '主动行为配置', passed: !!config.value?.proactive },
        ]);

        // Task 32.1：基于 dirty 状态判断是否已同步
        const isDirty = computed(() =>
            JSON.stringify(localModel.value) !== JSON.stringify(originalModel.value)
        );
        const isSynced = computed(() => !isDirty.value);

        // Task 32.2：路由离开时的未保存提示（beforeunload）
        function beforeUnloadHandler(e) {
            if (isDirty.value) {
                e.preventDefault();
                e.returnValue = '';
                return '';
            }
        }

        onMounted(() => {
            loadData();
            window.addEventListener('beforeunload', beforeUnloadHandler);
        });

        onBeforeUnmount(() => {
            window.removeEventListener('beforeunload', beforeUnloadHandler);
        });

        // 渲染单个配置卡片
        function renderConfigCard(eyebrow, title, schemaRef) {
            return h('article', {
                class: 'grid gap-3',
                style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
            }, [
                h('div', { class: 'card-header' }, [
                    h('div', { class: 'grid gap-1' }, [
                        h('span', { class: 'eyebrow' }, eyebrow),
                        h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, title),
                    ]),
                ]),
                h('div', { class: 'card-body' }, [
                    schemaRef.value.length > 0
                        ? h(SchemaForm, {
                            schema: schemaRef.value,
                            modelValue: localModel.value,
                            'onUpdate:modelValue': (v) => { localModel.value = v; },
                        })
                        : h(EmptyState, { title: '暂无配置项', desc: '该分类下没有可配置字段' }),
                ]),
            ]);
        }

        function renderWebSearchCard() {
            const renderForm = (schemaRef) => h(SchemaForm, {
                schema: schemaRef.value,
                modelValue: localModel.value,
                'onUpdate:modelValue': (value) => { localModel.value = value; },
            });
            return h('article', {
                class: 'grid gap-4',
                style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
            }, [
                h('div', { class: 'grid gap-1' }, [
                    h('span', { class: 'eyebrow' }, '联网搜索'),
                    h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '联网搜索'),
                ]),
                renderForm(webSearchBasicSchema),
                h('div', { class: 'grid gap-1' }, [
                    h('span', { class: 'eyebrow' }, '使用场景'),
                    renderForm(webSearchScenesSchema),
                ]),
                (webSearchAdvanced.value || ['custom', 'perplexity'].includes(localModel.value['web_search.backend']))
                    ? h('div', { class: 'grid gap-1' }, [
                        h('span', { class: 'eyebrow' }, '高级设置'),
                        renderForm(webSearchAdvancedSchema),
                    ])
                    : h(Button, {
                        type: 'ghost',
                        onClick: () => { webSearchAdvanced.value = true; },
                    }, () => [
                        h(Icon, { name: 'arrow-right', size: '1rem' }),
                        h('span', '高级设置'),
                    ]),
            ]);
        }

        function renderPersonaCard() {
            const persona = activePersona.value;
            return h('article', {
                class: 'grid gap-4',
                style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
            }, [
                h('div', { class: 'grid gap-1' }, [
                    h('span', { class: 'eyebrow' }, '当前人格'),
                    h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, persona?.name || '未配置人格'),
                    persona?.description ? h('p', { class: 'muted', style: 'margin:0;' }, persona.description) : null,
                ]),
                h(Button, {
                    type: 'primary',
                    onClick: () => { window.location.hash = '/personas'; },
                }, () => [
                    h(Icon, { name: 'arrow-right', size: '1rem' }),
                    h('span', '人格管理'),
                ]),
            ]);
        }

        return () => loading.value
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                // ═══ Section 1: hero-band — 配置状态 + 验证概览 ═══
                h('section', {
                    class: 'grid gap-4',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左侧：配置状态面板（hero-panel 样式，accent 背景）
                    h('article', {
                        class: 'grid gap-4',
                        style: 'background: hsl(var(--accent) / 0.22); border: 1px solid hsl(var(--accent)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 5); align-content: start;',
                    }, [
                        h('div', {
                            class: 'flex items-start justify-between gap-4',
                        }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '全局配置'),
                                h('h2', {
                                    style: 'margin:0; font-size:1.65rem; line-height:1.18; font-weight:600; text-wrap:balance; word-break:keep-all;',
                                }, isSynced.value ? '配置已保存' : '配置未保存'),
                            ]),
                            h(Badge, { type: isSynced.value ? 'success' : 'warning' }, () => [
                                h(Icon, { name: 'circle-check', size: '0.85rem' }),
                                h('span', { style: 'margin-left:calc(var(--spacing) * 0.8);' }, isSynced.value ? '已同步' : '有未保存修改'),
                            ]),
                        ]),
                        h('p', {
                            class: 'muted',
                            style: 'margin:0; font-size:0.95rem; line-height:1.6;',
                        }, lastSaved.value
                            ? `最近保存于 ${lastSaved.value} · 共 ${schema.value.length} 个配置项`
                            : `共 ${schema.value.length} 个配置项 · 版本 v${version.value}`),
                        h('div', { class: 'flex items-center gap-3 flex-wrap' }, [
                            h(Button, {
                                type: 'primary',
                                loading: saving.value,
                                onClick: save,
                                disabled: !isDirty.value,
                            }, () => [
                                h(Icon, { name: 'check', size: '1rem' }),
                                h('span', '保存配置'),
                            ]),
                            // Task 32.2：放弃修改按钮
                            h(Button, {
                                type: 'ghost',
                                onClick: discardChanges,
                                disabled: !isDirty.value,
                            }, () => [
                                h(Icon, { name: 'arrow-right', size: '1rem' }),
                                h('span', '放弃修改'),
                            ]),
                        ]),
                    ]),
                    // 右侧：验证概览 Card
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 5); align-content: start;',
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '配置检查'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '验证概览'),
                            ]),
                        ]),
                        h('div', { class: 'grid gap-0' },
                            validations.value.map((item, i) => h('div', {
                                class: 'flex items-center justify-between gap-3',
                                style: `padding: calc(var(--spacing) * 2.5) 0; ${i > 0 ? 'border-top: 1px solid hsl(var(--border));' : ''}`,
                            }, [
                                h('span', {
                                    style: 'font-size:0.97rem; color:hsl(var(--foreground));',
                                }, item.label),
                                h('span', {
                                    class: 'inline-flex items-center gap-1.5',
                                    style: `font-size:0.8rem; color: ${item.passed ? 'hsl(var(--chart-4))' : 'hsl(var(--muted-foreground))'};`,
                                }, [
                                    h(Icon, { name: 'circle-check', size: '0.9rem' }),
                                    h('span', item.passed ? '通过' : '未通过'),
                                ]),
                            ])),
                        ),
                    ]),
                ]),

                // ═══ Section 2: B站配置 — Task 29.2：引导到账号管理页 ═══
                h('section', {}, [
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, 'B站配置'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '账号与登录'),
                            ]),
                        ]),
                        h('div', { class: 'card-body' }, [
                            h('div', {
                                class: 'grid gap-2',
                                style: 'padding: calc(var(--spacing) * 3); border-radius: calc(var(--radius) * 0.76); background: hsl(var(--accent) / 0.12); border: 1px solid hsl(var(--accent) / 0.3);',
                            }, [
                                h('p', {
                                    class: 'm-0',
                                    style: 'font-size:0.95rem; line-height:1.6;',
                                }, 'B站账号配置已迁移到「账号管理」页面，请前往该页面管理账号凭证、登录状态与人格绑定。'),
                                h('div', { class: 'flex items-center gap-2' }, [
                                    h(Button, {
                                        type: 'primary',
                                        onClick: () => { window.location.hash = '/accounts'; },
                                    }, () => [
                                        h(Icon, { name: 'arrow-right', size: '1rem' }),
                                        h('span', '前往账号管理'),
                                    ]),
                                ]),
                            ]),
                        ]),
                    ]),
                ]),

                // ═══ Section 3: 表单网格 2 列 — 回复 + 主动行为 ═══
                h('section', {
                    class: 'grid gap-4',
                    style: 'grid-template-columns: repeat(2, minmax(0, 1fr));',
                }, [
                    renderConfigCard('回复配置', '评论与互动', replySchema),
                    renderConfigCard('主动配置', '定时任务', proactiveSchema),
                ]),

                // ═══ Section 4: Task 28 — 补全配置段 ═══
                h('section', {
                    class: 'grid gap-4',
                    style: 'grid-template-columns: repeat(2, minmax(0, 1fr));',
                }, [
                    renderWebSearchCard(),
                    renderPersonaCard(),
                ]),

                h('section', {
                    class: 'grid gap-4',
                    style: 'grid-template-columns: repeat(2, minmax(0, 1fr));',
                }, [
                    renderConfigCard('功能开关', 'Features', featuresSchema),
                    renderConfigCard('动态发布', 'Dynamic Publish', dynamicPublishSchema),
                ]),

                h('section', {
                    class: 'grid gap-4',
                    style: 'grid-template-columns: minmax(0, 1fr);',
                }, [
                    renderConfigCard(
                        '陪伴生活',
                        '日程 / 梦境日记 / 探索 / 创作',
                        companionSchema,
                    ),
                ]),

                h('section', {
                    class: 'grid gap-4',
                    style: 'grid-template-columns: repeat(2, minmax(0, 1fr));',
                }, [
                    renderConfigCard('互动预算', 'Interactions', interactionsSchema),
                    renderConfigCard('记忆系统', 'Memory', memorySchema),
                ]),

                h('section', {
                    class: 'grid gap-4',
                    style: 'grid-template-columns: repeat(2, minmax(0, 1fr));',
                }, [
                    renderConfigCard('全局默认', 'Global Defaults', globalDefaultsSchema),
                    renderConfigCard('模型请求限制', '多 Key 限流 / 视觉并发', modelRequestLimitsSchema),
                ]),

                h('section', {
                    class: 'grid gap-4',
                    style: 'grid-template-columns: repeat(2, minmax(0, 1fr));',
                }, [
                    renderConfigCard('日志设置', 'Logging', loggingSchema),
                    renderConfigCard('数据目录', 'Data Dir', dataDirSchema),
                ]),

                // ═══ Section 5: 操作栏 ═══
                h('div', {
                    class: 'flex items-center justify-end gap-3',
                    style: 'padding-top: calc(var(--spacing) * 1);',
                }, [
                    h(Button, {
                        type: 'ghost',
                        onClick: reload,
                    }, () => [
                        h(Icon, { name: 'arrow-right', size: '1rem' }),
                        h('span', '重载配置'),
                    ]),
                    // Task 32.2：放弃修改按钮
                    h(Button, {
                        type: 'ghost',
                        onClick: discardChanges,
                        disabled: !isDirty.value,
                    }, () => [
                        h('span', '放弃修改'),
                    ]),
                    h(Button, {
                        type: 'primary',
                        loading: saving.value,
                        onClick: save,
                        disabled: !isDirty.value,
                    }, () => [
                        h(Icon, { name: 'check', size: '1rem' }),
                        h('span', '保存配置'),
                    ]),
                ]),

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
    },
});
