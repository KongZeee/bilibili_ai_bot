// bilibot/web/static/js/components/SchemaForm.js
const { h, ref, reactive, computed, watch } = window.Vue;
import { FormInput, FormSelect, FormTextarea, Toggle, FormHint } from './common.js';

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
            const fieldId = field.id || `sf-${field.key.replace(/\./g, '-')}`;
            const autocomplete = field.sensitive ? 'off' : (field.autocomplete || undefined);
            const spellcheck = field.sensitive ? false : (field.spellcheck);

            switch (field.type) {
                case 'boolean':
                    return h('div', { class: 'flex items-center gap-2' }, [
                        h(Toggle, {
                            modelValue: !!value,
                            'onUpdate:modelValue': onInput,
                            id: fieldId,
                            name: fieldId,
                            ariaLabel: field.label,
                        }),
                        h('span', { class: 'form-hint' }, value ? '已启用' : '已禁用'),
                    ]);
                case 'select':
                    return h(FormSelect, {
                        modelValue: value,
                        'onUpdate:modelValue': onInput,
                        id: fieldId,
                        name: fieldId,
                        autocomplete,
                        options: field.options || [],
                    });
                case 'textarea':
                    return h(FormTextarea, {
                        modelValue: value ?? '',
                        'onUpdate:modelValue': onInput,
                        id: fieldId,
                        name: fieldId,
                        spellcheck,
                        rows: field.rows || 4,
                        placeholder: field.placeholder || '',
                    });
                case 'password':
                    return h(FormInput, {
                        modelValue: value ?? '',
                        'onUpdate:modelValue': onInput,
                        id: fieldId,
                        name: fieldId,
                        type: 'password',
                        autocomplete: 'off',
                        spellcheck: false,
                        placeholder: field.sensitive ? '编辑时留空表示不修改' : '',
                    });
                case 'number':
                    return h(FormInput, {
                        modelValue: value != null ? String(value) : '',
                        'onUpdate:modelValue': (v) => onInput(field.type === 'integer' ? parseInt(v) || 0 : parseFloat(v) || 0),
                        id: fieldId,
                        name: fieldId,
                        type: 'number',
                        min: field.min,
                        max: field.max,
                        autocomplete,
                        spellcheck,
                    });
                case 'string':
                default:
                    return h(FormInput, {
                        modelValue: value ?? '',
                        'onUpdate:modelValue': onInput,
                        id: fieldId,
                        name: fieldId,
                        autocomplete,
                        spellcheck,
                        placeholder: field.placeholder || '',
                    });
            }
        }

        return () => h('div', { class: 'schema-form' },
            groupedFields.value.map(group => h('div', {
                class: 'card',
                style: { marginBottom: 'calc(var(--spacing) * 4)' },
            }, [
                h('div', { class: 'card-header' }, [
                    h('div', { class: 'grid gap-1' }, [
                        h('span', { class: 'eyebrow' }, '配置分组'),
                        h('h3', {
                            style: 'margin:0; font-size:1.35rem; line-height:1.1; font-weight:500; text-wrap:balance; word-break:keep-all;',
                        }, group.name),
                    ]),
                ]),
                h('div', { class: 'card-body' }, [
                    h('div', { class: 'form-grid-2col' },
                        group.fields.map(field => {
                            const fieldId = field.id || `sf-${field.key.replace(/\./g, '-')}`;
                            return h('div', {
                                class: ['form-group', field.span === 2 ? 'span-2' : ''].filter(Boolean).join(' '),
                                style: field.span === 2 ? { gridColumn: '1 / -1' } : {},
                            }, [
                                h('label', { class: 'form-label', for: fieldId }, [
                                    field.label,
                                    field.immediate && h('span', { class: 'badge badge-info badge-sm', style: 'margin-left:calc(var(--spacing) * 1);' }, '即时'),
                                    field.sensitive && h('span', { class: 'badge badge-warning badge-sm', style: 'margin-left:calc(var(--spacing) * 1);' }, '敏感'),
                                ]),
                                renderField(field),
                                field.hint && h(FormHint, field.hint),
                            ]);
                        }),
                    ),
                ]),
            ])),
        );
    },
};
