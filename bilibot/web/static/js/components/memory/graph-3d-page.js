// components/memory/graph-3d-page.js - 3D 记忆图谱页（Golden Time 设计稿）
const { defineComponent, h, ref, computed, onMounted, onUnmounted, watch, nextTick } = window.Vue;
import { api } from '../../api.js';
import { Button, Loading, EmptyState, Icon } from '../common.js';
import { appState, showToast, refreshAccounts } from '../../state.js';

// 节点类型 → chart 色号映射（与 2D 版一致）
const TYPE_COLOR_INDEX = {
    event: 1,
    person: 5,
    topic: 3,
    organization: 4,
    place: 2,
    location: 2,
    media: 4,
    object: 3,
    concept: 3,
};
const TYPE_LABELS = {
    event: '经历事件',
    person: '人物实体',
    topic: '主题实体',
    organization: '组织实体',
    place: '地点实体',
    location: '地点实体',
    media: '内容实体',
    object: '对象实体',
    concept: '概念实体',
};
const RELATION_LABELS = {
    mentions: '提及实体',
    contradicts: '相互矛盾',
    supersedes: '替代旧结论',
    updates: '更新',
    reflection: '反思',
    related_to: '主题关联',
};

// 本地 vendor 路径（已消除 CDN 依赖，文件已下载到 /static/vendor/）
const THREE_URL = '/static/vendor/three.module.js';
const OC_URL = '/static/vendor/OrbitControls.js';

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
    // 略小于旧版（8–24），减少遮挡标签与边
    return Math.max(6.5, Math.min(20, 6.5 + deg * 1.15));
}

// 动态加载 Three.js + OrbitControls（本地 vendor 文件）
// OrbitControls.js 内部的 bare specifier 'three' 已替换为 './three.module.js'，
// 浏览器原生 ES module 解析即可，无需 fetch + Blob URL hack。
async function loadThree() {
    const THREE = await import(/* @vite-ignore */ THREE_URL);
    const { OrbitControls } = await import(/* @vite-ignore */ OC_URL);
    return { THREE, OrbitControls };
}

