// bilibot/web/static/js/pages/image-gen.js - 文生图配置页（Golden Time 设计稿）
const { h, ref, reactive, onMounted, computed } = window.Vue;
import { appState } from '../state.js';
import { api } from '../api.js';
import { Button, Badge, FormInput, FormSelect, FormTextarea, Toggle, FormHint, Loading } from '../components/common.js';

export const ImageGenPage = {
    name: 'ImageGenPage',
    setup() {
        const loading = ref(false);
        const testing = ref(false);
        const config = reactive({
            enabled: false,
            provider: 'agnes',
            api_base: '',
            api_key: '',
            model: 'dall-e-3',
            image_size: '1024x1024',
            response_format: 'b64_json',
            quality: 'standard',
            style: 'vivid',
            max_retries: 3,
            timeout: 60,
        });
        const testPrompt = ref('');
        const testResult = ref(null);

        const sizeOptions = [
            { value: '1024x1024', label: '1024x1024（正方形）' },
            { value: '1792x1024', label: '1792x1024（横向）' },
            { value: '1024x1792', label: '1024x1792（纵向）' },
            { value: '512x512', label: '512x512（小图）' },
        ];

        const formatOptions = [
            { value: 'b64_json', label: 'Base64 JSON（优先，避免二次下载）' },
            { value: 'url', label: 'URL（需二次下载）' },
        ];

        async function loadConfig() {
            loading.value = true;
            try {
                const res = await api.imageGen.getConfig();
                Object.assign(config, res.data || {});
                config.api_key = '';
            } catch (e) {
                appState.notify('加载配置失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function saveConfig() {
            loading.value = true;
            try {
                const payload = { ...config };
                if (!payload.api_key) delete payload.api_key;
                await api.imageGen.updateConfig(payload);
                appState.notify('配置已保存', 'success');
            } catch (e) {
                appState.notify('保存失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function runTest() {
            if (!testPrompt.value) {
                appState.notify('请输入测试提示词', 'warning');
                return;
            }
            testing.value = true;
            testResult.value = null;
            try {
                const res = await api.imageGen.test({
                    prompt: testPrompt.value,
                    config: { ...config, api_key: config.api_key || undefined },
                });
                testResult.value = res.data;
                appState.notify('生成完成', 'success');
            } catch (e) {
                testResult.value = { error: e.message || String(e) };
                appState.notify('生成失败：' + (e.message || e), 'danger');
            } finally {
                testing.value = false;
            }
        }

        onMounted(loadConfig);

        const cardStyle = 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;';

        return () => loading.value && !config.model
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                // ═══ hero-band：配置状态 + 验证概览 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左侧：hero-panel 配置状态
                    h('div', { class: 'hero-panel' }, [
                        h('div', { class: 'flex items-start justify-between gap-2 flex-wrap' }, [
                            h('span', { class: 'eyebrow' }, '文生图'),
                            h(Badge, { type: config.enabled ? 'success' : 'muted' }, () => config.enabled ? '已启用' : '已禁用'),
                        ]),
                        h('h2', {
                            style: 'margin:0; font-size:1.65rem; line-height:1.1; text-wrap:balance; word-break:keep-all;',
                        }, '图片生成配置'),
                        h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                            h('span', {
                                style: 'font-size:1.4rem; font-weight:500; line-height:1; font-variant-numeric:tabular-nums;',
                            }, config.provider || '-'),
                            h('span', { class: 'muted m-0', style: 'font-size:0.9rem;' }, '· ' + (config.model || '未配置模型')),
                        ]),
                        h('p', { class: 'muted m-0' }, '管理 AI 文生图服务商、模型与生成参数'),
                    ]),
                    // 右侧：验证概览 Card
                    h('article', {
                        class: 'grid gap-3',
                        style: cardStyle,
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '验证'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '测试概览'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-2' }, [
                            h('div', { class: 'flex items-center justify-between' }, [
                                h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '服务状态'),
                                h(Badge, { type: config.enabled ? 'success' : 'muted' }, () => config.enabled ? '可用' : '禁用'),
                            ]),
                            h('div', { class: 'flex items-center justify-between' }, [
                                h('span', { class: 'muted', style: 'font-size:0.88rem;' }, 'API Key'),
                                h(Badge, { type: 'info' }, () => config.api_key ? '已填写' : '需重新输入'),
                            ]),
                            h('div', { class: 'flex items-center justify-between' }, [
                                h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '最近测试'),
                                h('span', {
                                    style: 'font-size:0.88rem; color: hsl(var(--muted-foreground));',
                                }, testResult.value?.error ? '失败' : (testResult.value ? '成功' : '未测试')),
                            ]),
                        ]),
                    ]),
                ]),

                // ═══ 2 列表单网格：配置 + 测试 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1.15fr) minmax(0, 0.85fr);',
                }, [
                    // 左侧：配置 Card
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '配置'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '生成参数'),
                            ]),
                        ]),
                        h('div', { class: 'card-body form-grid-2col' }, [
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '启用文生图'),
                                h('div', { class: 'flex items-center gap-2' }, [
                                    h(Toggle, {
                                        modelValue: config.enabled,
                                        'onUpdate:modelValue': (v) => config.enabled = v,
                                    }),
                                    h('span', { class: 'form-hint' }, config.enabled ? '已启用' : '已禁用'),
                                ]),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '服务商'),
                                h(FormSelect, {
                                    modelValue: config.provider,
                                    'onUpdate:modelValue': (v) => config.provider = v,
                                    options: [
                                        { value: 'agnes', label: 'Agnes AI' },
                                        { value: 'openai', label: 'OpenAI 兼容' },
                                    ],
                                }),
                            ]),
                            h('div', { class: 'form-group span-2' }, [
                                h('label', { class: 'form-label' }, 'API Base URL'),
                                h(FormInput, {
                                    modelValue: config.api_base,
                                    'onUpdate:modelValue': (v) => config.api_base = v,
                                    placeholder: 'https://api.agnes.ai/v1',
                                }),
                            ]),
                            h('div', { class: 'form-group span-2' }, [
                                h('label', { class: 'form-label' }, 'API Key'),
                                h(FormInput, {
                                    modelValue: config.api_key,
                                    'onUpdate:modelValue': (v) => config.api_key = v,
                                    type: 'password',
                                    placeholder: '编辑时留空表示不修改',
                                }),
                                h(FormHint, '出于安全考虑，API Key 不回显；修改时请重新输入'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '模型名称'),
                                h(FormInput, {
                                    modelValue: config.model,
                                    'onUpdate:modelValue': (v) => config.model = v,
                                    placeholder: 'dall-e-3, stable-diffusion-xl 等',
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '图片尺寸'),
                                h(FormSelect, {
                                    modelValue: config.image_size,
                                    'onUpdate:modelValue': (v) => config.image_size = v,
                                    options: sizeOptions,
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '响应格式'),
                                h(FormSelect, {
                                    modelValue: config.response_format,
                                    'onUpdate:modelValue': (v) => config.response_format = v,
                                    options: formatOptions,
                                }),
                                h(FormHint, '优先使用 b64_json，避免二次下载'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '质量'),
                                h(FormSelect, {
                                    modelValue: config.quality,
                                    'onUpdate:modelValue': (v) => config.quality = v,
                                    options: [
                                        { value: 'standard', label: '标准' },
                                        { value: 'hd', label: '高清' },
                                    ],
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '风格'),
                                h(FormSelect, {
                                    modelValue: config.style,
                                    'onUpdate:modelValue': (v) => config.style = v,
                                    options: [
                                        { value: 'vivid', label: '生动' },
                                        { value: 'natural', label: '自然' },
                                    ],
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '最大重试次数'),
                                h(FormInput, {
                                    modelValue: String(config.max_retries),
                                    'onUpdate:modelValue': (v) => config.max_retries = parseInt(v) || 3,
                                    type: 'number',
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '超时（秒）'),
                                h(FormInput, {
                                    modelValue: String(config.timeout),
                                    'onUpdate:modelValue': (v) => config.timeout = parseInt(v) || 60,
                                    type: 'number',
                                }),
                            ]),
                        ]),
                    ]),

                    // 右侧：测试 Card
                    h('article', {
                        class: 'grid gap-3',
                        style: cardStyle,
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '测试'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '生成测试'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-3' }, [
                            h(FormTextarea, {
                                modelValue: testPrompt.value,
                                'onUpdate:modelValue': (v) => testPrompt.value = v,
                                rows: 3,
                                placeholder: '描述要生成的图片内容，如：一只穿着宇航服的橘猫在月球表面散步...',
                                label: '测试提示词',
                            }),
                            h(Button, {
                                type: 'primary',
                                onClick: runTest,
                                loading: testing.value,
                            }, () => '生成图片'),
                            testResult.value && h('div', { class: 'grid gap-2' }, [
                                h('span', { class: 'eyebrow' }, '生成结果'),
                                testResult.value.error
                                    ? h('div', {
                                        style: 'padding: calc(var(--spacing) * 3); border-radius: calc(var(--radius) * 0.76); background: hsl(var(--destructive) / 0.12); color: hsl(var(--destructive)); font-size:0.9rem;',
                                    }, testResult.value.error)
                                    : h('div', { class: 'grid gap-2' }, [
                                        h('img', {
                                            src: testResult.value.image || testResult.value.b64
                                                ? `data:image/png;base64,${testResult.value.b64}`
                                                : testResult.value.url,
                                            style: 'max-width:100%; border-radius: calc(var(--radius) * 0.76); border:1px solid hsl(var(--border));',
                                        }),
                                        testResult.value.revised_prompt && h('p', {
                                            class: 'muted m-0',
                                            style: 'font-size:0.82rem; line-height:1.6',
                                        }, `修订后提示词：${testResult.value.revised_prompt}`),
                                    ]),
                            ]),
                        ]),
                    ]),
                ]),

                // ═══ 操作栏 ═══
                h('div', { class: 'flex items-center justify-end gap-2' }, [
                    h(Button, { type: 'ghost', onClick: loadConfig }, () => '重置'),
                    h(Button, { type: 'primary', onClick: saveConfig, loading: loading.value }, () => '保存配置'),
                ]),
            ]);
    },
};
