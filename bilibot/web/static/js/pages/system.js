// bilibot/web/static/js/pages/system.js
import { h, ref, reactive, onMounted, computed } from '../vendor/vue.esm-browser.prod.js';
import { appState } from '../state.js';
import { api } from '../api.js';
import { Card, Button, Badge, DataTable, Modal, FormInput, FormTextarea, FormSelect, Toggle, EmptyState, Pagination } from '../components.js';
import { formatTime, formatDateTime } from '../utils.js';

export const SystemPage = {
    setup() {
        const activeTab = ref('security'); // security / backup

        // 安全控制
        const securityLoading = ref(false);
        const pauseStatus = ref(null);
        const blacklist = ref([]);
        const blacklistInput = ref('');
        const blacklistType = ref('uid');

        // 备份恢复
        const backupLoading = ref(false);
        const backups = ref([]);
        const creatingBackup = ref(false);
        const restoreModal = reactive({
            visible: false,
            backup: null,
            confirming: false,
        });

        async function loadPauseStatus() {
            securityLoading.value = true;
            try {
                const res = await api.safety.getPauseStatus();
                pauseStatus.value = res.data;
            } catch (e) {
                appState.notify('加载暂停状态失败：' + (e.message || e), 'danger');
            } finally {
                securityLoading.value = false;
            }
        }

        async function togglePause() {
            if (!pauseStatus.value) return;
            const isPaused = pauseStatus.value.paused;
            if (!confirm(isPaused ? '确认恢复 Bot 运行？' : '确认全局暂停 Bot？所有自动行为将停止。')) return;
            try {
                if (isPaused) {
                    await api.safety.resume();
                    appState.notify('已恢复运行', 'success');
                } else {
                    await api.safety.pause({ reason: '手动暂停' });
                    appState.notify('已暂停', 'warning');
                }
                loadPauseStatus();
            } catch (e) {
                appState.notify('操作失败：' + (e.message || e), 'danger');
            }
        }

        async function loadBlacklist() {
            try {
                const res = await api.safety.getBlacklist();
                blacklist.value = res.data.items || [];
            } catch (e) {
                appState.notify('加载黑名单失败：' + (e.message || e), 'danger');
            }
        }

        async function addBlacklist() {
            if (!blacklistInput.value) return;
            try {
                await api.safety.addBlacklist({
                    type: blacklistType.value,
                    value: blacklistInput.value,
                });
                blacklistInput.value = '';
                appState.notify('已添加到黑名单', 'success');
                loadBlacklist();
            } catch (e) {
                appState.notify('添加失败：' + (e.message || e), 'danger');
            }
        }

        async function removeBlacklist(item) {
            if (!confirm(`确认从黑名单移除 ${item.type}: ${item.value}？`)) return;
            try {
                await api.safety.removeBlacklist(item.id);
                appState.notify('已移除', 'success');
                loadBlacklist();
            } catch (e) {
                appState.notify('移除失败：' + (e.message || e), 'danger');
            }
        }

        async function loadBackups() {
            backupLoading.value = true;
            try {
                const res = await api.backup.list();
                backups.value = res.data.items || [];
            } catch (e) {
                appState.notify('加载备份列表失败：' + (e.message || e), 'danger');
            } finally {
                backupLoading.value = false;
            }
        }

        async function createBackup() {
            creatingBackup.value = true;
            try {
                await api.backup.create({ description: '手动备份' });
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
                await api.backup.restore(restoreModal.backup.id);
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
                const res = await api.backup.download(backup.id);
                const blob = new Blob([res], { type: 'application/octet-stream' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = backup.filename || `backup-${backup.id}.tar.gz`;
                a.click();
                URL.revokeObjectURL(url);
            } catch (e) {
                appState.notify('下载失败：' + (e.message || e), 'danger');
            }
        }

        async function deleteBackup(backup) {
            if (!confirm(`确认删除备份 ${backup.filename || backup.id}？此操作不可撤销。`)) return;
            try {
                await api.backup.delete(backup.id);
                appState.notify('备份已删除', 'success');
                loadBackups();
            } catch (e) {
                appState.notify('删除失败：' + (e.message || e), 'danger');
            }
        }

        onMounted(() => {
            loadPauseStatus();
            loadBlacklist();
            loadBackups();
        });

        const blacklistColumns = [
            { key: 'type', label: '类型', width: '100px' },
            { key: 'value', label: '值' },
            { key: 'created_at', label: '添加时间', width: '160px' },
            { key: 'actions', label: '操作', width: '100px' },
        ];

        const backupColumns = [
            { key: 'id', label: 'ID', width: '60px' },
            { key: 'filename', label: '文件名' },
            { key: 'size', label: '大小', width: '100px' },
            { key: 'created_at', label: '创建时间', width: '160px' },
            { key: 'actions', label: '操作', width: '240px' },
        ];

        const tabs = [
            { value: 'security', label: '安全控制' },
            { value: 'backup', label: '备份恢复' },
        ];

        return () => h('div', [
            // Tab 切换
            h('div', { class: 'tabs mb-4' },
                tabs.map(t => h('button', {
                    class: ['tab', activeTab.value === t.value && 'active'].filter(Boolean).join(' '),
                    onClick: () => activeTab.value = t.value,
                }, t.label)),
            ),

            // 安全控制 Tab
            activeTab.value === 'security' && h('div', [
                h(Card, { title: '全局暂停' }, {
                    default: () => h('div', { class: 'flex items-center justify-between' }, [
                        h('div', [
                            h('p', { class: 'mb-1' }, pauseStatus.value?.paused
                                ? 'Bot 当前已暂停，所有自动行为已停止'
                                : 'Bot 正在正常运行'),
                            pauseStatus.value?.paused_at && h('p', { class: 'text-muted', style: 'font-size:12px' },
                                `暂停时间：${formatTime(pauseStatus.value.paused_at)}`),
                            pauseStatus.value?.reason && h('p', { class: 'text-muted', style: 'font-size:12px' },
                                `原因：${pauseStatus.value.reason}`),
                        ]),
                        h(Button, {
                            type: pauseStatus.value?.paused ? 'primary' : 'danger',
                            onClick: togglePause,
                            loading: securityLoading.value,
                        }, () => pauseStatus.value?.paused ? '恢复运行' : '全局暂停'),
                    ]),
                }),

                h(Card, { title: '黑名单管理' }, {
                    action: () => h(Button, { size: 'sm', onClick: loadBlacklist }, () => '刷新'),
                    default: () => h('div', [
                        h('div', { class: 'flex gap-2 mb-4' }, [
                            h(FormSelect, {
                                modelValue: blacklistType.value,
                                'onUpdate:modelValue': (v) => blacklistType.value = v,
                                options: [
                                    { value: 'uid', label: '用户 UID' },
                                    { value: 'keyword', label: '关键词' },
                                    { value: 'ip', label: 'IP 地址' },
                                ],
                                style: 'width:140px',
                            }),
                            h(FormInput, {
                                modelValue: blacklistInput.value,
                                'onUpdate:modelValue': (v) => blacklistInput.value = v,
                                placeholder: '输入要拉黑的值',
                                style: 'flex:1',
                                onKeyup: (e) => { if (e.key === 'Enter') addBlacklist(); },
                            }),
                            h(Button, { type: 'primary', onClick: addBlacklist }, () => '添加'),
                        ]),
                        h(DataTable, {
                            columns: blacklistColumns,
                            rows: blacklist.value,
                            empty: '黑名单为空',
                        }, {
                            type: ({ value }) => h(Badge, { type: 'info', size: 'sm' }, () => value),
                            created_at: ({ value }) => h('span', { class: 'text-muted', style: 'font-size:12px' }, formatTime(value)),
                            actions: ({ row }) => h(Button, { size: 'sm', type: 'danger', onClick: () => removeBlacklist(row) }, () => '移除'),
                        }),
                    ]),
                }),
            ]),

            // 备份恢复 Tab
            activeTab.value === 'backup' && h('div', [
                h(Card, { title: '备份管理' }, {
                    action: () => h(Button, { type: 'primary', onClick: createBackup, loading: creatingBackup.value }, () => '创建备份'),
                    default: () => h(DataTable, {
                        columns: backupColumns,
                        rows: backups.value,
                        loading: backupLoading.value,
                        empty: '暂无备份',
                    }, {
                        size: ({ value }) => h('span', { class: 'text-muted' }, `${(value / 1024 / 1024).toFixed(2)} MB`),
                        created_at: ({ value }) => h('span', { class: 'text-muted', style: 'font-size:12px' }, formatTime(value)),
                        actions: ({ row }) => h('div', { class: 'flex gap-2' }, [
                            h(Button, { size: 'sm', onClick: () => downloadBackup(row) }, () => '下载'),
                            h(Button, { size: 'sm', type: 'primary', onClick: () => openRestore(row) }, () => '恢复'),
                            h(Button, { size: 'sm', type: 'danger', onClick: () => deleteBackup(row) }, () => '删除'),
                        ]),
                    }),
                }),
            ]),

            // 恢复确认弹窗
            h(Modal, {
                visible: restoreModal.visible,
                title: '确认恢复备份',
                'onUpdate:visible': (v) => restoreModal.visible = v,
            }, {
                default: () => h('div', { class: 'alert alert-warning' }, [
                    h('p', { class: 'mb-2' }, `即将恢复备份 #${restoreModal.backup?.id}（${restoreModal.backup?.filename || ''}）`),
                    h('p', { class: 'text-muted', style: 'font-size:13px' }, '恢复操作将覆盖当前数据，且不可撤销。建议在低峰期执行，恢复后需重启服务。'),
                ]),
                footer: () => h('div', { class: 'flex gap-2 justify-end' }, [
                    h(Button, { onClick: () => restoreModal.visible = false }, () => '取消'),
                    h(Button, { type: 'danger', onClick: confirmRestore, loading: restoreModal.confirming }, () => '确认恢复'),
                ]),
            }),
        ]);
    },
};
