// api.js - 统一 API 请求客户端
import { isRouteMissError } from './api-fallback.js';

const BASE_URL = '';

// 构建查询字符串，过滤掉 undefined/null/空字符串 值
// 避免 URLSearchParams 将 undefined 序列化为字符串 "undefined"
function buildQuery(params) {
    const sp = new URLSearchParams();
    if (params && typeof params === 'object') {
        for (const [k, v] of Object.entries(params)) {
            if (v === undefined || v === null || v === '') continue;
            sp.append(k, v);
        }
    }
    return sp.toString();
}

/** 解析唯一 bot 账号 id（单账号模式） */
function resolveSoleId(explicit) {
    if (explicit) return explicit;
    try {
        const st = window.appState;
        if (st?.currentAccountId) return st.currentAccountId;
        const a0 = st?.accounts?.[0];
        if (a0) return a0.account_id || a0.id || null;
    } catch (_) { /* ignore */ }
    return null;
}

/** 先试 flat 路由，仅 route-miss（404/NOT_FOUND/NO_ACCOUNT）时回退 nested（带 sole id） */
async function flatOrNested(flatUrl, nestedUrl, options = {}) {
    try {
        return await request(flatUrl, options);
    } catch (e) {
        if (nestedUrl && isRouteMissError(e)) {
            return request(nestedUrl, options);
        }
        throw e;
    }
}

async function request(url, options = {}) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 30000);
    let resp;
    try {
        resp = await fetch(BASE_URL + url, {
            ...options,
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                ...(options.headers || {}),
            },
            body: options.body ? JSON.stringify(options.body) : undefined,
            signal: controller.signal,
        });
    } catch (fetchErr) {
        clearTimeout(timeoutId);
        if (fetchErr.name === 'AbortError') {
            throw new Error('请求超时，请稍后重试');
        }
        throw new Error('网络连接失败，请检查网络');
    }
    clearTimeout(timeoutId);

    if (resp.status === 401) {
        // 保留当前 hash，登录后可回跳控制台页面
        try {
            const hash = window.location.hash || '';
            const next = hash && hash !== '#/login' ? hash : '';
            if (next) {
                sessionStorage.setItem('bilibot_login_return', next);
            }
        } catch (_) { /* sessionStorage 不可用时忽略 */ }
        window.location.href = '/login';
        throw new Error('未登录');
    }

    const data = await resp.json().catch(() => ({}));

    if (!resp.ok || data.success === false) {
        const err = new Error(data?.error?.message || data?.message || `HTTP ${resp.status}`);
        err.code = data?.error?.code;
        err.details = data?.error?.details;
        err.resp = data;
        err.status = resp.status;
        throw err;
    }

    return options.returnEnvelope ? data : (data.data !== undefined ? data.data : data);
}

