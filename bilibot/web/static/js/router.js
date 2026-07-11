// router.js - 轻量 hash 路由器
const { ref, reactive } = window.Vue;

export const route = reactive({
    path: '/',
    segments: [],
    params: {},
});

// 路由表：path pattern → { component, title }
const routes = [];

export function registerRoute(pattern, component, title) {
    routes.push({ pattern, segments: pattern.split('/').filter(Boolean), component, title });
}

export function findRoute(path) {
    const segments = path.split('/').filter(Boolean);
    for (const r of routes) {
        if (r.segments.length !== segments.length) continue;
        const params = {};
        let matched = true;
        for (let i = 0; i < r.segments.length; i++) {
            if (r.segments[i].startsWith(':')) {
                params[r.segments[i].slice(1)] = decodeURIComponent(segments[i]);
            } else if (r.segments[i] !== segments[i]) {
                matched = false;
                break;
            }
        }
        if (matched) return { route: r, params };
    }
    return null;
}

export function navigate(path) {
    window.location.hash = '#' + path;
}

export function initRouter() {
    function parse() {
        const hash = window.location.hash.slice(1) || '/';
        route.path = hash;
        route.segments = hash.split('/').filter(Boolean);
        const found = findRoute(hash);
        route.params = found?.params || {};
    }
    window.addEventListener('hashchange', parse);
    parse();
}

export function useRoute() {
    return route;
}
