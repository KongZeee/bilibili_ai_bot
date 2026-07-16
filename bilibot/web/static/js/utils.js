// utils.js - 通用工具函数

export function escHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

export function formatTime(ts) {
    if (!ts) return '-';
    const d = new Date(typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts);
    return d.toLocaleString('zh-CN', { hour12: false });
}

export function formatDateTime(ts) {
    if (!ts) return '-';
    const d = new Date(typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts);
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function getStatusType(status) {
    const map = {
        published: 'success',
        approved: 'success',
        success: 'success',
        ok: 'success',
        active: 'success',
        succeeded: 'success',
        failed: 'danger',
        rejected: 'danger',
        error: 'danger',
        banned: 'danger',
        expired: 'danger',
        result_unknown: 'danger',
        pending: 'warning',
        paused: 'warning',
        waiting: 'warning',
        generated: 'warning',
        awaiting_review: 'warning',
        retry_wait: 'warning',
        draft: 'info',
        running: 'info',
        publishing: 'info',
    };
    return map[String(status || '').toLowerCase()] || 'info';
}

/** 审计场景英文 → 中文 */
export const AUDIT_SCENE_LABELS = {
    reply_comment: '评论回复',
    proactive_comment: '主动评论',
    proactive_video: '主动视频',
    private_message: '私信',
    dynamic_post: '动态发布',
    memory_admin: '记忆管理',
    image_generation: '文生图',
    video_analysis: '视频分析',
    diary: '日记',
    dream: '梦境',
    life_plan: '日程',
    exploration: '探索',
    creative: '创作',
    companion: '陪伴生活',
};

/** 审计/生成状态英文 → 中文 */
export const AUDIT_STATUS_LABELS = {
    generated: '已生成',
    awaiting_review: '待审核',
    approved: '已通过',
    rejected: '已拒绝',
    publishing: '发布中',
    published: '已发布',
    retry_wait: '等待重试',
    result_unknown: '结果未知',
    failed: '失败',
    expired: '已过期',
    pending: '待处理',
    // 评论页筛选语义
    replied: '已回复',
    // 任务状态
    scheduled: '已调度',
    claimed: '执行中',
    running: '运行中',
    succeeded: '成功',
    cancelled: '已取消',
    interrupted: '已中断',
};

export function auditSceneLabel(scene) {
    if (!scene) return '-';
    return AUDIT_SCENE_LABELS[scene] || scene;
}

export function auditStatusLabel(status, { published } = {}) {
    if (published === true || published === 1) {
        if (!status || status === 'generated') return '已发布';
    }
    if (!status && published) return '已发布';
    if (!status) return '待处理';
    // 评论审计里 published 更贴近「已回复」
    if (status === 'published') return '已发布';
    return AUDIT_STATUS_LABELS[status] || status;
}

export function auditStatusBadgeType(status, { published } = {}) {
    if (published === true || published === 1 || status === 'published' || status === 'approved' || status === 'succeeded') {
        return 'success';
    }
    if (status === 'failed' || status === 'rejected' || status === 'expired' || status === 'result_unknown') {
        return 'danger';
    }
    if (status === 'publishing' || status === 'running' || status === 'claimed') {
        return 'info';
    }
    return 'warning';
}

export function formatRelative(ts) {
    if (!ts) return '-';
    const ms = typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts;
    const diff = (Date.now() - ms) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    return `${Math.floor(diff / 86400)} 天前`;
}

export function debounce(fn, delay = 300) {
    let timer;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), delay);
    };
}

export function downloadFile(filename, content, type = 'text/plain') {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}