export const MemoryGraph3DPage = defineComponent({
    name: 'MemoryGraph3DPage',
    setup() {
        const accountId = ref(null);
        const graphData = ref({ nodes: [], edges: [], memories: [], summary: {} });
        const totalCounts = ref({ nodes: 0, edges: 0, memories: 0 });
        const loading = ref(true);
        const loadError = ref('');
        const selectedNode = ref(null);
        const hoveredNode = ref(null);
        const filterCategory = ref('');
        const searchQuery = ref('');
        const layout = ref('sphere'); // sphere / helix
        const statsOpen = ref(false); // 统计信息面板（视图设置中切换）
        const canvasRef = ref(null);
        const threeReady = ref(false);
        const threeError = ref('');
        let threeCleanup = null;
        let threeCtx = null; // { scene, camera, renderer, controls, nodeMeshes, nodeGroup, edgeGroup, disposables, updateScene }
        let initializing = false;
        let mounted = false;

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

        const relationStats = computed(() => Object.entries(graphData.value.summary?.relation_breakdown || {})
            .map(([key, count]) => ({ key, label: RELATION_LABELS[key] || key, count: count || 0 }))
            .sort((a, b) => b.count - a.count));

        const avgDegree = computed(() => {
            const n = nodeCount.value;
            return n > 0 ? ((edgeCount.value * 2) / n).toFixed(1) : '0';
        });

        const maxDegree = computed(() => {
            const nodes = graphData.value.nodes || [];
            if (nodes.length === 0) return 0;
            return Math.max(...nodes.map(n => n.degree || 0));
        });

        // ── 选中节点详情（侧栏仅跟随选中，不跟 hover，避免拖拽闪烁） ──
        const detailNode = computed(() => selectedNode.value || null);

        const connectedNodes = computed(() => {
            if (!detailNode.value) return [];
            const sid = detailNode.value.id;
            const allNodes = graphData.value.nodes || [];
            const result = [];
            const seen = new Set();
            for (const e of (graphData.value.edges || [])) {
                if (e.source === sid || e.target === sid) {
                    const otherId = e.source === sid ? e.target : e.source;
                    if (!seen.has(otherId)) {
                        seen.add(otherId);
                        const node = allNodes.find(n => n.id === otherId);
                        if (node) {
                            result.push({
                                ...node,
                                relation_type: e.relation_type || 'related_to',
                                relation_weight: Number(e.weight || e.confidence || 0),
                            });
                        }
                    }
                }
            }
            return result;
        });

        const selectedMemory = computed(() => {
            if (!detailNode.value) return null;
            if (detailNode.value.node_kind !== 'event' && detailNode.value.type !== 'event') return null;
            const mid = detailNode.value.id;
            return (graphData.value.memories || []).find(m => (m.event_id || m.memory_id || m.id) === mid) || null;
        });

        // ── 数据加载 ──
        let loadSeq = 0;
        async function loadData() {
            const nextId = appState.currentAccountId
                || appState.accounts[0]?.account_id
                || appState.accounts[0]?.id
                || '';
            if (!nextId) {
                accountId.value = '';
                loading.value = false;
                graphData.value = { nodes: [], edges: [], memories: [], summary: {} };
                return;
            }
            if (accountId.value && accountId.value !== nextId) {
                graphData.value = { nodes: [], edges: [], memories: [], summary: {} };
                selectedNode.value = null;
            }
            accountId.value = nextId;
            const seq = ++loadSeq;
            loading.value = true;
            loadError.value = '';
            selectedNode.value = null;
            hoveredNode.value = null;
            try {
                const data = await api.memory.graph(accountId.value);
                if (seq !== loadSeq || accountId.value !== nextId) return;
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
                if (seq !== loadSeq) return;
                showToast('加载图谱失败: ' + e.message, 'error');
                loadError.value = e.message || '无法读取图谱数据';
                graphData.value = { nodes: [], edges: [], memories: [], summary: {} };
                totalCounts.value = { nodes: 0, edges: 0, memories: 0 };
            } finally {
                if (seq === loadSeq) loading.value = false;
            }
        }

        async function searchGraph() {
            if (!accountId.value) return;
            if (!searchQuery.value.trim()) {
                await loadData();
                return;
            }
            loading.value = true;
            loadError.value = '';
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
                loadError.value = e.message || '无法搜索图谱';
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

        function clearSelection() {
            selectedNode.value = null;
        }

        function toggleStats() {
            statsOpen.value = !statsOpen.value;
        }

        function onEscapeKey(e) {
            if (e.key !== 'Escape') return;
            const tag = (e.target && e.target.tagName) || '';
            if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target?.isContentEditable) return;
            if (statsOpen.value) {
                statsOpen.value = false;
                return;
            }
            if (selectedNode.value) {
                clearSelection();
            }
        }

        const layoutOptions = [
            { key: 'sphere', label: '球形' },
            { key: 'helix', label: '螺旋' },
        ];

        function destroyThree() {
            const cleanup = threeCleanup;
            threeCleanup = null;
            if (cleanup) cleanup();
            threeCtx = null;
            threeReady.value = false;
        }

        async function retryThree() {
            destroyThree();
            threeError.value = '';
            await nextTick();
            await ensureThree();
        }

        // ── Three.js 初始化 ──
        async function ensureThree() {
            if (initializing || !mounted) return;
            initializing = true;
            try {
                await nextTick();
                const container = canvasRef.value;
                if (!mounted || !container || !container.isConnected) return;
                if (filteredNodes.value.length === 0) return;
                if (threeCtx?.container === container) return;
                if (threeCtx) destroyThree();
                threeError.value = '';
                await initThree(container);
            } catch (e) {
                const message = e.message || String(e);
                threeError.value = message;
                threeReady.value = false;
                showToast('初始化 3D 场景失败: ' + message, 'error');
            } finally {
                initializing = false;
            }
        }

        async function initThree(container) {
            const { THREE, OrbitControls } = await loadThree();
            if (!mounted || filteredNodes.value.length === 0 || canvasRef.value !== container || !container.isConnected) return;

            // A renderer belongs to exactly one live graph container. Remove
            // orphan canvases left by an interrupted or older initializer.
            container.querySelectorAll(
                'canvas[data-memory-graph-renderer="true"], canvas[data-engine^="three.js"]'
            ).forEach(canvas => canvas.remove());

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
            renderer.domElement.dataset.memoryGraphRenderer = 'true';
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
            let hoveredMesh = null;
            let pointerDownPos = null;
            let pointerDragged = false;
            const DRAG_THRESHOLD = 4;

            // 统一 mesh 视觉：hover > selected > base
            function applyMeshVisual(mesh) {
                if (!mesh || !mesh.material) return;
                const isHovered = mesh === hoveredMesh;
                const isSelected = selectedNode.value
                    && mesh.userData
                    && mesh.userData.id === selectedNode.value.id;
                if (isHovered) {
                    mesh.scale.set(1.35, 1.35, 1.35);
                    mesh.material.emissiveIntensity = 0.65;
                } else if (isSelected) {
                    mesh.scale.set(1.22, 1.22, 1.22);
                    mesh.material.emissiveIntensity = 0.5;
                } else {
                    mesh.scale.set(1, 1, 1);
                    mesh.material.emissiveIntensity = 0.3;
                }
            }

            function refreshSelectionVisuals() {
                nodeMeshes.forEach(applyMeshVisual);
            }

            // ── 标签 Sprite（深色字 + 半透明底托，避免与浅色画布/节点糊成一团） ──
            function createLabel(text, colorIdx) {
                const cv = document.createElement('canvas');
                cv.width = 320;
                cv.height = 72;
                const ctx = cv.getContext('2d');
                const font = '600 26px Fraunces, ui-serif, Georgia, serif';
                ctx.font = font;
                const metrics = ctx.measureText(text);
                const textW = Math.min(metrics.width, 280);
                const padX = 18;
                const padY = 10;
                const pillW = textW + padX * 2 + 14; // 预留左侧色点
                const pillH = 40;
                const pillX = (cv.width - pillW) / 2;
                const pillY = (cv.height - pillH) / 2;
                const radius = 12;

                // 底托：高不透明浅底 + 描边，确保亮/暗主题下都可读
                ctx.beginPath();
                const r = radius;
                const x = pillX, y = pillY, w = pillW, h = pillH;
                ctx.moveTo(x + r, y);
                ctx.arcTo(x + w, y, x + w, y + h, r);
                ctx.arcTo(x + w, y + h, x, y + h, r);
                ctx.arcTo(x, y + h, x, y, r);
                ctx.arcTo(x, y, x + w, y, r);
                ctx.closePath();
                ctx.fillStyle = 'hsla(40, 16%, 98%, 0.94)';
                ctx.fill();
                ctx.strokeStyle = 'hsla(37, 16%, 20%, 0.18)';
                ctx.lineWidth = 1.5;
                ctx.stroke();

                // 类型色点
                const chartHsl = getChartHsl(colorIdx) || '55 20% 50%';
                const dotR = 5;
                const dotCx = pillX + padX;
                const dotCy = pillY + pillH / 2;
                ctx.beginPath();
                ctx.arc(dotCx, dotCy, dotR, 0, Math.PI * 2);
                ctx.fillStyle = `hsl(${chartHsl})`;
                ctx.fill();
                ctx.strokeStyle = 'hsla(37, 16%, 20%, 0.25)';
                ctx.lineWidth = 1;
                ctx.stroke();

                // 正文：固定深色，不随 chart 浅色变化
                ctx.font = font;
                ctx.fillStyle = 'hsl(37.5 15.7% 18%)';
                ctx.textAlign = 'left';
                ctx.textBaseline = 'middle';
                ctx.fillText(text, pillX + padX + 14, pillY + pillH / 2, 280);

                const texture = new THREE.CanvasTexture(cv);
                texture.minFilter = THREE.LinearFilter;
                const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false });
                const sprite = new THREE.Sprite(material);
                // 略放大，配合更高分辨率 canvas
                sprite.scale.set(72, 16, 1);
                disposables.push(texture, material);
                return sprite;
            }

            // ── 更新场景节点/边（筛选/布局变化时调用） ──
            function updateScene() {
                hoveredMesh = null;
                hoveredNode.value = null;
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
                const relationColors = {
                    mentions: 0x8a7d5b,
                    contradicts: 0xc35a45,
                    supersedes: 0x4f7b65,
                    updates: 0x4f7b65,
                    reflection: 0x7b668f,
                    related_to: 0xaaa08e,
                };
                filteredEdges.value.forEach(e => {
                    const sn = nodeMap.get(e.source);
                    const tn = nodeMap.get(e.target);
                    if (!sn || !tn) return;
                    const strong = (e.weight || 0) >= 0.75 || sn.degree > 5 || tn.degree > 5;
                    const points = [
                        new THREE.Vector3(sn.x, sn.y, sn.z),
                        new THREE.Vector3(tn.x, tn.y, tn.z),
                    ];
                    const geometry = new THREE.BufferGeometry().setFromPoints(points);
                    const material = new THREE.LineBasicMaterial({
                        color: relationColors[e.relation_type] || relationColors.related_to,
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
                        const haloGeo = new THREE.SphereGeometry(radius + 5, 32, 32);
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

                // 场景重建后：选中节点若已不在可见列表则清空，否则重应用高亮
                if (selectedNode.value) {
                    const stillVisible = nodeMeshes.some(
                        m => m.userData && m.userData.id === selectedNode.value.id
                    );
                    if (!stillVisible) {
                        selectedNode.value = null;
                    } else {
                        refreshSelectionVisuals();
                    }
                }
            }

            updateScene();
            threeReady.value = true;

            // ── Raycaster 悬停检测 ──
            const raycaster = new THREE.Raycaster();
            const mouse = new THREE.Vector2();

            function onPointerDown(e) {
                pointerDownPos = { x: e.clientX, y: e.clientY };
                pointerDragged = false;
            }

            function onPointerMove(e) {
                if (pointerDownPos) {
                    const dx = e.clientX - pointerDownPos.x;
                    const dy = e.clientY - pointerDownPos.y;
                    if ((dx * dx + dy * dy) > (DRAG_THRESHOLD * DRAG_THRESHOLD)) {
                        pointerDragged = true;
                    }
                }
                const rect = renderer.domElement.getBoundingClientRect();
                mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
                mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
                raycaster.setFromCamera(mouse, camera);
                const intersects = raycaster.intersectObjects(nodeMeshes);
                if (intersects.length > 0) {
                    const hit = intersects[0].object;
                    const node = hit.userData;
                    if (hoveredMesh !== hit) {
                        const prev = hoveredMesh;
                        hoveredMesh = hit;
                        if (prev) applyMeshVisual(prev);
                        applyMeshVisual(hoveredMesh);
                    }
                    hoveredNode.value = node;
                    renderer.domElement.style.cursor = 'pointer';
                } else {
                    if (hoveredMesh) {
                        const prev = hoveredMesh;
                        hoveredMesh = null;
                        applyMeshVisual(prev);
                    }
                    hoveredNode.value = null;
                    renderer.domElement.style.cursor = 'grab';
                }
            }

            function onPointerLeave() {
                pointerDownPos = null;
                if (hoveredMesh) {
                    const prev = hoveredMesh;
                    hoveredMesh = null;
                    applyMeshVisual(prev);
                }
                hoveredNode.value = null;
                renderer.domElement.style.cursor = 'grab';
            }

            function onClick(e) {
                // 拖拽旋转后的 click 不改选中
                if (pointerDragged) {
                    pointerDragged = false;
                    pointerDownPos = null;
                    return;
                }
                pointerDownPos = null;
                const rect = renderer.domElement.getBoundingClientRect();
                mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
                mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
                raycaster.setFromCamera(mouse, camera);
                const intersects = raycaster.intersectObjects(nodeMeshes);
                if (intersects.length > 0) {
                    selectedNode.value = intersects[0].object.userData;
                } else {
                    selectedNode.value = null;
                }
                refreshSelectionVisuals();
            }

            function onResize() {
                const nw = Math.max(container.clientWidth, 1);
                const nh = Math.max(container.clientHeight, 1);
                camera.aspect = nw / nh;
                camera.updateProjectionMatrix();
                renderer.setSize(nw, nh);
            }

            renderer.domElement.addEventListener('pointerdown', onPointerDown);
            renderer.domElement.addEventListener('pointermove', onPointerMove);
            renderer.domElement.addEventListener('pointerleave', onPointerLeave);
            renderer.domElement.addEventListener('click', onClick);
            window.addEventListener('resize', onResize);
            const resizeObserver = typeof ResizeObserver === 'function'
                ? new ResizeObserver(onResize)
                : null;
            resizeObserver?.observe(container);

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
                container,
                nodeMeshes, nodeGroup, edgeGroup, disposables, updateScene,
                refreshSelectionVisuals,
                listeners: { onPointerDown, onPointerMove, onPointerLeave, onClick, onResize, onControlsStart },
                autoRotateTimer, animationId,
            };

            // ── 清理函数 ──
            threeCleanup = () => {
                if (autoRotateTimer) clearTimeout(autoRotateTimer);
                if (animationId) cancelAnimationFrame(animationId);

                controls.removeEventListener('start', onControlsStart);
                renderer.domElement.removeEventListener('pointerdown', onPointerDown);
                renderer.domElement.removeEventListener('pointermove', onPointerMove);
                renderer.domElement.removeEventListener('pointerleave', onPointerLeave);
                renderer.domElement.removeEventListener('click', onClick);
                window.removeEventListener('resize', onResize);
                resizeObserver?.disconnect();

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
            mounted = true;
            document.addEventListener('keydown', onEscapeKey);
            if (!appState.accountsLoaded) {
                try { await refreshAccounts(); } catch (_) { /* toast */ }
            }
            await loadData();
        });

        onUnmounted(() => {
            mounted = false;
            document.removeEventListener('keydown', onEscapeKey);
            destroyThree();
        });

        // 筛选/布局变化时更新场景
        watch([filteredNodes, filteredEdges, layout], async () => {
            if (filteredNodes.value.length === 0) {
                selectedNode.value = null;
                destroyThree();
                return;
            }
            await nextTick();
            if (threeCtx && threeCtx.container !== canvasRef.value) destroyThree();
            if (threeCtx?.updateScene) {
                threeCtx.updateScene();
            } else {
                await ensureThree();
            }
        });

        // 选中变化时刷新 3D 高亮
        watch(selectedNode, () => {
            threeCtx?.refreshSelectionVisuals?.();
        });

        // 注意：不要在 statsOpen 时调用 resize。
        // 统计改为画布浮层后布局尺寸不变；误触发 setSize 会让节点看起来突然放大。

        watch(() => appState.currentAccountId, (newId, prevId) => {
            if (newId && newId !== prevId) loadData();
        });
        watch(() => appState.accountsLoaded, (loaded) => {
            if (loaded && !accountId.value) loadData();
        });

        function renderNodeDetail(node) {
            if (!node) return null;
            const colorIdx = TYPE_COLOR_INDEX[node.type] || 2;
            return h('div', { class: 'memory-node-sidebar-body' }, [
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
                // 记忆详情（仅 event 节点）
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
                                ariaLabel: `${RELATION_LABELS[n.relation_type] || n.relation_type}: ${n.label || n.id}`,
                                onClick: () => selectNode(n),
                            }, () => [
                                h('span', {
                                    style: `display:inline-block; width:0.6rem; height:0.6rem; border-radius:999px; background: hsl(var(--chart-${TYPE_COLOR_INDEX[n.type] || 2}));`,
                                }),
                                `${(n.label || String(n.id)).slice(0, 12)} · ${RELATION_LABELS[n.relation_type] || n.relation_type}`,
                            ]))
                        ),
                ]),
            ]);
        }

        function renderNodeSidebar() {
            const node = selectedNode.value;
            if (!node) return null;
            return h('aside', {
                class: 'memory-node-sidebar',
                'data-open': 'true',
                role: 'complementary',
                'aria-label': '节点详情',
                onClick: (e) => e.stopPropagation(),
            }, [
                h('div', { class: 'memory-node-sidebar-header' }, [
                    h('div', { class: 'grid gap-1 min-w-0' }, [
                        h('span', { class: 'eyebrow' }, '节点详情'),
                        h('h2', {
                            class: 'm-0',
                            style: 'font-size: 1.15rem; line-height: 1.15; word-break: break-all;',
                        }, '选中节点'),
                    ]),
                    h('button', {
                        type: 'button',
                        class: 'modal-close',
                        'aria-label': '关闭节点详情',
                        onClick: clearSelection,
                    }, '×'),
                ]),
                renderNodeDetail(node),
            ]);
        }

        function renderStatsPanel() {
            // 浮层挂在画布左上角：不改变左右栏布局，不触发 3D resize，不挡拖拽（仅面板自身可点）
            return h('aside', {
                class: 'memory-graph-stats-float',
                'data-open': statsOpen.value ? 'true' : 'false',
                role: 'complementary',
                'aria-label': '图谱统计信息',
                onClick: (e) => e.stopPropagation(),
                onPointerdown: (e) => e.stopPropagation(),
                onWheel: (e) => e.stopPropagation(),
            }, [
                h('div', { class: 'memory-node-sidebar-header' }, [
                    h('div', { class: 'grid gap-1 min-w-0' }, [
                        h('span', { class: 'eyebrow' }, '图谱分析'),
                        h('div', {
                            class: 'm-0',
                            style: 'font-size: 1rem; font-weight: 600; line-height: 1.2;',
                        }, '统计信息'),
                    ]),
                    h('button', {
                        type: 'button',
                        class: 'modal-close',
                        'aria-label': '关闭统计信息',
                        onClick: () => { statsOpen.value = false; },
                    }, '×'),
                ]),
                h('div', { class: 'memory-graph-stats-float-body' }, [
                    h('div', { class: 'grid gap-0', style: 'min-width: 0;' },
                        (categoryStats.value.length === 0
                            ? [h('p', { class: 'muted m-0', style: 'font-size: 0.88rem;' }, '暂无分类数据')]
                            : categoryStats.value.map(cat => h('div', {
                                key: cat.key,
                                class: 'memory-graph-stats-row',
                                style: filterCategory.value === cat.key
                                    ? 'background: hsl(var(--accent) / 0.12);'
                                    : '',
                                onClick: () => filterByCategory(cat.key),
                            }, [
                                h('div', { class: 'memory-graph-stats-row-main' }, [
                                    h('span', {
                                        style: `width: 0.7rem; height: 0.7rem; margin-top: 0.28rem; border-radius: 999px; background: hsl(var(--chart-${cat.colorIndex})); flex: 0 0 auto;`,
                                    }),
                                    h('span', { class: 'memory-graph-stats-row-label' }, cat.label),
                                ]),
                                h('div', { class: 'memory-graph-stats-row-meta' }, [
                                    h('span', { style: 'font-size: 0.95rem; font-weight: 600;' }, String(cat.count)),
                                    h('span', {
                                        style: 'color: hsl(var(--accent-foreground)); font-size: 0.78rem; font-weight: 500;',
                                    }, `${cat.percent}%`),
                                ]),
                            ]))
                        ),
                    ),
                    h('div', { class: 'grid gap-2', style: 'min-width: 0;' }, [
                        h('span', { class: 'eyebrow' }, '关系类型'),
                        relationStats.value.length
                            ? h('div', {
                                class: 'flex gap-2',
                                style: 'flex-wrap: wrap; min-width: 0;',
                            }, relationStats.value.map(relation =>
                                h('span', {
                                    key: relation.key,
                                    class: 'badge badge-info',
                                    style: 'white-space: normal; overflow-wrap: anywhere; max-width: 100%;',
                                }, `${relation.label} ${relation.count}`)
                            ))
                            : h('p', { class: 'muted m-0', style: 'font-size: .88rem;' }, '暂无关系数据'),
                    ]),
                    h('div', {
                        class: 'grid gap-2',
                        style: 'padding: calc(var(--spacing) * 2); background: hsl(var(--muted) / 0.3); border-radius: calc(var(--radius) * 0.76); border: 1px solid hsl(var(--border)); min-width: 0;',
                    }, [
                        h('span', { class: 'eyebrow' }, '图谱统计'),
                        h('div', {
                            class: 'grid gap-2',
                            style: 'grid-template-columns: 1fr 1fr; min-width: 0;',
                        }, [
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'muted', style: 'font-size: 0.75rem;' }, '总节点数'),
                                h('span', { style: 'font-size: 1.1rem; font-weight: 600;' }, String(nodeCount.value)),
                            ]),
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'muted', style: 'font-size: 0.75rem;' }, '总边数'),
                                h('span', { style: 'font-size: 1.1rem; font-weight: 600;' }, String(edgeCount.value)),
                            ]),
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'muted', style: 'font-size: 0.75rem;' }, '平均度数'),
                                h('span', { style: 'font-size: 1.1rem; font-weight: 600;' }, avgDegree.value),
                            ]),
                            h('div', { class: 'grid gap-1' }, [
                                h('span', { class: 'muted', style: 'font-size: 0.75rem;' }, '最大度数'),
                                h('span', { style: 'font-size: 1.1rem; font-weight: 600;' }, String(maxDegree.value)),
                            ]),
                        ]),
                    ]),
                ]),
            ]);
        }

        return () => {
            // 无账号
            if (!loading.value && !accountId.value) {
                return h('div', { class: 'view-frame' }, [
                    h(EmptyState, {
                        icon: 'folder',
                    title: '尚未登录 B站',
                    desc: '请先在「B站登录」完成接入后查看记忆图谱。',
                    }),
                ]);
            }

            if (loading.value && (graphData.value.nodes || []).length === 0) {
                return h(Loading);
            }

            const hasGraph = filteredNodes.value.length > 0;
            const memoryCount = totalCounts.value.memories || (graphData.value.memories || []).length;
            const toolbarMeta = `${memoryCount.toLocaleString()} 经历 · ${filteredNodes.value.length}/${nodeCount.value} 节点 · ${edgeCount.value.toLocaleString()} 关系`;

            return h('div', { class: 'memory-graph-page' }, [
                // ═══════ 左侧：视图设置 ═══════
                h('aside', {
                    class: 'memory-graph-sidebar',
                    'aria-label': '视图设置',
                    // 统计展开后 wheel 只滚左侧，避免冒泡导致页面/3D 抢滚动
                    onWheel: (e) => e.stopPropagation(),
                }, [
                    h('div', { class: 'memory-graph-toolbar-title' }, [
                        h('span', { class: 'eyebrow' }, '记忆图谱 · 可旋转视图'),
                        h('h1', '3D 关系图谱'),
                        h('div', { class: 'memory-graph-toolbar-meta' }, toolbarMeta),
                    ]),
                    // 布局
                    h('div', { class: 'memory-graph-sidebar-section' }, [
                        h('span', { class: 'memory-graph-sidebar-label' }, '布局'),
                        h('div', {
                            class: 'memory-graph-seg',
                            role: 'group',
                            'aria-label': '布局',
                        }, layoutOptions.map(opt => h('button', {
                            key: opt.key,
                            type: 'button',
                            'data-active': layout.value === opt.key ? 'true' : 'false',
                            onClick: () => { layout.value = opt.key; },
                        }, opt.label))),
                    ]),
                    // 视图设置：统计信息开关
                    h('div', { class: 'memory-graph-sidebar-section' }, [
                        h('span', { class: 'memory-graph-sidebar-label' }, '视图设置'),
                        h('div', {
                            class: 'memory-graph-seg',
                            role: 'group',
                            'aria-label': '统计信息',
                        }, [
                            h('button', {
                                type: 'button',
                                'data-active': statsOpen.value ? 'true' : 'false',
                                'aria-pressed': statsOpen.value ? 'true' : 'false',
                                onClick: toggleStats,
                            }, statsOpen.value ? '关闭统计' : '统计信息'),
                        ]),
                    ]),
                    // 搜索
                    h('div', { class: 'memory-graph-sidebar-section' }, [
                        h('span', { class: 'memory-graph-sidebar-label' }, '搜索'),
                        h('label', {
                            class: 'field-wrap memory-graph-search',
                            'aria-label': '搜索节点',
                        }, [
                            h(Icon, { name: 'funnel', size: '1.05rem' }),
                            h('input', {
                                class: 'field',
                                type: 'text',
                                placeholder: '搜索节点...',
                                value: searchQuery.value,
                                onInput: (e) => { searchQuery.value = e.target.value; },
                                onKeyup: (e) => { if (e.key === 'Enter') searchGraph(); },
                            }),
                        ]),
                    ]),
                    // 筛选操作
                    h('div', { class: 'memory-graph-sidebar-actions' }, [
                        filterCategory.value
                            ? h(Button, {
                                type: 'ghost',
                                size: 'sm',
                                onClick: () => { filterCategory.value = ''; },
                            }, () => [
                                h(Icon, { name: 'tag', size: '0.85rem' }),
                                TYPE_LABELS[filterCategory.value] || filterCategory.value,
                                ' ×',
                            ])
                            : null,
                        (filterCategory.value || searchQuery.value)
                            ? h(Button, {
                                size: 'sm',
                                type: 'ghost',
                                onClick: clearFilters,
                            }, () => '清除筛选')
                            : null,
                    ]),
                ]),

                // ═══════ 右侧：整页 3D 画布 ═══════
                h('div', { class: 'memory-graph-stage' }, [
                    hasGraph
                        ? h('div', { class: 'memory-graph-canvas' }, [
                            h('div', {
                                ref: canvasRef,
                                class: 'memory-graph-three-host',
                            }),
                            threeError.value
                                ? h('div', { class: 'memory-graph-overlay', role: 'alert' }, [
                                    h(EmptyState, {
                                        icon: 'triangle-alert',
                                        title: '3D 场景不可用',
                                        desc: threeError.value,
                                    }, {
                                        default: () => h(Button, { type: 'ghost', size: 'sm', onClick: retryThree }, () => '重新加载场景'),
                                    }),
                                ])
                                : !threeReady.value
                                    ? h('div', { class: 'memory-graph-overlay muted' }, '正在加载 3D 场景...')
                                    : null,
                            threeReady.value && !threeError.value
                                ? h('span', { class: 'memory-graph-hint' }, '拖拽旋转 · 滚轮缩放 · 点击节点')
                                : null,
                            // 统计浮层：不改变布局尺寸，不影响 3D 缩放
                            renderStatsPanel(),
                            renderNodeSidebar(),
                        ])
                        : h('div', {
                            class: 'grid',
                            style: 'height: 100%; min-height: inherit; place-items: center; padding: calc(var(--spacing) * 6);',
                        }, [h(EmptyState, {
                            icon: loadError.value ? 'triangle-alert' : 'folder',
                            title: loadError.value ? '图谱加载失败' : '暂无图谱数据',
                            desc: loadError.value || ((filterCategory.value || searchQuery.value)
                                ? '当前筛选条件下无匹配节点。'
                                : '记忆库中还没有可显示的事件或实体。'),
                        }, loadError.value ? {
                            default: () => h(Button, { type: 'ghost', size: 'sm', onClick: loadData }, () => '重新加载'),
                        } : undefined)]),
                ]),
            ]);
        };
    },
});
