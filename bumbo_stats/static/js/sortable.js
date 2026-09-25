/* sortable.js - 摄像头卡片拖拽排序
 *
 * 功能：
 *   - 用户可以拖动“实时监控”模块的 camera-card 改变显示位置
 *   - “打印机状态”模块的 printer-card 按相同位置同步重排
 *   - 位置写入 localStorage，刷新后保留
 *   - 保留用户拖出的“空格”（例如 4 卡拖成 2×2：(1,3) 和 (2,3) 留空）
 *
 * 设计要点：
 *   - 仅 camera-card 可拖（printer-card 不允许拖），避免两套 DOM 互拽导致顺序错乱
 *   - 同步使用 data-serial 匹配，不依赖 DOM 顺序的隐式约定
 *   - 拖拽中通过 placeholder 显示“将要插入的网格单元”，
 *     鼠标可在任意行/列/末尾暂停，placeholder 跟随到任意网格单元（不仅限卡片之间）
 *   - placeholder 是绝对定位，不参与 grid flow，避免破坏原有卡片布局
 *
 * 持久化格式：
 *   - 新格式：{ "<serial>": [row, col], ... }（按“位置”保存，可含空格）
 *   - 旧格式：[ "<serial>", ... ]（按“顺序”保存，刷新后变成紧凑 3×N）
 *   - loadSavedOrder 会自动识别两种格式，旧格式迁到新格式后保存
 */

const SORT_STORAGE_KEY = 'bumbo_printer_order_v1';
const GRID_COLS = 3;  // 与 dashboard.css 中 grid-template-columns: repeat(3, 1fr) 保持一致

/* ===== 工具函数 ===== */

/**
 * 把一个父容器里的直接子节点按 serials 数组的顺序重排。
 * 找不到对应 serial 的子节点保持原位（容错：localStorage 中残留的旧 serial）。
 */
function reorderContainerBySerials(container, dataSerialAttr, serials) {
    if (!container || !Array.isArray(serials) || serials.length === 0) return;
    const bySerial = new Map();
    Array.from(container.children).forEach(child => {
        const s = child.getAttribute(dataSerialAttr);
        if (s) bySerial.set(s, child);
    });

    // 按 serials 顺序重新 append（append 会移动已有节点，不复制）
    serials.forEach(serial => {
        const node = bySerial.get(serial);
        if (node) container.appendChild(node);
    });
}

/**
 * 同步 printer-cards / utilization-today 到 camera-grid 当前视觉顺序。
 * 视觉顺序由各 camera-card 的 inline grid-row / grid-column 决定（CSS Grid
 * 不再 auto-flow），所以必须按 grid 顺序而非 DOM 顺序读取 serials。
 */
function syncPrinterCards(cameraGrid) {
    const serials = getGridOrderedSerials(cameraGrid);
    if (serials.length === 0) return;

    const printerCards = document.getElementById('printer-cards');
    if (printerCards) reorderContainerBySerials(printerCards, 'data-serial', serials);

    // 产能列表也同步：utilization-today 下的 .utilization-row
    const utilList = document.getElementById('utilization-today');
    if (utilList) reorderContainerBySerials(utilList, 'data-serial', serials);

    // 趋势图 dataset 顺序是 charts.js 内部维护的，下次 loadMonthlyUsage 会重建
    // 这里无需干预，loadMonthlyUsage 每次按 printer_names（配置顺序）传入；
    // 为保证趋势图与用户拖拽顺序一致，重排后主动触发一次重载。
    if (typeof loadMonthlyUsage === 'function') {
        loadMonthlyUsage();
    }
}

/**
 * 按 grid-row/grid-column 由小到大读取 serials（CSS Grid 视觉顺序）。
 */
