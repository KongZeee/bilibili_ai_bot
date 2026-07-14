// components/layout.js - Golden Time 双栏布局外壳（含移动端抽屉式侧边栏）
const { defineComponent, computed, h, ref, watch, onMounted, onBeforeUnmount } = window.Vue;
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
            { path: '/llm', label: '模型管理', icon: 'box', meta: '多模型' },
            { path: '/model-routing', label: '模型分配', icon: 'arrow-right', meta: '路由' },
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
            { path: '/memory/graph', label: '记忆图谱', icon: 'map-pin', meta: '3D' },
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
    '/accounts': '管理 B站账号接入、Cookie 凭证、人格绑定与对话模型配置。',
    '/personas': '为不同账号配置专属 AI 人格，定义性格、语气与交互边界。',
    '/memory/graph': '以三维可旋转视角探索记忆节点间的关联关系。',
    '/memory/list': '浏览与管理 Bot 记忆库中的所有记忆条目，支持分类筛选与召回测试。',
    '/memory/recall': '测试 Bot 记忆库的召回能力，验证记忆检索效果。',
    '/config': '配置 B站账号、模型服务、回复策略与主动行为等全局参数。',
    '/comments': '查看与管理评论回复记录，跟踪互动状态。',
    '/logs': '查看系统运行日志，支持按级别和关键词筛选。',
    '/proactive': '管理与触发 Bot 的主动行为任务。',
    '/drafts': '审核与管理待发布的动态草稿。',
    '/image-gen': '配置与测试文生图功能。',
    '/video-analysis': '配置与测试视频理解功能。',
    '/system': '系统安全管理与备份恢复。',
    '/llm': '管理各类型模型服务商，配置模型名称与 API 密钥。',
    '/model-routing': '为对话、视觉、向量检索、语音识别、文生图各自指定当前服务商。',
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

// SideBar 组件 — 渲染全部导航分组（移动端为抽屉式）
export const Sidebar = defineComponent({
    name: 'Sidebar',
    props: {
        currentPath: String,
        open: { type: Boolean, default: false },
        onNavigate: { type: Function, default: null },
    },
    emits: ['navigate', 'close'],
    setup(props, { emit }) {
        const paused = ref(false);
        async function refreshPauseStatus() {
            try {
                const data = await api.safety.pauseStatus();
                paused.value = !!data?.paused;
            } catch (_) { /* 读取暂停状态失败，保持默认 */ }
        }
        onMounted(() => {
            refreshPauseStatus();
            // 路由切换时同步暂停态（系统页 pause/resume 后侧边栏需更新）
            window.addEventListener('hashchange', refreshPauseStatus);
        });
        onBeforeUnmount(() => {
            window.removeEventListener('hashchange', refreshPauseStatus);
        });
        return () => [
            // 遮罩层 — 仅移动端可见（CSS 控制），点击关闭抽屉
            h('div', {
                class: 'sidebar-overlay',
                'data-open': props.open ? 'true' : 'false',
                onClick: () => emit('close'),
            }),
            h('aside', {
                class: 'sidebar',
                id: 'app-sidebar',
                'data-open': props.open ? 'true' : 'false',
            }, [
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
                    h('strong', paused.value ? '已暂停' : 'Bot 运行中'),
                    h('span', { class: 'muted' }, '多账号运营工作台已就绪。'),
                    h('button', {
                        class: 'btn ghost',
                        type: 'button',
                        onClick: handleLogout,
                    }, '退出登录'),
                ]),
            ]),
        ];
    },
});

// Topbar 组件
export const Topbar = defineComponent({
    name: 'Topbar',
    props: {
        pageTitle: String,
        pageSubtitle: String,
        sidebarOpen: { type: Boolean, default: false },
        onNavigate: { type: Function, default: null },
        onToggleSidebar: { type: Function, default: null },
    },
    setup(props) {
        return () => h('header', { class: 'topbar' }, [
            h('div', { class: 'topbar-copy' }, [
                // 汉堡菜单按钮 — 桌面隐藏，移动端显示（CSS 控制）
                h('button', {
                    class: 'menu-toggle',
                    type: 'button',
                    'aria-label': props.sidebarOpen ? '关闭导航菜单' : '打开导航菜单',
                    'aria-expanded': props.sidebarOpen ? 'true' : 'false',
                    'aria-controls': 'app-sidebar',
                    onClick: () => props.onToggleSidebar && props.onToggleSidebar(),
                }, [
                    h('span', { class: 'menu-toggle-bar', 'aria-hidden': 'true' }),
                    h('span', { class: 'menu-toggle-bar', 'aria-hidden': 'true' }),
                    h('span', { class: 'menu-toggle-bar', 'aria-hidden': 'true' }),
                ]),
                h('div', { class: 'topbar-titles' }, [
                    h('h1', { class: 'page-title' }, props.pageTitle),
                    h('div', { class: 'page-subtitle' }, props.pageSubtitle),
                ]),
            ]),
            h('div', { class: 'topbar-actions' }, [
                h('button', {
                    class: 'btn primary',
                    type: 'button',
                    onClick: () => navigate('/'),
                }, '查看总览'),
            ]),
        ]);
    },
});

// AppShell 组件（双栏布局外壳 + 移动端抽屉状态管理）
export const AppShell = defineComponent({
    name: 'AppShell',
    props: {
        currentPath: String,
        pageTitle: String,
        pageSubtitle: String,
    },
    emits: ['navigate'],
    setup(props, { emit, slots }) {
        const sidebarOpen = ref(false);

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

        // 路由变化时自动关闭抽屉
        watch(() => props.currentPath, () => { sidebarOpen.value = false; });

        // ESC 键关闭抽屉
        const onKeydown = (e) => {
            if (e.key === 'Escape' && sidebarOpen.value) {
                sidebarOpen.value = false;
            }
        };
        onMounted(() => document.addEventListener('keydown', onKeydown));
        onBeforeUnmount(() => document.removeEventListener('keydown', onKeydown));

        const toggleSidebar = () => { sidebarOpen.value = !sidebarOpen.value; };

        return () => h('div', { class: 'app-shell' }, [
            h(Sidebar, {
                currentPath: props.currentPath,
                open: sidebarOpen.value,
                onClose: () => { sidebarOpen.value = false; },
                onNavigate: (path) => {
                    emit('navigate', path);
                    sidebarOpen.value = false;
                },
            }),
            h('main', { class: 'main-area' }, [
                h(Topbar, {
                    pageTitle: props.pageTitle,
                    pageSubtitle: subtitle.value,
                    sidebarOpen: sidebarOpen.value,
                    onToggleSidebar: toggleSidebar,
                    onNavigate: (path) => emit('navigate', path),
                }),
                h('div', { class: 'view-frame' }, slots.default?.()),
            ]),
        ]);
    },
});
