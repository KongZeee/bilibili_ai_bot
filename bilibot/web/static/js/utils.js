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
        failed: 'danger',
        rejected: 'danger',
        error: 'danger',
        banned: 'danger',
        pending: 'warning',
        paused: 'warning',
        waiting: 'warning',
        draft: 'info',
        running: 'info',
    };
    return map[String(status).toLowerCase()] || 'info';
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
