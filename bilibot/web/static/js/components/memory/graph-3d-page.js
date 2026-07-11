// components/memory/graph-3d-page.js - 3D 记忆图谱页（Golden Time 设计稿）
const { defineComponent, h, ref, computed, onMounted, onUnmounted, watch, nextTick } = window.Vue;
import { api } from '../../api.js';
import { Card, Button, Badge, Loading, EmptyState, Icon, HeroPanel } from '../common.js';
import { appState, showToast } from '../../state.js';

// 节点类型 → chart 色号映射（与 2D 版一致）
const TYPE_COLOR_INDEX = { summary: 1, person: 5, topic: 3 };
const TYPE_LABELS = { summary: '记忆节点', person: '用户节点', topic: '分类节点' };

// CDN 地址
const THREE_URL = 'https://cdn.jsdelivr.net/npm/three@0.169.0/build/three.module.js';
const OC_URL = 'https://cdn.jsdelivr.net/npm/three@0.169.0/examples/jsm/controls/OrbitControls.js';

// 读取 CSS 变量 --chart-N 的 HSL 字符串
function getChartHsl(index) {
    const style = getComputedStyle(document.documentElement);
    return style.getPropertyValue(`--chart-${index}`).trim();
}

// 读取 --foreground 用于 fog
function getForegroundHsl() {
    const style = getComputedStyle(document.documentElement);
    return style.getPropertyValue('--foreground').trim();
}

// 节点 3D 位置计算
function compute3DPositions(nodes, layoutType) {
    const n = nodes.length;
    if (n === 0) return;

    if (layoutType === 'helix') {
        // 螺旋布局：圆柱面螺旋
        nodes.forEach((node, i) => {
            const t = i / Math.max(1, n - 1);
            const angle = t * Math.PI * 8;
            const r = 220 + (node.degree || 1) * 4;
            const y = (t - 0.5) * 460;
            node.x = r * Math.cos(angle);
            node.y = y;
            node.z = r * Math.sin(angle);
        });
    } else {
        // sphere: fibonacci 球面分布
        const sorted = [...nodes].sort((a, b) => (b.degree || 0) - (a.degree || 0));
        const hubCount = Math.min(Math.ceil(n / 6), 4);
        sorted.forEach((node, i) => {
            const isHub = i < hubCount;
            const phi = Math.acos(1 - 2 * (i + 0.5) / n);
            const theta = Math.PI * (1 + Math.sqrt(5)) * (i + 0.5);
            const baseR = isHub ? 90 : 220;
            const r = baseR + (node.degree || 1) * 8;
            node.x = r * Math.sin(phi) * Math.cos(theta);
            node.y = r * Math.sin(phi) * Math.sin(theta);
            node.z = r * Math.cos(phi);
        });
    }
}

function nodeRadius3D(node) {
    const deg = node.degree || 1;
    return Math.max(8, Math.min(24, 8 + deg * 1.4));
}