function getGridOrderedSerials(grid) {
    return Array.from(grid.querySelectorAll('.camera-card'))
        .map(c => ({
            serial: c.getAttribute('data-serial'),
            row: parseInt(c.style.gridRow, 10) || 0,
            col: parseInt(c.style.gridColumn, 10) || 0,
        }))
        .sort((a, b) => (a.row - b.row) || (a.col - b.col))
        .map(x => x.serial)
        .filter(Boolean);
}

/**
 * 给 grid 里的每张 camera-card 锁定 grid-row / grid-column。
 * - CSS Grid 默认是 auto-flow (row)，会以 DOM 顺序紧凑填充；
 *   这正是用户反馈“会自动连续排列”的根因。
 * - 锁定后每张卡片占据固定 (row, col)，可被 swap / 任意位置插入，
 *   不再被 CSS 重新 flow。
 */
function lockGridPositions(grid, orderedSerials) {
    const cards = Array.from(grid.querySelectorAll('.camera-card'));
    if (cards.length === 0) return;

    // 以有序 serials 为主依据（可能来自 saved 或当前 DOM 顺序）
    const cardBySerial = new Map();
    cards.forEach(c => {
        const s = c.getAttribute('data-serial');
        if (s) cardBySerial.set(s, c);
    });

    let nextIdx = 0;
    const consume = (serial) => {
        const card = cardBySerial.get(serial);
        if (!card) return false;
        card.style.gridRow = Math.floor(nextIdx / GRID_COLS) + 1;
        card.style.gridColumn = (nextIdx % GRID_COLS) + 1;
        nextIdx++;
        return true;
    };

    if (Array.isArray(orderedSerials) && orderedSerials.length > 0) {
        orderedSerials.forEach(consume);
    }
    // 未列出的 serial（DOM 中存在但不在序列里，如后加入的打印机）追加到末尾
    cards.forEach(card => {
        const s = card.getAttribute('data-serial');
        if (s && !(orderedSerials || []).includes(s)) consume(s);
    });
}

/* ===== 持久化 ===== */

/**
 * 读取 localStorage 中的排序偏好。
 *
 * 返回值为对象 { "<serial>": [row, col], ... }：
 *   - 新格式：直接返回 {"serialA": [1,1], ...}
 *   - 旧格式（数组）：转换为 {serial: [按 GRID_COLS 紧凑计算出的位置]}
 *
 * 返 null 表示读取失败或存储为空。
 */
