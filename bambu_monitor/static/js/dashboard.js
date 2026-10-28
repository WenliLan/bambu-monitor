/* dashboard.js - WebSocket 客户端和 UI 更新 */

let ws = null;
let wsReconnectTimer = null;

function initWebSocket() {
    // 发起连接前先进入“连接中”态：显示遮罩 + 虚化内容，避免用户把尚未
    // 同步的旧数据误认为已连接（首屏与每次重连都会经过这里）。
    setWsStatus(false);
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
    // 未连接成功时给 <body> 加 .ws-connecting：CSS 据此显示居中提示框并虚化
    // 主内容，突出“暂未连接成功”；连接成功后移除，恢复正常。
    document.body.classList.toggle('ws-connecting', !connected);
}

/* ===== 状态更新 ===== */

function updateAllPrinters(data) {
    for (const [serial, status] of Object.entries(data)) {
        // 运行中新增的打印机不会出现在本页已渲染的 DOM 里：提示用户刷新，
        // 而不是让这台机器的卡片/提示音永远静默（P2-19）
        maybeShowUnknownPrinterBanner(serial);
        updatePrinterCard(serial, status);
        updateCameraBadge(serial, status.state);
        // 提示音与 DOM 更新解耦：即使卡片尚未渲染（新增打印机未刷新页面），
        // 也能根据数据层状态变化正常提醒（P2-19）
        maybeChimeStateChange(serial, status.state);
    }
}

let _unknownPrinterBanner = null;

/**
 * 当前页面没有该 serial 的卡片时，在右下角提示刷新。
 * 只提示一次，避免每 30 秒状态批次重复插入。
 */
function maybeShowUnknownPrinterBanner(serial) {
    if (_unknownPrinterBanner) return;
    const hasBadge = document.getElementById(`state-badge-${serial}`);
    const hasCard = document.querySelector(
        `.camera-card[data-serial="${CSS.escape(serial)}"]`
    );
    if (hasBadge || hasCard) return;

    const banner = document.createElement('div');
    banner.id = 'unknown-printer-banner';
    banner.className = 'unknown-printer-banner';

    const text = document.createElement('span');
    text.textContent = '检测到新增打印机，当前页面尚未渲染其卡片。';

    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = '刷新页面';
    button.addEventListener('click', () => location.reload());

    banner.append(text, button);
    document.body.appendChild(banner);
    _unknownPrinterBanner = banner;
}

function updatePrinterCard(serial, s) {
    // 状态徽章（打印机状态卡片右上角）
    const badge = document.getElementById(`badge-${serial}`);
    if (badge) {
        badge.className = `state-badge badge-${s.state.toLowerCase()}`;
        badge.textContent = s.state;
    }
    // 摄像头角标同样跟随状态变色（复用同一套颜色族）
    updateCameraBadge(serial, s.state);

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
    if (!el) return;
    // 与“打印机状态”卡片共用颜色族：camera-state 保留角标外观，
    // state-badge + badge-{state小写} 提供状态对应的背景/文字色。
    el.className = `camera-state state-badge badge-${state.toLowerCase()}`;
    el.textContent = state;

    // 非 running 状态：把运行状态字体放大并居中覆盖在监控画面中间，
    // 突出“已完成/暂停/离线”等需要关注的状态；running 时回到左上角
    // 固定小角标（.camera-overlay 默认位置），不遮挡画面。
    const overlay = el.closest('.camera-overlay');
    if (overlay) {
        overlay.classList.toggle('camera-state-center', state.toLowerCase() !== 'running');
    }
}

/* ===== 状态变化提示音（跨浏览器/移动端兼容） ===== */

const lastPrinterStates = {};
// 只在“打印中 → 需要关注”的转换时鸣响；IDLE/UNKNOWN 等中间态不重复提醒
const CHIME_FROM = 'RUNNING';
const CHIME_TO = new Set(['PAUSE', 'FINISH', 'FAILED', 'OFFLINE']);

function maybeChimeStateChange(serial, state) {
    if (!state) return;
    const normalized = String(state).toUpperCase();
    // UNKNOWN 是云端断流/重启首包的占位态：透传，不覆盖上一次真实状态，
    // 避免 RUNNING → UNKNOWN → FINISH 连响两次（P2-19）
    if (normalized === 'UNKNOWN') return;
    const prev = lastPrinterStates[serial];
    lastPrinterStates[serial] = normalized;
    if (prev === undefined) return;  // 首次看到只记录，刷新页面不鸣响
    if (prev === CHIME_FROM && CHIME_TO.has(normalized)) {
        playStateChime();
    }
}

/* ===== 提示音实现 ===== */
let _audioCtx = null;
let _chimeAudio = null;

// iOS 检测：iPhone/iPod UA，或 iPadOS 13+（伪装成 MacIntel 但带多点触控）。
// iOS Safari 的 Web Audio 输出受静音拨片屏蔽（静音时永远不出声），
// 必须降级到 HTML5 <audio>（媒体通道，不受静音拨片影响）。
function isIOSLike() {
    const ua = navigator.userAgent || '';
    if (/iP(hone|ad|od)/.test(ua)) return true;
    return navigator.platform === 'MacIntel' && (navigator.maxTouchPoints || 0) > 1;
}