export const api = {
    get: (url) => request(url, { method: 'GET' }),
    post: (url, body) => request(url, { method: 'POST', body }),
    patch: (url, body) => request(url, { method: 'PATCH', body }),
    delete: (url) => request(url, { method: 'DELETE' }),

    // 账号（列表仍用 /api/accounts；单 bot 操作优先 flat /api/account）
    accounts: {
        list: () => api.get('/api/accounts'),
        get: (id) => {
            const sole = resolveSoleId(id);
            return flatOrNested('/api/account', sole ? `/api/accounts/${sole}` : null);
        },
        create: (data) => api.post('/api/accounts', data),
        update: (id, data) => {
            const sole = resolveSoleId(id);
            return flatOrNested('/api/account', sole ? `/api/accounts/${sole}` : null, {
                method: 'PATCH',
                body: data,
            });
        },
        delete: (id) => api.delete(`/api/accounts/${id}`),
        // 单 bot：无默认账号切换；保留 no-op 以免旧调用报错
        setDefault: async (_id) => null,
        start: (id) => {
            const sole = resolveSoleId(id);
            return flatOrNested('/api/account/start', sole ? `/api/accounts/${sole}/start` : null, { method: 'POST' });
        },
        stop: (id) => {
            const sole = resolveSoleId(id);
            return flatOrNested('/api/account/stop', sole ? `/api/accounts/${sole}/stop` : null, { method: 'POST' });
        },
        bindPersona: (id, data) => {
            const sole = resolveSoleId(id);
            return flatOrNested(
                '/api/account/persona',
                sole ? `/api/accounts/${sole}/persona` : null,
                { method: 'POST', body: data },
            );
        },
        switchPersona: (id, personaId) => {
            const sole = resolveSoleId(id);
            return flatOrNested(
                '/api/account/switch-persona',
                sole ? `/api/accounts/${sole}/switch-persona` : null,
                { method: 'POST', body: { persona_id: personaId } },
            );
        },
        bindLlm: (id, llmId) => {
            const sole = resolveSoleId(id);
            return flatOrNested(
                '/api/account/llm',
                sole ? `/api/accounts/${sole}/llm` : null,
                { method: 'POST', body: { llm_id: llmId } },
            );
        },
        qrLogin: (id) => {
            const sole = resolveSoleId(id);
            return flatOrNested('/api/account/qr-login', sole ? `/api/accounts/${sole}/qr-login` : null, { method: 'POST' });
        },
        // 后端返回 qr_session_id / status(created|scanned|confirmed|expired|cancelled)
        qrPoll: (id, sid) => {
            const sole = resolveSoleId(id);
            return flatOrNested(
                `/api/account/qr-login/${sid}`,
                sole ? `/api/accounts/${sole}/qr-login/${sid}` : null,
            );
        },
        qrCancel: (id, sid) => {
            const sole = resolveSoleId(id);
            return flatOrNested(
                `/api/account/qr-login/${sid}/cancel`,
                sole ? `/api/accounts/${sole}/qr-login/${sid}/cancel` : null,
                { method: 'POST' },
            );
        },
        profiles: () => api.get('/api/accounts/profiles'),
        tasks: (id, params) => {
            const sole = resolveSoleId(id);
            const q = buildQuery(params);
            return flatOrNested(
                `/api/account/tasks?${q}`,
                sole ? `/api/accounts/${sole}/tasks?${q}` : null,
            );
        },
        getTask: (id, taskId) => {
            const sole = resolveSoleId(id);
            return flatOrNested(
                `/api/account/tasks/${taskId}`,
                sole ? `/api/accounts/${sole}/tasks/${taskId}` : null,
            );
        },
        cancelTask: (id, taskId) => {
            const sole = resolveSoleId(id);
            return flatOrNested(
                `/api/account/tasks/${taskId}/cancel`,
                sole ? `/api/accounts/${sole}/tasks/${taskId}/cancel` : null,
                { method: 'POST' },
            );
        },
        retryTask: (id, taskId) => {
            const sole = resolveSoleId(id);
            return flatOrNested(
                `/api/account/tasks/${taskId}/retry`,
                sole ? `/api/accounts/${sole}/tasks/${taskId}/retry` : null,
                { method: 'POST' },
            );
        },
        triggerProactiveVideo: (id) => {
            const sole = resolveSoleId(id);
            return flatOrNested(
                '/api/account/tasks/proactive-video',
                sole ? `/api/accounts/${sole}/tasks/proactive-video` : null,
                { method: 'POST' },
            );
        },
        triggerDynamic: (id) => {
            const sole = resolveSoleId(id);
            return flatOrNested(
                '/api/account/tasks/dynamic',
                sole ? `/api/accounts/${sole}/tasks/dynamic` : null,
                { method: 'POST' },
            );
        },
        triggerBangumi: (id) => {
            const sole = resolveSoleId(id);
            return flatOrNested(
                '/api/account/tasks/bangumi',
                sole ? `/api/accounts/${sole}/tasks/bangumi` : null,
                { method: 'POST' },
            );
        },
        companion: {
            state: (id) => {
                const sole = resolveSoleId(id);
                return flatOrNested(
                    '/api/companion/state',
                    sole ? `/api/accounts/${sole}/companion/state` : null,
                );
            },
            plan: (id) => {
                const sole = resolveSoleId(id);
                return flatOrNested(
                    '/api/companion/plan',
                    sole ? `/api/accounts/${sole}/companion/plan` : null,
                );
            },
            regeneratePlan: (id) => {
                const sole = resolveSoleId(id);
                return flatOrNested(
                    '/api/companion/plan/regenerate',
                    sole ? `/api/accounts/${sole}/companion/plan/regenerate` : null,
                    { method: 'POST' },
                );
            },
            diaries: (id) => {
                const sole = resolveSoleId(id);
                return flatOrNested(
                    '/api/companion/diaries',
                    sole ? `/api/accounts/${sole}/companion/diaries` : null,
                );
            },
            dreams: (id) => {
                const sole = resolveSoleId(id);
                return flatOrNested(
                    '/api/companion/dreams',
                    sole ? `/api/accounts/${sole}/companion/dreams` : null,
                );
            },
            notes: (id) => {
                const sole = resolveSoleId(id);
                return flatOrNested(
                    '/api/companion/notes',
                    sole ? `/api/accounts/${sole}/companion/notes` : null,
                );
            },
            bookshelf: (id) => {
                const sole = resolveSoleId(id);
                return flatOrNested(
                    '/api/companion/bookshelf',
                    sole ? `/api/accounts/${sole}/companion/bookshelf` : null,
                );
            },
            trigger: (id, action) => {
                const sole = resolveSoleId(id);
                const body = { action: action || 'tick' };
                return flatOrNested(
                    '/api/companion/trigger',
                    sole ? `/api/accounts/${sole}/companion/trigger` : null,
                    { method: 'POST', body, returnEnvelope: true },
                );
            },
        },
    },

    // 单 bot flat 客户端：优先 /api/account/*，失败回退 nested + sole id
    account: {
        get: () => api.accounts.get(),
        update: (body) => api.accounts.update(null, body),
        start: () => api.accounts.start(),
        stop: () => api.accounts.stop(),
        qrLogin: () => api.accounts.qrLogin(),
        qrPoll: (sid) => api.accounts.qrPoll(null, sid),
        qrCancel: (sid) => api.accounts.qrCancel(null, sid),
        tasks: {
            list: (params) => api.accounts.tasks(null, params),
            get: (taskId) => api.accounts.getTask(null, taskId),
            cancel: (taskId) => api.accounts.cancelTask(null, taskId),
            retry: (taskId) => api.accounts.retryTask(null, taskId),
            triggerProactiveVideo: () => api.accounts.triggerProactiveVideo(),
            triggerDynamic: () => api.accounts.triggerDynamic(),
            triggerBangumi: () => api.accounts.triggerBangumi(),
        },
    },

    // 陪伴生活 flat API（忽略 accountId，走 sole）
    companion: {
        state: (_id) => api.accounts.companion.state(_id),
        plan: (_id) => api.accounts.companion.plan(_id),
        regeneratePlan: (_id) => api.accounts.companion.regeneratePlan(_id),
        diaries: (_id) => api.accounts.companion.diaries(_id),
        dreams: (_id) => api.accounts.companion.dreams(_id),
        notes: (_id) => api.accounts.companion.notes(_id),
        bookshelf: (_id) => api.accounts.companion.bookshelf(_id),
        trigger: (_id, action) => api.accounts.companion.trigger(_id, action),
    },

    // LLM（旧 V2 接口，仍用于账号绑定等场景）
    llm: {
        list: () => api.get('/api/llm-providers'),
        create: (data) => api.post('/api/llm-providers', data),
        update: (id, data) => api.patch(`/api/llm-providers/${id}`, data),
        delete: (id, force) => api.delete(`/api/llm-providers/${id}${force ? '?force=true' : ''}`),
        setDefault: (id) => api.post(`/api/llm-providers/${id}/set-default`),
        test: (id) => api.post(`/api/llm-providers/${id}/test`),
    },

    // 模型路由（V3）：统一管理 chat/vision/embedding/asr/image 各类 Provider + 功能路由
    modelRouting: {
        getOverview: () => api.get('/api/model-routing'),
        updateRouting: (routing) => api.patch('/api/model-routing', routing),
        listByType: (type) => api.get(`/api/model-routing/${type}`),
        addProvider: (type, data) => api.post(`/api/model-routing/${type}`, data),
        deleteProvider: (type, id) => api.delete(`/api/model-routing/${type}/${id}`),
        updateProvider: (type, id, data) => api.patch(`/api/model-routing/${type}/${id}`, data),
        testProvider: (type, id) => api.post(`/api/model-routing/${type}/${id}/test`),
    },

    // 人格
    personas: {
        list: () => api.get('/api/personas'),
        get: (id) => api.get(`/api/personas/${id}`),
        create: (data) => api.post('/api/personas', data),
        update: (id, data) => api.patch(`/api/personas/${id}`, data),
        delete: (id) => api.delete(`/api/personas/${id}`),
        activate: (id) => api.post(`/api/personas/${id}/activate`),
        copy: (id) => api.post(`/api/personas/${id}/copy`),
        test: (id, data) => api.post('/api/personas/test', { ...data, persona_id: id }),
        export: (id) => api.get(`/api/personas/${id}/export`),
        import: (data) => api.post('/api/personas/import', data),
    },

    // 记忆：优先 flat /api/memory/*；旧签名 (accId, ...) 忽略 id 用 sole 回退
    memory: {
        stats: (accId) => {
            const sole = resolveSoleId(accId);
            return flatOrNested('/api/memory/stats', sole ? `/api/accounts/${sole}/memory/stats` : null);
        },
        list: (accId, params) => {
            const sole = resolveSoleId(accId);
            const q = buildQuery(params);
            return flatOrNested(
                `/api/memory?${q}`,
                sole ? `/api/accounts/${sole}/memory?${q}` : null,
            );
        },
        detail: (accId, memId) => {
            const sole = resolveSoleId(accId);
            // 兼容 flat 调用 detail(memId) 与旧 detail(accId, memId)
            const id = memId != null ? memId : accId;
            const soleForNested = memId != null ? sole : resolveSoleId(null);
            return flatOrNested(
                `/api/memory/${id}`,
                soleForNested ? `/api/accounts/${soleForNested}/memory/${id}` : null,
            );
        },
        search: (accId, data) => {
            // 兼容 search(data) 与 search(accId, data)
            const body = (data && typeof data === 'object') ? data : (typeof accId === 'object' ? accId : {});
            const sole = resolveSoleId(typeof accId === 'string' ? accId : null);
            return flatOrNested(
                '/api/memory/search',
                sole ? `/api/accounts/${sole}/memory/search` : null,
                { method: 'POST', body },
            );
        },
        recall: (accId, data) => {
            const body = (data && typeof data === 'object') ? data : (typeof accId === 'object' ? accId : {});
            const sole = resolveSoleId(typeof accId === 'string' ? accId : null);
            return flatOrNested(
                '/api/memory/recall',
                sole ? `/api/accounts/${sole}/memory/recall` : null,
                { method: 'POST', body },
            );
        },
        recallTraces: (accId, params) => {
            const sole = resolveSoleId(typeof accId === 'string' ? accId : null);
            const p = (params && typeof params === 'object') ? params : (typeof accId === 'object' ? accId : {});
            const q = buildQuery(p);
            return flatOrNested(
                `/api/memory/recall?${q}`,
                sole ? `/api/accounts/${sole}/memory/recall?${q}` : null,
            );
        },
        recallTrace: (accId, traceId) => {
            const sole = resolveSoleId(accId);
            const id = traceId != null ? traceId : accId;
            const soleForNested = traceId != null ? sole : resolveSoleId(null);
            return flatOrNested(
                `/api/memory/recall/${id}`,
                soleForNested ? `/api/accounts/${soleForNested}/memory/recall/${id}` : null,
            );
        },
        delete: (accId, memId) => {
            const sole = resolveSoleId(accId);
            const id = memId != null ? memId : accId;
            const soleForNested = memId != null ? sole : resolveSoleId(null);
            return flatOrNested(
                `/api/memory/${id}`,
                soleForNested ? `/api/accounts/${soleForNested}/memory/${id}` : null,
                { method: 'DELETE' },
            );
        },
        graph: (accId) => {
            const sole = resolveSoleId(accId);
            return flatOrNested('/api/memory/graph', sole ? `/api/accounts/${sole}/memory/graph` : null);
        },
        graphQuery: (accId, data) => {
            const body = (data && typeof data === 'object') ? data : (typeof accId === 'object' ? accId : {});
            const sole = resolveSoleId(typeof accId === 'string' ? accId : null);
            return flatOrNested(
                '/api/memory/graph/query',
                sole ? `/api/accounts/${sole}/memory/graph/query` : null,
                { method: 'POST', body },
            );
        },
        reindex: (accId, data = {}) => {
            const body = (data && typeof data === 'object' && !Array.isArray(data)) ? data : {};
            const sole = resolveSoleId(typeof accId === 'string' ? accId : null);
            return flatOrNested(
                '/api/memory/reindex',
                sole ? `/api/accounts/${sole}/memory/reindex` : null,
                { method: 'POST', body },
            );
        },
        jobs: (accId, params) => {
            const sole = resolveSoleId(typeof accId === 'string' ? accId : null);
            const p = (params && typeof params === 'object') ? params : (typeof accId === 'object' ? accId : {});
            const q = buildQuery(p);
            return flatOrNested(
                `/api/memory/jobs?${q}`,
                sole ? `/api/accounts/${sole}/memory/jobs?${q}` : null,
            );
        },
        retryJob: (accId, jobId) => {
            const sole = resolveSoleId(accId);
            const id = jobId != null ? jobId : accId;
            const soleForNested = jobId != null ? sole : resolveSoleId(null);
            return flatOrNested(
                `/api/memory/jobs/${id}/retry`,
                soleForNested ? `/api/accounts/${soleForNested}/memory/jobs/${id}/retry` : null,
                { method: 'POST', body: {} },
            );
        },
        retryDeadJobs: (accId, data = {}) => {
            const body = (data && typeof data === 'object') ? data : {};
            const sole = resolveSoleId(typeof accId === 'string' ? accId : null);
            return flatOrNested(
                '/api/memory/jobs/retry-dead',
                sole ? `/api/accounts/${sole}/memory/jobs/retry-dead` : null,
                { method: 'POST', body },
            );
        },
        deadJobReport: (accId, params = {}) => {
            const sole = resolveSoleId(typeof accId === 'string' ? accId : null);
            const q = buildQuery(params && typeof params === 'object' ? params : {});
            return flatOrNested(
                `/api/memory/jobs/dead-report?${q}`,
                sole ? `/api/accounts/${sole}/memory/jobs/dead-report?${q}` : null,
            );
        },
        migrate: (accId) => {
            const sole = resolveSoleId(accId);
            return flatOrNested(
                '/api/memory/migrate',
                sole ? `/api/accounts/${sole}/memory/migrate` : null,
                { method: 'POST' },
            );
        },
    },

    // 配置
    config: {
        schema: () => api.get('/api/config/schema'),
        full: () => api.get('/api/config/full'),
        fullWithMeta: () => request('/api/config/full', { method: 'GET', returnEnvelope: true }),
        // 返回完整响应 envelope（含 applied / config_revision），便于前端展示热重载契约
        patch: (data) => request('/api/config', { method: 'PATCH', body: data, returnEnvelope: true }),
        validate: () => api.post('/api/config/validate'),
        reload: () => api.post('/api/config/reload'),
        // 别名（Phase 5 页面使用）
        // 后端无 GET /api/config，改为并行拉取 schema + full 后组合返回
        getWithSchema: async () => {
            const [schema, config] = await Promise.all([
                api.get('/api/config/schema'),
                api.get('/api/config/full'),
            ]);
            return { data: { schema, config, version: 0 } };
        },
        update: (data) => request('/api/config', { method: 'PATCH', body: data, returnEnvelope: true }),
    },

    // 其他
    replies: (params) => api.get(`/api/replies?${buildQuery(params)}`),
    retryReply: (replyId, force = false) => api.post(
        `/api/replies/${replyId}/retry`,
        force ? { force: true } : {},
    ),
    replyContext: (replyId) => api.get(`/api/replies/${replyId}/context`),
    // 审计：list + 详情 + overview 统计（勿与 token-usage 混淆；路径均后端已注册）
    audits: (params) => api.get(`/api/audit/generations?${buildQuery(params)}`),
    audit: {
        list: (params) => api.get(`/api/audit/generations?${buildQuery(params)}`),
        get: (id) => api.get(`/api/audit/generations/${id}`),
        stats: () => api.get('/api/audit/stats'),
        analytics: () => api.get('/api/audit/analytics'),
    },
    logs: (params) => api.get(`/api/logs?${buildQuery(params)}`),
    // 日志下载为文本/流，与 backup.download 同理用 fetch blob
    logsDownload: async () => {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 60000);
        let resp;
        try {
            resp = await fetch('/api/logs/download', {
                credentials: 'same-origin',
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
                signal: controller.signal,
            });
        } catch (fetchErr) {
            clearTimeout(timeoutId);
            if (fetchErr.name === 'AbortError') {
                throw new Error('下载超时，请稍后重试');
            }
            throw new Error('网络连接失败，请检查网络');
        }
        clearTimeout(timeoutId);
        if (resp.status === 401) {
            try {
                const hash = window.location.hash || '';
                if (hash && hash !== '#/login') {
                    sessionStorage.setItem('bilibot_login_return', hash);
                }
            } catch (_) { /* ignore */ }
            window.location.href = '/login';
            throw new Error('未登录');
        }
        if (!resp.ok) {
            let msg = `下载失败: HTTP ${resp.status}`;
            const ct = (resp.headers.get('content-type') || '').toLowerCase();
            if (ct.includes('application/json')) {
                try {
                    const data = await resp.json();
                    msg = data?.error?.message || data?.message || msg;
                } catch (_) { /* ignore */ }
            }
            throw new Error(msg);
        }
        return await resp.blob();
    },
    status: () => api.get('/api/status'),
    // Token 用量（token-usage.js 等可经此封装调用，避免页面硬编码）
    tokenUsage: {
        summary: (params) => api.get(`/api/token-usage/summary?${buildQuery(params)}`),
        today: (params) => api.get(`/api/token-usage/today?${buildQuery(params)}`),
    },
    safety: {
        pauseStatus: () => api.get('/api/safety/pause-status'),
        pause: () => api.post('/api/safety/pause'),
        accountPauseStatus: (accId) => api.get(`/api/safety/accounts/${accId}/pause-status`),
        resumeAccount: (accId) => api.post(`/api/safety/accounts/${accId}/resume`, { confirm: true }),
        resume: () => api.post('/api/safety/resume'),
        blacklist: () => api.get('/api/safety/blacklist'),
        addBlacklist: (data) => api.post('/api/safety/blacklist', data),
        delBlacklist: (uid) => api.delete(`/api/safety/blacklist/${uid}`),
    },
    videoAnalysis: {
        get: () => api.get('/api/video-analysis'),
        patch: (data) => api.patch('/api/video-analysis', data),
        // 别名（Phase 5 页面使用）
        getConfig: () => api.get('/api/video-analysis'),
        updateConfig: (data) => api.patch('/api/video-analysis', data),
        test: (data) => api.post('/api/video-analysis/test', data),
    },
    imageGen: {
        get: () => api.get('/api/image-generation'),
        patch: (data) => api.patch('/api/image-generation', data),
        // 别名（Phase 5 页面使用）
        getConfig: () => api.get('/api/image-generation'),
        updateConfig: (data) => api.patch('/api/image-generation', data),
        test: (data) => api.post('/api/image-generation/test', data),
    },
    drafts: {
        // 优先 flat /api/dynamic-drafts；旧签名 (accId, ...) 忽略 id 用 sole 回退
        list: (accId, params) => {
            const sole = resolveSoleId(typeof accId === 'string' ? accId : null);
            const p = (params && typeof params === 'object') ? params : (typeof accId === 'object' ? accId : {});
            const q = buildQuery(p);
            return flatOrNested(
                `/api/dynamic-drafts?${q}`,
                sole ? `/api/accounts/${sole}/dynamic-drafts?${q}` : null,
            );
        },
        get: (accId, id) => {
            const draftId = id != null ? id : accId;
            const sole = id != null ? resolveSoleId(accId) : resolveSoleId(null);
            return flatOrNested(
                `/api/dynamic-drafts/${draftId}`,
                sole ? `/api/accounts/${sole}/dynamic-drafts/${draftId}` : null,
            );
        },
        // BUG F-004/F-006: approve 需 expected_revision；reject 后端读 note
        approve: (accId, id, body) => {
            const draftId = typeof id === 'string' || typeof id === 'number' ? id : accId;
            const payload = (body && typeof body === 'object') ? body
                : (id && typeof id === 'object' ? id : {});
            const sole = (typeof accId === 'string') ? resolveSoleId(accId) : resolveSoleId(null);
            return flatOrNested(
                `/api/dynamic-drafts/${draftId}/approve`,
                sole ? `/api/accounts/${sole}/dynamic-drafts/${draftId}/approve` : null,
                { method: 'POST', body: payload || {} },
            );
        },
        reject: (accId, id, reason) => {
            const draftId = id != null ? id : accId;
            const note = reason != null ? reason : '';
            const sole = id != null ? resolveSoleId(accId) : resolveSoleId(null);
            return flatOrNested(
                `/api/dynamic-drafts/${draftId}/reject`,
                sole ? `/api/accounts/${sole}/dynamic-drafts/${draftId}/reject` : null,
                { method: 'POST', body: { note } },
            );
        },
        retry: (accId, id) => {
            const draftId = id != null ? id : accId;
            const sole = id != null ? resolveSoleId(accId) : resolveSoleId(null);
            return flatOrNested(
                `/api/dynamic-drafts/${draftId}/retry`,
                sole ? `/api/accounts/${sole}/dynamic-drafts/${draftId}/retry` : null,
                { method: 'POST' },
            );
        },
        update: (accId, id, data) => {
            const draftId = id != null ? id : accId;
            const body = (data && typeof data === 'object') ? data
                : (id && typeof id === 'object' ? id : {});
            const sole = (typeof accId === 'string' && data != null) ? resolveSoleId(accId) : resolveSoleId(null);
            return flatOrNested(
                `/api/dynamic-drafts/${draftId}`,
                sole ? `/api/accounts/${sole}/dynamic-drafts/${draftId}` : null,
                { method: 'PATCH', body },
            );
        },
    },
    backup: {
        create: () => api.post('/api/backup/create'),
        list: () => api.get('/api/backup/list'),
        restore: (name) => api.post('/api/backup/restore', { name }),
        delete: (name) => api.delete(`/api/backup/${name}`),
        download: async (name) => {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 60000);
            let resp;
            try {
                resp = await fetch(`/api/backup/${name}/download`, {
                    credentials: 'same-origin',
                    headers: { 'X-Requested-With': 'XMLHttpRequest' },
                    signal: controller.signal,
                });
            } catch (fetchErr) {
                clearTimeout(timeoutId);
                if (fetchErr.name === 'AbortError') {
                    throw new Error('下载超时，请稍后重试');
                }
                throw new Error('网络连接失败，请检查网络');
            }
            clearTimeout(timeoutId);
            if (resp.status === 401) {
                try {
                    const hash = window.location.hash || '';
                    if (hash && hash !== '#/login') {
                        sessionStorage.setItem('bilibot_login_return', hash);
                    }
                } catch (_) { /* ignore */ }
                window.location.href = '/login';
                throw new Error('未登录');
            }
            if (!resp.ok) {
                let msg = `下载失败: HTTP ${resp.status}`;
                const ct = (resp.headers.get('content-type') || '').toLowerCase();
                if (ct.includes('application/json')) {
                    try {
                        const data = await resp.json();
                        msg = data?.error?.message || data?.message || msg;
                    } catch (_) { /* ignore */ }
                }
                throw new Error(msg);
            }
            return await resp.blob();
        },
    },

    // dynamicDrafts 别名（Phase 5 页面使用，与 drafts 同义）
    dynamicDrafts: null,
};

api.dynamicDrafts = api.drafts;
