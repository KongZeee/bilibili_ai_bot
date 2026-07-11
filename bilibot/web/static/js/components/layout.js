// components/layout.js - Golden Time 双栏布局外壳
const { defineComponent, computed, h } = window.Vue;
import { navigate } from '../router.js';
import { api } from '../api.js';

// 导航分组配置 — 所有分组同时展示，无需切换
export const NAV_GROUPS = [
    {
        label: '运营',
        items: [
            { path: '/', label: '总览', icon: 'house', meta: '今日' },
            { path: '/comments', label: '评论', icon: 'message-circle-more', meta: '互动' },
            { path: '/logs', label: '日志', icon: 'file', meta: '近期' },
        ],
    },
    {
        label: '账号与身份',
        items: [
            { path: '/accounts', label: '账号管理', icon: 'user', meta: '已接入' },
            { path: '/personas', label: '人格管理', icon: 'star', meta: '多角色' },
            { path: '/llm', label: 'LLM 管理', icon: 'box', meta: '多模型' },
        ],
    },
    {
        label: '内容创作',
        items: [
            { path: '/proactive', label: '主动行为', icon: 'send-horizontal', meta: '计划中' },
            { path: '/drafts', label: '动态草稿', icon: 'pen-line', meta: '待发布' },
            { path: '/image-gen', label: '文生图', icon: 'folder-open', meta: '素材' },
            { path: '/video-analysis', label: '视频理解', icon: 'mouse-pointer-click', meta: '解析' },
        ],
    },
    {
        label: '记忆与知识',
        items: [
            { path: '/memory/graph', label: '记忆图谱', icon: 'map-pin', meta: '关系' },
            { path: '/memory/graph-3d', label: '图谱 3D', icon: 'map-pin', meta: '3D' },
            { path: '/memory/list', label: '记忆列表', icon: 'folder', meta: '全部' },
            { path: '/memory/recall', label: '召回测试', icon: 'circle-check', meta: '验证' },
        ],
    },
    {
        label: '系统',
        items: [
            { path: '/system', label: '系统设置', icon: 'grip', meta: '基础' },
            { path: '/config', label: '全局配置', icon: 'tag', meta: '参数' },
        ],
    },
];

// 页面副标题映射
const PAGE_SUBTITLES = {
    '/': 'BiliBot 运营全景一览，实时掌握账号状态、互动表现与系统健康度。',
    '/accounts': '管理 B站账号接入、Cookie 凭证、人格绑定与 LLM 通道配置。',
    '/personas': '为不同账号配置专属 AI 人格，定义性格、语气与交互边界。',
    '/memory/list': '浏览与管理 Bot 记忆库中的所有记忆条目，支持分类筛选与召回测试。',
    '/memory/graph': '以图谱视角探索记忆节点间的关联关系，悬停查看节点概况。',
    '/memory/graph-3d': '以三维可旋转视角探索记忆节点间的关联关系。',
    '/memory/recall': '测试 Bot 记忆库的召回能力，验证记忆检索效果。',
    '/config': '配置 B站账号、LLM 服务、回复策略与主动行为等全局参数。',
    '/comments': '查看与管理评论回复记录，跟踪互动状态。',
    '/logs': '查看系统运行日志，支持按级别和关键词筛选。',
    '/proactive': '管理与触发 Bot 的主动行为任务。',
    '/drafts': '审核与管理待发布的动态草稿。',
    '/image-gen': '配置与测试文生图功能。',
    '/video-analysis': '配置与测试视频理解功能。',
    '/system': '系统安全管理与备份恢复。',
    '/llm': '管理 LLM Provider 列表，配置模型与 API 密钥。',
};

// 根据路径找到所属分组（返回分组标签）
export function findGroupByPath(path) {
    for (const g of NAV_GROUPS) {
        if (g.items.some(i => path === i.path || path.startsWith(i.path + '/'))) {
            return g.label;
        }
    }
    return NAV_GROUPS[0].label;
}

// 根据路径找到当前页面标题
export function findPageTitle(path) {
    for (const g of NAV_GROUPS) {
        for (const item of g.items) {
            if (path === item.path || path.startsWith(item.path + '/')) {
                return item.label;
            }
        }
    }
    return '总览';
}

// 判定路径是否激活：精确匹配或前缀匹配；根路径仅精确匹配
export function isActive(currentPath, path) {
    if (path === '/') return currentPath === '/';
    return currentPath === path || currentPath.startsWith(path + '/');
}

// 退出登录
async function handleLogout() {
    try {
        await api.post('/api/logout');
    } catch (e) {
        // 忽略错误，仍跳转登录页
    }
    window.location.href = '/login';
}

