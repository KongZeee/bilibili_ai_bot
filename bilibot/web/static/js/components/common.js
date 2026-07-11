// components/common.js - 通用 Vue 组件库
const { defineComponent, h, ref, computed, watch, onMounted, onUnmounted } = window.Vue;

// ═══════════════════════════════════════════════════
// Icon 组件 - 扁平化 SVG 图标系统
// 使用 Material Design / Feather 风格的 24x24 线性图标
// ═══════════════════════════════════════════════════
const ICON_PATHS = {
    // 运营总览
    dashboard: 'M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z',
    comments: 'M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z',
    logs: 'M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-4-6zM6 20V4h7v5h5v11H6z',
    // 账号与身份
    accounts: 'M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z',
    personas: 'M3 5h18v14H3V5zm2 2v10h14V7H5zm4 2h6v2H9V9z M12 2l3 3-3 3-3-3 3-3z',
    llm: 'M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5',
    // 内容创作
    sparkles: 'M12 2l1.5 4.5L18 8l-4.5 1.5L12 14l-1.5-4.5L6 8l4.5-1.5L12 2zM5 14l.75 2.25L8 17l-2.25.75L5 20l-.75-2.25L2 17l2.25-.75L5 14zm14 0l.75 2.25L22 17l-2.25.75L19 20l-.75-2.25L16 17l2.25-.75L19 14z',
    proactive: 'M17.65 6.35A7.958 7.958 0 0012 4a8 8 0 108 8h-2c0 3.31-2.69 6-6 6s-6-2.69-6-6 2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z',
    drafts: 'M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34a.996.996 0 00-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z',
    image: 'M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z',
    video: 'M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z',
    // 记忆与知识
    memory: 'M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z M12 8v4 M12 12l3 3',
    graph: 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z',
    search: 'M15.5 14h-.79l-.28-.27a6.5 6.5 0 10-.7.7l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0A4.5 4.5 0 1114 9.5 4.5 4.5 0 019.5 14z',
    // 系统
    system: 'M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4z',
    config: 'M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.488.488 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 00-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z',
    logout: 'M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z',
    // 通用
    empty: 'M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V5h14v14zM7 11h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2z',
    refresh: 'M17.65 6.35A7.958 7.958 0 0012 4a8 8 0 108 8h-2c0 3.31-2.69 6-6 6s-6-2.69-6-6 2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z',
};

export const Icon = defineComponent({
    name: 'Icon',
    props: {
        name: { type: String, required: true },
        size: { type: [Number, String], default: 20 },
    },
    inheritAttrs: true,
    setup(props, { attrs }) {
        return () => h('svg', {
            class: 'icon-svg',
            width: props.size,
            height: props.size,
            viewBox: '0 0 24 24',
            fill: 'currentColor',
            'aria-hidden': 'true',
            ...attrs,
        }, [
            h('path', { d: ICON_PATHS[props.name] || ICON_PATHS.empty }),
        ]);
    },
});

// Card 组件
export const Card = defineComponent({
    name: 'Card',
    props: {
        title: String,
        bordered: { type: Boolean, default: false },
    },
    setup(props, { slots }) {
        return () => h('div', { class: ['card', props.bordered ? 'card-bordered' : ''] }, [
            props.title
                ? h('div', { class: 'card-header' }, [
                    h('span', { class: 'card-title' }, props.title),
                    slots.action?.(),
                ])
                : null,
            h('div', { class: 'card-body' }, slots.default?.()),
        ]);
    },
});

// Button 组件
export const Button = defineComponent({
    name: 'Button',
    props: {
        type: { type: String, default: 'secondary' }, // primary/secondary/danger
        size: { type: String, default: 'md' }, // sm/md
        loading: Boolean,
        disabled: Boolean,
    },
    emits: ['click'],
    setup(props, { slots, emit }) {
        return () => h('button', {
            class: [
                'btn',
                `btn-${props.type}`,
                props.size === 'sm' ? 'btn-sm' : '',
                props.loading ? 'btn-loading' : '',
            ],
            disabled: props.disabled || props.loading,
            onClick: (e) => emit('click', e),
        }, [
            props.loading ? h('span', { class: 'spinner spinner-sm' }) : null,
            h('span', slots.default?.()),
        ]);
    },
});

