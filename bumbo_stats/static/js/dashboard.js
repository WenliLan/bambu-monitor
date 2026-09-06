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
            } else if (msg.type === 'ai_alarm') {
                showAiAlarm(msg);
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

/* ===== AI 异常报警横幅 ===== */

function showAiAlarm(msg) {
    const container = document.getElementById('ai-alarm-container');
    if (!container) return;

    const alarm = document.createElement('div');
    alarm.className = 'ai-alarm-banner';
    alarm.innerHTML = `
        <span class="ai-alarm-icon">🚨</span>
        <span class="ai-alarm-text">
            <strong>AI 异常报警</strong> — ${msg.printer_name}
            | 异常概率 ${(msg.probability * 100).toFixed(1)}%
            | ${msg.time}
        </span>
        <button class="ai-alarm-close" onclick="this.parentElement.remove()">&times;</button>
    `;
    container.appendChild(alarm);

    // 10 秒后自动消失
    setTimeout(() => {
        if (alarm.parentElement) alarm.remove();
    }, 10000);
}

/* ===== AI 异常检测弹窗 ===== */

async function detectAllPrinters() {
    const btn = document.getElementById('ai-detect-all-btn');
    if (!btn || btn.disabled) return;

    // 从 DOM 获取序列号和名称
    const printers = [];
    document.querySelectorAll('.camera-card').forEach(card => {
        const resultEl = card.querySelector('.ai-result');
        const nameEl = card.querySelector('.camera-name');
        if (resultEl && nameEl) {
            const serial = resultEl.id.replace('ai-result-', '');
            if (serial) printers.push({ serial, name: nameEl.textContent });
        }
    });

    if (printers.length === 0) {
        console.warn('detectAllPrinters: 未找到打印机');
        return;
    }

    btn.disabled = true;
    btn.textContent = '检测中...';

    // 创建弹窗
    const overlay = document.createElement('div');
    overlay.id = 'ai-modal-overlay';
    overlay.onclick = function(e) {
        if (e.target === overlay) closeAiModal();
    };
    overlay.innerHTML = `
        <div class="ai-modal" id="ai-modal">
            <div class="ai-modal-header">
                <span class="ai-modal-title">AI 异常检测</span>
                <button class="ai-modal-close" onclick="closeAiModal()">&times;</button>
            </div>
            <div class="ai-modal-body">
                <div class="ai-progress-bar">
                    <div class="ai-progress-fill" id="ai-progress-fill" style="width: 0%"></div>
                </div>
                <div class="ai-progress-text" id="ai-progress-text">准备中...</div>
                <div class="ai-results" id="ai-results"></div>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);

    const progressFill = document.getElementById('ai-progress-fill');
    const progressText = document.getElementById('ai-progress-text');
    const resultsEl = document.getElementById('ai-results');

    // 逐个请求（显示进度）
    for (let i = 0; i < printers.length; i++) {
        const { serial, name } = printers[i];
        const pct = Math.round((i / printers.length) * 100);
        progressFill.style.width = `${pct}%`;
        progressText.textContent = `检测中 (${i + 1}/${printers.length}): ${name}`;

        let result, error;
        try {
            const resp = await fetch(`/api/ai/detect/${serial}`, { method: 'POST' });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({ detail: 'Unknown error' }));
                error = err.detail || resp.statusText;
            } else {
                result = await resp.json();
            }
        } catch (e) {
            error = e.message;
        }

        // 添加结果行
        const row = document.createElement('div');
        if (error) {
            row.className = 'ai-result-row error';
            row.innerHTML = `
                <span class="ai-result-icon">⚠️</span>
                <span class="ai-result-name">${name}</span>
                <span class="ai-result-detail">${error}</span>
            `;
        } else {
            const isAlarm = result.anomaly;
            const prob = (result.probability * 100).toFixed(1);
            const threshold = (result.threshold * 100).toFixed(0);
            const ms = result.ms.toFixed(0);
            const state = result.printer_state || '--';
            const imgSrc = result.image ? `data:image/jpeg;base64,${result.image}` : '';
            
            row.className = `ai-result-row ${isAlarm ? 'alarm' : 'ok'}`;
            row.innerHTML = `
                <div class="ai-result-content">
                    <div class="ai-result-info">
                        <span class="ai-result-icon">${isAlarm ? '🔴' : '🟢'}</span>
                        <span class="ai-result-name">${name}</span>
                        <span class="ai-result-state">${state}</span>
                        <span class="ai-result-detail">
                            ${isAlarm ? '异常' : '正常'} P=${prob}%
                            <span class="ai-result-meta">(阈值=${threshold}%, ${ms}ms)</span>
                        </span>
                    </div>
                    ${imgSrc ? `<div class="ai-result-image"><img src="${imgSrc}" alt="${name}"></div>` : ''}
                </div>
            `;
        }
        resultsEl.appendChild(row);
    }

    // 完成
    progressFill.style.width = '100%';
    progressText.textContent = '检测完成';

    btn.disabled = false;
    btn.textContent = '调用异常检测';
}

function closeAiModal() {
    const overlay = document.getElementById('ai-modal-overlay');
    if (overlay) overlay.remove();
}