function loadSavedOrder() {
    try {
        const raw = localStorage.getItem(SORT_STORAGE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (parsed === null || typeof parsed !== 'object') return null;

        if (Array.isArray(parsed)) {
            // 旧格式：[serial, ...] → 按顺序填入 (row, col)，不要空格
            const obj = {};
            parsed.forEach((serial, idx) => {
                if (typeof serial === 'string' && serial.length > 0) {
                    obj[serial] = [
                        Math.floor(idx / GRID_COLS) + 1,
                        (idx % GRID_COLS) + 1,
                    ];
                }
            });
            return Object.keys(obj).length > 0 ? obj : null;
        }

        // 新格式：{serial: [row, col], ...}；逐项验证取值是 [row, col] 数字数组
        const validated = {};
        for (const [serial, pos] of Object.entries(parsed)) {
            if (Array.isArray(pos) && pos.length === 2 &&
                Number.isFinite(pos[0]) && Number.isFinite(pos[1]) &&
                pos[0] >= 1 && pos[1] >= 1 &&
                typeof serial === 'string' && serial.length > 0) {
                validated[serial] = [Math.floor(pos[0]), Math.floor(pos[1])];
            }
        }
        return Object.keys(validated).length > 0 ? validated : null;
    } catch (e) {
        console.warn('读取排序偏好失败:', e);
        return null;
    }
}

/**
 * 供 charts.js 调用：按 localStorage 中的偏好顺序重排月度产能数据，
 * 避免趋势图数据集中打印机的顺序与用户拖拽后的视觉顺序不一致。
 *
 * saved 现在是 {serial: [row, col]} 对象，不再只是顺序数组。我们取“阅读顺序”
 * （按 row/col 升序）作为排序顺序，规则与原代码一致。
 * @param {Array} monthlyData /api/usage/monthly 返回的对象数组
 * @returns {Array} 按偏好重排后的数组（无可用偏好时原样返回）
 */
function sortMonthlyDataByPreference(monthlyData) {
    if (!Array.isArray(monthlyData) || monthlyData.length <= 1) return monthlyData;
    const saved = loadSavedOrder();
    if (!saved || Object.keys(saved).length === 0) return monthlyData;

    // 按 row/col 升序推导出 rank；与 lockGridPositions / getCurrentPositions 的位置语义一致
    const rank = new Map();
    const orderedSerials = Object.entries(saved)
        .map(([serial, pos]) => ({ serial, row: pos[0], col: pos[1] }))
        .sort((a, b) => (a.row - b.row) || (a.col - b.col))
        .map(x => x.serial);
    orderedSerials.forEach((serial, idx) => rank.set(serial, idx));

    // 稳定排序：偏好顺序第一，其他按原顺序追加
    return monthlyData
        .map((item, idx) => ({ item, idx }))
        .sort((a, b) => {
            const ra = rank.has(a.item.printer_serial) ? rank.get(a.item.printer_serial) : Number.MAX_SAFE_INTEGER;
            const rb = rank.has(b.item.printer_serial) ? rank.get(b.item.printer_serial) : Number.MAX_SAFE_INTEGER;
            if (ra !== rb) return ra - rb;
            return a.idx - b.idx;  // 同 rank 时保持稳定
        })
        .map(x => x.item);
}

/**
 * 把当前位置表写入 localStorage。
 * positions 必须是 {serial: [row, col], ...} 格式；非法取值会被静默跳过。
 */
function saveOrder(positions) {
    try {
        const cleaned = {};
        if (positions && typeof positions === 'object' && !Array.isArray(positions)) {
            for (const [serial, pos] of Object.entries(positions)) {
                if (typeof serial !== 'string' || serial.length === 0) continue;
                if (!Array.isArray(pos) || pos.length !== 2) continue;
                const row = Number(pos[0]);
                const col = Number(pos[1]);
                if (!Number.isFinite(row) || !Number.isFinite(col)) continue;
                if (row < 1 || col < 1) continue;
                cleaned[serial] = [Math.floor(row), Math.floor(col)];
            }
        }
        if (Object.keys(cleaned).length === 0) {
            // 不要写入空对象：避免覆盖了合法配置反而丢失。
            // 调用方应负责判断是否要实际写入。
            console.warn('saveOrder: positions 为空，跳过 localStorage 写入');
            return;
        }
        localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify(cleaned));
    } catch (e) {
        console.warn('保存排序偏好失败:', e);
    }
}

/**
 * 从 camera-grid 中读取每张卡片的当前位置，返回 {serial: [row, col], ...}。
 * 跳过了未设置 gridRow/gridColumn 的卡片（极端情况下的兑底）。
 */
function getCurrentPositions(grid) {
    const positions = {};
    Array.from(grid.querySelectorAll('.camera-card')).forEach(card => {
        const serial = card.getAttribute('data-serial');
        if (!serial) return;
        const row = parseInt(card.style.gridRow, 10);
        const col = parseInt(card.style.gridColumn, 10);
        if (Number.isFinite(row) && Number.isFinite(col) && row >= 1 && col >= 1) {
            positions[serial] = [row, col];
        }
    });
    return positions;
}

/* ===== 初始化：按 localStorage 调整首屏顺序 ===== */

