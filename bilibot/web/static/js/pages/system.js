// bilibot/web/static/js/pages/system.js - 系统设置页（Golden Time 设计稿）
const { h, ref, reactive, onMounted, computed } = window.Vue;
import { appState } from '../state.js';
import { api } from '../api.js';
import { Button, Badge, Modal, ConfirmModal, createConfirmHelper, FormInput, EmptyState, Loading, Icon } from '../components/common.js';
import { SchemaForm } from '../components/SchemaForm.js';
import { formatTime } from '../utils.js';

// Task 28：schema 嵌套 dict → 扁平数组（与 config.js 一致的逻辑）
function flattenSchema(nested, prefix = '', category = '', labelPrefix = '') {
    const result = [];
    if (!nested || typeof nested !== 'object') return result;
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

export const SystemPage = {
    name: 'SystemPage',
    setup() {
        const activeTab = ref('security');

        // 安全控制
        const securityLoading = ref(false);
        const pauseStatus = ref(null);
        const blacklist = ref([]);
        const blacklistInput = ref('');

        // 备份恢复
        const backupLoading = ref(false);
        const backups = ref([]);
        const creatingBackup = ref(false);
        const restoreModal = reactive({
            visible: false,
            backup: null,
            confirming: false,
        });

        const { state: confirmState, showConfirm, handleConfirm } = createConfirmHelper();

        // Task 28：安全配置 / 面板配置（schema 驱动）
        const configSchema = ref([]);
        const configLocalModel = ref({});
        const configOriginalModel = ref({});
        const configVersion = ref(0);
        const configSaving = ref(false);
        const configLoading = ref(false);

        async function loadPauseStatus() {
            securityLoading.value = true;
            try {
                pauseStatus.value = await api.safety.pauseStatus();
            } catch (e) {
                appState.notify('加载暂停状态失败：' + (e.message || e), 'danger');
            } finally {
                securityLoading.value = false;
            }
        }

        async function togglePause() {
            if (!pauseStatus.value) return;
            const isPaused = pauseStatus.value.paused;
            showConfirm({
                title: isPaused ? '确认恢复' : '确认暂停',
                message: isPaused ? '确认恢复 Bot 运行？' : '确认全局暂停 Bot？所有自动行为将停止。',
                confirmText: isPaused ? '恢复运行' : '全局暂停',
                danger: !isPaused,
                action: async () => {
                    try {
                        if (isPaused) {
                            await api.safety.resume();
                            appState.notify('已恢复运行', 'success');
                        } else {
                            await api.safety.pause();
                            appState.notify('已暂停', 'warning');
                        }
                        loadPauseStatus();
                    } catch (e) {
                        appState.notify('操作失败：' + (e.message || e), 'danger');
                    }
                },
            });
        }

        async function loadBlacklist() {
            try {
                const data = await api.safety.blacklist();
                blacklist.value = data?.items || [];
            } catch (e) {
                appState.notify('加载黑名单失败：' + (e.message || e), 'danger');
            }
        }

        async function addBlacklist() {
            if (!blacklistInput.value) return;
            try {
                await api.safety.addBlacklist({
                    user_id: blacklistInput.value,
                    reason: '',
                });
                blacklistInput.value = '';
                appState.notify('已添加到黑名单', 'success');
                loadBlacklist();
            } catch (e) {
                appState.notify('添加失败：' + (e.message || e), 'danger');
            }
        }

        async function removeBlacklist(item) {
            showConfirm({
                title: '确认移除',
                message: `确认从黑名单移除 ${item.user_id || item.value}？`,
                confirmText: '移除',
                danger: true,
                action: async () => {
                    try {
                        await api.safety.delBlacklist(item.user_id || item.id || item.value);
                        appState.notify('已移除', 'success');
                        loadBlacklist();
                    } catch (e) {
                        appState.notify('移除失败：' + (e.message || e), 'danger');
                    }
                },
            });
        }

        async function loadBackups() {
            backupLoading.value = true;
            try {
                const data = await api.backup.list();
                // 后端 ok({"backups": [...]})，api 解包后为 { backups: [...] }
                backups.value = data?.backups || data?.items || (Array.isArray(data) ? data : []);
            } catch (e) {
                appState.notify('加载备份列表失败：' + (e.message || e), 'danger');
                backups.value = [];
            } finally {
                backupLoading.value = false;
            }
        }

        async function createBackup() {
            creatingBackup.value = true;
            try {
                await api.backup.create();
                appState.notify('备份已创建', 'success');
                loadBackups();
            } catch (e) {
                appState.notify('创建备份失败：' + (e.message || e), 'danger');
            } finally {
                creatingBackup.value = false;
            }
        }

        function openRestore(backup) {
            restoreModal.backup = backup;
            restoreModal.confirming = false;
            restoreModal.visible = true;
        }

        async function confirmRestore() {
            if (!restoreModal.backup) return;
            restoreModal.confirming = true;
            try {
                await api.backup.restore(restoreModal.backup.name);
                appState.notify('备份已恢复，建议重启服务', 'success');
                restoreModal.visible = false;
            } catch (e) {
                appState.notify('恢复失败：' + (e.message || e), 'danger');
            } finally {
                restoreModal.confirming = false;
            }
        }

        async function downloadBackup(backup) {
            try {
                const blob = await api.backup.download(backup.name);
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${backup.name}.tar.gz`;
                a.click();
                URL.revokeObjectURL(url);
            } catch (e) {
                appState.notify('下载失败：' + (e.message || e), 'danger');
            }
        }

        async function deleteBackup(backup) {
            showConfirm({
                title: '确认删除备份',
                message: `确认删除备份 ${backup.name}？此操作不可撤销。`,
                confirmText: '删除',
                danger: true,
                action: async () => {
                    try {
                        await api.backup.delete(backup.name);
                        appState.notify('备份已删除', 'success');
                        loadBackups();
                    } catch (e) {
                        appState.notify('删除失败：' + (e.message || e), 'danger');
                    }
                },
            });
        }

        // Task 28：加载配置 schema + 完整配置
        async function loadConfigData() {
            configLoading.value = true;
            try {
                const [schemaData, configResponse] = await Promise.all([
                    api.config.schema(),
                    api.config.fullWithMeta(),
                ]);
                configSchema.value = flattenSchema(schemaData || {});
                const configData = configResponse?.data || {};
                const flat = flattenConfig(configData || {});
                configLocalModel.value = flat;
                configOriginalModel.value = { ...flat };
                configVersion.value = configResponse?.config_revision || 0;
            } catch (e) {
                appState.notify('加载配置失败：' + (e.message || e), 'danger');
            } finally {
                configLoading.value = false;
            }
        }

        // Task 28：保存指定 category 的配置段
        async function saveConfigSection(category) {
            configSaving.value = true;
            try {
                const sectionKeys = new Set(
                    configSchema.value
                        .filter(f => f.category === category)
                        .map(f => f.key)
                );
                const filtered = {};
                for (const [key, value] of Object.entries(configLocalModel.value)) {
                    if (sectionKeys.has(key)) {
                        filtered[key] = value;
                    }
                }
                const nested = unflattenConfig(filtered);
                if (configVersion.value > 0) nested._expected_revision = configVersion.value;
                await api.config.patch(nested);
                appState.notify('配置已保存', 'success');
                await loadConfigData();
            } catch (e) {
                const msg = e.message || String(e);
                if (msg.includes('version') || msg.includes('409') || e.code === 'CONFIG_REVISION_CONFLICT') {
                    appState.notify('配置已被其他人修改，请刷新后重试', 'warning');
                    await loadConfigData();
                } else {
                    appState.notify('保存失败：' + msg, 'danger');
                }
            } finally {
                configSaving.value = false;
            }
        }

        // Task 28：按 category 过滤 schema
        const schemaByCategory = (cat) => computed(() =>
            configSchema.value.filter(f => (f.category || '通用') === cat)
        );
        const safetySchema = schemaByCategory('safety');
        const webSchema = schemaByCategory('web');

        // Task 28：配置 dirty 检测
        const configIsDirty = computed(() =>
            JSON.stringify(configLocalModel.value) !== JSON.stringify(configOriginalModel.value)
        );

        onMounted(() => {
            loadPauseStatus();
            loadBlacklist();
            loadBackups();
            loadConfigData();
        });

        const tabs = [
            { value: 'security', label: '安全控制' },
            { value: 'safety', label: '安全配置' },
            { value: 'web', label: '面板配置' },
            { value: 'backup', label: '备份恢复' },
        ];

        const cardStyle = 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;';

        const blacklistGrid = 'minmax(0, 1fr) minmax(0, 1.4fr) 7rem';
        const backupGrid = 'minmax(0, 1.6fr) 7rem 8rem 12rem';

        return () => h('div', { class: 'view-frame' }, [
            // ═══ hero-band：左侧系统概览 + 右侧操作面板 ═══
            h('section', {
                class: 'grid gap-3',
                style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
            }, [
                // 左侧：hero-panel 系统概览
                h('div', { class: 'hero-panel' }, [
                    h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                        h('span', { class: 'eyebrow' }, '系统设置'),
                        h(Badge, { type: pauseStatus.value?.paused ? 'warning' : 'success' },
                            () => pauseStatus.value?.paused ? '已暂停' : '运行中'),
                    ]),
                    h('h2', {
                        style: 'margin:0; font-size:1.65rem; line-height:1.1; text-wrap:balance; word-break:keep-all;',
                    }, '系统管理'),
                    h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                        h('span', {
                            style: 'font-size:2.4rem; font-weight:500; line-height:1; font-variant-numeric:tabular-nums;',
                        }, String(blacklist.value.length || 0)),
                        h('span', { class: 'muted m-0', style: 'font-size:0.9rem;' }, '条黑名单记录'),
                    ]),
                    h('p', { class: 'muted m-0' }, '管理全局暂停、黑名单与数据备份'),
                ]),
                // 右侧：操作面板 Card
                h('article', {
                    class: 'grid gap-3',
                    style: cardStyle,
                }, [
                    h('div', { class: 'card-header' }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '快速操作'),
                            h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '系统控制'),
                        ]),
                    ]),
                    h('div', { class: 'card-body grid gap-2' }, [
                        h('div', { class: 'flex items-center justify-between' }, [
                            h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '运行状态'),
                            h(Badge, { type: pauseStatus.value?.paused ? 'warning' : 'success' },
                                () => pauseStatus.value?.paused ? '已暂停' : '运行中'),
                        ]),
                        h('div', { class: 'flex items-center justify-between' }, [
                            h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '备份总数'),
                            h('span', {
                                style: 'font-size:0.88rem; font-variant-numeric: tabular-nums;',
                            }, String(backups.value.length || 0)),
                        ]),
                        h('div', { class: 'flex items-center gap-2 flex-wrap' }, [
                            h(Button, {
                                type: pauseStatus.value?.paused ? 'primary' : 'ghost',
                                onClick: togglePause,
                                loading: securityLoading.value,
                            }, () => pauseStatus.value?.paused ? '恢复运行' : '全局暂停'),
                            h(Button, {
                                type: 'ghost',
                                onClick: createBackup,
                                loading: creatingBackup.value,
                            }, () => '创建备份'),
                        ]),
                    ]),
                ]),
            ]),

            // ═══ Tab 切换 ═══
            h('div', { class: 'tabs' },
                tabs.map(t => h('button', {
                    class: ['tab', activeTab.value === t.value ? 'active' : ''].filter(Boolean).join(' '),
                    onClick: () => activeTab.value = t.value,
                }, t.label)),
            ),

            // ═══ Tab 1: 安全控制 ═══
            activeTab.value === 'security' && h('div', { class: 'grid gap-3' }, [
                // 暂停状态 Card
                h('article', {
                    class: 'grid gap-3',
                    style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                }, [
                    h('div', { class: 'card-header' }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '安全控制'),
                            h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '全局暂停'),
                        ]),
                    ]),
                    h('div', { class: 'card-body flex items-center justify-between gap-2 flex-wrap' }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('p', { class: 'm-0' }, pauseStatus.value?.paused
                                ? 'Bot 当前已暂停，所有自动行为已停止'
                                : 'Bot 正在正常运行'),
                            pauseStatus.value?.paused_at && h('p', {
                                class: 'muted m-0',
                                style: 'font-size:0.82rem;',
                            }, `暂停时间：${formatTime(pauseStatus.value.paused_at)}`),
                            pauseStatus.value?.reason && h('p', {
                                class: 'muted m-0',
                                style: 'font-size:0.82rem;',
                            }, `原因：${pauseStatus.value.reason}`),
                        ]),
                        h(Button, {
                            type: pauseStatus.value?.paused ? 'primary' : 'ghost',
                            onClick: togglePause,
                            loading: securityLoading.value,
                        }, () => pauseStatus.value?.paused ? '恢复运行' : '全局暂停'),
                    ]),
                ]),

                // 黑名单表格 Card
                h('article', {
                    class: 'grid gap-3',
                    style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                }, [
                    h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '安全控制'),
                            h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '黑名单管理'),
                        ]),
                        h(Button, { type: 'ghost', size: 'sm', onClick: loadBlacklist }, () => '刷新'),
                    ]),
                    h('div', { class: 'flex items-center gap-2 flex-wrap' }, [
                        h(FormInput, {
                            modelValue: blacklistInput.value,
                            'onUpdate:modelValue': (v) => blacklistInput.value = v,
                            placeholder: '输入用户 UID…',
                            id: 'blacklist-input',
                        }),
                        h(Button, { type: 'primary', onClick: addBlacklist }, () => '添加'),
                    ]),
                    blacklist.value.length === 0
                        ? h(EmptyState, { icon: 'folder', title: '黑名单为空', desc: '当前没有拉黑的用户' })
                        : h('div', { class: 'grid', style: 'gap:0; min-width:0;' }, [
                            h('div', {
                                class: 'grid items-center',
                                style: `grid-template-columns: ${blacklistGrid}; column-gap: calc(var(--spacing) * 2); padding-bottom: calc(var(--spacing) * 2); border-bottom: 1px solid hsl(var(--border)); color: hsl(var(--muted-foreground)); font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.14em;`,
                            }, [
                                h('span', { class: 'whitespace-nowrap' }, '用户 UID'),
                                h('span', { class: 'whitespace-nowrap' }, '原因'),
                                h('span', { class: 'whitespace-nowrap' }, '操作'),
                            ]),
                            ...blacklist.value.map(item => h('div', {
                                key: item.user_id || item.id || item.value,
                                class: 'grid items-center',
                                style: `grid-template-columns: ${blacklistGrid}; column-gap: calc(var(--spacing) * 2); padding: calc(var(--spacing) * 2.3) 0; border-top: 1px solid hsl(var(--border)); font-size: 0.95rem;`,
                            }, [
                                h('span', { class: 'badge badge-info' }, String(item.user_id || item.value || '-')),
                                h('span', { class: 'truncate', style: 'color: hsl(var(--muted-foreground)); font-size:0.88rem;' },
                                    item.reason || '用户拉黑'),
                                h('button', {
                                    class: 'btn btn-sm btn-danger',
                                    onClick: () => removeBlacklist(item),
                                }, '移除'),
                            ])),
                        ]),
                ]),
            ]),

            // ═══ Tab 2: 安全配置（schema 驱动）═══
            activeTab.value === 'safety' && (configLoading.value
                ? h(Loading)
                : h('div', { class: 'grid gap-3' }, [
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                    }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '安全配置'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '安全与审核'),
                            ]),
                            h('div', { class: 'flex items-center gap-2' }, [
                                h(Button, {
                                    type: 'ghost',
                                    size: 'sm',
                                    onClick: loadConfigData,
                                }, () => '刷新'),
                                h(Button, {
                                    type: 'primary',
                                    size: 'sm',
                                    onClick: () => saveConfigSection('safety'),
                                    loading: configSaving.value,
                                    disabled: !configIsDirty.value,
                                }, () => '保存'),
                            ]),
                        ]),
                        h('div', { class: 'card-body' }, [
                            safetySchema.value.length > 0
                                ? h(SchemaForm, {
                                    schema: safetySchema.value,
                                    modelValue: configLocalModel.value,
                                    'onUpdate:modelValue': (v) => { configLocalModel.value = v; },
                                })
                                : h(EmptyState, { title: '暂无配置项', desc: '该分类下没有可配置字段' }),
                        ]),
                    ]),
                ])),

            // ═══ Tab 3: 面板配置（schema 驱动）═══
            activeTab.value === 'web' && (configLoading.value
                ? h(Loading)
                : h('div', { class: 'grid gap-3' }, [
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                    }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '面板配置'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, 'Web 管理面板'),
                            ]),
                            h('div', { class: 'flex items-center gap-2' }, [
                                h(Button, {
                                    type: 'ghost',
                                    size: 'sm',
                                    onClick: loadConfigData,
                                }, () => '刷新'),
                                h(Button, {
                                    type: 'primary',
                                    size: 'sm',
                                    onClick: () => saveConfigSection('web'),
                                    loading: configSaving.value,
                                    disabled: !configIsDirty.value,
                                }, () => '保存'),
                            ]),
                        ]),
                        h('div', { class: 'card-body' }, [
                            webSchema.value.length > 0
                                ? h(SchemaForm, {
                                    schema: webSchema.value,
                                    modelValue: configLocalModel.value,
                                    'onUpdate:modelValue': (v) => { configLocalModel.value = v; },
                                })
                                : h(EmptyState, { title: '暂无配置项', desc: '该分类下没有可配置字段' }),
                        ]),
                    ]),
                    h('div', {
                        class: 'grid gap-2',
                        style: 'padding: calc(var(--spacing) * 3); border-radius: calc(var(--radius) * 0.76); background: hsl(var(--chart-5) / 0.08); border: 1px solid hsl(var(--chart-5) / 0.2);',
                    }, [
                        h('p', {
                            class: 'muted m-0',
                            style: 'font-size:0.88rem; line-height:1.6;',
                        }, '注意：修改 host / port / enabled / secret_key / secure_cookies 等字段需要重启应用才能生效。'),
                    ]),
                ])),

            // ═══ Tab 4: 备份恢复 ═══
            activeTab.value === 'backup' && h('div', { class: 'grid gap-3' }, [
                h('article', {
                    class: 'grid gap-3',
                    style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                }, [
                    h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '备份恢复'),
                            h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '备份列表'),
                        ]),
                        h(Button, {
                            type: 'primary',
                            onClick: createBackup,
                            loading: creatingBackup.value,
                        }, () => '创建备份'),
                    ]),
                    backups.value.length === 0
                        ? h(EmptyState, { icon: 'folder', title: '暂无备份', desc: '点击右上角创建第一个备份' })
                        : h('div', { class: 'grid', style: 'gap:0; min-width:0;' }, [
                            h('div', {
                                class: 'grid items-center',
                                style: `grid-template-columns: ${backupGrid}; column-gap: calc(var(--spacing) * 2); padding-bottom: calc(var(--spacing) * 2); border-bottom: 1px solid hsl(var(--border)); color: hsl(var(--muted-foreground)); font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.14em;`,
                            }, [
                                h('span', { class: 'whitespace-nowrap' }, '名称'),
                                h('span', { class: 'whitespace-nowrap' }, '文件数'),
                                h('span', { class: 'whitespace-nowrap' }, '创建时间'),
                                h('span', { class: 'whitespace-nowrap' }, '操作'),
                            ]),
                            ...backups.value.map(b => h('div', {
                                key: b.name,
                                class: 'grid items-center',
                                style: `grid-template-columns: ${backupGrid}; column-gap: calc(var(--spacing) * 2); padding: calc(var(--spacing) * 2.3) 0; border-top: 1px solid hsl(var(--border)); font-size: 0.95rem;`,
                            }, [
                                h('span', { class: 'truncate' }, b.name || '-'),
                                h('span', {
                                    class: 'whitespace-nowrap',
                                    style: 'color: hsl(var(--muted-foreground)); font-variant-numeric: tabular-nums; font-size:0.88rem;',
                                }, String(b.file_count || 0)),
                                h('span', {
                                    class: 'whitespace-nowrap',
                                    style: 'color: hsl(var(--muted-foreground)); font-variant-numeric: tabular-nums; font-size:0.82rem;',
                                }, formatTime(b.backup_timestamp)),
                                h('div', { class: 'flex items-center gap-1 flex-wrap' }, [
                                    h('button', {
                                        class: 'btn btn-sm ghost',
                                        onClick: () => downloadBackup(b),
                                    }, '下载'),
                                    h('button', {
                                        class: 'btn btn-sm primary',
                                        onClick: () => openRestore(b),
                                    }, '恢复'),
                                    h('button', {
                                        class: 'btn btn-sm btn-danger',
                                        onClick: () => deleteBackup(b),
                                    }, '删除'),
                                ]),
                            ])),
                        ]),
                ]),
            ]),

            // ═══ 恢复确认弹窗 ═══
            h(Modal, {
                modelValue: restoreModal.visible,
                title: '确认恢复备份',
                'onUpdate:modelValue': (v) => restoreModal.visible = v,
            }, {
                default: () => h('div', {
                    style: 'padding: calc(var(--spacing) * 3); border-radius: calc(var(--radius) * 0.76); background: hsl(var(--chart-5) / 0.12); border: 1px solid hsl(var(--chart-5) / 0.4); display:grid; gap: calc(var(--spacing) * 2);',
                }, [
                    h('p', { class: 'm-0', style: 'font-weight:500;' },
                        `即将恢复备份 ${restoreModal.backup?.name || ''}`),
                    h('p', {
                        class: 'muted m-0',
                        style: 'font-size:0.88rem; line-height:1.6;',
                    }, '恢复操作将覆盖当前数据，且不可撤销。建议在低峰期执行，恢复后需重启服务。'),
                ]),
                footer: () => h('div', { class: 'flex gap-2 justify-end' }, [
                    h(Button, { onClick: () => restoreModal.visible = false }, () => '取消'),
                    h(Button, { type: 'ghost', onClick: confirmRestore, loading: restoreModal.confirming }, () => '确认恢复'),
                ]),
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
};
