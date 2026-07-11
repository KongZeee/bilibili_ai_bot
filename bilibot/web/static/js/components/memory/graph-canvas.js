// components/memory/graph-canvas.js - 记忆图谱 Canvas 渲染
// 从 livingmemory/graph-2d.js 迁移，适配 Vue 响应式

export class MemoryGraphRenderer {
    constructor(canvas, options = {}) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.nodes = [];
        this.edges = [];
        this.memories = [];
        this.nodePositions = new Map();
        this.camera = { x: 0, y: 0, zoom: 1 };
        this.dragging = null;
        this.hoveredNode = null;
        this.options = {
            nodeRadius: 20,
            Colors: {
                summary: '#6750A4',
                person: '#2E7D32',
                topic: '#0288D1',
                memory: '#ED6C02',
            },
            ...options,
        };
        this.onNodeClick = options.onNodeClick || (() => {});
        this.onNodeHover = options.onNodeHover || (() => {});
        this.setupEvents();
    }

    setData(data) {
        this.nodes = data.nodes || [];
        this.edges = data.edges || [];
        this.memories = data.memories || [];
        this.layout();
        this.render();
    }

    layout() {
        // 简单力导向布局
        const w = this.canvas.width;
        const h = this.canvas.height;
        const cx = w / 2, cy = h / 2;
        const n = this.nodes.length;
        if (n === 0) return;

        // 初始圆形布局
        this.nodes.forEach((node, i) => {
            const angle = (i / n) * Math.PI * 2;
            const r = Math.min(w, h) * 0.3;
            this.nodePositions.set(node.id, {
                x: cx + Math.cos(angle) * r,
                y: cy + Math.sin(angle) * r,
                vx: 0, vy: 0,
            });
        });

        // 迭代力导向
        for (let iter = 0; iter < 100; iter++) {
            // 斥力
            for (let i = 0; i < this.nodes.length; i++) {
                for (let j = i + 1; j < this.nodes.length; j++) {
                    const a = this.nodePositions.get(this.nodes[i].id);
                    const b = this.nodePositions.get(this.nodes[j].id);
                    const dx = b.x - a.x, dy = b.y - a.y;
                    const dist = Math.sqrt(dx * dx + dy * dy) || 1;
                    const force = 2000 / (dist * dist);
                    a.vx -= (dx / dist) * force;
                    a.vy -= (dy / dist) * force;
                    b.vx += (dx / dist) * force;
                    b.vy += (dy / dist) * force;
                }
            }
            // 引力（边）
            for (const edge of this.edges) {
                const a = this.nodePositions.get(edge.source);
                const b = this.nodePositions.get(edge.target);
                if (!a || !b) continue;
                const dx = b.x - a.x, dy = b.y - a.y;
                const dist = Math.sqrt(dx * dx + dy * dy) || 1;
                const force = (dist - 100) * 0.01;
                a.vx += (dx / dist) * force;
                a.vy += (dy / dist) * force;
                b.vx -= (dx / dist) * force;
                b.vy -= (dy / dist) * force;
            }
            // 更新位置
            for (const node of this.nodes) {
                const p = this.nodePositions.get(node.id);
                p.x += p.vx * 0.1;
                p.y += p.vy * 0.1;
                p.vx *= 0.9;
                p.vy *= 0.9;
            }
        }
    }

    render() {
        const ctx = this.ctx;
        const w = this.canvas.width;
        const h = this.canvas.height;
        ctx.clearRect(0, 0, w, h);
        ctx.save();
        ctx.translate(this.camera.x, this.camera.y);
        ctx.scale(this.camera.zoom, this.camera.zoom);

        // 绘制边
        ctx.strokeStyle = '#CAC4D0';
        ctx.lineWidth = 1;
        for (const edge of this.edges) {
            const a = this.nodePositions.get(edge.source);
            const b = this.nodePositions.get(edge.target);
            if (!a || !b) continue;
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(b.x, b.y);
            ctx.stroke();
        }

        // 绘制节点
        for (const node of this.nodes) {
            const p = this.nodePositions.get(node.id);
            if (!p) continue;
            const color = this.options.colors[node.type] || this.options.colors.memory;
            const isHovered = this.hoveredNode === node.id;

            ctx.beginPath();
            ctx.arc(p.x, p.y, isHovered ? 24 : 20, 0, Math.PI * 2);
            ctx.fillStyle = color;
            ctx.fill();
            ctx.strokeStyle = '#fff';
            ctx.lineWidth = 2;
            ctx.stroke();

            // 标签
            ctx.fillStyle = '#1D1B20';
            ctx.font = '12px Roboto, sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText(node.label || node.id, p.x, p.y + 35);
        }

        ctx.restore();
    }

    setupEvents() {
        let isDragging = false;
        let lastX = 0, lastY = 0;

        this.canvas.addEventListener('mousedown', (e) => {
            const rect = this.canvas.getBoundingClientRect();
            const x = (e.clientX - rect.left - this.camera.x) / this.camera.zoom;
            const y = (e.clientY - rect.top - this.camera.y) / this.camera.zoom;

            // 检查是否点击了节点
            for (const node of this.nodes) {
                const p = this.nodePositions.get(node.id);
                if (!p) continue;
                const dx = x - p.x, dy = y - p.y;
                if (Math.sqrt(dx * dx + dy * dy) < 20) {
                    this.dragging = node.id;
                    return;
                }
            }

            isDragging = true;
            lastX = e.clientX;
            lastY = e.clientY;
        });

        this.canvas.addEventListener('mousemove', (e) => {
            const rect = this.canvas.getBoundingClientRect();
            const x = (e.clientX - rect.left - this.camera.x) / this.camera.zoom;
            const y = (e.clientY - rect.top - this.camera.y) / this.camera.zoom;

            if (this.dragging) {
                const p = this.nodePositions.get(this.dragging);
                if (p) { p.x = x; p.y = y; this.render(); }
                return;
            }

            if (isDragging) {
                this.camera.x += e.clientX - lastX;
                this.camera.y += e.clientY - lastY;
                lastX = e.clientX;
                lastY = e.clientY;
                this.render();
                return;
            }

            // hover 检测
            let hovered = null;
            for (const node of this.nodes) {
                const p = this.nodePositions.get(node.id);
                if (!p) continue;
                const dx = x - p.x, dy = y - p.y;
                if (Math.sqrt(dx * dx + dy * dy) < 20) {
                    hovered = node.id;
                    break;
                }
            }
            if (hovered !== this.hoveredNode) {
                this.hoveredNode = hovered;
                this.onNodeHover(hovered ? this.nodes.find(n => n.id === hovered) : null);
                this.render();
            }
        });

        this.canvas.addEventListener('mouseup', () => {
            if (this.dragging) {
                this.dragging = null;
            }
            isDragging = false;
        });

        this.canvas.addEventListener('click', (e) => {
            if (this.hoveredNode) {
                const node = this.nodes.find(n => n.id === this.hoveredNode);
                if (node) this.onNodeClick(node);
            }
        });

        this.canvas.addEventListener('wheel', (e) => {
            e.preventDefault();
            const delta = e.deltaY > 0 ? 0.9 : 1.1;
            this.camera.zoom *= delta;
            this.camera.zoom = Math.max(0.3, Math.min(3, this.camera.zoom));
            this.render();
        });
    }

    resize() {
        const rect = this.canvas.getBoundingClientRect();
        this.canvas.width = rect.width;
        this.canvas.height = rect.height;
        this.render();
    }

    destroy() {
        // 移除事件监听（canvas 销毁时自动清理）
    }
}
