// api.js - 统一 API 请求客户端

const BASE_URL = '';

async function request(url, options = {}) {
    const resp = await fetch(BASE_URL + url, {
        ...options,
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            ...(options.headers || {}),
        },
        body: options.body ? JSON.stringify(options.body) : undefined,
    });

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

    return data.data !== undefined ? data.data : data;
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
        tasks: (id) => api.get(`/api/accounts/${id}/tasks`),
    },

    // LLM
    llm: {
        list: () => api.get('/api/llm-providers'),
        create: (data) => api.post('/api/llm-providers', data),
        update: (id, data) => api.patch(`/api/llm-providers/${id}`, data),
        delete: (id, force) => api.delete(`/api/llm-providers/${id}${force ? '?force=true' : ''}`),
        setDefault: (id) => api.post(`/api/llm-providers/${id}/set-default`),
        test: (id) => api.post(`/api/llm-providers/${id}/test`),
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
        test: (id, data) => api.post(`/api/personas/${id}/test`, data),
        export: (id) => api.get(`/api/personas/${id}/export`),
        import: (data) => api.post('/api/personas/import', data),
    },

    // 记忆
    memory: {
        stats: (accId) => api.get(`/api/accounts/${accId}/memory/stats`),
        list: (accId, params) => api.get(`/api/accounts/${accId}/memory?${new URLSearchParams(params)}`),
        search: (accId, data) => api.post(`/api/accounts/${accId}/memory/search`, data),
        delete: (accId, memId) => api.delete(`/api/accounts/${accId}/memory/${memId}`),
        graph: (accId) => api.get(`/api/accounts/${accId}/memory/graph`),
        graphQuery: (accId, data) => api.post(`/api/accounts/${accId}/memory/graph/query`, data),
        migrate: (accId) => api.post(`/api/accounts/${accId}/memory/migrate`),
    },

    // 配置
    config: {
        schema: () => api.get('/api/config/schema'),
        full: () => api.get('/api/config/full'),
        patch: (data) => api.patch('/api/config', data),
        validate: () => api.post('/api/config/validate'),
        reload: () => api.post('/api/config/reload'),
    },

    // 其他
    replies: (params) => api.get(`/api/replies?${new URLSearchParams(params)}`),
    audits: (params) => api.get(`/api/audit/generations?${new URLSearchParams(params)}`),
    logs: (params) => api.get(`/api/logs?${new URLSearchParams(params)}`),
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
    },
    imageGen: {
        get: () => api.get('/api/image-generation'),
        patch: (data) => api.patch('/api/image-generation', data),
    },
    drafts: {
        list: (accId) => api.get(`/api/accounts/${accId}/dynamic-drafts`),
        get: (accId, id) => api.get(`/api/accounts/${accId}/dynamic-drafts/${id}`),
        approve: (accId, id) => api.post(`/api/accounts/${accId}/dynamic-drafts/${id}/approve`),
        reject: (accId, id, reason) => api.post(`/api/accounts/${accId}/dynamic-drafts/${id}/reject`, { reason }),
        retry: (accId, id) => api.post(`/api/accounts/${accId}/dynamic-drafts/${id}/retry`),
    },
    backup: {
        create: () => api.post('/api/backup/create'),
        list: () => api.get('/api/backup/list'),
        restore: (name) => api.post('/api/backup/restore', { name }),
        delete: (name) => api.delete(`/api/backup/${name}`),
    },
};
