// bilibot/web/static/js/pages/video-analysis.js
const { h, ref, reactive, onMounted, computed, watch } = window.Vue;
import { appState } from '../state.js';
import { api } from '../api.js';
import { Card, Button, Badge, FormInput, FormSelect, FormTextarea, Toggle, FormHint } from '../components/common.js';

export const VideoAnalysisPage = {
    setup() {
        const loading = ref(false);
        const testing = ref(false);
        const config = reactive({
            enabled: false,
            frame_extractor: 'ffmpeg',
            max_frames: 8,
            min_scene_len: 2.0,
            sampling_interval: 2.0,
            llm_provider_id: '',
            vision_model: '',
            description_prompt: '',
            analysis_timeout: 120,
        });
        const testResult = ref(null);
        const testUrl = ref('');

        const extractorOptions = [
            { value: 'ffmpeg', label: 'FFmpeg（无依赖，兼容性好）' },
            { value: 'katna', label: 'Katna（关键帧提取，需 pip install katna）' },
            { value: 'scenedetect', label: 'PySceneDetect（场景检测，需 pip install scenedetect[video]）' },
        ];

        async function loadConfig() {
            loading.value = true;
            try {
                const res = await api.videoAnalysis.getConfig();
                Object.assign(config, res.data || {});
            } catch (e) {
                appState.notify('加载配置失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function saveConfig() {
            loading.value = true;
            try {
                await api.videoAnalysis.updateConfig(config);
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
                    config: config,
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

        return () => h('div', [
            h(Card, { title: '视频理解配置' }, {
                default: () => h('div', { class: 'form-grid-2col' }, [
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
                        h('label', { class: 'form-label' }, '最大帧数'),
                        h(FormInput, {
                            modelValue: String(config.max_frames),
                            'onUpdate:modelValue': (v) => config.max_frames = parseInt(v) || 8,
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
                            modelValue: String(config.analysis_timeout),
                            'onUpdate:modelValue': (v) => config.analysis_timeout = parseInt(v) || 120,
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
                footer: () => h('div', { class: 'flex gap-2 justify-end' }, [
                    h(Button, { onClick: loadConfig }, () => '重置'),
                    h(Button, { type: 'primary', onClick: saveConfig, loading: loading.value }, () => '保存配置'),
                ]),
            }),

            h(Card, { title: '视频分析测试' }, {
                default: () => h('div', [
                    h('div', { class: 'form-group' }, [
                        h('label', { class: 'form-label' }, '测试视频 URL（B站视频 BV 号或链接）'),
                        h('div', { class: 'flex gap-2' }, [
                            h(FormInput, {
                                modelValue: testUrl.value,
                                'onUpdate:modelValue': (v) => testUrl.value = v,
                                placeholder: 'https://www.bilibili.com/video/BVxxxxxx',
                                style: 'flex:1',
                            }),
                            h(Button, { type: 'primary', onClick: runTest, loading: testing.value }, () => '开始分析'),
                        ]),
                    ]),
                    testResult.value && h('div', { class: 'mt-4' }, [
                        h('h4', { class: 'mb-2' }, '分析结果：'),
                        testResult.value.error
                            ? h('div', { class: 'alert alert-danger' }, testResult.value.error)
                            : h('div', [
                                testResult.value.description && h('p', { style: 'line-height:1.6; margin-bottom:12px' },
                                    testResult.value.description),
                                testResult.value.frames && h('div', { class: 'flex gap-2 flex-wrap' },
                                    (testResult.value.frames || []).map((f, i) => h('div', { class: 'frame-thumb' }, [
                                        h('img', { src: f, style: 'max-width:120px; border-radius:8px; border:1px solid var(--outline-variant)' }),
                                        h('span', { class: 'text-muted', style: 'font-size:11px' }, `帧 ${i + 1}`),
                                    ])),
                                ),
                                testResult.value.duration && h('p', { class: 'text-muted mt-2', style: 'font-size:12px' },
                                    `视频时长：${testResult.value.duration}s | 提取帧数：${testResult.value.frames?.length || 0}`),
                            ]),
                    ]),
                ]),
            }),
        ]);
    },
};
