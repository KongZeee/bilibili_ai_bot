// pages/model-routing.js - 模型分配页面（Golden Time 设计稿）
// 显示 5 个功能卡片，每个卡片可下拉切换当前路由的 Provider
const { defineComponent, h, ref, reactive, onMounted, computed } = window.Vue;
import { api } from '../api.js';
import { showToast } from '../state.js';
import { Button, Badge, Loading, Icon } from '../components/common.js';
import { navigate } from '../router.js';

// 功能卡片元信息（与后端 PROVIDER_TYPES / FEATURE_LABELS 对齐）
const FEATURE_META = [
    { type: 'chat',      label: '对话',       icon: 'message-circle-more', desc: '主动回复 / 动态 / 记忆提取' },
    { type: 'vision',    label: '视觉',       icon: 'circle-check',        desc: '视频画面理解' },
    { type: 'embedding', label: '向量检索',   icon: 'star',                desc: '记忆向量检索' },
    { type: 'asr',       label: '语音识别',   icon: 'file',                desc: '视频音频转写' },
    { type: 'image',     label: '文生图',     icon: 'folder-open',         desc: '动态配图生成' },
];

export const ModelRoutingPage = defineComponent({
    name: 'ModelRoutingPage',
    setup() {
        const loading = ref(false);
        const overview = ref({ routing: {}, features: {}, local_whisper: {} });
        // 各功能卡片下拉选择中的临时值（未保存）
        const pending = reactive({});
        // 各功能卡片切换中状态
        const switching = reactive({});

        async function loadOverview() {
            loading.value = true;
            try {
                const data = await api.modelRouting.getOverview();
                overview.value = data || {};
                // 同步 pending 为当前路由值
                for (const feat of FEATURE_META) {
                    pending[feat.type] = data?.features?.[feat.type]?.routed_provider_id || '';
                }
            } catch (e) {
                showToast('加载模型路由失败: ' + e.message, 'error');
            } finally {
                loading.value = false;
            }
        }

        async function switchProvider(type) {
            const providerId = pending[type] || '';
            switching[type] = true;
            try {
                await api.modelRouting.updateRouting({ [type]: providerId });
                showToast('路由已更新', 'success');
                await loadOverview();
            } catch (e) {
                showToast('切换失败: ' + e.message, 'error');
                // 回退 pending
                pending[type] = overview.value?.features?.[type]?.routed_provider_id || '';
            } finally {
                switching[type] = false;
            }
        }

        onMounted(loadOverview);

        const features = computed(() => overview.value.features || {});
        const routing = computed(() => overview.value.routing || {});

        // 渲染单个功能卡片
        function renderFeatureCard(feat) {
            const info = features.value[feat.type] || {};
            const providers = info.providers || [];
            const routed = info.routed_provider;
            const routedId = info.routed_provider_id || '';
            const isSwitching = !!switching[feat.type];
            const selectId = `mr-select-${feat.type}`;
            const cardStyle = 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start; display: grid; gap: calc(var(--spacing) * 3);';

            return h('article', {
                key: feat.type,
                class: 'grid',
                style: cardStyle,
            }, [
                // 顶部：图标 + 名称 + 状态徽标
                h('div', { class: 'flex items-start justify-between gap-2' }, [
                    h('div', { class: 'flex items-center gap-2' }, [
                        h('span', {
                            class: 'inline-flex items-center justify-center',
                            style: 'width: 2.2rem; height: 2.2rem; border-radius: calc(var(--radius) * 0.6); background: hsl(var(--accent) / 0.18); color: hsl(var(--accent-foreground));',
                        }, [h(Icon, { name: feat.icon, size: '1.15rem' })]),
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow', style: 'margin:0;' }, feat.label),
                            h('h3', {
                                style: 'margin:0; font-size:1.1rem; line-height:1.2; font-weight:500;',
                            }, feat.label + ' 模型'),
                        ]),
                    ]),
                    h(Badge, { type: routed ? 'success' : 'warning' }, () => routed ? '已路由' : '未设置'),
                ]),

                // 中部：当前路由的 Provider 信息
                h('div', { class: 'grid gap-1' }, [
                    h('span', { class: 'muted', style: 'font-size:0.82rem;' }, feat.desc),
                    routed
                        ? h('div', { class: 'grid gap-1', style: 'margin-top:calc(var(--spacing) * 1);' }, [
                            h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                                h('span', {
                                    style: 'font-size:1.15rem; font-weight:500; line-height:1.2;',
                                }, routed.model || '-'),
                                h('span', { class: 'muted', style: 'font-size:0.82rem;' }, routed.name || routed.id),
                            ]),
                            h('span', {
                                class: 'truncate muted',
                                style: 'font-size:0.8rem;',
                            }, routed.base_url || '-'),
                        ])
                        : h('div', {
                            style: 'padding: calc(var(--spacing) * 2); border-radius: calc(var(--radius) * 0.6); background: hsl(var(--muted) / 0.18); color: hsl(var(--muted-foreground)); font-size:0.86rem; margin-top:calc(var(--spacing) * 1);',
                        }, '尚未分配服务商'),
                ]),

                // 底部：服务商下拉切换
                h('div', { class: 'grid gap-1' }, [
                    h('label', {
                        class: 'form-label',
                        for: selectId,
                        style: 'font-size:0.78rem; letter-spacing: 0.06em;',
                    }, '切换服务商'),
                    h('div', { class: 'flex items-center gap-2' }, [
                        h('select', {
                            class: 'form-input',
                            id: selectId,
                            value: pending[feat.type] ?? routedId,
                            disabled: isSwitching || providers.length === 0,
                            onChange: (e) => { pending[feat.type] = e.target.value; },
                            style: 'flex: 1 1 auto;',
                        }, providers.length === 0
                            ? [h('option', { value: '' }, '暂无可用服务商')]
                            : [
                                h('option', { value: '' }, '— 未分配 —'),
                                ...providers.map(p => h('option', {
                                    value: p.id,
                                }, `${p.name || p.id} · ${p.model || '-'}`)),
                            ]),
                        h(Button, {
                            type: 'primary',
                            size: 'sm',
                            loading: isSwitching,
                            disabled: providers.length === 0 || (pending[feat.type] ?? routedId) === routedId,
                            onClick: () => switchProvider(feat.type),
                        }, () => '应用'),
                    ]),
                    h('span', {
                        class: 'muted',
                        style: 'font-size:0.74rem;',
                    }, `共 ${providers.length} 个服务商`),
                ]),
            ]);
        }

        return () => loading.value && !features.value.chat
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                // ═══ hero-band：模型分配总览 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左侧：hero-panel
                    h('div', { class: 'hero-panel' }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('span', { class: 'eyebrow' }, '模型分配'),
                            h(Badge, { type: 'info' }, () => '功能路由'),
                        ]),
                        h('h2', {
                            style: 'margin:0; font-size:1.65rem; line-height:1.1; text-wrap:balance; word-break:keep-all;',
                        }, '功能 → 服务商路由'),
                        h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                            h('span', {
                                style: 'font-size:2.4rem; font-weight:500; line-height:1; font-variant-numeric:tabular-nums;',
                            }, String(FEATURE_META.filter(f => routing.value[f.type]).length || 0)),
                            h('span', { class: 'muted m-0', style: 'font-size:0.9rem;' }, `/ ${FEATURE_META.length} 项功能已路由`),
                        ]),
                        h('p', { class: 'muted m-0' }, '为对话、视觉、向量检索、语音识别、文生图各自指定服务商'),
                    ]),
                    // 右侧：操作引导
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '快速操作'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '服务商管理'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-2' }, [
                            h('p', { class: 'muted m-0', style: 'font-size:0.88rem; line-height:1.6;' }, '本页只为各功能指定已有服务商；新增 / 编辑 / 删除请前往「模型管理」。'),
                            h('div', { class: 'flex items-center gap-2 flex-wrap' }, [
                                h(Button, {
                                    type: 'primary',
                                    size: 'sm',
                                    onClick: () => navigate('/llm'),
                                }, () => '前往模型管理'),
                                h(Button, {
                                    type: 'ghost',
                                    size: 'sm',
                                    onClick: loadOverview,
                                    loading: loading.value,
                                }, () => '刷新'),
                            ]),
                        ]),
                    ]),
                ]),

                // ═══ 5 个功能卡片网格 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr));',
                }, FEATURE_META.map(renderFeatureCard)),
            ]);
    },
});