function applySavedOrder() {
    const grid = document.getElementById('camera-grid');
    if (!grid) return;

    try {
        // saved 为 null 表示无偏好；为 {serial: [row, col]} 表示有“位置”偏好。
        const saved = loadSavedOrder();
        const cards = Array.from(grid.querySelectorAll('.camera-card'));

        if (!saved || Object.keys(saved).length === 0) {
            // 无偏好：按 DOM 顺序紧凑填充。不创建任何空格。
            const serials = cards.map(c => c.getAttribute('data-serial')).filter(Boolean);
            lockGridPositions(grid, serials);
            syncPrinterCards(grid);
            return;
        }

        // 有偏好：逐张卡片按 saved[serial] 锁定 (row, col)；
        // - saved 中不存在的 serial （后加的打印机）追加到尾部
        // - 同一位置冲突（saved 数据异常）后续卡片继续后移
        const placed = new Set();
        const placedKeys = new Set();
        const placedOrder = [];  // 用于 syncPrinterCards 的阅读顺序
        const remaining = [];

        cards.forEach(card => {
            const serial = card.getAttribute('data-serial');
            if (!serial) return;
            const pos = saved[serial];
            if (Array.isArray(pos) && pos.length === 2 &&
                Number.isFinite(pos[0]) && Number.isFinite(pos[1])) {
                const row = Math.floor(pos[0]);
                const col = Math.floor(pos[1]);
                if (row >= 1 && col >= 1) {
                    const key = `${row},${col}`;
                    if (!placedKeys.has(key)) {
                        card.style.gridRow = row;
                        card.style.gridColumn = col;
                        placed.add(card);
                        placedKeys.add(key);
                        placedOrder.push(card);
                        return;
                    }
                }
            }
            remaining.push(card);
        });

        // 未落入 saved 的卡片：从末尾连续填充，跨过已占位位置。
        let row = 1, col = 1;
        remaining.forEach(card => {
            while (placedKeys.has(`${row},${col}`)) {
                col++;
                if (col > GRID_COLS) { col = 1; row++; }
            }
            card.style.gridRow = row;
            card.style.gridColumn = col;
            placedKeys.add(`${row},${col}`);
            placedOrder.push(card);
            col++;
            if (col > GRID_COLS) { col = 1; row++; }
        });

        syncPrinterCards(grid);
    } finally {
        // 不论 lockGridPositions / syncPrinterCards 是否抛错，都必须取消 camera-grid
        // 的 visibility: hidden。否则用户会看到“空白区域”，甚至卡片全部不显示。
        // 加入 .layout-ready 后，dashboard.css 里的 .camera-grid.layout-ready 会
        // 令 visibility: visible，从此 CSS Grid 按刚刚锁定的 inline grid-row/col
        // 渲染，不再出现“auto-flow 紧凑排列闪烁”。
        grid.classList.add('layout-ready');
    }
}

/* ===== 拖拽交互 ===== */

let dragSource = null;        // 正在拖拽的 camera-card
let placeholder = null;       // 占位元素（动态创建）
let lastTargetIndex = -1;     // 最近一次 dragover 算出的“逻辑网格索引”

/**
 * 创建并缓存 placeholder 元素。
 * placeholder 默认隐藏，dragstart 时显示，dragover 时按鼠标坐标移动。
 */
function ensurePlaceholder(grid) {
    if (placeholder && placeholder.parentElement === grid) return placeholder;
    placeholder = document.createElement('div');
    placeholder.className = 'drag-placeholder';
    placeholder.setAttribute('aria-hidden', 'true');
    grid.appendChild(placeholder);
    return placeholder;
}

/**
 * 计算鼠标坐标对应的网格单元“逻辑位置” (row, col) → linear index = row * GRID_COLS + col。
 * - cols 限制在 [0, GRID_COLS-1]
 * - row 不设上限，允许在“最后一行下方”的空白位置
 * - index 最终在 drop 时会被 clamp 到 [0, children.length] 范围
 */
