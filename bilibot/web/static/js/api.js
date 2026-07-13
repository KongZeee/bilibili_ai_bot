// api.js - 统一 API 请求客户端

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
        window.location.href = '/login';
        throw new Error('未登录');
    }

    const data = await resp.json().catch(() => ({}));

    if (!resp.ok || data.success === false) {
        const err = new Error(data?.error?.message || data?.message || `HTTP ${resp.status}`);
        err.code = data?.error?.code;
        err.details = data?.error?.details;
        err.resp = data;
        throw err;
    }

    return options.returnEnvelope ? data : (data.data !== undefined ? data.data : data);
}

export const api = {
    get: (url) => request(url, { method: 'GET' }),
    post: (url, body) => request(url, { method: 'POST', body }),
    patch: (url, body) => request(url, { method: 'PATCH', body }),
    delete: (url) => request(url, { method: 'DELETE' }),

    // 账号
    accounts: {
        list: () => api.get('/api/accounts'),
        get: (id) => api.get(`/api/accounts/${id}`),
        create: (data) => api.post('/api/accounts', data),
        update: (id, data) => api.patch(`/api/accounts/${id}`, data),
        delete: (id) => api.delete(`/api/accounts/${id}`),
        setDefault: (id) => api.post(`/api/accounts/${id}/set-default`),
        start: (id) => api.post(`/api/accounts/${id}/start`),
        stop: (id) => api.post(`/api/accounts/${id}/stop`),
        bindPersona: (id, data) => api.post(`/api/accounts/${id}/persona`, data),
        switchPersona: (id, personaId) => api.post(`/api/accounts/${id}/switch-persona`, { persona_id: personaId }),
        bindLlm: (id, llmId) => api.post(`/api/accounts/${id}/llm`, { llm_id: llmId }),
        qrLogin: (id) => api.post(`/api/accounts/${id}/qr-login`),
        qrPoll: (id, sid) => api.get(`/api/accounts/${id}/qr-login/${sid}`),
        qrCancel: (id, sid) => api.post(`/api/accounts/${id}/qr-login/${sid}/cancel`),
        profiles: () => api.get('/api/accounts/profiles'),
        tasks: (id, params) => api.get(`/api/accounts/${id}/tasks?${buildQuery(params)}`),
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

    // 记忆
    memory: {
        stats: (accId) => api.get(`/api/accounts/${accId}/memory/stats`),
        list: (accId, params) => api.get(`/api/accounts/${accId}/memory?${buildQuery(params)}`),
        detail: (accId, memId) => api.get(`/api/accounts/${accId}/memory/${memId}`),
        search: (accId, data) => api.post(`/api/accounts/${accId}/memory/search`, data),
        recall: (accId, data) => api.post(`/api/accounts/${accId}/memory/recall`, data),
        recallTraces: (accId, params) => api.get(`/api/accounts/${accId}/memory/recall?${buildQuery(params)}`),
        recallTrace: (accId, traceId) => api.get(`/api/accounts/${accId}/memory/recall/${traceId}`),
        delete: (accId, memId) => api.delete(`/api/accounts/${accId}/memory/${memId}`),
        graph: (accId) => api.get(`/api/accounts/${accId}/memory/graph`),
        graphQuery: (accId, data) => api.post(`/api/accounts/${accId}/memory/graph/query`, data),
        reindex: (accId, data = {}) => api.post(`/api/accounts/${accId}/memory/reindex`, data),
        jobs: (accId, params) => api.get(`/api/accounts/${accId}/memory/jobs?${buildQuery(params)}`),
        retryJob: (accId, jobId) => api.post(`/api/accounts/${accId}/memory/jobs/${jobId}/retry`, {}),
        migrate: (accId) => api.post(`/api/accounts/${accId}/memory/migrate`),
    },

    // 配置
    config: {
        schema: () => api.get('/api/config/schema'),
        full: () => api.get('/api/config/full'),
        fullWithMeta: () => request('/api/config/full', { method: 'GET', returnEnvelope: true }),
        patch: (data) => api.patch('/api/config', data),
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
        update: (data) => api.patch('/api/config', data),
    },

    // 其他
    replies: (params) => api.get(`/api/replies?${buildQuery(params)}`),
    audits: (params) => api.get(`/api/audit/generations?${buildQuery(params)}`),
    logs: (params) => api.get(`/api/logs?${buildQuery(params)}`),
    status: () => api.get('/api/status'),
    safety: {
        pauseStatus: () => api.get('/api/safety/pause-status'),
        pause: () => api.post('/api/safety/pause'),
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
        list: (accId, params) => api.get(`/api/accounts/${accId}/dynamic-drafts?${buildQuery(params)}`),
        get: (accId, id) => api.get(`/api/accounts/${accId}/dynamic-drafts/${id}`),
        // BUG F-004/F-006: approve 需 expected_revision；reject 后端读 note
        approve: (accId, id, body) => api.post(`/api/accounts/${accId}/dynamic-drafts/${id}/approve`, body || {}),
        reject: (accId, id, reason) => api.post(`/api/accounts/${accId}/dynamic-drafts/${id}/reject`, { note: reason }),
        retry: (accId, id) => api.post(`/api/accounts/${accId}/dynamic-drafts/${id}/retry`),
        update: (accId, id, data) => api.patch(`/api/accounts/${accId}/dynamic-drafts/${id}`, data),
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
                window.location.href = '/login';
                throw new Error('未登录');
            }
            if (!resp.ok) throw new Error(`下载失败: HTTP ${resp.status}`);
            return await resp.blob();
        },
    },

    // dynamicDrafts 别名（Phase 5 页面使用，与 drafts 同义）
    dynamicDrafts: null,
};

api.dynamicDrafts = api.drafts;
