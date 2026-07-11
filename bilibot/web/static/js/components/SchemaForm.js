// bilibot/web/static/js/components/SchemaForm.js
import { h, ref, reactive, computed, watch } from '../vendor/vue.esm-browser.prod.js';
import { FormInput, FormSelect, FormTextarea, Toggle, FormHint } from '../components.js';

/**
 * Schema 驱动的表单组件
 * @param {Array} schema - 字段定义数组
 *   { key, label, type: 'string'|'number'|'boolean'|'select'|'textarea'|'password', category, default, options, sensitive, immediate, min, max, hint }
 * @param {Object} model - 表单数据
 */
export const SchemaForm = {
    props: {
        schema: { type: Array, default: () => [] },
        modelValue: { type: Object, default: () => ({}) },
    },
    emits: ['update:modelValue'],
    setup(props, { emit }) {
        const localModel = reactive({ ...props.modelValue });

        watch(() => props.modelValue, (v) => {
            Object.assign(localModel, v);
        }, { deep: true });

        function update(key, value) {
            localModel[key] = value;
            emit('update:modelValue', { ...localModel });
        }

        // 按 category 分组
        const groupedFields = computed(() => {
            const groups = {};
            for (const field of props.schema) {
                const cat = field.category || '通用';
                if (!groups[cat]) groups[cat] = [];
                groups[cat].push(field);
            }
            return Object.entries(groups).map(([name, fields]) => ({ name, fields }));
        });

        function renderField(field) {
            const value = localModel[field.key];
            const onInput = (v) => update(field.key, v);

            switch (field.type) {
                case 'boolean':
                    return h('div', { class: 'flex items-center gap-2' }, [
                        h(Toggle, { modelValue: !!value, 'onUpdate:modelValue': onInput }),
                        h('span', { class: 'form-hint' }, value ? '已启用' : '已禁用'),
                    ]);
                case 'select':
                    return h(FormSelect, {
                        modelValue: value,
                        'onUpdate:modelValue': onInput,
                        options: field.options || [],
                    });
                case 'textarea':
                    return h(FormTextarea, {
                        modelValue: value ?? '',
                        'onUpdate:modelValue': onInput,
                        rows: field.rows || 4,
                        placeholder: field.placeholder || '',
                    });
                case 'password':
                    return h(FormInput, {
                        modelValue: value ?? '',
                        'onUpdate:modelValue': onInput,
                        type: 'password',
                        placeholder: field.sensitive ? '编辑时留空表示不修改' : '',
                    });
                case 'number':
                    return h(FormInput, {
                        modelValue: value != null ? String(value) : '',
                        'onUpdate:modelValue': (v) => onInput(field.type === 'integer' ? parseInt(v) || 0 : parseFloat(v) || 0),
                        type: 'number',
                        min: field.min,
                        max: field.max,
                    });
                case 'string':
                default:
                    return h(FormInput, {
                        modelValue: value ?? '',
                        'onUpdate:modelValue': onInput,
                        placeholder: field.placeholder || '',
                    });
            }
        }

        return () => h('div', { class: 'schema-form' },
            groupedFields.value.map(group => h('div', { class: 'schema-group mb-4' }, [
                h('h4', { class: 'schema-group-title mb-3', style: 'font-size:14px; font-weight:600; color:var(--on-surface-variant); padding-bottom:8px; border-bottom:1px solid var(--outline-variant)' },
                    group.name),
                h('div', { class: 'form-grid-2col' },
                    group.fields.map(field => h('div', {
                        class: ['form-group', field.span === 2 && 'span-2'].filter(Boolean).join(' '),
                    }, [
                        h('label', { class: 'form-label' }, [
                            field.label,
                            field.immediate && h('span', { class: 'badge badge-info ml-2', style: 'font-size:10px; padding:2px 6px; background:var(--info-bg); color:var(--info); border-radius:4px' }, '即时'),
                            field.sensitive && h('span', { class: 'badge badge-warning ml-1', style: 'font-size:10px; padding:2px 6px; background:var(--warning-bg); color:var(--warning); border-radius:4px' }, '敏感'),
                        ]),
                        renderField(field),
                        field.hint && h(FormHint, field.hint),
                    ])),
                ),
            ])),
        );
    },
};
