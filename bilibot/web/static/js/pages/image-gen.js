// bilibot/web/static/js/pages/image-gen.js
import { h, ref, reactive, onMounted, computed } from '../vendor/vue.esm-browser.prod.js';
import { appState } from '../state.js';
import { api } from '../api.js';
import { Card, Button, Badge, FormInput, FormSelect, FormTextarea, Toggle, FormHint } from '../components.js';

export const ImageGenPage = {
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
                // 注意：api_key 不回显
                Object.assign(config, res.data || {});
                config.api_key = ''; // 清空，编辑时需重新输入
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
                // api_key 为空时不提交（保持原值）
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

        return () => h('div', [
            h(Card, { title: '文生图配置' }, {
                default: () => h('div', { class: 'form-grid-2col' }, [
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
                footer: () => h('div', { class: 'flex gap-2 justify-end' }, [
                    h(Button, { onClick: loadConfig }, () => '重置'),
                    h(Button, { type: 'primary', onClick: saveConfig, loading: loading.value }, () => '保存配置'),
                ]),
            }),

            h(Card, { title: '文生图测试' }, {
                default: () => h('div', [
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '测试提示词'),
                        h(FormTextarea, {
                            modelValue: testPrompt.value,
                            'onUpdate:modelValue': (v) => testPrompt.value = v,
                            rows: 3,
                            placeholder: '描述要生成的图片内容，如：一只穿着宇航服的橘猫在月球表面散步...',
                        }),
                    ]),
                    h('div', { class: 'mb-3' }, [
                        h(Button, { type: 'primary', onClick: runTest, loading: testing.value }, () => '生成图片'),
                    ]),
                    testResult.value && h('div', { class: 'mt-4' }, [
                        h('h4', { class: 'mb-2' }, '生成结果：'),
                        testResult.value.error
                            ? h('div', { class: 'alert alert-danger' }, testResult.value.error)
                            : h('div', [
                                h('img', {
                                    src: testResult.value.image || testResult.value.b64
                                        ? `data:image/png;base64,${testResult.value.b64}`
                                        : testResult.value.url,
                                    style: 'max-width:100%; border-radius:12px; border:1px solid var(--outline-variant); margin-bottom:12px',
                                }),
                                testResult.value.revised_prompt && h('p', { class: 'text-muted', style: 'font-size:12px; line-height:1.6' },
                                    `修订后提示词：${testResult.value.revised_prompt}`),
                            ]),
                    ]),
                ]),
            }),
        ]);
    },
};