function computeGridIndex(grid, clientX, clientY) {
    const rect = grid.getBoundingClientRect();
    const cs = getComputedStyle(grid);
    // gap 可能为 column-gap / row-gap 两种写法，降级到通用的 gap
    const colGap = parseFloat(cs.columnGap || cs.gap) || 0;
    const rowGap = parseFloat(cs.rowGap || cs.gap) || 0;

    // 以当前未被拖动的第一张卡片为参考，避免高度不一致导致错位
    const cards = grid.querySelectorAll('.camera-card:not(.dragging)');
    if (cards.length === 0) return { row: 0, col: 0, index: 0 };

    const sample = cards[0].getBoundingClientRect();
    const cellWidth = sample.width;
    const cellHeight = sample.height;
    const colStep = cellWidth + colGap;
    const rowStep = cellHeight + rowGap;

    // 鼠标相对网格左上角的坐标
    const relX = clientX - rect.left;
    const relY = clientY - rect.top;

    // 列：四舍五入到最近的列中心，便于跨越列边界时仍可点击到邻近列
    let col = Math.round(relX / colStep);
    if (col < 0) col = 0;
    if (col >= GRID_COLS) col = GRID_COLS - 1;

    // 行：可超过现有行数（允许拖到“最后一行下方”的位置）
    let row = Math.round(relY / rowStep);
    if (row < 0) row = 0;

    return { row, col, index: row * GRID_COLS + col };
}

/**
 * 根据鼠标坐标更新 placeholder 的 left/top/width/height，
 * 使其覆盖“将要插入的”网格单元，并缓存逻辑索引供 drop 使用。
 */
function updatePlaceholder(grid, e) {
    if (!placeholder) return -1;
    const { row, col, index } = computeGridIndex(grid, e.clientX, e.clientY);

    const cards = grid.querySelectorAll('.camera-card:not(.dragging)');
    if (cards.length === 0) {
        placeholder.style.display = 'none';
        lastTargetIndex = -1;
        return -1;
    }

    const cs = getComputedStyle(grid);
    const colGap = parseFloat(cs.columnGap || cs.gap) || 0;
    const rowGap = parseFloat(cs.rowGap || cs.gap) || 0;

    const sample = cards[0].getBoundingClientRect();
    const cellWidth = sample.width;
    const cellHeight = sample.height;

    const left = col * (cellWidth + colGap);
    const top = row * (cellHeight + rowGap);

    placeholder.style.left = `${left}px`;
    placeholder.style.top = `${top}px`;
    placeholder.style.width = `${cellWidth}px`;
    placeholder.style.height = `${cellHeight}px`;
    placeholder.style.display = 'block';

    lastTargetIndex = index;
    return index;
}

/**
 * 把 dragSource 移动到 placeholder 对应的网格单元位置。
 *
 * 与“sequential move”不同，这里采用 swap 模式：
 *  - dragSource 占上目标 (row, col)
 *  - 如果目标位置原本有 targetCard，让 targetCard 去 dragSource 原位
 *  - 其他卡片不动 → 不会“连续填补空位”，用户看到的就是拖拽意图
 *
 * DOM 顺序保持不变（不再 insertBefore），由各 camera-card 的 inline
 * grid-row / grid-column 决定视觉位置，从而避免 CSS Grid auto-flow。
 *
 * lastTargetIndex 必须在调用前已由 updatePlaceholder 设置。
 */
function commitDrop(grid) {
    if (!dragSource) return;

    const targetRow = Math.floor(lastTargetIndex / GRID_COLS) + 1;
    const targetCol = (lastTargetIndex % GRID_COLS) + 1;

    const sourceRow = parseInt(dragSource.style.gridRow, 10) || 1;
    const sourceCol = parseInt(dragSource.style.gridColumn, 10) || 1;

    // 拖到自己身上：noop（避免同一个卡片跳位）
    if (sourceRow === targetRow && sourceCol === targetCol) {
        // 同步 syncPrinterCards：即使 sourceRow === targetRow 也让 printer-card 跟随
        // 首次加载后这里会跳过后续 swap，但仍然走同步逻辑，避免边界问题
        syncPrinterCards(grid);
        return;
    }

    // 寻找目标位置原本占据的卡片
    let targetCard = null;
    Array.from(grid.querySelectorAll('.camera-card')).forEach(c => {
        if (c === dragSource) return;
        const r = parseInt(c.style.gridRow, 10);
        const col = parseInt(c.style.gridColumn, 10);
        if (r === targetRow && col === targetCol) targetCard = c;
    });

    // dragSource 占据目标位置
    dragSource.style.gridRow = targetRow;
    dragSource.style.gridColumn = targetCol;

    // 目标位置原本有卡片 → swap，让它去 dragSource 原位
    if (targetCard) {
        targetCard.style.gridRow = sourceRow;
        targetCard.style.gridColumn = sourceCol;
    }

    // swap 完成瞬间立即同步 printer-cards / utilization-today：
    // 这样即便后续的 drop 处理被中断或发生顺序异常，printer-card
    // 也会跟随 swap 而不是被拖到下一个事件循环才同步。
    syncPrinterCards(grid);
}

