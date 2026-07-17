// components/common.js - 通用 Vue 组件库
const { defineComponent, h, ref, reactive, computed, watch, onMounted, onBeforeUnmount, onUnmounted } = window.Vue;

// ═══════════════════════════════════════════════════
// Icon 组件 - 基于 CSS mask 的图标系统
// SVG 图标文件位于 /static/icons/{name}.svg
// background-color: currentColor 由 [data-icon] CSS 规则处理
// ═══════════════════════════════════════════════════
export const Icon = defineComponent({
    name: 'Icon',
    props: {
        name: { type: String, required: true },
        size: { type: [String, Number], default: null },
    },
    setup(props) {
        return () => {
            const style = {
                '-webkit-mask-image': `url('/static/icons/${props.name}.svg')`,
                'mask-image': `url('/static/icons/${props.name}.svg')`,
            };
            if (props.size) {
                const s = typeof props.size === 'number' ? props.size + 'px' : props.size;
                style.width = s;
                style.height = s;
            }
            return h('span', {
                'data-icon': '',
                style: style,
                'aria-hidden': 'true',
            });
        };
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
            slots.footer ? h('div', { class: 'card-footer' }, slots.footer()) : null,
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
        ariaLabel: String, // 图标按钮的无障碍标签
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
            'aria-label': props.ariaLabel || undefined,
            'aria-busy': props.loading || undefined,
            onClick: (e) => emit('click', e),
        }, [
            props.loading ? h('span', { class: 'spinner spinner-sm', 'aria-hidden': 'true' }) : null,
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
        id: String,
        name: String,
        ariaLabel: String,
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        return () => h('label', { class: ['toggle', props.disabled ? 'toggle-disabled' : ''], for: props.id || undefined }, [
            h('input', {
                type: 'checkbox',
                id: props.id || undefined,
                name: props.name || undefined,
                checked: props.modelValue,
                disabled: props.disabled,
                'aria-label': props.ariaLabel || undefined,
                onChange: (e) => emit('update:modelValue', e.target.checked),
            }),
            h('span', { class: 'toggle-slider', 'aria-hidden': 'true' }),
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
        id: String,
        name: String,
        autocomplete: String,
        spellcheck: { type: Boolean, default: undefined },
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        const hintId = props.id ? `${props.id}-hint` : undefined;
        const errorId = props.id ? `${props.id}-error` : undefined;
        const describedBy = [hintId, props.error ? errorId : null].filter(Boolean).join(' ') || undefined;
        return () => h('div', { class: 'form-group' }, [
            props.label ? h('label', { class: 'form-label', for: props.id || undefined }, props.label) : null,
            h('input', {
                class: ['form-input', props.error ? 'form-input-error' : ''],
                type: props.type,
                value: props.modelValue,
                placeholder: props.placeholder,
                disabled: props.disabled,
                id: props.id || undefined,
                name: props.name || undefined,
                autocomplete: props.autocomplete || undefined,
                spellcheck: props.spellcheck,
                'aria-invalid': props.error ? 'true' : undefined,
                'aria-describedby': describedBy,
                onInput: (e) => emit('update:modelValue', e.target.value),
            }),
            props.hint ? h('div', { class: 'form-hint', id: hintId }, props.hint) : null,
            props.error ? h('div', { class: 'form-error', id: errorId, role: 'alert' }, props.error) : null,
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
        error: String,
        disabled: Boolean,
        id: String,
        name: String,
        autocomplete: String,
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        const hintId = props.id ? `${props.id}-hint` : undefined;
        const errorId = props.id ? `${props.id}-error` : undefined;
        const describedBy = [hintId, props.error ? errorId : null].filter(Boolean).join(' ') || undefined;
        const normalizedOptions = computed(() => (props.options || []).map(option =>
            typeof option === 'string' || typeof option === 'number'
                ? { value: String(option), label: String(option) }
                : option
        ));
        return () => h('div', { class: 'form-group' }, [
            props.label ? h('label', { class: 'form-label', for: props.id || undefined }, props.label) : null,
            h('select', {
                class: ['form-input', props.error ? 'form-input-error' : ''],
                value: props.modelValue,
                disabled: props.disabled,
                id: props.id || undefined,
                name: props.name || undefined,
                autocomplete: props.autocomplete || undefined,
                'aria-invalid': props.error ? 'true' : undefined,
                'aria-describedby': describedBy,
                onChange: (e) => emit('update:modelValue', e.target.value),
            }, normalizedOptions.value.map(opt =>
                h('option', { value: opt.value }, opt.label)
            )),
            props.hint ? h('div', { class: 'form-hint', id: hintId }, props.hint) : null,
            props.error ? h('div', { class: 'form-error', id: errorId, role: 'alert' }, props.error) : null,
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
        const modalRef = ref(null);
        let prevFocus = null;
        let keydownHandler = null;

        const trapFocus = (e) => {
            if (e.key === 'Escape') {
                close();
                return;
            }
            if (e.key !== 'Tab') return;
            const modal = modalRef.value;
            if (!modal) return;
            const focusables = modal.querySelectorAll(
                'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
            );
            if (focusables.length === 0) {
                e.preventDefault();
                modal.focus();
                return;
            }
            const first = focusables[0];
            const last = focusables[focusables.length - 1];
            if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
            }
        };

        onMounted(() => {
            // watch modelValue 变化
        });
        onBeforeUnmount(() => {
            if (keydownHandler) {
                document.removeEventListener('keydown', keydownHandler);
                keydownHandler = null;
            }
            if (prevFocus) {
                try { prevFocus.focus(); } catch (_) { /* ignore */ }
                prevFocus = null;
            }
        });

        // 用 watch 监听 modelValue；打开前先卸旧 handler，防止重复挂载
        watch(() => props.modelValue, (val) => {
            if (val) {
                prevFocus = document.activeElement;
                setTimeout(() => {
                    if (modalRef.value) {
                        modalRef.value.focus();
                    }
                }, 0);
                if (keydownHandler) {
                    document.removeEventListener('keydown', keydownHandler);
                }
                keydownHandler = trapFocus;
                document.addEventListener('keydown', keydownHandler);
            } else {
                if (keydownHandler) {
                    document.removeEventListener('keydown', keydownHandler);
                    keydownHandler = null;
                }
                if (prevFocus) {
                    try { prevFocus.focus(); } catch (_) { /* ignore */ }
                    prevFocus = null;
                }
            }
        });

        const titleId = `modal-title-${Math.random().toString(36).slice(2, 8)}`;

        return () => props.modelValue
            ? h('div', {
                class: 'modal-overlay',
                onClick: close,
                role: 'presentation',
            }, [
                h('div', {
                    class: 'modal',
                    ref: modalRef,
                    style: { maxWidth: props.width },
                    role: 'dialog',
                    'aria-modal': 'true',
                    'aria-labelledby': titleId,
                    tabindex: '-1',
                    onClick: (e) => e.stopPropagation(),
                }, [
                    h('div', { class: 'modal-header' }, [
                        h('h3', { id: titleId }, props.title || ''),
                        h('button', {
                            class: 'modal-close',
                            onClick: close,
                            'aria-label': '关闭',
                        }, '×'),
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
    props: { icon: { type: String, default: 'folder' }, title: String, desc: String },
    setup(props, { slots }) {
        return () => h('div', { class: 'empty-state' }, [
            h('div', { class: 'empty-icon' }, [h(Icon, { name: props.icon, size: 48 })]),
            props.title ? h('h3', props.title) : null,
            props.desc ? h('p', { class: 'text-muted' }, props.desc) : null,
            slots.default?.(),
        ]);
    },
});

// ConfirmModal 组件 - 统一确认/输入对话框（替代原生 confirm/prompt）
export const ConfirmModal = defineComponent({
    name: 'ConfirmModal',
    props: {
        modelValue: Boolean,
        title: { type: String, default: '确认操作' },
        message: { type: String, default: '' },
        confirmText: { type: String, default: '确认' },
        cancelText: { type: String, default: '取消' },
        danger: { type: Boolean, default: false },
        prompt: { type: Boolean, default: false },
        promptPlaceholder: { type: String, default: '' },
        promptValue: { type: String, default: '' },
    },
    emits: ['update:modelValue', 'confirm', 'cancel'],
    setup(props, { emit }) {
        const inputValue = ref(props.promptValue);
        watch(() => props.modelValue, (v) => {
            if (v) inputValue.value = props.promptValue;
        });
        function close() { emit('update:modelValue', false); }
        function handleConfirm() {
            emit('confirm', props.prompt ? inputValue.value : undefined);
            close();
        }
        function handleCancel() {
            emit('cancel');
            close();
        }
        return () => h(Modal, {
            modelValue: props.modelValue,
            title: props.title,
            width: '480px',
            'onUpdate:modelValue': (v) => emit('update:modelValue', v),
        }, {
            default: () => h('div', { style: 'display:grid; gap:calc(var(--spacing) * 2);' }, [
                props.message
                    ? h('p', { class: 'm-0', style: 'line-height:1.6; color:hsl(var(--foreground));' }, props.message)
                    : null,
                props.prompt
                    ? h(FormInput, {
                        modelValue: inputValue.value,
                        'onUpdate:modelValue': (v) => inputValue.value = v,
                        placeholder: props.promptPlaceholder,
                        autocomplete: 'off',
                        spellcheck: false,
                    })
                    : null,
            ]),
            footer: () => h('div', { style: 'display:flex; gap:calc(var(--spacing) * 2); justify-content:flex-end;' }, [
                h(Button, { onClick: handleCancel }, () => props.cancelText),
                h(Button, {
                    type: props.danger ? 'danger' : 'primary',
                    onClick: handleConfirm,
                }, () => props.confirmText),
            ]),
        });
    },
});

// createConfirmHelper - 创建确认对话框的响应式状态与控制函数
// 返回 { state, showConfirm, handleConfirm }，配合 ConfirmModal 组件使用
export function createConfirmHelper() {
    const state = reactive({
        visible: false,
        title: '确认操作',
        message: '',
        confirmText: '确认',
        cancelText: '取消',
        danger: false,
        prompt: false,
        promptPlaceholder: '',
        action: null,
    });
    function showConfirm(opts) {
        state.title = opts.title || '确认操作';
        state.message = opts.message || '';
        state.confirmText = opts.confirmText || '确认';
        state.cancelText = opts.cancelText || '取消';
        state.danger = !!opts.danger;
        state.prompt = !!opts.prompt;
        state.promptPlaceholder = opts.promptPlaceholder || '';
        state.action = opts.action || null;
        state.visible = true;
    }
    function handleConfirm(val) {
        const action = state.action;
        state.visible = false;
        if (action) action(val);
    }
    return { state, showConfirm, handleConfirm };
}

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

// DataTable 组件（支持虚拟滚动）
export const DataTable = defineComponent({
    name: 'DataTable',
    props: {
        columns: Array, // [{key, label, width}]
        rows: Array,
        loading: Boolean,
        virtualScroll: { type: Boolean, default: false },
        itemHeight: { type: Number, default: 48 },
        maxHeight: { type: String, default: '600px' },
    },
    setup(props, { slots }) {
        const scrollTop = ref(0);
        const containerRef = ref(null);
        const BUFFER = 5;
        const THRESHOLD = 50; // 超过此项数才启用虚拟滚动

        const shouldVirtualize = computed(() =>
            props.virtualScroll && (props.rows || []).length > THRESHOLD
        );

        const visibleRange = computed(() => {
            if (!shouldVirtualize.value) return { start: 0, end: (props.rows || []).length };
            const total = props.rows.length;
            const itemH = props.itemHeight;
            const viewH = containerRef.value?.clientHeight || 600;
            const start = Math.max(0, Math.floor(scrollTop.value / itemH) - BUFFER);
            const visibleCount = Math.ceil(viewH / itemH) + BUFFER * 2;
            const end = Math.min(total, start + visibleCount);
            return { start, end };
        });

        const onScroll = (e) => {
            scrollTop.value = e.target.scrollTop;
        };

        return () => {
            const rows = props.rows || [];
            const { start, end } = visibleRange.value;
            const visibleRows = shouldVirtualize.value ? rows.slice(start, end) : rows;

            const tbodyChildren = props.loading
                ? [h('tr', h('td', {
                      colspan: props.columns.length,
                      style: 'text-align:center;padding:32px',
                  }, [h('div', { class: 'loading' }, h('div', { class: 'spinner' }))]))]
                : visibleRows.map((row, i) =>
                    h('tr', { key: shouldVirtualize.value ? start + i : i },
                        props.columns.map(col =>
                            h('td', slots[col.key]
                                ? slots[col.key]({ row, value: row[col.key] })
                                : String(row[col.key] ?? '')
                            )
                        )
                    )
                );

            // 虚拟滚动时，在可见行前后加占位 spacer
            const spacerBefore = shouldVirtualize.value && start > 0
                ? h('tr', { style: { height: `${start * props.itemHeight}px` }, 'aria-hidden': 'true' })
                : null;
            const spacerAfter = shouldVirtualize.value && end < rows.length
                ? h('tr', { style: { height: `${(rows.length - end) * props.itemHeight}px` }, 'aria-hidden': 'true' })
                : null;

            return h('div', {
                class: 'table-container',
                style: shouldVirtualize.value ? { maxHeight: props.maxHeight, overflowY: 'auto' } : {},
                ref: containerRef,
                onScroll: shouldVirtualize.value ? onScroll : undefined,
            }, [
                h('table', [
                    h('thead', h('tr',
                        props.columns.map(col =>
                            h('th', { style: col.width ? { width: col.width } : {} }, col.label)
                        )
                    )),
                    h('tbody', [spacerBefore, ...tbodyChildren, spacerAfter].filter(Boolean)),
                ]),
            ]);
        };
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
        id: String,
        name: String,
        spellcheck: { type: Boolean, default: undefined },
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        const hintId = props.id ? `${props.id}-hint` : undefined;
        const errorId = props.id ? `${props.id}-error` : undefined;
        const describedBy = [hintId, props.error ? errorId : null].filter(Boolean).join(' ') || undefined;
        return () => h('div', { class: 'form-group' }, [
            props.label ? h('label', { class: 'form-label', for: props.id || undefined }, props.label) : null,
            h('textarea', {
                class: ['form-input', props.error ? 'form-input-error' : ''],
                value: props.modelValue,
                placeholder: props.placeholder,
                rows: props.rows,
                disabled: props.disabled,
                id: props.id || undefined,
                name: props.name || undefined,
                spellcheck: props.spellcheck,
                'aria-invalid': props.error ? 'true' : undefined,
                'aria-describedby': describedBy,
                onInput: (e) => emit('update:modelValue', e.target.value),
            }),
            props.hint ? h('div', { class: 'form-hint', id: hintId }, props.hint) : null,
            props.error ? h('div', { class: 'form-error', id: errorId, role: 'alert' }, props.error) : null,
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

        return () => h('nav', { class: 'pagination flex items-center gap-2', 'aria-label': '分页导航' }, [
            h('span', { class: 'text-muted', style: 'font-size:12px' },
                `共 ${props.total} 条`),
            h('button', {
                class: 'btn btn-secondary btn-sm',
                disabled: props.page <= 1,
                'aria-label': '上一页',
                onClick: () => go(props.page - 1),
            }, '上一页'),
            pages.value.map(p => h('button', {
                key: p,
                class: ['btn', 'btn-sm', p === props.page ? 'btn-primary' : 'btn-secondary'],
                'aria-label': `第 ${p} 页${p === props.page ? '（当前页）' : ''}`,
                'aria-current': p === props.page ? 'page' : undefined,
                onClick: () => go(p),
            }, String(p))),
            h('button', {
                class: 'btn btn-secondary btn-sm',
                disabled: props.page >= totalPages.value,
                'aria-label': '下一页',
                onClick: () => go(props.page + 1),
            }, '下一页'),
        ]);
    },
});

// KpiCard 组件 - KPI 指标卡片
export const KpiCard = defineComponent({
    name: 'KpiCard',
    props: {
        eyebrow: { type: String, default: '指标' },
        iconName: { type: String, default: '' },
        value: { type: [String, Number], required: true },
        trend: { type: String, default: '' },
        trendDirection: { type: String, default: 'up' },
        label: { type: String, default: '' },
        valueLabel: { type: String, default: '' },
    },
    setup(props) {
        return () => h('article', {
            class: 'card',
            style: { display: 'grid', gap: 'calc(var(--spacing) * 3)', alignContent: 'start', minHeight: '10rem' },
        }, [
            h('div', {
                class: 'flex items-center justify-between gap-2',
            }, [
                h('span', { class: 'eyebrow' }, props.eyebrow),
                props.iconName ? h(Icon, { name: props.iconName, size: '1.15rem' }) : null,
            ]),
            h('div', { class: 'grid gap-1' }, [
                props.valueLabel ? h('span', { class: 'kpi-label' }, props.valueLabel) : null,
                h('div', { class: 'flex items-baseline gap-2 flex-wrap' }, [
                    h('span', { class: 'kpi-value' }, String(props.value)),
                    props.trend ? h('span', { class: ['kpi-trend', props.trendDirection === 'down' ? 'down' : 'up'] }, [
                        h(Icon, { name: props.trendDirection === 'down' ? 'arrow-down' : 'arrow-up', size: '0.8rem' }),
                        props.trend,
                    ]) : null,
                ]),
            ]),
            props.label ? h('p', { class: 'muted m-0' }, props.label) : null,
        ]);
    },
});

// HeroPanel 组件 - 页面顶部大号摘要面板
export const HeroPanel = defineComponent({
    name: 'HeroPanel',
    props: {
        eyebrow: { type: String, default: '' },
        title: { type: String, default: '' },
        badge: { type: String, default: '' },
        badgeType: { type: String, default: 'success' },
        trend: { type: String, default: '' },
        ctaText: { type: String, default: '' },
        ctaIcon: { type: String, default: '' },
        onCta: { type: Function, default: null },
    },
    emits: ['cta'],
    setup(props, { emit, slots }) {
        return () => h('div', { class: 'hero-panel' }, [
            (props.eyebrow || props.badge) ? h('div', {
                class: 'flex items-start justify-between gap-2 flex-wrap',
            }, [
                props.eyebrow ? h('span', { class: 'eyebrow' }, props.eyebrow) : null,
                props.badge ? h('span', { class: ['badge', `badge-${props.badgeType}`] }, props.badge) : null,
            ]) : null,
            props.title ? h('h2', {
                style: 'margin:0; font-size:1.65rem; line-height:1.1; text-wrap:balance; word-break:keep-all;',
            }, props.title) : null,
            slots.default ? slots.default() : null,
            props.trend ? h('p', { class: 'muted m-0' }, props.trend) : null,
            props.ctaText ? h('button', {
                class: 'btn primary',
                onClick: (e) => { emit('cta', e); if (props.onCta) props.onCta(e); },
            }, [
                props.ctaIcon ? h(Icon, { name: props.ctaIcon, size: '1.05rem' }) : null,
                h('span', props.ctaText),
            ]) : null,
        ]);
    },
});

// ActionList 组件 - 带 chevron-right 的可点击列表项
export const ActionList = defineComponent({
    name: 'ActionList',
    props: {
        items: {
            type: Array,
            default: () => [],
        },
    },
    setup(props) {
        return () => h('div', { class: 'action-list' },
            props.items.map((item, i) => {
                const content = [
                    h('span', { class: 'action-list-item-copy' }, [
                        item.iconName ? h('span', { class: 'action-list-item-icon' }, [
                            h(Icon, { name: item.iconName, size: '1rem' }),
                        ]) : null,
                        h('span', { class: 'truncate', style: 'font-size:0.97rem;' }, item.label),
                    ]),
                    h(Icon, { name: 'chevron-right', size: '1rem' }),
                ];
                const cls = 'action-list-item';
                if (item.href) {
                    return h('a', {
                        key: i,
                        class: cls,
                        href: item.href,
                    }, content);
                }
                return h('button', {
                    key: i,
                    class: cls,
                    onClick: (e) => item.onClick && item.onClick(e),
                }, content);
            }),
        );
    },
});

// ProgressBar 组件 - 进度条
export const ProgressBar = defineComponent({
    name: 'ProgressBar',
    props: {
        value: { type: Number, required: true },
        max: { type: Number, default: 100 },
        colorToken: { type: String, default: '' },
        label: { type: String, default: '' },
        showValue: { type: Boolean, default: false },
    },
    setup(props) {
        const percent = computed(() => {
            const p = props.max > 0 ? Math.min(100, Math.max(0, (props.value / props.max) * 100)) : 0;
            return Math.round(p);
        });
        return () => h('div', { class: 'grid gap-1' }, [
            (props.label || props.showValue) ? h('div', {
                class: 'flex items-center justify-between gap-2',
            }, [
                props.label ? h('span', { class: 'muted', style: 'font-size:0.88rem;' }, props.label) : null,
                props.showValue ? h('span', {
                    style: 'font-size:0.82rem; color:hsl(var(--muted-foreground)); font-variant-numeric:tabular-nums;',
                }, `${props.value} / ${props.max}`) : null,
            ]) : null,
            h('div', { class: 'progress-bar' }, [
                h('div', {
                    class: ['progress-bar-fill', props.colorToken ? props.colorToken : ''].filter(Boolean).join(' '),
                    style: { width: `${percent.value}%` },
                    role: 'progressbar',
                    'aria-valuenow': props.value,
                    'aria-valuemin': 0,
                    'aria-valuemax': props.max,
                }),
            ]),
        ]);
    },
});

// StatusDot 组件 - 状态指示点
export const StatusDot = defineComponent({
    name: 'StatusDot',
    props: {
        status: { type: String, default: 'offline' },
        label: { type: String, default: '' },
    },
    setup(props) {
        return () => h('span', {
            class: ['status-dot', props.status],
        }, props.label);
    },
});
