// pages/logs.js - 日志查看页（黑色控制台样式）
const { defineComponent, h, ref, onMounted, computed, nextTick } = window.Vue;
import { api } from '../api.js';
import { showToast } from '../state.js';
import { Button, FormInput, Loading, EmptyState, Icon } from '../components/common.js';

export const LogsPage = defineComponent({
    name: 'LogsPage',
    setup() {
        const logs = ref([]);
        const loading = ref(false);
        const level = ref('');
        const keyword = ref('');
        const limit = ref(200);
        const autoScroll = ref(true);
        const consoleBodyRef = ref(null);

        const levelFilters = [
            { value: '', label: '全部', color: '#00ff9c' },
            { value: 'DEBUG', label: '调试', color: '#56b6c2' },
            { value: 'INFO', label: '信息', color: '#61afef' },
            { value: 'WARNING', label: '警告', color: '#e5c07b' },
            { value: 'ERROR', label: '错误', color: '#e06c75' },
        ];

        const LEVEL_ZH = {
            DEBUG: '调试',
            INFO: '信息',
            WARNING: '警告',
            ERROR: '错误',
            CRITICAL: '严重',
        };

        // 统计各级别数量
        const levelCounts = computed(() => {
            const counts = { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0, CRITICAL: 0 };
            logs.value.forEach(l => {
                const lv = (l.level || 'INFO').toUpperCase();
                if (counts[lv] !== undefined) counts[lv]++;
            });
            return counts;
        });

        async function load() {
            loading.value = true;
            try {
                const data = await api.logs({
                    level: level.value || undefined,
                    keyword: keyword.value || undefined,
                    limit: limit.value,
                });
                logs.value = data.items || data || [];
                if (autoScroll.value) {
                    await nextTick();
                    scrollToBottom();
                }
            } catch (e) {
                showToast('加载失败: ' + e.message, 'error');
            } finally { loading.value = false; }
        }

        function scrollToBottom() {
            const el = consoleBodyRef.value;
            if (el) el.scrollTop = el.scrollHeight;
        }

        async function download() {
            try {
                const resp = await fetch('/api/logs/download', { credentials: 'same-origin' });
                if (resp.status === 401) {
                    window.location.href = '/login';
                    return;
                }
                const blob = await resp.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url; a.download = 'bililog.txt'; a.click();
                URL.revokeObjectURL(url);
                showToast('日志已下载', 'success');
            } catch (e) { showToast('下载失败', 'error'); }
        }

        function setLevel(v) {
            level.value = v;
            load();
        }

        function levelColor(lv) {
            const map = {
                DEBUG: '#56b6c2',
                INFO: '#61afef',
                WARNING: '#e5c07b',
                ERROR: '#e06c75',
                CRITICAL: '#ff6b6b',
            };
            return map[(lv || '').toUpperCase()] || '#abb2bf';
        }

        onMounted(load);

        return () => loading.value && logs.value.length === 0
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                // ═══ 终端标题栏 ═══
                h('div', {
                    style: 'display:flex; align-items:center; justify-content:space-between; gap:0.75rem; flex-wrap:wrap; padding:0.6rem 1rem; background:#1a1a1a; border:1px solid #2a2a2a; border-bottom:none; border-radius:8px 8px 0 0;',
                }, [
                    // 左侧：红黄绿圆点 + 标题
                    h('div', {
                        style: 'display:flex; align-items:center; gap:0.75rem;',
                    }, [
                        h('div', {
                            style: 'display:flex; gap:0.4rem;',
                        }, [
                            h('span', { style: 'width:11px; height:11px; border-radius:50%; background:#ff5f57; display:inline-block;' }),
                            h('span', { style: 'width:11px; height:11px; border-radius:50%; background:#febc2e; display:inline-block;' }),
                            h('span', { style: 'width:11px; height:11px; border-radius:50%; background:#28c840; display:inline-block;' }),
                        ]),
                        h('span', {
                            style: 'color:#e5e5e5; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:0.82rem; font-weight:500;',
                        }, '系统日志'),
                    ]),
                    // 右侧：状态
                    h('div', {
                        style: 'display:flex; align-items:center; gap:0.6rem; color:#888; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:0.75rem;',
                    }, [
                        h('span', { style: `color:${levelCounts.value.ERROR > 0 ? '#e06c75' : '#56b6c2'};` },
                            `● ${logs.value.length} 行`),
                        levelCounts.value.ERROR > 0 && h('span', { style: 'color:#e06c75;' },
                            `✕ ${levelCounts.value.ERROR} 错误`),
                        levelCounts.value.WARNING > 0 && h('span', { style: 'color:#e5c07b;' },
                            `⚠ ${levelCounts.value.WARNING} 警告`),
                    ]),
                ]),

                // ═══ 工具栏 ═══
                h('div', {
                    style: 'display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap; padding:0.5rem 0.75rem; background:#1e1e1e; border:1px solid #2a2a2a; border-top:none; border-bottom:none;',
                }, [
                    // 级别筛选标签
                    ...levelFilters.map(f => h('button', {
                        key: f.value,
                        style: `padding:0.2rem 0.55rem; border-radius:4px; border:1px solid ${level.value === f.value ? f.color : '#333'}; background:${level.value === f.value ? f.color + '22' : 'transparent'}; color:${level.value === f.value ? f.color : '#888'}; font-family:ui-monospace,monospace; font-size:0.72rem; cursor:pointer; transition:all 0.15s;`,
                        onClick: () => setLevel(f.value),
                    }, f.label)),
                    // 分隔
                    h('span', { style: 'flex:1;' }),
                    // 搜索框
                    h('input', {
                        value: keyword.value,
                        onInput: (e) => keyword.value = e.target.value,
                        onKeyup: (e) => { if (e.key === 'Enter') load(); },
                        placeholder: '搜索关键词…',
                        style: 'flex:0 1 220px; min-width:140px; padding:0.25rem 0.6rem; background:#0d0d0d; border:1px solid #333; border-radius:4px; color:#e5e5e5; font-family:ui-monospace,monospace; font-size:0.78rem; outline:none;',
                    }),
                    h('button', {
                        style: 'padding:0.25rem 0.7rem; border-radius:4px; border:1px solid #2d5a2d; background:#1a3a1a; color:#28c840; font-family:ui-monospace,monospace; font-size:0.72rem; cursor:pointer;',
                        onClick: load,
                    }, '↻ 刷新'),
                    h('button', {
                        style: 'padding:0.25rem 0.7rem; border-radius:4px; border:1px solid #333; background:transparent; color:#888; font-family:ui-monospace,monospace; font-size:0.72rem; cursor:pointer;',
                        onClick: () => { autoScroll.value = !autoScroll.value; if (autoScroll.value) scrollToBottom(); },
                    }, autoScroll.value ? '⇩ 自动滚动' : '⇩ 暂停'),
                    h('button', {
                        style: 'padding:0.25rem 0.7rem; border-radius:4px; border:1px solid #333; background:transparent; color:#888; font-family:ui-monospace,monospace; font-size:0.72rem; cursor:pointer;',
                        onClick: download,
                    }, '⤓ 下载'),
                ]),

                // ═══ 终端日志输出区 ═══
                h('div', {
                    ref: (el) => { consoleBodyRef.value = el; },
                    style: 'background:#0d0d0d; border:1px solid #2a2a2a; border-top:none; border-radius:0 0 8px 8px; padding:0.75rem 0.5rem; max-height:65vh; min-height:400px; overflow-y:auto; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Courier New",monospace; font-size:0.8rem; line-height:1.55;',
                }, [
                    logs.value.length === 0
                        ? h('div', {
                            style: 'color:#555; text-align:center; padding:3rem 0; font-style:italic;',
                        }, '暂无日志')
                        : logs.value.map((l, i) => {
                            const lv = (l.level || 'INFO').toUpperCase();
                            return h('div', {
                                key: i,
                                style: 'display:flex; align-items:flex-start; gap:0; padding:0.1rem 0.5rem; border-radius:2px; transition:background 0.1s; white-space:pre; overflow-x:hidden;',
                                onMouseenter: (e) => e.target.style.background = '#1a1a1a',
                                onMouseleave: (e) => e.target.style.background = 'transparent',
                            }, [
                                h('span', {
                                    style: 'flex:0 0 3.5rem; color:#444; user-select:none; text-align:right; padding-right:0.6rem; min-width:3.5rem;',
                                }, String(i + 1).padStart(4, ' ')),
                                h('span', {
                                    style: 'flex:0 0 auto; color:#666; padding-right:0.6rem; white-space:nowrap;',
                                }, (l.ts || l.timestamp || '').padEnd(19, ' ')),
                                h('span', {
                                    style: `flex:0 0 auto; color:${levelColor(l.level)}; font-weight:600; padding-right:0.6rem; white-space:nowrap;`,
                                    title: lv,
                                }, `[${LEVEL_ZH[lv] || lv}]`.padEnd(6, ' ')),
                                h('span', {
                                    style: 'flex:1; min-width:0; color:#d4d4d4; white-space:pre-wrap; word-break:break-all;',
                                }, l.line || l.message || l.msg || ''),
                            ]);
                        }),
                ]),
            ]);
    },
});