function hidePlaceholder() {
    if (placeholder) placeholder.style.display = 'none';
    lastTargetIndex = -1;
}

function initCameraSortable() {
    const grid = document.getElementById('camera-grid');
    if (!grid) return;
    ensurePlaceholder(grid);

    grid.addEventListener('dragstart', (e) => {
        const card = e.target.closest('.camera-card');
        if (!card) return;
        dragSource = card;
        card.classList.add('dragging');
        // Firefox 要求 setData 才能让拖拽真正生效
        try {
            e.dataTransfer.setData('text/plain', card.getAttribute('data-serial') || '');
            e.dataTransfer.effectAllowed = 'move';
        } catch (_) { /* 某些浏览器 setData 抛错可忽略 */ }
        // 立刻按当前鼠标位置显示 placeholder，避免首帧跳变
        updatePlaceholder(grid, e);
    });

    grid.addEventListener('dragend', () => {
        if (dragSource) dragSource.classList.remove('dragging');
        hidePlaceholder();
        dragSource = null;
    });

    grid.addEventListener('dragover', (e) => {
        // 必须 preventDefault 才能触发 drop
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
        updatePlaceholder(grid, e);
    });

    grid.addEventListener('dragleave', (e) => {
        // 仅当真正离开 grid 容器才隐藏 placeholder
        // （进入子元素时 relatedTarget 在 grid 内部，不应清除）
        if (e.relatedTarget && grid.contains(e.relatedTarget)) return;
        // 拖到 grid 外部区域时保留 placeholder，以便用户回到 grid 还能看到
        //（浏览器不保证 dragleave/dragenter 在网格内对子元素的对称性）
    });

    grid.addEventListener('drop', (e) => {
        e.preventDefault();
        if (!dragSource) return;
        // 最后再算一次，避免 placeholder 位置落后于最新鼠标
        updatePlaceholder(grid, e);
        // commitDrop 内部已完成：swap 位置 + syncPrinterCards（同步重排
        // printer-cards / utilization-today）。这里只负责清理、持久化。
        commitDrop(grid);
        hidePlaceholder();

        // 同步移除 .dragging：浏览器 dragend 事件不一定总在 drop 后触发，
        // （某些拖出区域、跨 iframe、或与其他事件竞争时）依赖 dragend 会导致
        // 卡片长期处于 opacity: 0.4 的“变灰”状态。这里在 drop 里立即移除。
        const dropped = dragSource;
        dropped.classList.remove('dragging');

        // 持久化当前位置表到 localStorage。getCurrentPositions 读 gridRow/gridColumn
        // 为每个 serial 生成 {serial: [row, col], ...}；保存后下次 applySavedOrder
        // 仍会把卡片放回同一 (row, col)，空格（如拖出 2×2 后 (1,3)/(2,3) 留空）也保留。
        saveOrder(getCurrentPositions(grid));
        // 注意：dragSource 留给 dragend 清理（以应对拖拽到 drop target 外部的场景）。
        // 这里不主动清空，避免 dragend 里 if (dragSource) 跳过后续清理。
    });
}