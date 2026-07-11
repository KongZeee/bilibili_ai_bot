// pages/config.js - 全局配置（Golden Time 设计稿）
const { defineComponent, h, ref, reactive, computed, onMounted, watch } = window.Vue;
import { api } from '../api.js';
import { Card, Button, Badge, Loading, EmptyState, Icon, HeroPanel } from '../components/common.js';
import { SchemaForm } from '../components/SchemaForm.js';
import { showToast } from '../state.js';

// 后端 schema 可能是嵌套 dict，SchemaForm 需要扁平数组
function flattenSchema(nested, prefix = '', category = '') {
    const result = [];
    if (!nested || typeof nested !== 'object') return result;
    // 已经是数组则直接返回
    if (Array.isArray(nested)) return nested;
    for (const [key, def] of Object.entries(nested)) {
        if (!def || typeof def !== 'object') continue;
        const dotKey = prefix ? `${prefix}.${key}` : key;
        const cat = category || key;
        if (def.type === 'object' && def.fields) {
            result.push(...flattenSchema(def.fields, dotKey, cat));
        } else {
            result.push({
                key: dotKey,
                id: `cfg-${dotKey.replace(/\./g, '-')}`,
                label: def.label || key,
                type: def.sensitive ? 'password' : (def.type || 'string'),
                category: cat,
                sensitive: !!def.sensitive,
                options: def.options,
                hint: def.description,
                deprecated: !!def.deprecated,
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

export const ConfigPage = defineComponent({
    name: 'ConfigPage',
    setup() {
        const schema = ref([]);
        const config = ref({});
        const localModel = ref({});
        const loading = ref(true);
        const saving = ref(false);
        const version = ref(0);
        const lastSaved = ref(null);

        async function loadData() {
            loading.value = true;
            try {
                const [schemaData, configData] = await Promise.all([
                    api.config.schema(),
                    api.config.full(),
                ]);
                schema.value = flattenSchema(schemaData || {});
                config.value = configData || {};
                localModel.value = flattenConfig(configData || {});
                version.value = configData?.config_revision || 0;
            } catch (e) {
                showToast('加载配置失败: ' + e.message, 'error');
            } finally {
                loading.value = false;
            }
        }

        async function save() {
            saving.value = true;
            try {
                const nested = unflattenConfig(localModel.value);
                if (version.value > 0) nested._expected_revision = version.value;
                await api.config.patch(nested);
                showToast('配置保存成功', 'success');
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

        async function reload() {
            try {
                await api.config.reload();
                showToast('配置已重载', 'success');
                await loadData();
            } catch (e) {
                showToast('重载失败: ' + e.message, 'error');
            }
        }

        // 按 category 过滤 schema
        const schemaByCategory = (cat) => computed(() => schema.value.filter(f => (f.category || '通用') === cat));

        const bilibiliSchema = schemaByCategory('bilibili');
        const replySchema = schemaByCategory('reply');
        const proactiveSchema = schemaByCategory('proactive');

        // 验证清单
        const validations = computed(() => [
            { label: 'B站账号配置', passed: !!config.value?.bilibili?.dede_user_id },
            { label: 'LLM 服务连接', passed: !!config.value?.llm?.api_key },
            { label: '回复策略设置', passed: !!config.value?.reply },
            { label: '主动行为配置', passed: !!config.value?.proactive },
        ]);

        // 配置是否已同步
        const isSynced = computed(() => version.value > 0 || lastSaved.value !== null);

        onMounted(loadData);

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
                            h(Badge, { type: 'success' }, () => [
                                h(Icon, { name: 'circle-check', size: '0.85rem' }),
                                h('span', { style: 'margin-left:calc(var(--spacing) * 0.8);' }, '已同步'),
                            ]),
                        ]),
                        h('p', {
                            class: 'muted',
                            style: 'margin:0; font-size:0.95rem; line-height:1.6;',
                        }, lastSaved.value
                            ? `最近保存于 ${lastSaved.value} · 共 ${schema.value.length} 个配置项`
                            : `共 ${schema.value.length} 个配置项 · 版本 v${version.value}`),
                        h('div', { class: 'flex items-center gap-3' }, [
                            h(Button, {
                                type: 'primary',
                                loading: saving.value,
                                onClick: save,
                            }, () => [
                                h(Icon, { name: 'check', size: '1rem' }),
                                h('span', '保存配置'),
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

                // ═══ Section 2: B站配置（LLM 配置已迁移到「模型分配」页面）═══
                h('section', {}, [
                    renderConfigCard('B站配置', '账号与登录', bilibiliSchema),
                ]),

                // ═══ Section 3: 表单网格 2 列 — 回复 + 主动行为 ═══
                h('section', {
                    class: 'grid gap-4',
                    style: 'grid-template-columns: repeat(2, minmax(0, 1fr));',
                }, [
                    renderConfigCard('回复配置', '评论与互动', replySchema),
                    renderConfigCard('主动配置', '定时任务', proactiveSchema),
                ]),

                // ═══ Section 4: 操作栏 ═══
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
                    h(Button, {
                        type: 'primary',
                        loading: saving.value,
                        onClick: save,
                    }, () => [
                        h(Icon, { name: 'check', size: '1rem' }),
                        h('span', '保存配置'),
                    ]),
                ]),
            ]);
    },
});
