/* charts.js - Chart.js 月度产能折线图 */

let monthlyChart = null;

// 打印机颜色方案
const CHART_COLORS = [
    '#4f8cff',  // 蓝
    '#22c55e',  // 绿
    '#eab308',  // 黄
    '#ef4444',  // 红
    '#a855f7',  // 紫
];

function initMonthlyChart() {
    const ctx = document.getElementById('monthly-chart');
    if (!ctx) return;

    monthlyChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false,
            },
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        color: '#8b8fa8',
                        padding: 16,
                        usePointStyle: true,
                        pointStyle: 'line',
                    },
                },
                tooltip: {
                    backgroundColor: '#1a1d27',
                    titleColor: '#e4e6f0',
                    bodyColor: '#e4e6f0',
                    borderColor: '#2e3348',
                    borderWidth: 1,
                    padding: 12,
                    callbacks: {
                        label: function(ctx) {
                            return `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)}%`;
                        }
                    }
                },
                annotation: undefined,
            },
            scales: {
                x: {
                    grid: { color: '#2e3348' },
                    ticks: {
                        color: '#8b8fa8',
                        maxTicksLimit: 10,
                        font: { size: 11 },
                    },
                },
                y: {
                    min: 0,
                    max: 100,
                    grid: { color: '#2e3348' },
                    ticks: {
                        color: '#8b8fa8',
                        callback: v => `${v}%`,
                        stepSize: 20,
                    },
                    title: {
                        display: true,
                        text: '产能饱和度 (%)',
                        color: '#8b8fa8',
                    },
                },
            },
        },
    });

    // 添加 100% 满产能参考线（使用插件或手动绘制）
    drawFullCapacityLine();
}

function drawFullCapacityLine() {
    if (!monthlyChart) return;

    // 使用 Chart.js 内置 annotation 方式（简单方案：用一条虚线数据集）
    // 我们通过在 loadMonthlyUsage 中添加一个固定数据集来实现
}

async function loadMonthlyUsage() {
    try {
        const resp = await fetch('/api/usage/monthly?days=30');
        const monthlyData = await resp.json();
        updateMonthlyChart(monthlyData);
    } catch (e) {
        console.error('加载月度数据失败:', e);
    }
}

function updateMonthlyChart(monthlyData) {
    if (!monthlyChart || !monthlyData.length) return;

    // 跟随用户在摄像头区域拖拽后的排序偏好（sortable.js 提供，可选依赖）
    if (typeof sortMonthlyDataByPreference === 'function') {
        monthlyData = sortMonthlyDataByPreference(monthlyData);
    }

    // 从第一台打印机提取日期标签
    const labels = monthlyData[0].points.map(p => {
        const d = new Date(p.date);
        return `${d.getMonth() + 1}/${d.getDate()}`;
    });

    const datasets = monthlyData.map((printer, idx) => ({
        label: printer.printer_name,
        data: printer.points.map(p => p.utilization_percent),
        borderColor: CHART_COLORS[idx % CHART_COLORS.length],
        backgroundColor: CHART_COLORS[idx % CHART_COLORS.length] + '20',
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: CHART_COLORS[idx % CHART_COLORS.length],
        tension: 0.3,
        fill: false,
    }));

    // 添加 100% 满产能参考线
    datasets.push({
        label: '满产能 (100%)',
        data: labels.map(() => 100),
        borderColor: '#6b7280',
        borderWidth: 1,
        borderDash: [6, 4],
        pointRadius: 0,
        pointHoverRadius: 0,
        fill: false,
    });

    monthlyChart.data.labels = labels;
    monthlyChart.data.datasets = datasets;
    monthlyChart.update('none');
}

// 每小时刷新一次月度数据
setInterval(loadMonthlyUsage, 3600000);
