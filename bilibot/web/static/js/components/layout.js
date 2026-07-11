// components/layout.js - 双栏布局组件
const { defineComponent, computed, h } = window.Vue;
import { Icon } from './common.js';

// 导航分组配置
export const NAV_GROUPS = [
    {
        id: 'overview',
        icon: 'dashboard',
        label: '运营总览',
        items: [
            { path: '/', label: '总览', icon: 'dashboard' },
            { path: '/comments', label: '评论', icon: 'comments' },
            { path: '/logs', label: '日志', icon: 'logs' },
        ],
    },
    {
        id: 'identity',
        icon: 'accounts',
        label: '账号与身份',
        items: [
            { path: '/accounts', label: '账号管理', icon: 'accounts' },
            { path: '/personas', label: '人格管理', icon: 'personas' },
            { path: '/llm', label: 'LLM 管理', icon: 'llm' },
        ],
    },
    {
        id: 'creation',
        icon: 'sparkles',
        label: '内容创作',
        items: [
            { path: '/proactive', label: '主动行为', icon: 'proactive' },
            { path: '/drafts', label: '动态草稿', icon: 'drafts' },
            { path: '/image-gen', label: '文生图', icon: 'image' },
            { path: '/video-analysis', label: '视频理解', icon: 'video' },
        ],
    },
    {
        id: 'memory',
        icon: 'memory',
        label: '记忆与知识',
        items: [
            { path: '/memory/graph', label: '记忆图谱', icon: 'graph' },
            { path: '/memory/list', label: '记忆列表', icon: 'drafts' },
            { path: '/memory/recall', label: '召回测试', icon: 'search' },
        ],
    },
    {
        id: 'system',
        icon: 'config',
        label: '系统',
        items: [
            { path: '/system', label: '系统设置', icon: 'system' },
            { path: '/config', label: '全局配置', icon: 'config' },
        ],
    },
];

// 根据路径找到所属分组
export function findGroupByPath(path) {
    for (const g of NAV_GROUPS) {
        if (g.items.some(i => path === i.path || path.startsWith(i.path + '/'))) {
            return g.id;
        }
    }
    return 'overview';
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

// ActivityBar 组件
export const ActivityBar = defineComponent({
    name: 'ActivityBar',
    props: { activeGroup: String },
    emits: ['select'],
    setup(props, { emit }) {
        return () => h('div', { class: 'activity-bar' }, [
            h('div', { class: 'logo' }, 'B'),
            ...NAV_GROUPS.map(g =>
                h('div', {
                    class: ['activity-item', props.activeGroup === g.id ? 'active' : ''],
                    title: g.label,
                    onClick: () => emit('select', g.id),
                }, [
                    h(Icon, { name: g.icon, size: 22 }),
                ])
            ),
            h('div', { class: 'spacer' }),
            h('button', {
                class: 'logout-btn',
                title: '退出登录',
                onClick: () => { window.location.href = '/login'; },
            }, [h(Icon, { name: 'logout', size: 20 })]),
        ]);
    },
});

// SideBar 组件
export const SideBar = defineComponent({
    name: 'SideBar',
    props: { activeGroup: String, currentPath: String },
    emits: ['navigate'],
    setup(props, { emit }) {
        const group = computed(() =>
            NAV_GROUPS.find(g => g.id === props.activeGroup) || NAV_GROUPS[0]
        );

        return () => h('div', { class: 'sidebar' }, [
            h('div', { class: 'sidebar-header' }, group.value.label),
            h('div', { class: 'sidebar-nav' },
                group.value.items.map(item =>
                    h('div', {
                        class: ['sidebar-item',
                            (props.currentPath === item.path ||
                             props.currentPath.startsWith(item.path + '/')) ? 'active' : ''],
                        onClick: () => emit('navigate', item.path),
                    }, [
                        h(Icon, { name: item.icon, size: 18 }),
                        h('span', item.label),
                    ])
                )
            ),
        ]);
    },
});

// AppShell 组件（双栏布局外壳）
export const AppShell = defineComponent({
    name: 'AppShell',
    props: { currentPath: String, pageTitle: String },
    emits: ['navigate'],
    setup(props, { emit, slots }) {
        const activeGroup = computed(() => findGroupByPath(props.currentPath));

        return () => h('div', { class: 'app-shell' }, [
            h(ActivityBar, {
                activeGroup: activeGroup.value,
                onSelect: (gid) => {
                    // 切换分组时导航到该组第一个页面
                    const g = NAV_GROUPS.find(x => x.id === gid);
                    if (g && g.items[0]) emit('navigate', g.items[0].path);
                },
            }),
            h(SideBar, {
                activeGroup: activeGroup.value,
                currentPath: props.currentPath,
                onNavigate: (path) => emit('navigate', path),
            }),
            h('div', { class: 'main-content' }, [
                h('div', { class: 'main-header' }, [
                    h('h1', props.pageTitle),
                ]),
                h('div', { class: 'main-body' }, slots.default?.()),
            ]),
        ]);
    },
});
