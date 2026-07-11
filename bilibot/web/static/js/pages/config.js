// bilibot/web/static/js/pages/config.js
import { h, ref, reactive, onMounted, computed } from '../vendor/vue.esm-browser.prod.js';
import { appState } from '../state.js';
import { api } from '../api.js';
import { Card, Button, Badge, SchemaForm } from '../components.js';

export const ConfigPage = {
    setup() {
        const loading = ref(false);
        const saving = ref(false);
        const schema = ref([]);
        const configData = reactive({});
        const version = ref(0);
        const lastSaved = ref(null);
        const diff = ref({});

        async function loadConfig() {
            loading.value = true;
            try {
                const res = await api.config.getWithSchema();
                schema.value = res.data.schema || [];
                Object.assign(configData, res.data.config || {});
                version.value = res.data.version || 0;
                diff.value = {};
            } catch (e) {
                appState.notify('加载配置失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function saveConfig() {
            saving.value = true;
            try {
                // 构造 payload：只发送有变化的字段 + version
                const payload = { ...configData, version: version.value };
                // 敏感字段为空时不发送（保持原值）
                for (const field of schema.value) {
                    if (field.sensitive && !payload[field.key]) {
                        delete payload[field.key];
                    }
                }
                const res = await api.config.update(payload);
                version.value = res.data.version || version.value + 1;
                lastSaved.value = new Date().toLocaleString();
                diff.value = {};
                appState.notify('配置已保存', 'success');
                // 检查是否需要重启
                if (res.data.need_restart) {
                    appState.notify('部分配置需重启后生效', 'warning');
                }
            } catch (e) {
                const msg = e.message || String(e);
                if (msg.includes('version') || msg.includes('409')) {
                    appState.notify('配置已被其他人修改，请刷新后重试', 'warning');
                    loadConfig();
                } else {
                    appState.notify('保存失败：' + msg, 'danger');
                }
            } finally {
                saving.value = false;
            }
        }

        async function resetConfig() {
            if (!confirm('确认重置为默认配置？此操作不可撤销。')) return;
            try {
                await api.config.reset();
                appState.notify('配置已重置', 'success');
                loadConfig();
            } catch (e) {
                appState.notify('重置失败：' + (e.message || e), 'danger');
            }
        }

        async function exportConfig() {
            try {
                const res = await api.config.export();
                const blob = new Blob([JSON.stringify(res.data, null, 2)], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `config-${new Date().toISOString().slice(0, 10)}.json`;
                a.click();
                URL.revokeObjectURL(url);
                appState.notify('配置已导出', 'success');
            } catch (e) {
                appState.notify('导出失败：' + (e.message || e), 'danger');
            }
        }

        onMounted(loadConfig);

        return () => h('div', [
            h(Card, { title: '系统配置' }, {
                action: () => h('div', { class: 'flex gap-2' }, [
                    h(Button, { size: 'sm', onClick: exportConfig }, () => '导出'),
                    h(Button, { size: 'sm', type: 'danger', onClick: resetConfig }, () => '重置默认'),
                ]),
                default: () => [
                    h(SchemaForm, {
                        schema: schema.value,
                        modelValue: configData,
                        'onUpdate:modelValue': (v) => Object.assign(configData, v),
                    }),
                ],
                footer: () => h('div', { class: 'flex justify-between items-center' }, [
                    h('div', { class: 'text-muted', style: 'font-size:12px' }, [
                        version.value > 0 && h('span', `当前版本 v${version.value}`),
                        lastSaved.value && h('span', { class: 'ml-2' }, `| 上次保存：${lastSaved.value}`),
                    ]),
                    h('div', { class: 'flex gap-2' }, [
                        h(Button, { onClick: loadConfig, disabled: saving.value }, () => '重新加载'),
                        h(Button, { type: 'primary', onClick: saveConfig, loading: saving.value }, () => '保存配置'),
                    ]),
                ]),
            }),
        ]);
    },
};
