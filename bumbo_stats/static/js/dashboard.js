/* dashboard.js - WebSocket 客户端和 UI 更新 */

let ws = null;
let wsReconnectTimer = null;

function initWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${location.host}/ws`);

    ws.onopen = () => {
        setWsStatus(true);
        // 发送心跳
        ws._heartbeat = setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) ws.send('ping');
        }, 30000);
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            if (msg.type === 'status_batch') {
                updateAllPrinters(msg.data);
            }
        } catch (e) { /* ignore parse errors */ }
    };

    ws.onclose = () => {
        setWsStatus(false);
        clearInterval(ws._heartbeat);
        // 自动重连
        wsReconnectTimer = setTimeout(initWebSocket, 3000);
    };

    ws.onerror = () => ws.close();
}

function setWsStatus(connected) {
    const dot = document.getElementById('ws-status');
    const text = document.getElementById('ws-text');
    dot.className = `ws-dot ${connected ? 'ws-connected' : 'ws-disconnected'}`;
    text.textContent = connected ? '已连接' : '连接中...';
}

/* ===== 状态更新 ===== */

function updateAllPrinters(data) {
    for (const [serial, status] of Object.entries(data)) {
        updatePrinterCard(serial, status);
        updateCameraBadge(serial, status.state);
    }
}

function updatePrinterCard(serial, s) {
    // 状态徽章
    const badge = document.getElementById(`badge-${serial}`);
    const cameraBadge = document.getElementById(`state-badge-${serial}`);
    if (badge) {
        badge.className = `state-badge badge-${s.state.toLowerCase()}`;
        badge.textContent = s.state;
    }
    if (cameraBadge) cameraBadge.textContent = s.state;

    // 进度
    const prog = document.getElementById(`progress-${serial}`);
    const progText = document.getElementById(`progress-text-${serial}`);
    if (prog) prog.style.width = `${s.progress}%`;
    if (progText) progText.textContent = s.progress > 0 ? `${s.progress}%` : '--';

    // 温度
    setText(`nozzle-${serial}`, `${s.nozzle_temp.toFixed(0)} / ${s.nozzle_target_temp.toFixed(0)}°C`);
    setText(`bed-${serial}`, `${s.bed_temp.toFixed(0)} / ${s.bed_target_temp.toFixed(0)}°C`);
    setText(`chamber-${serial}`, `${s.chamber_temp.toFixed(0)}°C`);

    // 剩余时间
    const rem = document.getElementById(`remaining-${serial}`);
    if (rem) {
        if (s.remaining_time > 0) {
            const h = Math.floor(s.remaining_time / 60);
            const m = s.remaining_time % 60;
            rem.textContent = h > 0 ? `${h}h ${m}m` : `${m}m`;
        } else {
            rem.textContent = '--';
        }
    }

    // 层数（云端API不一定返回total_layers，有current_layer就显示）
    setText(`layers-${serial}`,
        s.current_layer > 0 ? `${s.current_layer}${s.total_layers > 0 ? ' / ' + s.total_layers : ''}` : '--');

    // 文件名
    const file = document.getElementById(`file-${serial}`);
    if (file) {
        file.textContent = s.gcode_file || '--';
        file.title = s.gcode_file || '';
    }
}

function updateCameraBadge(serial, state) {
    const el = document.getElementById(`state-badge-${serial}`);
    if (el) el.textContent = state;
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

/* ===== 产能饱和度加载 ===== */

async function loadTodayUsage() {
    try {
        const resp = await fetch('/api/usage/today');
        const items = await resp.json();
        items.forEach(item => updateUtilizationRow(item));
    } catch (e) { console.error('加载今日产能失败:', e); }
}

function updateUtilizationRow(item) {
    const pct = item.utilization_percent;
    const pctEl = document.getElementById(`util-pct-${item.printer_serial}`);
    const barEl = document.getElementById(`util-bar-${item.printer_serial}`);
    const timeEl = document.getElementById(`util-time-${item.printer_serial}`);

    if (pctEl) pctEl.textContent = `${pct.toFixed(1)}%`;
    if (barEl) {
        barEl.style.width = `${Math.min(pct, 100)}%`;
        barEl.className = `util-bar ${getUtilBarColor(pct)}`;
    }
    if (timeEl) timeEl.textContent = item.print_time_display;
}

function getUtilBarColor(pct) {
    if (pct < 60) return 'util-bar-green';
    if (pct < 85) return 'util-bar-yellow';
    return 'util-bar-red';
}

// 定时刷新今日产能（每30秒）
setInterval(loadTodayUsage, 30000);