// Toggle 组件（开关）
export const Toggle = defineComponent({
    name: 'Toggle',
    props: {
        modelValue: Boolean,
        disabled: Boolean,
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        return () => h('label', { class: ['toggle', props.disabled ? 'toggle-disabled' : ''] }, [
            h('input', {
                type: 'checkbox',
                checked: props.modelValue,
                disabled: props.disabled,
                onChange: (e) => emit('update:modelValue', e.target.checked),
            }),
            h('span', { class: 'toggle-slider' }),
        ]);
    },
});

// Badge 组件
export const Badge = defineComponent({
    name: 'Badge',
    props: {
        type: { type: String, default: 'info' }, // success/warning/danger/info
        size: { type: String, default: 'md' },
    },
    setup(props, { slots }) {
        return () => h('span', {
            class: ['badge', `badge-${props.type}`, props.size === 'sm' ? 'badge-sm' : ''],
        }, slots.default?.());
    },
});

// FormInput 组件
export const FormInput = defineComponent({
    name: 'FormInput',
    props: {
        modelValue: [String, Number],
        type: { type: String, default: 'text' },
        placeholder: String,
        label: String,
        hint: String,
        error: String,
        disabled: Boolean,
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        return () => h('div', { class: 'form-group' }, [
            props.label ? h('label', { class: 'form-label' }, props.label) : null,
            h('input', {
                class: ['form-input', props.error ? 'form-input-error' : ''],
                type: props.type,
                value: props.modelValue,
                placeholder: props.placeholder,
                disabled: props.disabled,
                onInput: (e) => emit('update:modelValue', e.target.value),
            }),
            props.hint ? h('div', { class: 'form-hint' }, props.hint) : null,
            props.error ? h('div', { class: 'form-error' }, props.error) : null,
        ]);
    },
});

// FormSelect 组件
export const FormSelect = defineComponent({
    name: 'FormSelect',
    props: {
        modelValue: [String, Number],
        options: Array, // [{value, label}]
        label: String,
        hint: String,
        disabled: Boolean,
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        return () => h('div', { class: 'form-group' }, [
            props.label ? h('label', { class: 'form-label' }, props.label) : null,
            h('select', {
                class: 'form-input',
                value: props.modelValue,
                disabled: props.disabled,
                onChange: (e) => emit('update:modelValue', e.target.value),
            }, (props.options || []).map(opt =>
                h('option', { value: opt.value }, opt.label)
            )),
            props.hint ? h('div', { class: 'form-hint' }, props.hint) : null,
        ]);
    },
});

// Modal 组件
export const Modal = defineComponent({
    name: 'Modal',
    props: {
        modelValue: Boolean,
        title: String,
        width: { type: String, default: '560px' },
    },
    emits: ['update:modelValue', 'close'],
    setup(props, { slots, emit }) {
        const close = () => { emit('update:modelValue', false); emit('close'); };

        return () => props.modelValue
            ? h('div', { class: 'modal-overlay', onClick: close }, [
                h('div', {
                    class: 'modal',
                    style: { maxWidth: props.width },
                    onClick: (e) => e.stopPropagation(),
                }, [
                    h('div', { class: 'modal-header' }, [
                        h('h3', props.title || ''),
                        h('button', { class: 'modal-close', onClick: close }, '×'),
                    ]),
                    h('div', { class: 'modal-body' }, slots.default?.()),
                    slots.footer
                        ? h('div', { class: 'modal-footer' }, slots.footer())
                        : null,
                ])
            ])
            : null;
    },
});

// EmptyState 组件
export const EmptyState = defineComponent({
    name: 'EmptyState',
    props: { icon: { type: String, default: 'empty' }, title: String, desc: String },
    setup(props, { slots }) {
        return () => h('div', { class: 'empty-state' }, [
            h('div', { class: 'empty-icon' }, [h(Icon, { name: props.icon, size: 48 })]),
            props.title ? h('h3', props.title) : null,
            props.desc ? h('p', { class: 'text-muted' }, props.desc) : null,
            slots.default?.(),
        ]);
    },
});