// 内联 data-icon span（CSS mask 图标）
function iconSpan(name) {
    return h('span', {
        'data-icon': '',
        style: {
            '-webkit-mask-image': `url('/static/icons/${name}.svg')`,
            'mask-image': `url('/static/icons/${name}.svg')`,
        },
        'aria-hidden': 'true',
    });
}

// SideBar 组件 — 渲染全部导航分组
export const Sidebar = defineComponent({
    name: 'Sidebar',
    props: {
        currentPath: String,
        onNavigate: { type: Function, default: null },
    },
    emits: ['navigate'],
    setup(props, { emit }) {
        return () => h('aside', { class: 'sidebar' }, [
            h('div', { class: 'brand-lockup' }, [
                h('span', { class: 'brand-kicker' }, 'BiliBot 控制台'),
                h('div', { class: 'brand-name' }, 'BiliBot'),
                h('div', { class: 'workspace-note' }, '面向 B站 AI 机器人的多账号运营工作台，统一调度人格、记忆与内容创作。'),
            ]),
            ...NAV_GROUPS.map(group =>
                h('nav', { class: 'nav-group', 'aria-label': group.label }, [
                    h('div', { class: 'nav-label' }, group.label),
                    ...group.items.map(item => {
                        const active = isActive(props.currentPath, item.path);
                        return h('a', {
                            class: 'nav-item',
                            href: '#' + item.path,
                            'data-active': active ? 'true' : 'false',
                            'aria-current': active ? 'page' : undefined,
                            onClick: (e) => {
                                e.preventDefault();
                                emit('navigate', item.path);
                            },
                        }, [
                            h('span', { class: 'nav-copy' }, [
                                h('span', { class: 'nav-icon' }, [iconSpan(item.icon)]),
                                h('span', item.label),
                            ]),
                            h('span', { class: 'nav-meta' }, item.meta),
                        ]);
                    }),
                ])
            ),
            h('div', { class: 'sidebar-footer' }, [
                h('span', { class: 'eyebrow' }, '当前状态'),
                h('strong', 'Bot 运行中'),
                h('span', { class: 'muted' }, '多账号运营工作台已就绪。'),
                h('button', {
                    class: 'btn ghost',
                    type: 'button',
                    onClick: handleLogout,
                }, '退出登录'),
            ]),
        ]);
    },
});

// Topbar 组件
export const Topbar = defineComponent({
    name: 'Topbar',
    props: {
        pageTitle: String,
        pageSubtitle: String,
        onNavigate: { type: Function, default: null },
    },
    setup(props) {
        return () => h('header', { class: 'topbar' }, [
            h('div', { class: 'topbar-copy' }, [
                h('h1', { class: 'page-title' }, props.pageTitle),
                h('div', { class: 'page-subtitle' }, props.pageSubtitle),
            ]),
            h('div', { class: 'topbar-actions' }, [
                h('label', { class: 'field-wrap', 'aria-label': '搜索' }, [
                    iconSpan('funnel'),
                    h('input', {
                        class: 'field',
                        type: 'text',
                        placeholder: '搜索账号、人格或记忆',
                    }),
                ]),
                h('button', {
                    class: 'icon-btn',
                    type: 'button',
                    'aria-label': '通知',
                }, [iconSpan('mail')]),
                h('button', { class: 'btn ghost', type: 'button' }, '刷新状态'),
                h('button', {
                    class: 'btn primary',
                    type: 'button',
                    onClick: () => navigate('/'),
                }, '查看总览'),
            ]),
        ]);
    },
});

// AppShell 组件（双栏布局外壳）
export const AppShell = defineComponent({
    name: 'AppShell',
    props: {
        currentPath: String,
        pageTitle: String,
        pageSubtitle: String,
    },
    emits: ['navigate'],
    setup(props, { emit, slots }) {
        const subtitle = computed(() => {
            if (props.pageSubtitle) return props.pageSubtitle;
            if (PAGE_SUBTITLES[props.currentPath]) return PAGE_SUBTITLES[props.currentPath];
            for (const path of Object.keys(PAGE_SUBTITLES)) {
                if (path !== '/' && props.currentPath.startsWith(path + '/')) {
                    return PAGE_SUBTITLES[path];
                }
            }
            return '';
        });

        return () => h('div', { class: 'app-shell' }, [
            h(Sidebar, {
                currentPath: props.currentPath,
                onNavigate: (path) => emit('navigate', path),
            }),
            h('main', { class: 'main-area' }, [
                h(Topbar, {
                    pageTitle: props.pageTitle,
                    pageSubtitle: subtitle.value,
                    onNavigate: (path) => emit('navigate', path),
                }),
                h('div', { class: 'view-frame' }, slots.default?.()),
            ]),
        ]);
    },
});
