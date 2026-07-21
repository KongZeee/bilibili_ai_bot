// bilibot/web/static/js/pages/video-analysis.js - 视频理解配置页（Golden Time 设计稿）
// ASR/Vision 模型配置已迁移到「模型分配」页，本页保留抽帧/资源边界配置 + 只读 Provider 信息
const { h, ref, reactive, onMounted } = window.Vue;
import { appState } from '../state.js';
import { api } from '../api.js';
import { Button, Badge, FormInput, FormSelect, FormTextarea, Toggle, FormHint, Loading, EmptyState } from '../components/common.js';

export const VideoAnalysisPage = {
    name: 'VideoAnalysisPage',
    setup() {
        const loading = ref(false);
        const testing = ref(false);
        const config = reactive({
            enabled: false,
            frame_extractor: 'ffmpeg',
            scenedetect_threshold: 27.0,
            image_max_size: 768,
            max_keyframes: 32,
            vision_window_size: 5,
            vision_requests_per_minute: 10,
            vision_frame_max_retries: 2,
            vision_frame_retry_backoff_seconds: 1.5,
            vision_min_success_ratio: 0.5,
            analysis_timeout_seconds: 600,
            max_duration_seconds: 600,
            max_concurrent_global: 1,
            max_concurrent_per_account: 1,
            download_timeout_seconds: 90,
            preprocess_timeout_seconds: 180,
            // Task 28：补全缺失的 4 个资源边界字段
            max_download_bytes: 209715200,
            max_local_whisper_workers: 1,
            temp_disk_quota_bytes: 1073741824,
            temp_dir: '',
        });
        // 只读：从 model-routing 获取
        const visionProvider = ref(null);
        const asrProvider = ref(null);
        const localWhisper = ref(null);
        // Task 28：local_whisper 编辑能力
        const localWhisperEdit = reactive({
            enabled: false,
            model_size: 'base',
            device: 'cpu',
            compute_type: 'int8',
        });
        const testResult = ref(null);
        const testUrl = ref('');

        const whisperModelOptions = [
            { value: 'tiny', label: 'tiny（最快，最不准确）' },
            { value: 'base', label: 'base（推荐入门）' },
            { value: 'small', label: 'small（平衡）' },
            { value: 'medium', label: 'medium（较慢，更准确）' },
            { value: 'large-v3', label: 'large-v3（最慢，最准确）' },
        ];
        const whisperDeviceOptions = [
            { value: 'cpu', label: 'CPU' },
            { value: 'cuda', label: 'CUDA（NVIDIA GPU）' },
            { value: 'directml', label: 'DirectML（Windows GPU）' },
        ];
        const whisperComputeOptions = [
            { value: 'int8', label: 'int8（最快，CPU 推荐）' },
            { value: 'int8_float16', label: 'int8_float16（GPU 平衡）' },
            { value: 'float16', label: 'float16（GPU 推荐）' },
            { value: 'float32', label: 'float32（最准确，最慢）' },
        ];

        const extractorOptions = [
            { value: 'ffmpeg', label: 'FFmpeg（无依赖，兼容性好）' },
            { value: 'katna', label: 'Katna（关键帧提取，需 pip install katna）' },
            { value: 'scenedetect', label: 'PySceneDetect（场景检测，需 pip install scenedetect）' },
        ];

        async function loadData() {
            loading.value = true;
            try {
                const [vaRes, overview] = await Promise.all([
                    api.videoAnalysis.getConfig(),
                    api.modelRouting.getOverview(),
                ]);
                const data = vaRes || {};
                config.enabled = data.enabled ?? false;
                config.frame_extractor = data.frame_extractor || 'ffmpeg';
                config.scenedetect_threshold = data.scenedetect_threshold ?? 27.0;
                config.image_max_size = data.image_max_size ?? 768;
                config.max_keyframes = data.max_keyframes ?? 32;
                config.vision_window_size = data.vision_window_size ?? 5;
                config.vision_requests_per_minute = data.vision_requests_per_minute ?? 10;
                config.vision_frame_max_retries = data.vision_frame_max_retries ?? 2;
                config.vision_frame_retry_backoff_seconds = data.vision_frame_retry_backoff_seconds ?? 1.5;
                config.vision_min_success_ratio = data.vision_min_success_ratio ?? 0.5;
                config.analysis_timeout_seconds = data.analysis_timeout_seconds ?? 600;
                config.max_duration_seconds = data.max_duration_seconds ?? 600;
                config.max_concurrent_global = data.max_concurrent_global ?? 1;
                config.max_concurrent_per_account = data.max_concurrent_per_account ?? 1;
                config.download_timeout_seconds = data.download_timeout_seconds ?? 90;
                config.preprocess_timeout_seconds = data.preprocess_timeout_seconds ?? 180;
                // Task 28：补全 4 个缺失字段
                config.max_download_bytes = data.max_download_bytes ?? 209715200;
                config.max_local_whisper_workers = data.max_local_whisper_workers ?? 1;
                config.temp_disk_quota_bytes = data.temp_disk_quota_bytes ?? 1073741824;
                config.temp_dir = data.temp_dir ?? '';
                // 只读 provider 信息
                visionProvider.value = overview.features?.vision?.routed_provider || null;
                asrProvider.value = overview.features?.asr?.routed_provider || null;
                localWhisper.value = overview.local_whisper || null;
                // Task 28：同步 local_whisper 编辑字段
                const lw = overview.local_whisper || {};
                localWhisperEdit.enabled = lw.enabled ?? false;
                localWhisperEdit.model_size = lw.model_size || 'base';
                localWhisperEdit.device = lw.device || 'cpu';
                localWhisperEdit.compute_type = lw.compute_type || 'int8';
            } catch (e) {
                appState.notify('加载配置失败：' + (e.message || e), 'danger');
            } finally {
                loading.value = false;
            }
        }

        async function saveConfig() {
            loading.value = true;
            try {
                // Task 28：包含 4 个新字段 + local_whisper 子段
                await api.videoAnalysis.updateConfig({
                    ...config,
                    local_whisper: { ...localWhisperEdit },
                });
                appState.notify('配置已保存', 'success');
                await loadData();
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
                });
                testResult.value = res;
                appState.notify('分析完成', 'success');
            } catch (e) {
                testResult.value = { error: e.message || String(e) };
                appState.notify('分析失败：' + (e.message || e), 'danger');
            } finally {
                testing.value = false;
            }
        }

        onMounted(loadData);

        const cardStyle = 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;';

        // 只读 Provider 信息行
        function providerRow(label, p) {
            return h('div', { class: 'flex items-center justify-between' }, [
                h('span', { class: 'muted', style: 'font-size:0.88rem;' }, label),
                p
                    ? h('span', { style: 'font-size:0.88rem;' }, `${p.name || p.id} · ${p.model || '-'}`)
                    : h(Badge, { type: 'muted' }, () => '未配置'),
            ]);
        }

        return () => loading.value && !config.frame_extractor
            ? h(Loading)
            : h('div', { class: 'view-frame' }, [
                // ═══ hero-band ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
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
                            h('span', { class: 'muted m-0', style: 'font-size:0.9rem;' }, '· ' + (config.image_max_size || 768) + 'px'),
                        ]),
                        h('p', { class: 'muted m-0' }, '管理帧提取与资源边界；模型配置由「模型分配」页统一管理'),
                    ]),
                    h('article', {
                        class: 'grid gap-3',
                        style: cardStyle,
                    }, [
                        h('div', { class: 'card-header' }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'eyebrow' }, '状态'),
                                h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '模型路由概览'),
                            ]),
                        ]),
                        h('div', { class: 'card-body grid gap-2' }, [
                            providerRow('视觉模型', visionProvider.value),
                            providerRow('语音识别模型', asrProvider.value),
                            h('div', { class: 'flex items-center justify-between' }, [
                                h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '本地语音识别'),
                                h(Badge, { type: localWhisper.value?.enabled ? 'success' : 'muted' }, () => localWhisper.value?.enabled ? '已启用' : '未启用'),
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

                // ═══ 分析参数 + 测试 ═══
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1.15fr) minmax(0, 0.85fr);',
                }, [
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
                                h('label', { class: 'form-label' }, 'scenedetect 阈值'),
                                h(FormInput, {
                                    modelValue: String(config.scenedetect_threshold),
                                    'onUpdate:modelValue': (v) => config.scenedetect_threshold = parseFloat(v) || 27.0,
                                    type: 'number',
                                }),
                                h(FormHint, '越小越敏感（仅 scenedetect 模式生效）'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '关键帧最大边长（px）'),
                                h(FormInput, {
                                    modelValue: String(config.image_max_size),
                                    'onUpdate:modelValue': (v) => config.image_max_size = parseInt(v) || 768,
                                    type: 'number',
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '抽帧上限（帧）'),
                                h(FormInput, {
                                    modelValue: String(config.max_keyframes),
                                    'onUpdate:modelValue': (v) => {
                                        const n = parseInt(v, 10);
                                        config.max_keyframes = Number.isFinite(n)
                                            ? Math.max(1, Math.min(n, 64))
                                            : 150;
                                    },
                                    type: 'number',
                                }),
                                h(FormHint, '镜头数 ≤ 本值则按镜头全抽；超过则等距抽样（默认 32，高细节可用 48-64）'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '视觉窗口大小（帧数）'),
                                h(FormInput, {
                                    modelValue: String(config.vision_window_size),
                                    'onUpdate:modelValue': (v) => config.vision_window_size = parseInt(v) || 5,
                                    type: 'number',
                                }),
                                h(FormHint, '期望并发；实际受「配置页 → 模型请求限制」的视觉并发硬顶与密钥数约束'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '视觉请求起步速率（次/分钟）'),
                                h(FormInput, {
                                    modelValue: String(config.vision_requests_per_minute),
                                    'onUpdate:modelValue': (v) => {
                                        const n = parseInt(v, 10);
                                        config.vision_requests_per_minute = Number.isFinite(n)
                                            ? Math.max(1, Math.min(n, 600))
                                            : 10;
                                    },
                                    type: 'number',
                                }),
                                h(FormHint, '多 key 时运行时会按密钥数线性放大；与模型请求限制共同决定吞吐'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '单帧失败额外重试次数'),
                                h(FormInput, {
                                    modelValue: String(config.vision_frame_max_retries),
                                    'onUpdate:modelValue': (v) => {
                                        const n = parseInt(v, 10);
                                        config.vision_frame_max_retries = Number.isFinite(n)
                                            ? Math.max(0, Math.min(n, 5))
                                            : 2;
                                    },
                                    type: 'number',
                                }),
                                h(FormHint, '连接错误/超时等瞬时故障的额外重试（0–5）'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '单帧重试退避基数（秒）'),
                                h(FormInput, {
                                    modelValue: String(config.vision_frame_retry_backoff_seconds),
                                    'onUpdate:modelValue': (v) => {
                                        const n = parseFloat(v);
                                        config.vision_frame_retry_backoff_seconds = Number.isFinite(n)
                                            ? Math.max(0, Math.min(n, 30))
                                            : 1.5;
                                    },
                                    type: 'number',
                                }),
                                h(FormHint, '第 n 次重试等待 n × 该值秒'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '最低成功帧比例'),
                                h(FormInput, {
                                    modelValue: String(config.vision_min_success_ratio),
                                    'onUpdate:modelValue': (v) => {
                                        const n = parseFloat(v);
                                        config.vision_min_success_ratio = Number.isFinite(n)
                                            ? Math.max(0, Math.min(n, 1))
                                            : 0.5;
                                    },
                                    type: 'number',
                                }),
                                h(FormHint, 'require_complete 时达到此比例即可继续（0–1，默认 0.5）'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '视频时长上限（秒）'),
                                h(FormInput, {
                                    modelValue: String(config.max_duration_seconds),
                                    'onUpdate:modelValue': (v) => config.max_duration_seconds = parseInt(v) || 600,
                                    type: 'number',
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '分析超时（秒）'),
                                h(FormInput, {
                                    modelValue: String(config.analysis_timeout_seconds),
                                    'onUpdate:modelValue': (v) => config.analysis_timeout_seconds = parseInt(v) || 600,
                                    type: 'number',
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '下载超时（秒）'),
                                h(FormInput, {
                                    modelValue: String(config.download_timeout_seconds),
                                    'onUpdate:modelValue': (v) => config.download_timeout_seconds = parseInt(v) || 90,
                                    type: 'number',
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '预处理超时（秒）'),
                                h(FormInput, {
                                    modelValue: String(config.preprocess_timeout_seconds),
                                    'onUpdate:modelValue': (v) => config.preprocess_timeout_seconds = parseInt(v) || 180,
                                    type: 'number',
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '全局并发上限'),
                                h(FormInput, {
                                    modelValue: String(config.max_concurrent_global),
                                    'onUpdate:modelValue': (v) => config.max_concurrent_global = parseInt(v) || 1,
                                    type: 'number',
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '单账号并发上限'),
                                h(FormInput, {
                                    modelValue: String(config.max_concurrent_per_account),
                                    'onUpdate:modelValue': (v) => config.max_concurrent_per_account = parseInt(v) || 1,
                                    type: 'number',
                                }),
                            ]),
                            // Task 28：补全 4 个缺失的资源边界字段
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '下载体积上限（字节）'),
                                h(FormInput, {
                                    modelValue: String(config.max_download_bytes),
                                    'onUpdate:modelValue': (v) => config.max_download_bytes = parseInt(v) || 209715200,
                                    type: 'number',
                                }),
                                h(FormHint, 'VID-503：单次视频下载的最大字节数（默认 200MB）'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '本地语音识别最大并发'),
                                h(FormInput, {
                                    modelValue: String(config.max_local_whisper_workers),
                                    'onUpdate:modelValue': (v) => config.max_local_whisper_workers = parseInt(v) || 1,
                                    type: 'number',
                                }),
                                h(FormHint, '本地语音识别转写的最大并发数'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '临时目录磁盘配额（字节）'),
                                h(FormInput, {
                                    modelValue: String(config.temp_disk_quota_bytes),
                                    'onUpdate:modelValue': (v) => config.temp_disk_quota_bytes = parseInt(v) || 1073741824,
                                    type: 'number',
                                }),
                                h(FormHint, '视频临时文件的磁盘配额上限（默认 1GB）'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '临时目录'),
                                h(FormInput, {
                                    modelValue: config.temp_dir,
                                    'onUpdate:modelValue': (v) => config.temp_dir = v,
                                    placeholder: '留空则使用 {data_dir}/video_temp',
                                }),
                                h(FormHint, '视频下载与处理的临时目录路径'),
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

                // ═══ 模型路由只读 Card ═══
                h('article', {
                    class: 'grid gap-3',
                    style: cardStyle,
                }, [
                    h('div', { class: 'card-header' }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '模型配置'),
                            h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '视听模型服务商（只读）'),
                        ]),
                    ]),
                    h('div', { class: 'card-body grid gap-3' }, [
                        h('div', { class: 'form-grid-2col' }, [
                            // 视觉模型
                            h('div', { class: 'form-group span-2' }, [
                                h('label', { class: 'form-label' }, '视觉模型'),
                                visionProvider.value
                                    ? h('div', { class: 'grid gap-1' }, [
                                        h('div', { class: 'flex items-center justify-between' }, [
                                            h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '服务商'),
                                            h('span', { style: 'font-size:0.88rem;' }, visionProvider.value.name || visionProvider.value.id),
                                        ]),
                                        h('div', { class: 'flex items-center justify-between' }, [
                                            h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '模型'),
                                            h('span', { style: 'font-size:0.88rem;' }, visionProvider.value.model || '-'),
                                        ]),
                                        h('div', { class: 'flex items-center justify-between' }, [
                                            h('span', { class: 'muted', style: 'font-size:0.88rem;' }, 'API 密钥'),
                                            h(Badge, { type: visionProvider.value.has_api_key ? 'success' : 'danger' }, () => visionProvider.value.has_api_key ? '已配置' : '未配置'),
                                        ]),
                                    ])
                                    : h(EmptyState, { title: '未配置视觉服务商', desc: '请到模型分配页配置视觉类型' }),
                            ]),
                            // 语音识别模型
                            h('div', { class: 'form-group span-2' }, [
                                h('label', { class: 'form-label' }, '语音识别'),
                                asrProvider.value
                                    ? h('div', { class: 'grid gap-1' }, [
                                        h('div', { class: 'flex items-center justify-between' }, [
                                            h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '服务商'),
                                            h('span', { style: 'font-size:0.88rem;' }, asrProvider.value.name || asrProvider.value.id),
                                        ]),
                                        h('div', { class: 'flex items-center justify-between' }, [
                                            h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '模型'),
                                            h('span', { style: 'font-size:0.88rem;' }, asrProvider.value.model || '-'),
                                        ]),
                                        h('div', { class: 'flex items-center justify-between' }, [
                                            h('span', { class: 'muted', style: 'font-size:0.88rem;' }, 'API 密钥'),
                                            h(Badge, { type: asrProvider.value.has_api_key ? 'success' : 'danger' }, () => asrProvider.value.has_api_key ? '已配置' : '未配置'),
                                        ]),
                                    ])
                                    : h(EmptyState, { title: '未配置语音识别服务商', desc: '请到模型分配页配置语音识别类型' }),
                            ]),
                            // 本地语音识别
                            localWhisper.value?.enabled && h('div', { class: 'form-group span-2' }, [
                                h('label', { class: 'form-label' }, '本地语音识别'),
                                h('div', { class: 'grid gap-1' }, [
                                    h('div', { class: 'flex items-center justify-between' }, [
                                        h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '模型大小'),
                                        h('span', { style: 'font-size:0.88rem;' }, localWhisper.value.model_size || 'base'),
                                    ]),
                                    h('div', { class: 'flex items-center justify-between' }, [
                                        h('span', { class: 'muted', style: 'font-size:0.88rem;' }, '设备 / 计算类型'),
                                        h('span', { style: 'font-size:0.88rem;' }, `${localWhisper.value.device || 'cpu'} / ${localWhisper.value.compute_type || 'int8'}`),
                                    ]),
                                ]),
                            ]),
                        ]),
                        h('div', { class: 'flex items-center gap-2' }, [
                            h(Button, {
                                type: 'ghost',
                                onClick: () => { window.location.hash = '/model-routing'; },
                            }, () => '去模型分配页'),
                        ]),
                    ]),
                ]),

                // ═══ Task 28：本地 Whisper 编辑 Card ═══
                h('article', {
                    class: 'grid gap-3',
                    style: cardStyle,
                }, [
                    h('div', { class: 'card-header' }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '语音识别'),
                            h('h2', { style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500;' }, '本地语音识别'),
                        ]),
                    ]),
                    h('div', { class: 'card-body grid gap-3' }, [
                        h('div', { class: 'form-grid-2col' }, [
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '启用本地语音识别'),
                                h('div', { class: 'flex items-center gap-2' }, [
                                    h(Toggle, {
                                        modelValue: localWhisperEdit.enabled,
                                        'onUpdate:modelValue': (v) => localWhisperEdit.enabled = v,
                                    }),
                                    h('span', { class: 'form-hint' }, localWhisperEdit.enabled ? '已启用' : '已禁用'),
                                ]),
                                h(FormHint, '启用后可在无云端语音服务时使用本地模型转写'),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '模型大小'),
                                h(FormSelect, {
                                    modelValue: localWhisperEdit.model_size,
                                    'onUpdate:modelValue': (v) => localWhisperEdit.model_size = v,
                                    options: whisperModelOptions,
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '设备'),
                                h(FormSelect, {
                                    modelValue: localWhisperEdit.device,
                                    'onUpdate:modelValue': (v) => localWhisperEdit.device = v,
                                    options: whisperDeviceOptions,
                                }),
                            ]),
                            h('div', { class: 'form-group' }, [
                                h('label', { class: 'form-label' }, '计算类型'),
                                h(FormSelect, {
                                    modelValue: localWhisperEdit.compute_type,
                                    'onUpdate:modelValue': (v) => localWhisperEdit.compute_type = v,
                                    options: whisperComputeOptions,
                                }),
                            ]),
                        ]),
                        h('div', {
                            class: 'grid gap-2',
                            style: 'padding: calc(var(--spacing) * 3); border-radius: calc(var(--radius) * 0.76); background: hsl(var(--chart-5) / 0.08); border: 1px solid hsl(var(--chart-5) / 0.2);',
                        }, [
                            h('p', {
                                class: 'muted m-0',
                                style: 'font-size:0.88rem; line-height:1.6;',
                            }, '本地语音识别需安装 faster-whisper。使用 GPU 时需对应驱动与 CUDA 支持。修改后保存即生效，下次视频分析将使用新配置。'),
                        ]),
                    ]),
                ]),

                // ═══ 操作栏 ═══
                h('div', { class: 'flex items-center justify-end gap-2' }, [
                    h(Button, { type: 'ghost', onClick: loadData }, () => '重置'),
                    h(Button, { type: 'primary', onClick: saveConfig, loading: loading.value }, () => '保存配置'),
                ]),
            ]);
    },
};
