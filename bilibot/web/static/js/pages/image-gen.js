// bilibot/web/static/js/pages/image-gen.js - 文生图配置页（Golden Time 设计稿）
// 模型配置已迁移到「模型分配」页，本页仅保留 with_image 开关 + 只读 Provider 信息 + 连接测试
const { h, ref, reactive, onMounted, computed } = window.Vue;
import { appState } from '../state.js';
import { api } from '../api.js';
import { Button, Badge, FormInput, FormTextarea, Toggle, FormHint, Loading, EmptyState } from '../components/common.js';

export const ImageGenPage = {
    name: 'ImageGenPage',
    setup() {
        const loading = ref(false);
        const testing = ref(false);
        const withImage = ref(false);
        const savingWithImage = ref(false);
        const provider = ref(null);        // 路由到的 image provider
        const testResult = ref(null);      // {success, message}
        const genPrompt = ref('一只可爱的猫坐在窗台上，阳光明媚');
        const generating = ref(false);
        const generatedImage = ref(null);  // {b64, prompt, model, size}
        const genError = ref('');

        async function loadData() {
            loading.value = true;
            try {
                const [igRes, overview] = await Promise.all([
                    api.imageGen.getConfig(),
                    api.modelRouting.getOverview(),
                ]);
                withImage.value = igRes.with_image ?? false;
                const feat = overview.features?.image;
                provider.value = feat?.routed_provider || null;
            } catch (e) {
                appState.notify('加载配置失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function toggleWithImage(val) {
            withImage.value = val;
            savingWithImage.value = true;
            try {
                await api.imageGen.updateConfig({ with_image: val });
                appState.notify(val ? '动态配图已启用' : '动态配图已关闭', 'success');
            } catch (e) {
                withImage.value = !val; // 回滚
                appState.notify('保存失败：' + (e.message || e), 'danger');
            } finally {
                savingWithImage.value = false;
            }
        }

        async function testConnection() {
            if (!provider.value?.id) {
                appState.notify('未路由文生图 Provider，请先到模型分配页配置', 'warning');
                return;
            }
            testing.value = true;
            testResult.value = null;
            try {
                const res = await api.modelRouting.testProvider('image', provider.value.id);
                testResult.value = res;
            } catch (e) {
                testResult.value = { success: false, message: e.message || String(e) };
            } finally {
                testing.value = false;
            }
        }

        async function generateImage() {
            const p = genPrompt.value.trim();
            if (!p) {
                genError.value = '请输入 prompt 提示词';
                return;
            }
            if (!provider.value?.id) {
                genError.value = '未路由文生图 Provider，请先到模型分配页配置';
                return;
            }
            generating.value = true;
            generatedImage.value = null;
            genError.value = '';
            try {
                const res = await api.imageGen.test({ prompt: p });
                if (res && res.image_b64) {
                    generatedImage.value = {
                        src: 'data:image/png;base64,' + res.image_b64,
                        prompt: res.prompt || p,
                        model: res.model || '',
                        size: res.size || 0,
                    };
                } else {
                    genError.value = '生成失败：返回空结果';
                }
            } catch (e) {
                genError.value = '生成失败：' + (e.message || String(e));
            } finally {
                generating.value = false;
            }
        }

        onMounted(loadData);

        const cardStyle = 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;';

        return () => loading.value && !provider.value
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                // ═══ hero-band ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    h('div', { class: 'hero-panel' }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('span', { class: 'eyebrow' }, '文生图'),
                            h(Badge, { type: provider.value ? 'success' : 'muted' }, () => provider.value ? '已路由' : '未路由'),
                        ]),
                        h('h2', {
                            style: 'margin:0; font-size:1.65rem; line-height:1.1; text-wrap:balance; word-break:keep-all;',
                        }, '图片生成'),
                        h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                            h('span', {
                                style: 'font-size:1.4rem; font-weight:500; line-height:1; font-variant-numeric:tabular-nums;',
                            }, provider.value?.model || '未配置'),
                            h('span', { class: 'muted m-0', style: 'font-size:0.9rem;' }, provider.value?.name ? '· ' + provider.value.name : ''),
                        ]),
                        h('p', { class: 'muted m-0' }, '模型配置由「模型分配」页统一管理'),
                    ]),
                    h('article', {
                        class: 'grid gap-3',
                        style: cardStyle,
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '状态'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '功能概览'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-2' }, [
                            h('div', { class: 'flex items-center justify-between' }, [
                                h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '文生图 Provider'),
                                h(Badge, { type: provider.value ? 'success' : 'muted' }, () => provider.value ? provider.value.name : '未路由'),
                            ]),
                            h('div', { class: 'flex items-center justify-between' }, [
                                h('span', { class: 'muted', style: 'font-size:0.88rem;' }, 'API Key'),
                                h(Badge, { type: provider.value?.has_api_key ? 'success' : 'danger' }, () => provider.value?.has_api_key ? '已配置' : '未配置'),
                            ]),
                            h('div', { class: 'flex items-center justify-between' }, [
                                h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '动态配图'),
                                h(Badge, { type: withImage.value ? 'success' : 'muted' }, () => withImage.value ? '已启用' : '未启用'),
                            ]),
                            h('div', { class: 'flex items-center justify-between' }, [
                                h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '最近测试'),
                                h('span', {
                                    style: 'font-size:0.88rem; color: hsl(var(--muted-foreground));',
                                }, testResult.value ? (testResult.value.success ? '通过' : '失败') : '未测试'),
                            ]),
                        ]),
                    ]),
                ]),

                // ═══ Provider 只读信息 + 动态配图开关 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1.2fr) minmax(0, 0.8fr);',
                }, [
                    // 左侧：Provider 只读卡片
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '模型配置'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '文生图 Provider（只读）'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-3' }, [
                            provider.value
                                ? h('div', { class: 'grid gap-2' }, [
                                    h('div', { class: 'flex items-center justify-between' }, [
                                        h('span', { class: 'muted', style: 'font-size:0.88rem;' }, 'Provider ID'),
                                        h('span', { style: 'font-size:0.88rem; font-variant-numeric:tabular-nums;' }, provider.value.id),
                                    ]),
                                    h('div', { class: 'flex items-center justify-between' }, [
                                        h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '名称'),
                                        h('span', { style: 'font-size:0.88rem;' }, provider.value.name || '-'),
                                    ]),
                                    h('div', { class: 'flex items-center justify-between' }, [
                                        h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '模型'),
                                        h('span', { style: 'font-size:0.88rem;' }, provider.value.model || '-'),
                                    ]),
                                    h('div', { class: 'flex items-center justify-between' }, [
                                        h('span', { class: 'muted', style: 'font-size:0.88rem;' }, 'Base URL'),
                                        h('span', { style: 'font-size:0.82rem; word-break:break-all; text-align:right; max-width:60%;' }, provider.value.base_url || '-'),
                                    ]),
                                    h('div', { class: 'flex items-center justify-between' }, [
                                        h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '默认尺寸'),
                                        h('span', { style: 'font-size:0.88rem;' }, provider.value.default_size || '1024x768'),
                                    ]),
                                ])
                                : h(EmptyState, {
                                    title: '未路由文生图 Provider',
                                    desc: '请到「模型分配」页添加并路由 image 类型 Provider',
                                }),
                            h('div', { class: 'flex items-center gap-2 flex-wrap' }, [
                                h(Button, {
                                    type: 'primary',
                                    onClick: testConnection,
                                    loading: testing.value,
                                    disabled: !provider.value,
                                }, () => '测试连接'),
                                h(Button, {
                                    type: 'ghost',
                                    onClick: () => { window.location.hash = '/model-routing'; },
                                }, () => '去模型分配页'),
                            ]),
                            testResult.value && h('div', {
                                style: `padding: calc(var(--spacing) * 3); border-radius: calc(var(--radius) * 0.76); font-size:0.88rem; background: ${testResult.value.success ? 'hsl(var(--chart-4) / 0.12)' : 'hsl(var(--destructive) / 0.12)'}; color: ${testResult.value.success ? 'hsl(var(--chart-4))' : 'hsl(var(--destructive))'};`,
                            }, testResult.value.success ? '连接成功' : ('连接失败: ' + (testResult.value.message || ''))),
                        ]),
                    ]),

                    // 右侧：动态配图开关
                    h('article', {
                        class: 'grid gap-3',
                        style: cardStyle,
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '功能开关'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '动态配图'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-3' }, [
                            h('div', { class: 'flex items-center gap-2' }, [
                                h(Toggle, {
                                    modelValue: withImage.value,
                                    'onUpdate:modelValue': toggleWithImage,
                                    disabled: savingWithImage.value,
                                }),
                                h('span', { class: 'form-hint' }, withImage.value ? '已启用' : '已禁用'),
                            ]),
                            h(FormHint, '启用后，发布动态时将自动调用文生图 Provider 生成配图'),
                            !provider.value && withImage.value && h('div', {
                                style: 'padding: calc(var(--spacing) * 2); border-radius: calc(var(--radius) * 0.76); background: hsl(var(--destructive) / 0.08); color: hsl(var(--destructive)); font-size:0.82rem;',
                            }, '已启用动态配图但未路由 Provider，动态发布时将跳过配图'),
                        ]),
                    ]),
                ]),

                // ═══ 图片生成测试 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr);',
                }, [
                    h('article', {
                        class: 'grid gap-3',
                        style: cardStyle,
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '生成测试'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '用自定义 Prompt 生成图片'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-3' }, [
                            h(FormTextarea, {
                                modelValue: genPrompt.value,
                                'onUpdate:modelValue': (v) => genPrompt.value = v,
                                placeholder: '输入图片描述，如：一只橘猫坐在窗台上，窗外是夕阳...',
                                rows: 3,
                                disabled: generating.value,
                            }),
                            h('div', { class: 'flex items-center gap-2 flex-wrap' }, [
                                h(Button, {
                                    type: 'primary',
                                    onClick: generateImage,
                                    loading: generating.value,
                                    disabled: !provider.value || !genPrompt.value.trim(),
                                }, () => '生成图片'),
                                h('span', { class: 'muted', style: 'font-size:0.82rem;' },
                                    provider.value ? `模型: ${provider.value.model || '-'}` : '未路由 Provider'),
                            ]),
                            genError.value && h('div', {
                                style: 'padding: calc(var(--spacing) * 2); border-radius: calc(var(--radius) * 0.76); background: hsl(var(--destructive) / 0.08); color: hsl(var(--destructive)); font-size:0.88rem;',
                            }, genError.value),
                            generatedImage.value && h('div', {
                                class: 'grid gap-2',
                                style: 'padding: calc(var(--spacing) * 3); border-radius: calc(var(--radius) * 0.76); background: hsl(var(--card)); border: 1px solid hsl(var(--border));',
                            }, [
                                h('img', {
                                    src: generatedImage.value.src,
                                    alt: generatedImage.value.prompt,
                                    style: 'width: 100%; max-width: 512px; height: auto; border-radius: calc(var(--radius) * 0.5); display: block; margin: 0 auto;',
                                }),
                                h('div', {
                                    class: 'flex items-center justify-between gap-2 flex-wrap',
                                    style: 'font-size:0.82rem; color: hsl(var(--muted-foreground));',
                                }, [
                                    h('span', `Prompt: ${generatedImage.value.prompt}`),
                                    h('span', `${generatedImage.value.size ? (generatedImage.value.size / 1024).toFixed(1) + ' KB' : ''} · ${generatedImage.value.model || ''}`),
                                ]),
                            ]),
                        ]),
                    ]),
                ]),
            ]);
    },
};