// 动态加载 Three.js + OrbitControls（不依赖 importmap）
// OrbitControls.js 内部 `import * as THREE from 'three'` 为 bare specifier，
// 通过 fetch + 字符串替换 + Blob URL 加载，避免修改 HTML shell 加 importmap。
async function loadThree() {
    const THREE = await import(/* @vite-ignore */ THREE_URL);

    let OrbitControls;
    try {
        const resp = await fetch(OC_URL);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        let src = await resp.text();
        // 将 bare specifier 'three' 替换为完整 URL
        src = src.replace(/from\s*['"]three['"]/g, `from '${THREE_URL}'`);
        const blob = new Blob([src], { type: 'application/javascript' });
        const blobUrl = URL.createObjectURL(blob);
        try {
            const mod = await import(/* @vite-ignore */ blobUrl);
            OrbitControls = mod.OrbitControls;
        } finally {
            // 模块已加载并缓存，可安全 revoke
            URL.revokeObjectURL(blobUrl);
        }
    } catch (e) {
        throw new Error('加载 OrbitControls 失败: ' + e.message);
    }

    return { THREE, OrbitControls };
}

export const MemoryGraph3DPage = defineComponent({
    name: 'MemoryGraph3DPage',
    setup() {
        const accountId = ref(null);
        const graphData = ref({ nodes: [], edges: [], memories: [], summary: {} });
        const totalCounts = ref({ nodes: 0, edges: 0, memories: 0 });
        const loading = ref(true);
        const selectedNode = ref(null);
        const hoveredNode = ref(null);
        const filterCategory = ref('');
        const searchQuery = ref('');
        const layout = ref('sphere'); // sphere / helix
        const canvasRef = ref(null);
        const threeReady = ref(false);
        let threeCleanup = null;
        let threeCtx = null; // { scene, camera, renderer, controls, nodeMeshes, nodeGroup, edgeGroup, disposables, updateScene }
        let initializing = false;

        // ── 过滤后的节点/边 ──
        const filteredNodes = computed(() => {
            let nodes = graphData.value.nodes || [];
            if (filterCategory.value) {
                nodes = nodes.filter(n => n.type === filterCategory.value);
            }
            if (searchQuery.value.trim()) {
                const q = searchQuery.value.trim().toLowerCase();
                nodes = nodes.filter(n => (n.label || '').toLowerCase().includes(q));
            }
            // 节点过多时按度数截断，保持 3D 场景可读
            if (nodes.length > 80) {
                nodes = [...nodes].sort((a, b) => (b.degree || 0) - (a.degree || 0)).slice(0, 80);
            }
            return nodes;
        });

        const filteredNodeIds = computed(() => new Set(filteredNodes.value.map(n => n.id)));

        const filteredEdges = computed(() => {
            const ids = filteredNodeIds.value;
            return (graphData.value.edges || []).filter(e =>
                ids.has(e.source) && ids.has(e.target)
            );
        });

        // ── 统计 ──
        const nodeCount = computed(() => totalCounts.value.nodes || (graphData.value.nodes || []).length);
        const edgeCount = computed(() => totalCounts.value.edges || (graphData.value.edges || []).length);

        const categoryStats = computed(() => {
            const breakdown = graphData.value.summary?.node_type_breakdown || {};
            const total = Object.values(breakdown).reduce((a, b) => a + (b || 0), 0) || nodeCount.value || 1;
            return Object.entries(breakdown)
                .map(([key, count]) => ({
                    key,
                    label: TYPE_LABELS[key] || key,
                    count: count || 0,
                    percent: Math.round(((count || 0) / total) * 100),
                    colorIndex: TYPE_COLOR_INDEX[key] || 2,
                }))
                .sort((a, b) => b.count - a.count);
        });

        const avgDegree = computed(() => {
            const n = nodeCount.value;
            return n > 0 ? ((edgeCount.value * 2) / n).toFixed(1) : '0';
        });

        const maxDegree = computed(() => {
            const nodes = graphData.value.nodes || [];
            if (nodes.length === 0) return 0;
            return Math.max(...nodes.map(n => n.degree || 0));
        });

        // ── 选中节点的关联节点 ──
        const connectedNodes = computed(() => {
            if (!selectedNode.value) return [];
            const sid = selectedNode.value.id;
            const allNodes = graphData.value.nodes || [];
            const result = [];
            const seen = new Set();
            for (const e of (graphData.value.edges || [])) {
                if (e.source === sid || e.target === sid) {
                    const otherId = e.source === sid ? e.target : e.source;
                    if (!seen.has(otherId)) {
                        seen.add(otherId);
                        const node = allNodes.find(n => n.id === otherId);
                        if (node) result.push(node);
                    }
                }
            }
            return result;
        });

        const selectedMemory = computed(() => {
            if (!selectedNode.value) return null;
            if (selectedNode.value.type !== 'summary') return null;
            const mid = selectedNode.value.id;
            return (graphData.value.memories || []).find(m => m.memory_id === mid) || null;
        });

        // ── 数据加载 ──
        async function loadData() {
            accountId.value = appState.currentAccountId || appState.accounts[0]?.id;
            if (!accountId.value) {
                loading.value = false;
                return;
            }
            loading.value = true;
            selectedNode.value = null;
            hoveredNode.value = null;
            try {
                const data = await api.memory.graph(accountId.value);
                const snap = data.snapshot || { nodes: [], edges: [], memories: [] };
                graphData.value = {
                    nodes: snap.nodes || [],
                    edges: snap.edges || [],
                    memories: snap.memories || [],
                    summary: data.summary || {},
                };
                totalCounts.value = {
                    nodes: data.graph_nodes || (snap.nodes || []).length,
                    edges: data.graph_edges || (snap.edges || []).length,
                    memories: data.total_memories || (snap.memories || []).length,
                };
            } catch (e) {
                showToast('加载图谱失败: ' + e.message, 'error');
                graphData.value = { nodes: [], edges: [], memories: [], summary: {} };
                totalCounts.value = { nodes: 0, edges: 0, memories: 0 };
            } finally {
                loading.value = false;
            }
        }

        async function searchGraph() {
            if (!accountId.value) return;
            if (!searchQuery.value.trim()) {
                await loadData();
                return;
            }
            loading.value = true;
            selectedNode.value = null;
            try {
                const data = await api.memory.graphQuery(accountId.value, { keyword: searchQuery.value.trim() });
                const snap = data.snapshot || { nodes: [], edges: [], memories: [] };
                graphData.value = {
                    nodes: snap.nodes || [],
                    edges: snap.edges || [],
                    memories: snap.memories || [],
                    summary: data.summary || {},
                };
                totalCounts.value = {
                    nodes: data.graph_nodes || (snap.nodes || []).length,
                    edges: data.graph_edges || (snap.edges || []).length,
                    memories: data.total_memories || (snap.memories || []).length,
                };
            } catch (e) {
                showToast('搜索失败: ' + e.message, 'error');
            } finally {
                loading.value = false;
            }
        }

        function filterByCategory(cat) {
            filterCategory.value = filterCategory.value === cat ? '' : cat;
        }

        function clearFilters() {
            filterCategory.value = '';
            searchQuery.value = '';
            loadData();
        }

        function selectNode(node) {
            selectedNode.value = node;
        }

        const layoutOptions = [
            { key: 'sphere', label: '球形' },
            { key: 'helix', label: '螺旋' },
        ];

        // ── Three.js 初始化 ──
        async function ensureThree() {
            if (initializing || threeCtx) return;
            if (!canvasRef.value) return;
            if ((graphData.value.nodes || []).length === 0) return;
            initializing = true;
            try {
                await nextTick();
                await initThree();
            } catch (e) {
                showToast('初始化 3D 场景失败: ' + (e.message || e), 'error');
            } finally {
                initializing = false;
            }
        }

        async function initThree() {
            if (!canvasRef.value) return;
            const container = canvasRef.value;

            let THREE, OrbitControls;
            try {
                ({ THREE, OrbitControls } = await loadThree());
            } catch (e) {
                showToast('加载 Three.js 失败: ' + e.message, 'error');
                return;
            }

            // ── 场景 ──
            const scene = new THREE.Scene();
            const fogColorHsl = getForegroundHsl();
            let fogColor = 0x3b352b;
            try {
                fogColor = new THREE.Color(`hsl(${fogColorHsl})`);
            } catch (e) { /* fallback */ }
            scene.fog = new THREE.Fog(fogColor, 600, 1600);

            // ── 相机 ──
            const cw = Math.max(container.clientWidth, 1);
            const ch = Math.max(container.clientHeight, 1);
            const camera = new THREE.PerspectiveCamera(60, cw / ch, 0.1, 3000);
            camera.position.set(0, 0, 620);

            // ── 渲染器 ──
            const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
            renderer.setClearColor(0x000000, 0);
            renderer.setSize(cw, ch);
            renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
            container.appendChild(renderer.domElement);
            renderer.domElement.style.display = 'block';
            renderer.domElement.style.cursor = 'grab';

            // ── OrbitControls ──
            const controls = new OrbitControls(camera, renderer.domElement);
            controls.enableDamping = true;
            controls.dampingFactor = 0.05;
            const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
            controls.autoRotate = !prefersReduced;
            controls.autoRotateSpeed = 0.5;
            controls.minDistance = 150;
            controls.maxDistance = 1500;

            // 3 秒后停止自动旋转
            const autoRotateTimer = setTimeout(() => { controls.autoRotate = false; }, 3000);

            // 用户交互时暂停自动旋转
            function onControlsStart() { controls.autoRotate = false; }
            controls.addEventListener('start', onControlsStart);

            // ── 灯光 ──
            const ambientLight = new THREE.AmbientLight(0xffffff, 0.55);
            scene.add(ambientLight);
            const pointLight = new THREE.PointLight(0xe89c4e, 1.2, 1500);
            pointLight.position.set(200, 200, 200);
            scene.add(pointLight);
            const pointLight2 = new THREE.PointLight(0xccc4b3, 0.5, 1000);
            pointLight2.position.set(-200, -100, 200);
            scene.add(pointLight2);

            // ── 节点/边组 ──
            const nodeGroup = new THREE.Group();
            const edgeGroup = new THREE.Group();
            scene.add(nodeGroup);
            scene.add(edgeGroup);

            const nodeMeshes = [];
            const disposables = []; // { dispose } 资源

            // ── 标签 Sprite ──
            function createLabel(text, colorIdx) {
                const cv = document.createElement('canvas');
                cv.width = 256;
                cv.height = 64;
                const ctx = cv.getContext('2d');
                ctx.font = '600 26px Fraunces, ui-serif, serif';
                const hsl = getChartHsl(colorIdx);
                ctx.fillStyle = `hsl(${hsl})`;
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(text, 128, 32);
                const texture = new THREE.CanvasTexture(cv);
                texture.minFilter = THREE.LinearFilter;
                const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false });
                const sprite = new THREE.Sprite(material);
                sprite.scale.set(60, 15, 1);
                disposables.push(texture, material);
                return sprite;
            }

            // ── 更新场景节点/边（筛选/布局变化时调用） ──
            function updateScene() {
                // 清理旧的节点/边
                while (nodeGroup.children.length) nodeGroup.remove(nodeGroup.children[0]);
                while (edgeGroup.children.length) edgeGroup.remove(edgeGroup.children[0]);
                nodeMeshes.length = 0;
                disposables.forEach(d => { if (d && typeof d.dispose === 'function') d.dispose(); });
                disposables.length = 0;

                const nodes = filteredNodes.value.map(n => ({ ...n }));
                if (nodes.length === 0) return;
                compute3DPositions(nodes, layout.value);
                const nodeMap = new Map(nodes.map(n => [n.id, n]));

                // 边
                const borderColor = 0xddd3c0;
                const accentColor = 0x8a7d5b;
                filteredEdges.value.forEach(e => {
                    const sn = nodeMap.get(e.source);
                    const tn = nodeMap.get(e.target);
                    if (!sn || !tn) return;
                    const strong = (sn.degree > 5 || tn.degree > 5);
                    const points = [
                        new THREE.Vector3(sn.x, sn.y, sn.z),
                        new THREE.Vector3(tn.x, tn.y, tn.z),
                    ];
                    const geometry = new THREE.BufferGeometry().setFromPoints(points);
                    const material = new THREE.LineBasicMaterial({
                        color: strong ? accentColor : borderColor,
                        transparent: true,
                        opacity: strong ? 0.8 : 0.35,
                    });
                    const line = new THREE.Line(geometry, material);
                    edgeGroup.add(line);
                    disposables.push(geometry, material);
                });

                // 节点
                nodes.forEach((node, i) => {
                    const colorIdx = TYPE_COLOR_INDEX[node.type] || 2;
                    let color = new THREE.Color(0xccc4b3);
                    try {
                        color = new THREE.Color(`hsl(${getChartHsl(colorIdx)})`);
                    } catch (e) { /* fallback */ }
                    const radius = nodeRadius3D(node);
                    const geometry = new THREE.SphereGeometry(radius, 32, 32);
                    const material = new THREE.MeshStandardMaterial({
                        color: color,
                        emissive: color,
                        emissiveIntensity: 0.3,
                        roughness: 0.4,
                        metalness: 0.2,
                    });
                    const sphere = new THREE.Mesh(geometry, material);
                    sphere.position.set(node.x, node.y, node.z);
                    sphere.userData = node;
                    nodeGroup.add(sphere);
                    nodeMeshes.push(sphere);
                    disposables.push(geometry, material);

                    // hub 节点 halo
                    if (i === 0 || (node.degree || 0) > 5) {
                        const haloGeo = new THREE.SphereGeometry(radius + 6, 32, 32);
                        const haloMat = new THREE.MeshBasicMaterial({
                            color: color,
                            transparent: true,
                            opacity: 0.18,
                            side: THREE.DoubleSide,
                        });
                        const halo = new THREE.Mesh(haloGeo, haloMat);
                        halo.position.copy(sphere.position);
                        nodeGroup.add(halo);
                        disposables.push(haloGeo, haloMat);
                    }

                    // 标签
                    const labelText = (node.label || String(node.id)).slice(0, 10);
                    const label = createLabel(labelText, colorIdx);
                    label.position.set(node.x, node.y + radius + 8, node.z);
                    nodeGroup.add(label);
                });
            }

            updateScene();
            threeReady.value = true;

            // ── Raycaster 悬停检测 ──
            const raycaster = new THREE.Raycaster();
            const mouse = new THREE.Vector2();
            let hoveredMesh = null;

            function onPointerMove(e) {
                const rect = renderer.domElement.getBoundingClientRect();
                mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
                mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
                raycaster.setFromCamera(mouse, camera);
                const intersects = raycaster.intersectObjects(nodeMeshes);
                if (intersects.length > 0) {
                    const hit = intersects[0].object;
                    const node = hit.userData;
                    if (hoveredMesh !== hit) {
                        if (hoveredMesh) {
                            hoveredMesh.scale.set(1, 1, 1);
                            hoveredMesh.material.emissiveIntensity = 0.3;
                        }
                        hoveredMesh = hit;
                        hoveredMesh.scale.set(1.35, 1.35, 1.35);
                        hoveredMesh.material.emissiveIntensity = 0.65;
                    }
                    hoveredNode.value = node;
                    renderer.domElement.style.cursor = 'pointer';
                } else {
                    if (hoveredMesh) {
                        hoveredMesh.scale.set(1, 1, 1);
                        hoveredMesh.material.emissiveIntensity = 0.3;
                        hoveredMesh = null;
                    }
                    hoveredNode.value = null;
                    renderer.domElement.style.cursor = 'grab';
                }
            }

            function onPointerLeave() {
                if (hoveredMesh) {
                    hoveredMesh.scale.set(1, 1, 1);
                    hoveredMesh.material.emissiveIntensity = 0.3;
                    hoveredMesh = null;
                }
                hoveredNode.value = null;
                renderer.domElement.style.cursor = 'grab';
            }

            function onClick(e) {
                const rect = renderer.domElement.getBoundingClientRect();
                mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
                mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
                raycaster.setFromCamera(mouse, camera);
                const intersects = raycaster.intersectObjects(nodeMeshes);
                if (intersects.length > 0) {
                    selectedNode.value = intersects[0].object.userData;
                }
            }

            function onResize() {
                const nw = Math.max(container.clientWidth, 1);
                const nh = Math.max(container.clientHeight, 1);
                camera.aspect = nw / nh;
                camera.updateProjectionMatrix();
                renderer.setSize(nw, nh);
            }

            renderer.domElement.addEventListener('pointermove', onPointerMove);
            renderer.domElement.addEventListener('pointerleave', onPointerLeave);
            renderer.domElement.addEventListener('click', onClick);
            window.addEventListener('resize', onResize);

            // ── 动画循环 ──
            let animationId = null;
            function animate() {
                animationId = requestAnimationFrame(animate);
                controls.update();
                renderer.render(scene, camera);
            }
            animate();

            threeCtx = {
                THREE, OrbitControls, scene, camera, renderer, controls,
                nodeMeshes, nodeGroup, edgeGroup, disposables, updateScene,
                listeners: { onPointerMove, onPointerLeave, onClick, onResize, onControlsStart },
                autoRotateTimer, animationId,
            };

            // ── 清理函数 ──
            threeCleanup = () => {
                if (autoRotateTimer) clearTimeout(autoRotateTimer);
                if (animationId) cancelAnimationFrame(animationId);

                controls.removeEventListener('start', onControlsStart);
                renderer.domElement.removeEventListener('pointermove', onPointerMove);
                renderer.domElement.removeEventListener('pointerleave', onPointerLeave);
                renderer.domElement.removeEventListener('click', onClick);
                window.removeEventListener('resize', onResize);

                controls.dispose();

                // 清理节点/边资源
                disposables.forEach(d => { if (d && typeof d.dispose === 'function') d.dispose(); });
                disposables.length = 0;
                while (nodeGroup.children.length) nodeGroup.remove(nodeGroup.children[0]);
                while (edgeGroup.children.length) edgeGroup.remove(edgeGroup.children[0]);
                nodeMeshes.length = 0;

                // 清理灯光
                scene.remove(ambientLight);
                scene.remove(pointLight);
                scene.remove(pointLight2);

                renderer.dispose();
                if (renderer.domElement.parentNode === container) {
                    container.removeChild(renderer.domElement);
                }

                threeCtx = null;
                threeReady.value = false;
            };
        }

        onMounted(async () => {
            await loadData();
            await ensureThree();
        });

        onUnmounted(() => {
            if (threeCleanup) threeCleanup();
        });

        // 筛选/布局变化时更新场景
        watch([filteredNodes, filteredEdges, layout], () => {
            if (threeCtx && threeCtx.updateScene) {
                threeCtx.updateScene();
            } else {
                ensureThree();
            }
        });

        watch(() => appState.currentAccountId, (newId) => {
            if (newId) loadData();
        });

        return () => {
            // 无账号
            if (!loading.value && !accountId.value) {
                return h('div', { class: 'view-frame' }, [
                    h(EmptyState, {
                        icon: 'folder',
                        title: '暂无账号',
                        desc: '请先在账号管理中添加 B站 账号后查看记忆图谱。',
                    }),
                ]);
            }

            if (loading.value && (graphData.value.nodes || []).length === 0) {
                return h(Loading);
            }

            const hasGraph = (graphData.value.nodes || []).length > 0;
            const heroTitle = `${nodeCount.value.toLocaleString()} 条记忆 · ${categoryStats.value.length} 个分类 · ${edgeCount.value.toLocaleString()} 条关联`;

            return h('div', { class: 'view-frame' }, [
                // ═══════ Section 1: hero-band — 统计面板 + 图谱控制 ═══════
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);',
                }, [
                    // 左：统计面板（hero-panel accent 背景）
                    h(HeroPanel, {
                        eyebrow: '记忆图谱 3D',
                        title: heroTitle,
                        badge: '3D 视图',
                        badgeType: 'info',
                    }, () => h('div', {
                        class: 'flex items-baseline gap-2 flex-wrap',
                    }, [
                        h('span', {
                            style: 'font-size: 2.4rem; line-height: 1; font-weight: 700; color: hsl(var(--accent-foreground));',
                        }, nodeCount.value.toLocaleString()),
                        h('span', {
                            class: 'muted',
                            style: 'font-size: 0.92rem;',
                        }, `条记忆节点 · ${edgeCount.value.toLocaleString()} 条关联`),
                    ])),

                    // 右：图谱控制面板
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '图谱控制'),
                            h('h2', {
                                class: 'm-0',
                                style: 'font-size: 1.35rem; line-height: 1.08;',
                            }, '视图设置'),
                        ]),
                        // 分类筛选
                        h('div', { class: 'flex items-center gap-2', style: 'flex-wrap: wrap;' }, [
                            h(Button, {
                                type: 'ghost',
                                onClick: () => filterCategory.value = '',
                            }, () => [
                                h(Icon, { name: 'tag', size: '0.9rem' }),
                                filterCategory.value ? (TYPE_LABELS[filterCategory.value] || filterCategory.value) : '全部分类',
                                h(Icon, { name: 'chevron-down', size: '0.75rem' }),
                            ]),
                        ]),
                        // 布局切换
                        h('div', {
                            class: 'inline-flex',
                            style: 'border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.76); overflow: hidden;',
                        }, layoutOptions.map(opt => h('button', {
                            key: opt.key,
                            type: 'button',
                            class: 'btn',
                            style: `min-height: 2.5rem; padding: calc(var(--spacing) * 1.5) calc(var(--spacing) * 3); font-size: 0.88rem; border: 0; border-radius: 0; box-shadow: none; ${
                                layout.value === opt.key
                                    ? 'background: hsl(var(--primary)); color: hsl(var(--primary-foreground));'
                                    : 'background: transparent; color: hsl(var(--accent-foreground));'
                            }${opt.key !== 'sphere' ? ' border-left: 1px solid hsl(var(--border));' : ''}`,
                            onClick: () => layout.value = opt.key,
                        }, opt.label))),
                        // 搜索框
                        h('label', {
                            class: 'field-wrap',
                            style: 'width: 100%; min-width: 0;',
                            'aria-label': '搜索节点',
                        }, [
                            h(Icon, { name: 'funnel', size: '1.05rem' }),
                            h('input', {
                                class: 'field',
                                type: 'text',
                                placeholder: '搜索节点名称...',
                                value: searchQuery.value,
                                onInput: (e) => searchQuery.value = e.target.value,
                                onKeyup: (e) => { if (e.key === 'Enter') searchGraph(); },
                            }),
                        ]),
                        h('p', {
                            class: 'muted m-0',
                            style: 'font-size: 0.82rem; line-height: 1.5;',
                        }, '拖拽旋转 · 滚轮缩放 · 悬停查看'),
                    ]),
                ]),

                // ═══════ Section 2: Three.js 3D 画布 ═══════
                h('section', {}, [
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4);',
                    }, [
                        // 面板头
                        h('div', {
                            class: 'flex items-end justify-between gap-3',
                            style: 'flex-wrap: wrap;',
                        }, [
                            h('div', { class: 'grid gap-1 min-w-0' }, [
                                h('span', { class: 'eyebrow' }, '3D 关系图谱'),
                                h('h2', {
                                    class: 'm-0',
                                    style: 'font-size: 1.35rem; line-height: 1.08;',
                                }, '可旋转视图'),
                            ]),
                            h('div', { class: 'flex items-center gap-2', style: 'flex-wrap: wrap;' }, [
                                h('span', {
                                    style: 'font-size: 0.88rem; color: hsl(var(--muted-foreground));',
                                }, `显示 ${filteredNodes.value.length} / ${nodeCount.value} 节点`),
                                ...(filterCategory.value || searchQuery.value
                                    ? [h(Button, { size: 'sm', type: 'ghost', onClick: clearFilters }, () => '清除筛选')]
                                    : []),
                            ]),
                        ]),
                        // 3D 画布容器 / 空状态
                        hasGraph
                            ? h('div', {
                                ref: canvasRef,
                                style: 'width: 100%; height: 65vh; min-height: 480px; position: relative; border-radius: calc(var(--radius) * 0.82); overflow: hidden; background: radial-gradient(ellipse at center, hsl(var(--foreground) / 0.12), hsl(var(--foreground) / 0.04));',
                            }, !threeReady.value ? h('div', {
                                style: 'position: absolute; inset: 0; display: grid; place-items: center; color: hsl(var(--muted-foreground)); font-size: 0.92rem;',
                            }, '正在加载 3D 场景...') : null)
                            : h('div', {
                                class: 'grid',
                                style: 'padding: calc(var(--spacing) * 6) 0; justify-items: center; border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82);',
                            }, [h(EmptyState, { icon: 'folder', title: '暂无图谱数据', desc: '记忆库为空，或当前筛选条件下无匹配节点。' })]),
                    ]),
                ]),

                // ═══════ Section 3: split grid — 分类图例/统计 + 选中节点详情 ═══════
                h('section', {
                    class: 'grid gap-3',
                    style: 'grid-template-columns: minmax(0, 1.15fr) minmax(18rem, 0.85fr);',
                }, [
                    // 左：分类图例 + 图谱统计
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '图谱分析'),
                            h('h2', {
                                class: 'm-0',
                                style: 'font-size: 1.35rem; line-height: 1.08;',
                            }, '统计信息'),
                        ]),
                        // 分类图例
                        h('div', { class: 'grid gap-0' },
                            (categoryStats.value.length === 0
                                ? [h('p', { class: 'muted m-0', style: 'font-size: 0.92rem;' }, '暂无分类数据')]
                                : categoryStats.value.map(cat => h('div', {
                                    key: cat.key,
                                    class: 'flex items-center justify-between gap-2',
                                    style: `padding: calc(var(--spacing) * 2.2) calc(var(--spacing) * 1.5); border-bottom: 1px solid hsl(var(--border)); cursor: pointer; border-radius: calc(var(--radius) * 0.3);${filterCategory.value === cat.key ? ' background: hsl(var(--accent) / 0.12);' : ''}`,
                                    onClick: () => filterByCategory(cat.key),
                                }, [
                                    h('div', { class: 'flex items-center gap-2' }, [
                                        h('span', {
                                            style: `width: 0.85rem; height: 0.85rem; border-radius: 999px; background: hsl(var(--chart-${cat.colorIndex})); flex: 0 0 auto;`,
                                        }),
                                        h('span', { style: 'font-size: 0.98rem;' }, cat.label),
                                    ]),
                                    h('div', { class: 'flex items-baseline gap-1' }, [
                                        h('span', { style: 'font-size: 1.1rem; font-weight: 600;' }, String(cat.count)),
                                        h('span', { class: 'muted', style: 'font-size: 0.85rem;' }, '节点'),
                                        h('span', { style: 'color: hsl(var(--muted-foreground)); font-size: 0.85rem;' }, '·'),
                                        h('span', {
                                            style: 'color: hsl(var(--accent-foreground)); font-size: 0.88rem; font-weight: 500;',
                                        }, `${cat.percent}%`),
                                    ]),
                                ]))
                            ),
                        ),
                        // 图谱统计
                        h('div', {
                            class: 'grid gap-2',
                            style: 'padding: calc(var(--spacing) * 3); background: hsl(var(--muted) / 0.3); border-radius: calc(var(--radius) * 0.76); border: 1px solid hsl(var(--border));',
                        }, [
                            h('span', { class: 'eyebrow' }, '图谱统计'),
                            h('div', {
                                class: 'grid gap-2',
                                style: 'grid-template-columns: 1fr 1fr;',
                            }, [
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', { class: 'muted', style: 'font-size: 0.82rem;' }, '总节点数'),
                                    h('span', { style: 'font-size: 1.4rem; font-weight: 600;' }, String(nodeCount.value)),
                                ]),
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', { class: 'muted', style: 'font-size: 0.82rem;' }, '总边数'),
                                    h('span', { style: 'font-size: 1.4rem; font-weight: 600;' }, String(edgeCount.value)),
                                ]),
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', { class: 'muted', style: 'font-size: 0.82rem;' }, '平均度数'),
                                    h('span', { style: 'font-size: 1.4rem; font-weight: 600;' }, avgDegree.value),
                                ]),
                                h('div', { class: 'grid gap-1' }, [
                                    h('span', { class: 'muted', style: 'font-size: 0.82rem;' }, '最大度数'),
                                    h('span', { style: 'font-size: 1.4rem; font-weight: 600;' }, String(maxDegree.value)),
                                ]),
                            ]),
                        ]),
                    ]),

                    // 右：选中/悬停节点详情
                    h('article', {
                        class: 'grid gap-3',
                        style: 'background: hsl(var(--card)); border: 1px solid hsl(var(--border)); border-radius: calc(var(--radius) * 0.82); padding: calc(var(--spacing) * 4); align-content: start;',
                    }, [
                        h('div', { class: 'grid gap-1' }, [
                            h('span', { class: 'eyebrow' }, '节点详情'),
                            h('h2', {
                                class: 'm-0',
                                style: 'font-size: 1.35rem; line-height: 1.08;',
                            }, selectedNode.value ? '选中节点' : (hoveredNode.value ? '悬停节点' : '选中节点')),
                        ]),
                        (selectedNode.value || hoveredNode.value
                            ? (() => {
                                const node = selectedNode.value || hoveredNode.value;
                                const colorIdx = TYPE_COLOR_INDEX[node.type] || 2;
                                return h('div', { class: 'grid gap-3' }, [
                                    // 分类标签
                                    h('div', {
                                        class: 'inline-flex items-center gap-2 w-fit',
                                        style: `padding: calc(var(--spacing) * 1.2) calc(var(--spacing) * 2.5); border-radius: 999px; background: hsl(var(--chart-${colorIdx}) / 0.18); border: 1px solid hsl(var(--chart-${colorIdx}) / 0.35);`,
                                    }, [
                                        h('span', {
                                            style: `width: 0.7rem; height: 0.7rem; border-radius: 999px; background: hsl(var(--chart-${colorIdx}));`,
                                        }),
                                        h('span', { style: 'font-size: 0.88rem; font-weight: 500;' },
                                            TYPE_LABELS[node.type] || node.type || '未分类'),
                                    ]),
                                    // 节点名称
                                    h('h3', {
                                        class: 'm-0',
                                        style: 'font-size: 1.15rem; line-height: 1.3; word-break: break-all;',
                                    }, node.label || String(node.id)),
                                    // 记忆详情（仅 summary 节点）
                                    selectedMemory.value
                                        ? h('p', {
                                            class: 'm-0',
                                            style: 'line-height: 1.6; font-size: 0.95rem; color: hsl(var(--foreground));',
                                        }, selectedMemory.value.content || selectedMemory.value.summary || '-')
                                        : null,
                                    // 数据行
                                    h('div', {
                                        class: 'grid gap-2',
                                        style: 'grid-template-columns: 1fr 1fr 1fr; padding: calc(var(--spacing) * 3) 0; border-top: 1px solid hsl(var(--border)); border-bottom: 1px solid hsl(var(--border));',
                                    }, [
                                        h('div', { class: 'grid gap-1' }, [
                                            h('span', { class: 'muted', style: 'font-size: 0.78rem;' }, '节点类型'),
                                            h('span', { style: 'font-size: 0.95rem; font-weight: 500;' },
                                                TYPE_LABELS[node.type] || node.type || '-'),
                                        ]),
                                        h('div', { class: 'grid gap-1' }, [
                                            h('span', { class: 'muted', style: 'font-size: 0.78rem;' }, '关联数'),
                                            h('span', { style: 'font-size: 0.95rem; font-weight: 500;' },
                                                String(node.degree || 0)),
                                        ]),
                                        h('div', { class: 'grid gap-1' }, [
                                            h('span', { class: 'muted', style: 'font-size: 0.78rem;' }, '权重'),
                                            h('span', { style: 'font-size: 0.95rem; font-weight: 500;' },
                                                (node.weight || 0).toFixed(2)),
                                        ]),
                                    ]),
                                    // 关联节点
                                    h('div', { class: 'grid gap-2' }, [
                                        h('span', { class: 'eyebrow' }, '关联节点'),
                                        connectedNodes.value.length === 0
                                            ? h('p', { class: 'muted m-0', style: 'font-size: 0.92rem;' }, '无关联节点')
                                            : h('div', { class: 'flex gap-2', style: 'flex-wrap: wrap;' },
                                                connectedNodes.value.slice(0, 12).map(n => h(Button, {
                                                    key: `cn-${n.id}`,
                                                    type: 'ghost',
                                                    size: 'sm',
                                                    onClick: () => selectNode(n),
                                                }, () => [
                                                    h('span', {
                                                        style: `display:inline-block; width:0.6rem; height:0.6rem; border-radius:999px; background: hsl(var(--chart-${TYPE_COLOR_INDEX[n.type] || 2}));`,
                                                    }),
                                                    (n.label || String(n.id)).slice(0, 12),
                                                ]))
                                            ),
                                    ]),
                                ]);
                            })()
                            : h(EmptyState, {
                                icon: 'circle-question-mark',
                                title: '未选中节点',
                                desc: '点击 3D 图谱中的节点以查看详情。',
                            })
                        ),
                    ]),
                ]),
            ]);
        };
    },
});