// Loading 组件
export const Loading = defineComponent({
    name: 'Loading',
    props: { size: { type: String, default: 'md' } },
    setup(props) {
        return () => h('div', { class: 'loading' }, [
            h('div', { class: ['spinner', props.size === 'sm' ? 'spinner-sm' : ''] }),
        ]);
    },
});

// DataTable 组件
export const DataTable = defineComponent({
    name: 'DataTable',
    props: {
        columns: Array, // [{key, label, width}]
        rows: Array,
        loading: Boolean,
    },
    setup(props, { slots }) {
        return () => h('div', { class: 'table-container' }, [
            h('table', [
                h('thead', h('tr',
                    props.columns.map(col =>
                        h('th', { style: col.width ? { width: col.width } : {} }, col.label)
                    )
                )),
                h('tbody',
                    props.loading
                        ? [h('tr', h('td', {
                              colspan: props.columns.length,
                              style: 'text-align:center;padding:32px',
                          }, [h('div', { class: 'loading' }, h('div', { class: 'spinner' }))]))]
                        : (props.rows || []).map((row, idx) =>
                            h('tr', { key: idx },
                                props.columns.map(col =>
                                    h('td', slots[col.key]
                                        ? slots[col.key]({ row, value: row[col.key] })
                                        : String(row[col.key] ?? '')
                                    )
                                )
                            )
                        )
                ),
            ]),
        ]);
    },
});

// FormTextarea 组件
export const FormTextarea = defineComponent({
    name: 'FormTextarea',
    props: {
        modelValue: String,
        placeholder: String,
        label: String,
        hint: String,
        error: String,
        rows: { type: [Number, String], default: 4 },
        disabled: Boolean,
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        return () => h('div', { class: 'form-group' }, [
            props.label ? h('label', { class: 'form-label' }, props.label) : null,
            h('textarea', {
                class: ['form-input', props.error ? 'form-input-error' : ''],
                value: props.modelValue,
                placeholder: props.placeholder,
                rows: props.rows,
                disabled: props.disabled,
                onInput: (e) => emit('update:modelValue', e.target.value),
            }),
            props.hint ? h('div', { class: 'form-hint' }, props.hint) : null,
            props.error ? h('div', { class: 'form-error' }, props.error) : null,
        ]);
    },
});

// FormHint 组件 - 表单字段提示文本
export const FormHint = defineComponent({
    name: 'FormHint',
    props: { text: String },
    setup(props, { slots }) {
        return () => h('div', { class: 'form-hint' }, props.text || slots.default?.());
    },
});

// Pagination 组件 - 分页
export const Pagination = defineComponent({
    name: 'Pagination',
    props: {
        page: { type: Number, default: 1 },
        pageSize: { type: Number, default: 20 },
        total: { type: Number, default: 0 },
    },
    emits: ['update:page', 'change'],
    setup(props, { emit }) {
        const totalPages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize) || 1));

        function go(p) {
            const clamped = Math.min(Math.max(1, p), totalPages.value);
            if (clamped === props.page) return;
            emit('update:page', clamped);
            emit('change', { page: clamped, pageSize: props.pageSize });
        }

        // 生成页码按钮（当前页前后各 2 页）
        const pages = computed(() => {
            const tp = totalPages.value;
            const cur = props.page;
            const start = Math.max(1, cur - 2);
            const end = Math.min(tp, cur + 2);
            const arr = [];
            for (let i = start; i <= end; i++) arr.push(i);
            return arr;
        });

        return () => h('div', { class: 'pagination flex items-center gap-2' }, [
            h('span', { class: 'text-muted', style: 'font-size:12px' },
                `共 ${props.total} 条`),
            h('button', {
                class: 'btn btn-secondary btn-sm',
                disabled: props.page <= 1,
                onClick: () => go(props.page - 1),
            }, '上一页'),
            pages.value.map(p => h('button', {
                key: p,
                class: ['btn', 'btn-sm', p === props.page ? 'btn-primary' : 'btn-secondary'],
                onClick: () => go(p),
            }, String(p))),
            h('button', {
                class: 'btn btn-secondary btn-sm',
                disabled: props.page >= totalPages.value,
                onClick: () => go(props.page + 1),
            }, '下一页'),
        ]);
    },
});
