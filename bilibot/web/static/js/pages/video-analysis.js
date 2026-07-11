// bilibot/web/static/js/pages/video-analysis.js - 视频理解配置页（Golden Time 设计稿）
const { h, ref, reactive, onMounted, computed, watch } = window.Vue;
import { appState } from '../state.js';
import { api } from '../api.js';
import { Button, Badge, FormInput, FormSelect, FormTextarea, Toggle, FormHint, Loading } from '../components/common.js';

export const VideoAnalysisPage = {
    name: 'VideoAnalysisPage',
    setup() {
        const loading = ref(false);
        const testing = ref(false);
        const config = reactive({
            enabled: false,
            frame_extractor: 'ffmpeg',
            vision_window_size: 8,
            min_scene_len: 2.0,
            sampling_interval: 2.0,
            llm_provider_id: '',
            vision_model: '',
            description_prompt: '',
            analysis_timeout_seconds: 120,
            asr_model: '',
            asr_api_key: '',
            asr_base_url: '',
            asr_whisper_model_size: '',
            asr_whisper_device: '',
            asr_whisper_compute_type: '',
        });
        const testResult = ref(null);
        const testUrl = ref('');

        const extractorOptions = [
            { value: 'ffmpeg', label: 'FFmpeg（无依赖，兼容性好）' },
            { value: 'katna', label: 'Katna（关键帧提取，需 pip install katna）' },
            { value: 'scenedetect', label: 'PySceneDetect（场景检测，需 pip install scenedetect[video]）' },
        ];

        const asrModelOptions = [
            { value: '', label: '不使用 ASR' },
            { value: 'api', label: 'API（在线语音识别）' },
            { value: 'whisper', label: 'Whisper（本地语音识别）' },
        ];

        const whisperModelSizeOptions = [
            { value: 'tiny', label: 'tiny' },
            { value: 'base', label: 'base' },
            { value: 'small', label: 'small' },
            { value: 'medium', label: 'medium' },
            { value: 'large', label: 'large' },
        ];

        const whisperDeviceOptions = [
            { value: 'cpu', label: 'CPU' },
            { value: 'cuda', label: 'CUDA（GPU）' },
        ];

        const whisperComputeTypeOptions = [
            { value: 'int8', label: 'int8' },
            { value: 'float16', label: 'float16' },
            { value: 'float32', label: 'float32' },
        ];

        async function loadConfig() {
            loading.value = true;
            try {
                const res = await api.videoAnalysis.getConfig();
                const data = res.data || {};
                Object.assign(config, data);
                // 后端 ASR 子段展开为扁平字段供表单使用
                const asr = data.asr || {};
                config.asr_model = asr.model || '';
                config.asr_api_key = asr.api_key || '';
                config.asr_base_url = asr.base_url || '';
                config.asr_whisper_model_size = asr.whisper_model_size || '';
                config.asr_whisper_device = asr.whisper_device || '';
                config.asr_whisper_compute_type = asr.whisper_compute_type || '';
            } catch (e) {
                appState.notify('加载配置失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        function buildPayload() {
            // 将扁平 ASR 字段包装为后端期望的 asr 子对象
            const { asr_model, asr_api_key, asr_base_url,
                    asr_whisper_model_size, asr_whisper_device, asr_whisper_compute_type,
                    ...rest } = config;
            return {
                ...rest,
                asr: {
                    model: asr_model,
                    api_key: asr_api_key,
                    base_url: asr_base_url,
                    whisper_model_size: asr_whisper_model_size,
                    whisper_device: asr_whisper_device,
                    whisper_compute_type: asr_whisper_compute_type,
                },
            };
        }

        async function saveConfig() {
            loading.value = true;
            try {
                await api.videoAnalysis.updateConfig(buildPayload());
                appState.notify('配置已保存', 'success');
            } catch (e) {
                appState.notify('保存失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function runTest() {
            if (!testUrl.value) {
                appState.notify('请输入测试视频 URL', 'warning');
                return;
            }
            testing.value = true;
            testResult.value = null;
            try {
                const res = await api.videoAnalysis.test({
                    video_url: testUrl.value,
                    config: buildPayload(),
                });
                testResult.value = res.data;
                appState.notify('分析完成', 'success');
            } catch (e) {
                testResult.value = { error: e.message || String(e) };
                appState.notify('分析失败：' + (e.message || e), 'danger');
            } finally {
                testing.value = false;
            }
        }

        onMounted(() => {
            loadConfig();
            if (appState.llmProviders.length === 0) appState.refreshLlmProviders();
        });

        const llmOptions = computed(() =>
            appState.llmProviders.map(p => ({ value: p.id, label: `${p.name} (${p.model})` })),
        );

        const cardStyle = 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;';

        return () => loading.value && !config.frame_extractor
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
                            h('span', { class: 'eyebrow' }, '视频理解'),
                            h(Badge, { type: config.enabled ? 'success' : 'muted' }, () => config.enabled ? '已启用' : '已禁用'),
                        ]),
                        h('h2', {
                            style: 'margin:0; font-size:1.65rem; line-height:1.1; text-wrap:balance; word-break:keep-all;',
                        }, '视频分析配置'),
                        h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                            h('span', {
                                style: 'font-size:1.4rem; font-weight:500; line-height:1; font-variant-numeric:tabular-nums;',
                            }, config.frame_extractor || '-'),
                            h('span', { class: 'muted m-0', style: 'font-size:0.9rem;' }, '· ' + (config.vision_model || '默认视觉模型')),
                        ]),
                        h('p', { class: 'muted m-0' }, '管理视频帧提取、视觉模型与分析参数'),
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
                                h('span', { class: 'muted', style: 'font-size:0.88rem;' }, 'LLM Provider'),
                                h(Badge, { type: 'info' }, () => config.llm_provider_id ? '已指定' : '使用账号绑定'),
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
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '分析参数'),
                            ]),
                        ]),
                        h('div', { class: 'card-body form-grid-2col' }, [
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '启用视频理解'),
                                h('div', { class: 'flex items-center gap-2' }, [
                                    h(Toggle, {
                                        modelValue: config.enabled,
                                        'onUpdate:modelValue': (v) => config.enabled = v,
                                    }),
                                    h('span', { class: 'form-hint' }, config.enabled ? '已启用' : '已禁用'),
                                ]),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '帧提取方法'),
                                h(FormSelect, {
                                    modelValue: config.frame_extractor,
                                    'onUpdate:modelValue': (v) => config.frame_extractor = v,
                                    options: extractorOptions,
                                }),
                                h(FormHint, 'katna/scenedetect 需额外安装依赖；ffmpeg 为默认回退方案'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '视觉窗口大小（帧数）'),
                                h(FormInput, {
                                    modelValue: String(config.vision_window_size),
                                    'onUpdate:modelValue': (v) => config.vision_window_size = parseInt(v) || 8,
                                    type: 'number',
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '最小场景长度（秒）'),
                                h(FormInput, {
                                    modelValue: String(config.min_scene_len),
                                    'onUpdate:modelValue': (v) => config.min_scene_len = parseFloat(v) || 2.0,
                                    type: 'number',
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '采样间隔（秒）'),
                                h(FormInput, {
                                    modelValue: String(config.sampling_interval),
                                    'onUpdate:modelValue': (v) => config.sampling_interval = parseFloat(v) || 2.0,
                                    type: 'number',
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '分析超时（秒）'),
                                h(FormInput, {
                                    modelValue: String(config.analysis_timeout_seconds),
                                    'onUpdate:modelValue': (v) => config.analysis_timeout_seconds = parseInt(v) || 120,
                                    type: 'number',
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '使用的 LLM Provider'),
                                h(FormSelect, {
                                    modelValue: config.llm_provider_id,
                                    'onUpdate:modelValue': (v) => config.llm_provider_id = v,
                                    options: [{ value: '', label: '使用账号绑定 LLM' }, ...llmOptions.value],
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '视觉模型名称'),
                                h(FormInput, {
                                    modelValue: config.vision_model,
                                    'onUpdate:modelValue': (v) => config.vision_model = v,
                                    placeholder: '如 gpt-4o, qwen-vl-max（留空用默认）',
                                }),
                            ]),
                            h('div', { class: 'form-group span-2' }, [
                                h('label', { class: 'form-label' }, '描述提示词'),
                                h(FormTextarea, {
                                    modelValue: config.description_prompt,
                                    'onUpdate:modelValue': (v) => config.description_prompt = v,
                                    rows: 4,
                                    placeholder: '描述视频帧内容时使用的系统提示词...',
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
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '分析测试'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-3' }, [
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '测试视频 URL（B站视频 BV 号或链接）'),
                                h(FormInput, {
                                    modelValue: testUrl.value,
                                    'onUpdate:modelValue': (v) => testUrl.value = v,
                                    placeholder: 'https://www.bilibili.com/video/BVxxxxxx',
                                }),
                            ]),
                            h(Button, {
                                type: 'primary',
                                onClick: runTest,
                                loading: testing.value,
                            }, () => '开始分析'),
                            testResult.value && h('div', { class: 'grid gap-2' }, [
                                h('span', { class: 'eyebrow' }, '分析结果'),
                                testResult.value.error
                                    ? h('div', {
                                        style: 'padding: calc(var(--spacing) * 3); border-radius: calc(var(--radius) * 0.76); background: hsl(var(--destructive) / 0.12); color: hsl(var(--destructive)); font-size:0.9rem;',
                                    }, testResult.value.error)
                                    : h('div', { class: 'grid gap-2' }, [
                                        testResult.value.description && h('p', {
                                            class: 'm-0',
                                            style: 'line-height:1.6; font-size:0.92rem;',
                                        }, testResult.value.description),
                                        testResult.value.frames && h('div', { class: 'flex gap-2 flex-wrap' },
                                            (testResult.value.frames || []).map((f, i) => h('div', {
                                                class: 'grid gap-1',
                                                style: 'justify-items:center;',
                                            }, [
                                                h('img', {
                                                    src: f,
                                                    style: 'max-width:120px; border-radius: calc(var(--radius) * 0.76); border:1px solid hsl(var(--border));',
                                                }),
                                                h('span', {
                                                    class: 'muted',
                                                    style: 'font-size:0.72rem;',
                                                }, `帧 ${i + 1}`),
                                            ])),
                                        ),
                                        testResult.value.duration && h('p', {
                                            class: 'muted m-0',
                                            style: 'font-size:0.82rem;',
                                        }, `视频时长：${testResult.value.duration}s | 提取帧数：${testResult.value.frames?.length || 0}`),
                                    ]),
                            ]),
                        ]),
                    ]),
                ]),

                // ═══ ASR 配置 Card ═══
                h('article', {
                    class: 'grid gap-3',
                    style: cardStyle,
                }, [
                    h('div', { class: 'card-header' }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '语音识别'),
                            h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, 'ASR 配置'),
                        ]),
                    ]),
                    h('div', { class: 'card-body form-grid-2col' }, [
                        h('div', { class: 'form-group' }, [
                            h('label', { class: 'form-label' }, 'ASR 模型'),
                            h(FormSelect, {
                                modelValue: config.asr_model,
                                'onUpdate:modelValue': (v) => config.asr_model = v,
                                options: asrModelOptions,
                            }),
                            h(FormHint, '选择 API 在线识别或 Whisper 本地识别'),
                        ]),
                        h('div', { class: 'form-group' }, [
                            h('label', { class: 'form-label' }, 'ASR API Key'),
                            h(FormInput, {
                                modelValue: config.asr_api_key,
                                'onUpdate:modelValue': (v) => config.asr_api_key = v,
                                type: 'password',
                                placeholder: 'ASR 服务密钥（留空不修改）',
                            }),
                        ]),
                        h('div', { class: 'form-group' }, [
                            h('label', { class: 'form-label' }, 'ASR Base URL'),
                            h(FormInput, {
                                modelValue: config.asr_base_url,
                                'onUpdate:modelValue': (v) => config.asr_base_url = v,
                                placeholder: '如 https://api.example.com/v1',
                            }),
                        ]),
                        h('div', { class: 'form-group' }, [
                            h('label', { class: 'form-label' }, 'Whisper 模型大小'),
                            h(FormSelect, {
                                modelValue: config.asr_whisper_model_size,
                                'onUpdate:modelValue': (v) => config.asr_whisper_model_size = v,
                                options: whisperModelSizeOptions,
                            }),
                        ]),
                        h('div', { class: 'form-group' }, [
                            h('label', { class: 'form-label' }, 'Whisper 设备'),
                            h(FormSelect, {
                                modelValue: config.asr_whisper_device,
                                'onUpdate:modelValue': (v) => config.asr_whisper_device = v,
                                options: whisperDeviceOptions,
                            }),
                        ]),
                        h('div', { class: 'form-group' }, [
                            h('label', { class: 'form-label' }, 'Whisper 计算类型'),
                            h(FormSelect, {
                                modelValue: config.asr_whisper_compute_type,
                                'onUpdate:modelValue': (v) => config.asr_whisper_compute_type = v,
                                options: whisperComputeTypeOptions,
                            }),
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