function playStateChime() {
    try {
        if (isIOSLike()) {
            playChimeHtmlAudio();
            return;
        }
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) {
            playChimeHtmlAudio();  // 无 Web Audio 的老浏览器降级
            return;
        }
        _audioCtx = _audioCtx || new AC();
        if (_audioCtx.state === 'suspended') {
            // resume() 是异步的：在回调里发声，否则“恰好在 suspended
            // 状态发生的那次状态变化”会被同步检查吐掉（首次漏报）。
            _audioCtx.resume().then(() => emitOscillatorChime()).catch(() => {});
            return;
        }
        emitOscillatorChime();
    } catch (e) {
        // 发声失败不影响状态更新
        console.warn('状态提示音播放失败:', e);
    }
}

// Web Audio 合成双音“叮—咚”：两个短正弦波错峰发声，带包络避免爆音
function emitOscillatorChime() {
    try {
        if (!_audioCtx || _audioCtx.state !== 'running') return;
        const t0 = _audioCtx.currentTime;
        [[880, 0, 0.16], [660, 0.20, 0.22]].forEach(([freq, delay, dur]) => {
            const osc = _audioCtx.createOscillator();
            const gain = _audioCtx.createGain();
            osc.type = 'sine';
            osc.frequency.value = freq;
            gain.gain.setValueAtTime(0.0001, t0 + delay);
            gain.gain.exponentialRampToValueAtTime(0.25, t0 + delay + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.0001, t0 + delay + dur);
            osc.connect(gain).connect(_audioCtx.destination);
            osc.start(t0 + delay);
            osc.stop(t0 + delay + dur + 0.01);
        });
    } catch (e) {
        console.warn('状态提示音播放失败:', e);
    }
}

// HTML5 <audio> 提示音：运行时合成 16bit PCM WAV（880Hz→660Hz 双音）
// 转 data URI，不依赖外部音频文件；受安卓/iOS 媒体音量控制，且 iOS
// 上不受静音拨片屏蔽（走媒体通道）。
function makeChimeWavDataUri() {
    const rate = 8000;          // 8kHz 采样足够提示音清晰度，体积小
    const segs = [[880, 0.13], [660, 0.18]];
    const total = Math.floor(rate * 0.4);
    const pcm = new Int16Array(total);
    let idx = 0;
    for (const [freq, dur] of segs) {
        const n = Math.floor(rate * dur);
        for (let i = 0; i < n && idx < total; i++, idx++) {
            // 淡入淡出包络，避免起始咔哒声
            const env = Math.min(1, i / (rate * 0.01), (n - i) / (rate * 0.03));
            pcm[idx] = Math.sin(2 * Math.PI * freq * (i / rate)) * 12000 * env;
        }
        idx += Math.floor(rate * 0.04); // 两段间 40ms 间隙
    }
    const buf = new ArrayBuffer(44 + pcm.byteLength);
    const dv = new DataView(buf);
    const wstr = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
    wstr(0, 'RIFF'); dv.setUint32(4, 36 + pcm.byteLength, true); wstr(8, 'WAVE');
    wstr(12, 'fmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true);
    dv.setUint16(22, 1, true); dv.setUint32(24, rate, true);
    dv.setUint32(28, rate * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
    wstr(36, 'data'); dv.setUint32(40, pcm.byteLength, true);
    new Int16Array(buf, 44).set(pcm);
    let bin = '';
    const bytes = new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i += 4096) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 4096));
    }
    return 'data:audio/wav;base64,' + btoa(bin);
}

function playChimeHtmlAudio() {
    try {
        _chimeAudio = _chimeAudio || new Audio(makeChimeWavDataUri());
        _chimeAudio.currentTime = 0;
        const p = _chimeAudio.play();
        if (p && p.catch) p.catch(() => {});  // 自动播放被拦时静默降级
    } catch (e) {
        console.warn('HTML5 提示音播放失败:', e);
    }
}

// 用户首次交互时“解锁”音频（任意一种事件触发一次即可）：
// - 桌面 Chrome/Edge/Firefox：创建并 resume AudioContext（自动播放策略
//   要求先有用户手势，否则永远 suspended）；
// - iOS：在手势内 play 一次 <audio> 解锁媒体会话，后续才能自动出声。
function primeAudio() {
    try {
        if (isIOSLike()) {
            _chimeAudio = _chimeAudio || new Audio(makeChimeWavDataUri());
            const p = _chimeAudio.play();
            if (p && p.then) p.then(() => { _chimeAudio.pause(); _chimeAudio.currentTime = 0; }).catch(() => {});
            return;
        }
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        _audioCtx = _audioCtx || new AC();
        if (_audioCtx.state === 'suspended') _audioCtx.resume().catch(() => {});
    } catch (e) { /* ignore */ }
}

// pointerdown 覆盖现代桌面/移动浏览器；touchend/keydown 兑底老 iOS 与纯键盘环境
document.addEventListener('pointerdown', primeAudio, { once: true });
document.addEventListener('touchend', primeAudio, { once: true });
document.addEventListener('keydown', primeAudio, { once: true });

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
