"""测试前端模板渲染

测试用例:
  TC-TPL-01: base.html 包含必要的 CDN 引用
  TC-TPL-02: dashboard.html 渲染包含所有区域
  TC-TPL-03: 打印机配置正确传递到模板
  TC-TPL-04: 摄像头角标已具备 state-badge 钩子与初始状态类（状态色覆盖前提）
  TC-TPL-05: dashboard.css 含 camera-state.state-badge 特异性覆盖，避免角标被拉成胶囊
  TC-TPL-06: camera-card 具备 draggable="true" + data-serial（拖拽排序前提）
  TC-TPL-07: dashboard.html 引入 sortable.js 且会调用 applySavedOrder / initCameraSortable
  TC-TPL-08: sortable.js 含 drop 同步重排 + localStorage 持久化逻辑
  TC-TPL-09: dashboard.css 含拖拽状态视觉反馈（.dragging / .drag-over）
  TC-TPL-10: charts.js 在 updateMonthlyChart 中应用拖拽偏好顺序
  TC-TPL-11: dashboard.css 提供 .drag-placeholder 视觉与 .camera-grid 定位上下文
  TC-TPL-12: sortable.js 使用 placeholder 任意网格单元定位（不再依赖卡片 drag-over）
  TC-TPL-13: sortable.js 采用 swap 模式 + grid-row/col 锁定，避免 CSS Grid auto-flow 造成“自动连续排列”
  TC-TPL-14: sortable.js drop 后立即移除 .dragging，避免拖拽后变灰
  TC-TPL-15: dashboard.html 在 sortable.js 加载后立即同步调用 applySavedOrder，避免 paint 闪烁为紧凑排列
  TC-TPL-16: sortable.js commitDrop 内调用 syncPrinterCards，让打印机状态卡片跟随 swap 后的视觉顺序
  TC-TPL-17: dashboard.css 默认隐藏 camera-grid，.layout-ready 时可见，避免刷新闪烁为紧凑排列
  TC-TPL-18: sortable.js applySavedOrder 必须给 camera-grid 加 .layout-ready 类，且仪表代码不会永久隐藏
  TC-TPL-19: sortable.js 按“位置”保存持久化（{serial: [row, col]}），保留空格（2×2 / 空格拖拽）
  TC-TPL-20: sortable.js loadSavedOrder 兼容旧格式（数组 [serial, ...]）并自动迁移为位置对象
  TC-TPL-21: sortable.js applySavedOrder 能按位置对象恢复带空格的布局
  TC-TPL-22: 连接中遮罩——未连接时 base.html 有 .connect-overlay，dashboard.css 虚化主内容，dashboard.js 切换 body.ws-connecting
  TC-TPL-23: dashboard.js 打印机状态变化时播放提示音（Web Audio），首次见到不鸣响
  TC-TPL-24: 非 running 状态角标放大居中覆盖监控画面（CSS .camera-state-center），running 回左上角
"""

from pathlib import Path
import pytest
from jinja2 import Environment, FileSystemLoader


TEMPLATES_DIR = Path(__file__).parent.parent / "bambu_monitor" / "templates"


@pytest.fixture
def env():
    return Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))


class TestBaseTemplate:
    def test_tc_tpl_01_base_has_cdn(self):
        """TC-TPL-01: base.html 保留 Chart.js、移除未使用的 HTMX（P3-13）"""
        content = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
        assert "htmx.org" not in content, (
            "页面已无 hx-* 属性，不应再加载 HTMX（避免额外网络请求与版本漂移）"
        )
        assert "chart.js" in content.lower() or "chart.umd" in content.lower()
        assert "dashboard.css" in content
        assert "?v={{ asset_ver }}" in content, (
            "静态资源必须带版本号，升级后浏览器不会命中旧缓存"
        )



class TestDashboardTemplate:
    def test_tc_tpl_02_sections_present(self):
        """TC-TPL-02: dashboard.html 包含三大区域"""
        content = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
        # 摄像头区域
        assert "camera-grid" in content
        # 状态卡片
        assert "printer-cards" in content
        # 产能饱和度
        assert "utilization" in content.lower()
        # 月度折线图
        assert "monthly-chart" in content

    def test_tc_tpl_03_printer_loop(self):
        """TC-TPL-03: 模板循环渲染打印机"""
        content = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
        assert "{% for printer in printers %}" in content or "for printer in printers" in content
        assert "printer.serial" in content
        assert "printer.name" in content

    def test_tc_tpl_04_camera_state_has_state_badge_hook(self):
        """TC-TPL-04: 摄像头角标 class 含 state-badge 与初始 badge-offline

        只有当 HTML 中已经有 state-badge + badge-{state} 的类组合，
        前端 JS updateCameraBadge() 才能在状态变化时仅切 badge-* 部分，
        同时角标底色才会在渲染首帧就跟随离线状态着色（避免黑底闪现）。
        """
        content = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
        # 角标需要同时挂上 camera-state / state-badge / badge-offline 三个类
        assert 'class="camera-state state-badge badge-offline"' in content, (
            "摄像头角标必须同时携带 camera-state / state-badge / badge-offline，"
            "以便复用 .badge-* 颜色族并避免首次刷新为黑底"
        )

    def test_tc_tpl_05_css_camera_state_overrides_padding_and_radius(self):
        """TC-TPL-05: dashboard.css 提供 .camera-state.state-badge 特异性覆盖

        .state-badge 默认 padding: 3px 10px / border-radius: 12px (胶囊),
        若不加特异覆盖，角标会被拉成胶囊并偏离原紧凑外观。
        """
        css = (TEMPLATES_DIR.parent / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")
        assert ".camera-state.state-badge" in css, (
            "dashboard.css 必须提供 .camera-state.state-badge 选择器，"
            "覆写 .state-badge 默认的 padding 与 border-radius"
        )
        # 覆盖至少包含 padding 与 border-radius 两项
        idx = css.index(".camera-state.state-badge")
        snippet = css[idx:idx + 200]
        assert "padding" in snippet
        assert "border-radius" in snippet

    def test_tc_tpl_06_camera_card_draggable(self):
        """TC-TPL-06: 摄像头卡片同时具备 draggable="true" 与 data-serial

        两者缺一不可：
        - draggable 让浏览器响应拖拽手势
        - data-serial 是 sortable.js 同步打印机状态卡片的唯一键
        """
        content = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
        assert 'draggable="true"' in content, (
            "camera-card 必须设 draggable=\"true\" 才能被浏览器识别为可拖拽元素"
        )
        # data-serial 出现在 camera-card 的开标签内（不是 img 标签里）
        assert 'class="camera-card" draggable="true" data-serial="{{ printer.serial }}"' in content, (
            "camera-card 必须同时携带 draggable=\"true\" 与 data-serial=\"{{ printer.serial }}\""
        )

    def test_tc_tpl_07_dashboard_loads_sortable_js(self):
        """TC-TPL-07: dashboard.html 引入 sortable.js 并在初始化时调用核心函数

        仅引入 JS 不够：DOMContentLoaded 里必须调用 applySavedOrder（首屏读
        localStorage）与 initCameraSortable（注册拖拽事件）。
        """
        content = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
        assert '/static/js/sortable.js' in content, "dashboard.html 必须引入 sortable.js"
        assert 'applySavedOrder()' in content, "DOMContentLoaded 里必须调用 applySavedOrder() 读取用户偏好"
        assert 'initCameraSortable()' in content, "DOMContentLoaded 里必须调用 initCameraSortable() 注册拖拽事件"

    def test_tc_tpl_08_sortable_js_has_sync_and_persistence(self):
        """TC-TPL-08: sortable.js 实现 drop 后同步打印机卡片并持久化顺序"""
        js_path = TEMPLATES_DIR.parent / "static" / "js" / "sortable.js"
        assert js_path.exists(), "sortable.js 必须存在"
        js = js_path.read_text(encoding="utf-8")
        # localStorage 持久化
        assert 'localStorage' in js, "sortable.js 必须使用 localStorage 持久化用户排序偏好"
        # 同步打印机状态卡片：必须调用 reorderContainerBySerials 且使用 printer-cards 容器
        assert 'printer-cards' in js, "拖拽后必须同步重排 printer-cards 容器"
        assert 'reorderContainerBySerials' in js, "需要统一的容器重排工具"
        # drop 处理：使用 swap 模式（grid-row/grid-column），不再 insertBefore
        assert 'gridRow' in js and 'gridColumn' in js, (
            "drop 处理必须通过设置 inline gridRow/gridColumn 切换卡片位置（swap 模式）"
        )
        # 保存新顺序
        assert 'saveOrder' in js, "drop 后必须调用 saveOrder 持久化新顺序"

    def test_tc_tpl_09_css_has_drag_feedback(self):
        """TC-TPL-09: dashboard.css 提供 .dragging / .drag-over 视觉反馈"""
        css = (TEMPLATES_DIR.parent / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")
        assert '.camera-card.dragging' in css, "需要 .camera-card.dragging 反馈拖拽中卡片"
        assert '.camera-card.drag-over' in css, "需要 .camera-card.drag-over 反馈放置位置"
        assert 'cursor: grab' in css or 'cursor:grab' in css, "需要 cursor: grab 提示可拖拽"

    def test_tc_tpl_10_charts_follows_drag_preference(self):
        """TC-TPL-10: 趋势图数据集顺序跟随用户在摄像头区域的拖拽偏好

        charts.js 必须调用 sortable.js 提供的 sortMonthlyDataByPreference，
        否则用户拖拽后趋势图的图例颜色和数据点与摄像头/打印机卡片的视觉
        顺序不一致。
        """
        js_path = TEMPLATES_DIR.parent / "static" / "js" / "charts.js"
        js = js_path.read_text(encoding="utf-8")
        assert 'sortMonthlyDataByPreference' in js, (
            "charts.js#updateMonthlyChart 必须调用 sortMonthlyDataByPreference "
            "以让趋势图数据集顺序跟随用户拖拽偏好"
        )
        # 调用必须发生在 updateMonthlyChart 函数体内
        idx = js.index('function updateMonthlyChart')
        snippet = js[idx:idx + 800]
        assert 'sortMonthlyDataByPreference' in snippet

    def test_tc_tpl_11_placeholder_css_and_grid_relative(self):
        """TC-TPL-11: dashboard.css 提供 .drag-placeholder 样式与 .camera-grid 定位上下文

        为支持"拖到任意框位"：
        - .camera-grid 必须是 position: relative，否则 absolute placeholder 没有参照
        - .drag-placeholder 必须是 position: absolute，且 pointer-events: none 避免拦截拖拽事件
        """
        css = (TEMPLATES_DIR.parent / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")

        # 1. .camera-grid 必须是定位上下文
        grid_idx = css.index('.camera-grid {')
        grid_block = css[grid_idx:grid_idx + 600]
        assert 'position: relative' in grid_block, (
            ".camera-grid 必须设 position: relative 才能作为 .drag-placeholder 的定位参照"
        )

        # 2. .drag-placeholder 必备样式
        assert '.drag-placeholder' in css, "需要 .drag-placeholder 样式提供拖拽占位视觉"
        ph_idx = css.index('.drag-placeholder {')
        ph_block = css[ph_idx:ph_idx + 400]
        assert 'position: absolute' in ph_block, ".drag-placeholder 必须 absolute 定位"
        assert 'pointer-events: none' in ph_block, (
            ".drag-placeholder 必须 pointer-events: none，否则会拦截 drop 事件"
        )

    def test_tc_tpl_12_sortable_uses_placeholder_grid_index(self):
        """TC-TPL-12: sortable.js 用 placeholder 在任意网格单元定位

        从“自动连续排列”升级为“自由挪到任意框位置”：
        - 必须有 computeGridIndex（基于网格列/行计算逻辑索引）
        - 必须有 commitDrop（按 index 把卡片插入到对应位置）
        - 不能仍依赖 .drag-over 卡片高亮（已被 placeholder 取代）
        """
        js_path = TEMPLATES_DIR.parent / "static" / "js" / "sortable.js"
        js = js_path.read_text(encoding="utf-8")

        assert 'function computeGridIndex' in js, (
            "sortable.js 必须实现 computeGridIndex：基于鼠标坐标反算网格单元"
        )
        assert 'function updatePlaceholder' in js, (
            "sortable.js 必须实现 updatePlaceholder：移动 placeholder 到当前鼠标所在网格单元"
        )
        assert 'function commitDrop' in js, (
            "sortable.js 必须实现 commitDrop：按 placeholder 索引把卡片插入到对应位置"
        )
        # drop 处理不再依赖 target.closest('.camera-card') 的旧逻辑
        drop_idx = js.index("addEventListener('drop'")
        drop_block = js[drop_idx:drop_idx + 1200]
        assert 'commitDrop' in drop_block, "drop 处理必须委托 commitDrop 执行最终插入"
        assert "closest('.camera-card')" not in drop_block, (
            "drop 不再依赖 target.closest('.camera-card')，"
            "否则会回退到“仅能在卡片之间插入”的连续排列行为"
        )

    def test_tc_tpl_13_swap_mode_with_grid_row_col(self):
        """TC-TPL-13: sortable.js 采用 swap + 锁定 grid-row/col，避免 CSS Grid auto-flow

        现象：CSS Grid 默认 grid-auto-flow: row 会以 DOM 顺序紧凑填充，
        即使 insertBefore 修改了 DOM 顺序，视觉仍然“连续排列”。

        修复：
        1. 必须有 lockGridPositions：初始化/读 localStorage 后给每张 camera-card
           设置 inline gridRow/gridColumn（CSS Grid 旁路 auto-flow）
        2. 必须有 getGridOrderedSerials：syncPrinterCards / saveOrder 按视觉顺序
           而不是 DOM 顺序读取 serials
        3. commitDrop 采用 swap：dragSource 占 target，target 占 dragSource 原位；
           DOM 顺序不动，只改 inline gridRow/gridColumn
        """
        js_path = TEMPLATES_DIR.parent / "static" / "js" / "sortable.js"
        js = js_path.read_text(encoding="utf-8")

        # 1. lockGridPositions：锁定 gridRow/gridColumn
        assert 'function lockGridPositions' in js, (
            "sortable.js 必须实现 lockGridPositions，"
            "为每张 camera-card 锁定 inline gridRow/gridColumn（旁路 CSS Grid auto-flow）"
        )
        lock_idx = js.index('function lockGridPositions')
        lock_block = js[lock_idx:lock_idx + 600]
        assert 'gridRow' in lock_block and 'gridColumn' in lock_block, (
            "lockGridPositions 必须通过设置 card.style.gridRow / gridColumn 锁定位置"
        )

        # 2. getGridOrderedSerials：按视觉顺序读取 serials
        assert 'function getGridOrderedSerials' in js, (
            "sortable.js 必须实现 getGridOrderedSerials，"
            "syncPrinterCards/saveOrder 要按 grid-row/col 顺序而非 DOM 顺序读取"
        )
        grid_idx_fn = js.index('function getGridOrderedSerials')
        grid_block_fn = js[grid_idx_fn:grid_idx_fn + 400]
        assert 'parseInt' in grid_block_fn and 'gridRow' in grid_block_fn, (
            "getGridOrderedSerials 必须解析 inline gridRow/gridColumn 进行排序"
        )

        # 3. commitDrop：swap 模式
        commit_idx = js.index('function commitDrop')
        commit_block = js[commit_idx:commit_idx + 1500]
        # 读 sourceRow/sourceCol（dragSource 原位）
        assert 'sourceRow' in commit_block and 'sourceCol' in commit_block, (
            "commitDrop 必须读取 dragSource 当前 gridRow/gridColumn 作为原位"
        )
        # 找 targetCard（目标位置原占据的卡片）
        assert 'targetCard' in commit_block, (
            "commitDrop 必须查找目标位置原占据的卡片（targetCard）以便 swap"
        )
        # 写 targetCard 的 gridRow/gridColumn → targetCard 移到 dragSource 原位
        assert 'targetCard.style.gridRow' in commit_block and 'targetCard.style.gridColumn' in commit_block, (
            "commitDrop 必须把 targetCard.style.gridRow/gridColumn 设为 dragSource 原位（swap）"
        )
        # 不能再 insertBefore（会触发 auto-flow 重排）
        assert 'insertBefore' not in commit_block, (
            "commitDrop 不应再 insertBefore，DOM 顺序保持不变，位置由 gridRow/gridColumn 决定"
        )

        # 4. syncPrinterCards / saveOrder 走 getGridOrderedSerials（按视觉顺序）
        #    注意：syncPrinterCards(grid) 调用点也存在于 drop 事件处理里，
        #    只能依赖函数定义处的局部上下文。函数体内的 getGridOrderedSerials 调用
        #    会出现在函数定义之后、函数体结束之前。
        sync_fn_idx = js.index('function syncPrinterCards')
        # 取函数体（一直到 function getGridOrderedSerials 之前）
        next_fn_idx = js.index('function getGridOrderedSerials')
        sync_block = js[sync_fn_idx:next_fn_idx]
        assert 'getGridOrderedSerials' in sync_block, (
            "syncPrinterCards 函数体必须用 getGridOrderedSerials 读取 serials"
        )

    def test_tc_tpl_14_drop_clears_dragging_class(self):
        """TC-TPL-14: sortable.js drop 后立即移除 .dragging，避免拖拽后卡片变灰

        浏览器 HTML5 Drag API：drop 总会随后触发 dragend，但在某些场景下
        dragend 不一定按时触发（如跨 iframe / 被遮挡 / 拖拽到外部边界）。
        依赖 dragend 清理 .dragging 会导致卡片长期 opacity: 0.4 “变灰”。

        修复：drop 事件处理器必须在 commitDrop 之后立即调用
        ``dragSource.classList.remove('dragging')``，不依赖 dragend。
        """
        js_path = TEMPLATES_DIR.parent / "static" / "js" / "sortable.js"
        js = js_path.read_text(encoding="utf-8")

        # 取 drop 处理代码（commitDrop 后必须紧跟 .dragging 清理）
        drop_idx = js.index("addEventListener('drop'")
        drop_block = js[drop_idx:drop_idx + 1500]

        # 关键：commitDrop 后必须出现 classList.remove('dragging')
        assert "classList.remove('dragging')" in drop_block, (
            "drop 处理必须在 commitDrop 之后同步移除 .dragging class，"
            "不依赖 dragend（dragend 不可靠）"
        )
        # 不能在 drop 里把 dragSource 设为 null 后再等 dragend（再移除 bug .dragging）
        # commitDrop 后 `dragSource = null` 会导致 dragend 中 if (dragSource) 跳过清理。
        # 这里检查 drop 块是否在 classList.remove('dragging') 之后又把 dragSource 清空。
        clear_pos = drop_block.find("classList.remove('dragging')")
        null_pos = drop_block.find("dragSource = null", clear_pos)
        assert null_pos == -1, (
            "drop 中 classList.remove('dragging') 之后不应再 dragSource = null，"
            "否则 dragend 中的 if (dragSource) 会跳过清理逻辑（重新出问题）"
        )

    def test_tc_tpl_15_apply_saved_order_before_dom_content_loaded(self):
        """TC-TPL-15: dashboard.html 在 sortable.js 加载后立即同步调用 applySavedOrder

        为避免首次 paint 出现紧凑排列闪烁（CSS Grid auto-flow 在 JS 设置
        gridRow/Column 之前先 paint）：
        - applySavedOrder() 调用必须位于 sortable.js 加载后的同步代码中
        - 不能在 DOMContentLoaded 回调里（那会在 paint 之后才执行）
        """
        content = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")

        sortable_idx = content.find("/static/js/sortable.js")
        dcl_idx = content.find("addEventListener('DOMContentLoaded'")

        assert sortable_idx != -1, "must sortable.js script tag exists"
        assert dcl_idx != -1, "must DOMContentLoaded event listener exists"

        # 1. applySavedOrder() 调用必须出现在 sortable.js 加载之后
        apply_idx = content.find('applySavedOrder()')
        assert apply_idx != -1 and apply_idx > sortable_idx, (
            "applySavedOrder() 调用必须出现在 sortable.js 加载之后，"
            "否则函数未定义"
        )

        # 2. applySavedOrder() 调用必须出现在 DOMContentLoaded 之前
        #    （位于 sortable.js 加载后的同步脚本中，让 paint 之前设置好 gridRow/Column）
        assert apply_idx < dcl_idx, (
            "applySavedOrder() 必须同步调用（在 sortable.js 加载后、DOMContentLoaded 之前），"
            "避免 paint 时 CSS Grid auto-flow 渲染为紧凑排列闪烁"
        )

    def test_tc_tpl_16_commit_drop_syncs_printer_cards(self):
        """TC-TPL-16: sortable.js commitDrop 内调用 syncPrinterCards，让打印机状态卡片跟随 swap 后的视觉顺序

        需求：“打印机状态”模块要跟随“监控”模块的排列。
        技术上：camera-card 用 inline grid-row/col 锁定位置，printer-card 没有
        inline 样式（视觉顺序 = DOM 顺序）。拖拽 swap 后，必须立即调用
        syncPrinterCards 重新按 data-serial 顺序 append printer-cards /
        utilization-today 的子节点，让两者同步。

        验证要点：
        1. commitDrop 函数体（从 function commitDrop 到下一个 function 之前）
           内部必须出现 syncPrinterCards(grid) 调用。
        2. noop 分支（拖到自己身上）也必须调用 syncPrinterCards，避免边角场景漏同步。
        3. drop 处理中不能再次直接调用 syncPrinterCards（commitDrop 内已调用，
           重复调用会浪费一次 reorderContainerBySerials + loadMonthlyUsage）。
        """
        js_path = TEMPLATES_DIR.parent / "static" / "js" / "sortable.js"
        js = js_path.read_text(encoding="utf-8")

        # 取 commitDrop 函数体（到下一个 function 声明之前）
        commit_idx = js.index("function commitDrop")
        # 找下一个 "function " 声明以界定 commitDrop 结束
        next_fn_idx = js.index("\nfunction ", commit_idx + 1)
        commit_block = js[commit_idx:next_fn_idx]

        # 1. commitDrop 函数体内必须出现 syncPrinterCards(grid) 调用
        assert "syncPrinterCards(grid)" in commit_block, (
            "commitDrop 内必须调用 syncPrinterCards(grid)，"
            "让 printer-cards / utilization-today 跟随 swap 后的视觉顺序。"
            "否则拖拽后“打印机状态”卡片会停留在原始 DOM 顺序，"
            "与“监控”模块的视觉顺序不一致。"
        )

        # 2. 拖到自己身子的 noop 分支也必须同步一次（边角场景）
        #    在 commit_block 里搜 noop 分支关键字（targetRow/Col === sourceRow/Col）
        #    不强制要求两次调用，但至少要出现一次（测试目的：确认主分支同步）
        #    同时验证 noop 分支附近没有"return"先于 syncPrinterCards
        noop_marker = "sourceRow === targetRow && sourceCol === targetCol"
        if noop_marker in commit_block:
            noop_idx = commit_block.index(noop_marker)
            # 在 noop 分支中查找 syncPrinterCards 调用
            noop_section = commit_block[noop_idx:]
            assert "syncPrinterCards(grid)" in noop_section, (
                "拖到自己身子的 noop 分支内也必须调用 syncPrinterCards(grid)，"
                "以应对“刷新后首次拖拽到原位” 等边界场景下 printer-card 顺序不同步。"
            )

        # 3. drop 处理中不能再直接调用 syncPrinterCards（commitDrop 已负责同步）
        drop_idx = js.index("addEventListener('drop'")
        drop_block = js[drop_idx:drop_idx + 1500]
        # drop 处理应在 commitDrop(grid) 之后仅做：清理 .dragging + saveOrder。
        # 若 drop 中又出现 syncPrinterCards(grid)，说明逻辑重复。
        assert "syncPrinterCards(grid)" not in drop_block, (
            "drop 处理不应再直接调用 syncPrinterCards(grid)；"
            "commitDrop 内已负责同步 printer-cards / utilization-today，"
            "重复调用会多走一次 reorderContainerBySerials + loadMonthlyUsage，"
            "既浪费也增加未来维护负担。"
        )

    def test_tc_tpl_17_camera_grid_default_hidden_layout_ready_visible(self):
        """TC-TPL-17: dashboard.css 默认隐藏 camera-grid，.layout-ready 时可见，避免刷新闪烁

        背景：即使把 applySavedOrder 调用移到 sortable.js 加载后的同步脚本中，
        现代浏览器仍可能在 sortable.js 加载之前“投机 paint”，让用户看到 CSS Grid
        auto-flow 渲染出的“紧凑排列”闪烁，再被 JS 设置的 inline grid-row/col
        纠正。

        修复：
        1. .camera-grid 默认 visibility: hidden（卡片不显示，但占位空间仍保留）
        2. .camera-grid.layout-ready → visibility: visible
        3. sortable.js 的 applySavedOrder 在 lockGridPositions 之后给 grid 加 .layout-ready

        这样：浏览器首次 paint 时看到的只是占位空间（卡片不可见），JS 设置好
        inline grid-row/col 后才“出现”在正确位置，用户不会再看到 auto-flow 闪烁。
        """
        css_path = TEMPLATES_DIR.parent / "static" / "css" / "dashboard.css"
        css = css_path.read_text(encoding="utf-8")

        # 1. .camera-grid 默认 visibility: hidden
        grid_idx = css.index(".camera-grid {")
        # 取足够大的区间（visibility:hidden 在 CSS 块中可能隔几行出现，
        # 原 400 字符不足以覆盖含多行注释的情况）
        grid_block = css[grid_idx:grid_idx + 800]
        assert "visibility: hidden" in grid_block, (
            ".camera-grid 默认必须 visibility: hidden，避免 JS 设置 inline grid-row/col "
            "之前浏览器投机 paint 出 auto-flow 紧凑排列闪烁。"
            "如未设置，请检查 dashboard.css 是否被还原。"
        )

        # 2. .camera-grid.layout-ready → visibility: visible
        assert ".camera-grid.layout-ready" in css, (
            "需要 .camera-grid.layout-ready 规则配合 sortable.js 取消隐藏"
        )
        ready_idx = css.index(".camera-grid.layout-ready")
        ready_block = css[ready_idx:ready_idx + 200]
        assert "visibility: visible" in ready_block, (
            ".camera-grid.layout-ready 必须把 visibility 设为 visible，"
            "否则 sortable.js 加 .layout-ready 类后 camera-grid 仍不可见"
        )

    def test_tc_tpl_18_apply_saved_order_adds_layout_ready_class(self):
        """TC-TPL-18: sortable.js applySavedOrder 必须给 camera-grid 添加 .layout-ready 类

        验证 sortable.js 的 applySavedOrder 末尾（不依赖前置逻辑是否成功）都会
        给 camera-grid 添加 .layout-ready，使 dashboard.css 中默认的
        visibility: hidden 被取消。

        为保证异常下也不会出现“永久隐藏”，应在 finally 块里加 .layout-ready。
        """
        js_path = TEMPLATES_DIR.parent / "static" / "js" / "sortable.js"
        js = js_path.read_text(encoding="utf-8")

        # 1. applySavedOrder 函数体必须出现 layout-ready 字样
        apply_idx = js.index("function applySavedOrder")
        # 找下一个 function / top-level 声明结束位置
        next_fn_idx = js.index("\nfunction ", apply_idx + 1)
        apply_block = js[apply_idx:next_fn_idx]
        assert "layout-ready" in apply_block, (
            "applySavedOrder 必须给 camera-grid 添加 layout-ready 类，"
            "让 dashboard.css 中默认的 visibility: hidden 被取消。"
            "否则刷新后即使定位正确，用户也看不到卡片。"
        )
        assert "classList.add('layout-ready')" in apply_block, (
            "applySavedOrder 必须用 classList.add('layout-ready') 添加类名，"
            "而非 setAttribute 或直接操作 style（确保 CSS 选择器能命中）。"
        )

        # 2. layout-ready 添加必须在 finally 块里，避免抛错时永久隐藏
        assert "} finally {" in apply_block, (
            "applySavedOrder 必须把 layout-ready 的添加放在 finally 块里，"
            "保证 lockGridPositions / syncPrinterCards 抛错时仍能取消隐藏。"
        )
        # finally 块内必须有 layout-ready
        finally_idx = apply_block.index("} finally {")
        finally_section = apply_block[finally_idx:]
        assert "layout-ready" in finally_section, (
            "finally 块里必须有 classList.add('layout-ready')，"
            "覆盖“锁定逻辑报错但 grid 应仍可见”的异常路径。"
        )

        # 3. dashboard.html 必须有兜底（500ms setTimeout），防止 sortable.js 加载失败
        content = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
        assert "layout-ready" in content, (
            "dashboard.html 必须包含 layout-ready 兑底逻辑，"
            "在 sortable.js 加载失败 / applySavedOrder 未调用时也能保证可见。"
        )
        # 兑底是 setTimeout
        assert "setTimeout" in content and "layout-ready" in content, (
            "兑底应使用 setTimeout + classList.add('layout-ready')，"
            "在主逻辑失败时延迟兑底。"
        )


    def test_tc_tpl_19_save_order_uses_position_object_with_gaps(self):
        """TC-TPL-19: sortable.js 按"位置"保存持久化，保留空格

        用户拖出 2×2 布局（如 [A,B,_]/[C,D,_]）后刷新，仍要保留空格。

        实现要点：
        1. saveOrder 接受 positions 对象 {serial: [row, col], ...}，
           而不是只接受 serials 数组。
        2. drop 处理调用 saveOrder(getCurrentPositions(grid))，
           后者从 inline gridRow/gridColumn 推导每个 serial 的 (row, col)。
        3. localStorage 中保存的是对象（不是数组），Object 形如
           {"A": [1,1], "B": [1,2], "C": [2,1], "D": [2,2]}，
           其中 (1,3)/(2,3) 故意不出现，对应"空格"。
        """
        js_path = TEMPLATES_DIR.parent / "static" / "js" / "sortable.js"
        js = js_path.read_text(encoding="utf-8")

        # 1. saveOrder 必须接受 positions 对象
        save_idx = js.index("function saveOrder")
        next_fn_idx = js.index("\nfunction ", save_idx + 1)
        save_block = js[save_idx:next_fn_idx]
        assert "positions" in save_block, (
            "saveOrder 必须接受 positions 参数（位置表对象），"
            "以便持久化含空格的布局"
        )
        # 保存时走 JSON.stringify(cleaned)，cleaned 是对象
        assert "JSON.stringify" in save_block, (
            "saveOrder 必须用 JSON.stringify 序列化 cleaned 对象"
        )

        # 2. drop 处理中调用 saveOrder(getCurrentPositions(grid))
        drop_idx = js.index("addEventListener('drop'")
        drop_block = js[drop_idx:drop_idx + 1800]
        assert "saveOrder" in drop_block, "drop 处理必须调用 saveOrder 持久化新位置"
        assert "getCurrentPositions" in drop_block, (
            "drop 处理必须调用 getCurrentPositions 读出每张卡片的 (row, col) 再 saveOrder"
        )

        # 3. 必须有 getCurrentPositions(grid) 函数定义，且读 inline gridRow/gridColumn
        assert "function getCurrentPositions" in js, (
            "sortable.js 必须实现 getCurrentPositions(grid)，"
            "从每张 camera-card 的 inline gridRow/gridColumn 推导位置表"
        )
        get_idx = js.index("function getCurrentPositions")
        get_block = js[get_idx:get_idx + 500]
        assert "gridRow" in get_block and "gridColumn" in get_block, (
            "getCurrentPositions 必须读取 card.style.gridRow / gridColumn"
        )

    def test_tc_tpl_20_load_saved_order_backward_compatible_array(self):
        """TC-TPL-20: sortable.js loadSavedOrder 兼容旧格式（数组），自动迁移为位置对象

        背景：旧版 saveOrder 写入的是 [serial, ...] 数组；新版改成对象。
        升级时不能让老用户的偏好"丢失"或"变成紧凑排列"。

        兼容契约：
        - JSON.parse 后是数组：按顺序填入 (row, col)（紧凑填充，不留空格）
          然后以对象形式返回 → 调用方下次保存时自然变对象格式。
        - JSON.parse 后是对象：逐项验证 pos 是 [row, col] 数字数组；非法项丢弃。
        - 解析异常 / 非对象 / 空对象：返回 null。
        """
        js_path = TEMPLATES_DIR.parent / "static" / "js" / "sortable.js"
        js = js_path.read_text(encoding="utf-8")

        # 1. loadSavedOrder 函数定义
        load_idx = js.index("function loadSavedOrder")
        next_fn_idx = js.index("\nfunction ", load_idx + 1)
        load_block = js[load_idx:next_fn_idx]

        # 2. 兼容分支：Array.isArray(parsed)
        assert "Array.isArray" in load_block, (
            "loadSavedOrder 必须用 Array.isArray 判断旧格式（数组 vs 对象）"
        )
        # 数组分支必须把每个 serial 翻译成 [row, col]（按 GRID_COLS 计算）
        assert "GRID_COLS" in load_block and "Math.floor" in load_block, (
            "数组分支必须用 GRID_COLS 把旧序列压缩填入 (row, col)，"
            "且 row 用 Math.floor(idx / GRID_COLS) 计算"
        )

        # 3. 对象分支：Object.entries 验证每项是 [row, col] 数字数组
        assert "Object.entries" in load_block, (
            "对象分支必须用 Object.entries 遍历位置表验证每一项"
        )
        assert "Number.isFinite" in load_block, (
            "对象分支必须用 Number.isFinite 校验 row/col 是有限数字"
        )

        # 4. 解析异常兜底：try/catch + 返回 null
        assert "try" in load_block and "catch" in load_block, (
            "loadSavedOrder 必须 try/catch 包裹 JSON.parse，避免损坏的存储抛错"
        )
        assert "return null" in load_block, (
            "loadSavedOrder 必须 return null 表示读取失败 / 空，"
            "与 sortMonthlyDataByPreference 的 null 检查契约一致"
        )

    def test_tc_tpl_21_apply_saved_order_restores_gap_layout(self):
        """TC-TPL-21: sortable.js applySavedOrder 能按位置对象恢复带空格的布局

        用户场景：4 张卡拖成 [A,B,_]/[C,D,_]，刷新后 saved = {"A":[1,1],"B":[1,2],"C":[2,1],"D":[2,2]}。
        applySavedOrder 必须把 A/B/C/D 还原到对应 (row, col)，(1,3)/(2,3) 留空。

        实现要求：
        1. 用 placedKeys Set 跟踪已占位 (row, col)，避免冲突；
        2. 未落入 saved 的卡片（后加的打印机）从末尾连续填充并跳过已占位位置；
        3. 不依赖 DOM 顺序 compact 填入（否则空格被自动填补）。
        """
        js_path = TEMPLATES_DIR.parent / "static" / "js" / "sortable.js"
        js = js_path.read_text(encoding="utf-8")

        # 1. applySavedOrder 必须定义
        apply_idx = js.index("function applySavedOrder")
        next_fn_idx = js.index("\nfunction ", apply_idx + 1)
        apply_block = js[apply_idx:next_fn_idx]

        # 2. placedKeys 跟踪已占位 (row, col)
        assert "placedKeys" in apply_block, (
            "applySavedOrder 必须用 placedKeys Set 跟踪已占位位置，"
            "确保 saved 中缺失的 (row, col) 不被填补（A→(1,1),B→(1,2) 后 (1,3) 留空）"
        )

        # 3. 未落入 saved 的卡片（remaining）需要"跨过"已占位位置
        assert "remaining" in apply_block, (
            "applySavedOrder 必须把\u201c未落入 saved\u201d 的卡片收集到 remaining，"
            "再从末尾连续填充并跳过 placedKeys"
        )
        # 跳过条件：placedKeys.has(`${row},${col}`)
        assert "while" in apply_block and "placedKeys.has" in apply_block, (
            "remaining 填充时必须 while (placedKeys.has(`${row},${col}`)) 跨过空格"
        )

        # 4. 反例：不能直接紧凑填入（不允许 col = (nextIdx % GRID_COLS) + 1 在 saved 分支里）
        #    旧的 lockGridPositions 紧凑逻辑仍然允许存在（用于 saved 为空 / remaining 路径），
        #    但 saved 分支必须靠 placedKeys 锁定位置。
        # 验证 placedKeys 在 saved 分支已被使用
        assert apply_block.count("placedKeys.has") >= 2, (
            "placedKeys.has 至少出现 2 次：一次在 saved 分支防冲突，"
            "一次在 remaining 填充时跨过空格"
        )

    def test_tc_tpl_22_connecting_overlay_blurs_content(self):
        """TC-TPL-22: 连接中遮罩——未连接时高亮提示 + 虚化主内容

        需求：在网页端“连接中”（WebSocket 未连接成功）时，在界面中间
        显示一个大提示框，并虚化其他内容，避免用户误以为已连接。

        实现契约：
        1. base.html 默认含 .connect-overlay（内含中央提示框），默认隐藏，
           由 CSS 控制可见（不能写死在内联 style，否则设置页也会高亮）。
        2. dashboard.css：body.ws-connecting 时显示 .connect-overlay 并给
           main.container 加 filter: blur 虚化；遮罩 z-index 低于 navbar（保证可导航）。
        3. dashboard.js：setWsStatus(connected) 必须根据 !connected 切换
           body 的 ws-connecting 类（未连接 → 加，已连接 → 去）。
        """
        base = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
        css = (TEMPLATES_DIR.parent / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")
        js = (TEMPLATES_DIR.parent / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")

        # 1. base.html 含连接遮罩与提示框
        assert "connect-overlay" in base, (
            "base.html 必须含 .connect-overlay 遮罩容器，用于未连接时展示中央提示框"
        )
        assert "connect-dialog" in base, (
            ".connect-overlay 内部需有居中的 .connect-dialog 提示框"
        )
        # 提示框默认隐藏不能靠内联 style="display:flex"（否则设置页也会高亮），
        # 必须由 CSS 的 body.ws-connecting 选择器控制。
        overlay_idx = base.index("connect-overlay")
        overlay_tag = base[overlay_idx - 60:overlay_idx + 120]
        assert "display:flex" not in overlay_tag.replace(" ", ""), (
            "connect-overlay 默认应隐藏（由 CSS body.ws-connecting 控制显示），"
            "不能写死内联 display:flex，否则共享 base.html 的设置页也会被遮挡"
        )

        # 2. dashboard.css 的虚化与显示规则
        assert "body.ws-connecting .connect-overlay" in css, (
            "dashboard.css 必须在 body.ws-connecting 下显示 .connect-overlay"
        )
        # 主内容虚化：filter: blur
        blur_idx = css.index("body.ws-connecting main.container")
        blur_block = css[blur_idx:blur_idx + 200]
        assert "filter" in blur_block and "blur" in blur_block, (
            "body.ws-connecting 必须给 main.container 加 filter: blur 虚化主内容"
        )
        assert "pointer-events: none" in blur_block, (
            "虚化时必须禁用主内容交互（pointer-events: none），"
            "避免用户在未连接时误操作旧数据"
        )

        # 3. dashboard.js setWsStatus 切换 body.ws-connecting
        set_idx = js.index("function setWsStatus")
        set_block = js[set_idx:set_idx + 500]
        assert "ws-connecting" in set_block, (
            "setWsStatus 必须切换 body 的 ws-connecting 类以联动遮罩"
        )
        assert "classList.toggle" in set_block and "!connected" in set_block, (
            "setWsStatus 应用 classList.toggle('ws-connecting', !connected)："
            "未连接时加类、已连接时去类"
        )

    def test_tc_tpl_23_state_change_plays_chime(self):
        """TC-TPL-23: 任意打印机状态变化时播放提示音，提醒完成/异常

        契约：
        1. dashboard.js 维护 lastPrinterStates 映射，由 updateAllPrinters →
           maybeChimeStateChange 检测状态变化（与 DOM 解耦）；只有
           “RUNNING → PAUSE/FINISH/FAILED/OFFLINE”才调用 playStateChime；
        2. 首次看到某 serial（prev === undefined）只记录不鸣响，
           避免刷新页面时成排噪声；
        3. playStateChime 用 Web Audio（createOscillator）合成，不依赖音频文件；
           整体 try/catch，发声失败不影响状态更新。
        """
        js_path = TEMPLATES_DIR.parent / "static" / "js" / "dashboard.js"
        js = js_path.read_text(encoding="utf-8")

        assert "lastPrinterStates" in js, (
            "dashboard.js 必须维护 lastPrinterStates 记录每台打印机上次状态，"
            "用于检测状态变化"
        )
        assert "function playStateChime" in js, (
            "必须实现 playStateChime：用 Web Audio 合成提示音，不依赖音频文件"
        )
        chime_idx = js.index("function playStateChime")
        chime_block = js[chime_idx:chime_idx + 1400]
        assert "createOscillator" in chime_block, (
            "提示音必须由 AudioContext.createOscillator 合成"
        )
        assert "try" in chime_block and "catch" in chime_block, (
            "playStateChime 必须 try/catch：浏览器自动播放策略/不支持时"
            "静默降级，不能影响状态更新主流程"
        )

        # 状态变化检测已与 DOM 解耦：updateAllPrinters 对每台设备调用
        # maybeChimeStateChange，即使卡片尚未渲染（新增设备未刷新页面）也能提醒
        all_idx = js.index("function updateAllPrinters")
        all_block = js[all_idx:all_idx + 1200]
        assert "maybeChimeStateChange(serial, status.state)" in all_block, (
            "updateAllPrinters 必须对每台设备调用 maybeChimeStateChange，"
            "将提示音与 DOM 渲染解耦"
        )
        chime_state_idx = js.index("function maybeChimeStateChange")
        state_block = js[chime_state_idx:chime_state_idx + 1200]
        assert "playStateChime()" in state_block, (
            "maybeChimeStateChange 必须在状态变化时调用 playStateChime()"
        )
        assert "prev === undefined" in state_block, (
            "首次看到某 serial 只能记录状态，不得鸣响，"
            "避免刷新页面时所有卡片成排发声"
        )
        assert "const CHIME_FROM = 'RUNNING'" in js, (
            "必须以 RUNNING 作为鸣响起点"
        )
        assert "CHIME_TO.has(normalized)" in state_block, (
            "只有离开 RUNNING 进入 PAUSE/FINISH/FAILED/OFFLINE 才鸣响"
        )
        assert "lastPrinterStates[serial] = normalized" in state_block, (
            "鸣响判断后必须更新 lastPrinterStates，同一状态不重复鸣响"
        )

        # 跨浏览器/移动端兼容：
        # 1. suspended 时必须在 resume().then 回调里发声（同步检查会漏首次）
        resume_idx = js.index("resume().then(() => emitOscillatorChime())")
        assert resume_idx != -1, (
            "AudioContext 处于 suspended 时必须在 resume() 异步回调里发声，"
            "否则恰好在 suspended 状态发生的那次变化会被吐掉（首次漏报）"
        )
        # 2. iOS 静音拨片会屏蔽 Web Audio，必须降级 HTML5 <audio>
        assert "function isIOSLike" in js, (
            "必须检测 iOS/iPadOS：iOS Safari 的 Web Audio 输出受静音拨片屏蔽"
        )
        assert "function playChimeHtmlAudio" in js and "function makeChimeWavDataUri" in js, (
            "iOS/老浏览器必须降级到 HTML5 <audio> + 运行时合成 WAV data URI"
            "（媒体通道不受 iOS 静音拨片影响）"
        )
        ios_idx = js.index("function playStateChime")
        ios_block = js[ios_idx:ios_idx + 400]
        assert "isIOSLike()" in ios_block and "playChimeHtmlAudio()" in ios_block, (
            "playStateChime 必须在 iOS 分支优先走 HTML5 Audio 通道"
        )
        # 3. 解锁手势监听覆盖 pointerdown/touchend/keydown（老 iOS/纯键盘环境）
        assert "addEventListener('pointerdown', primeAudio" in js, (
            "必须监听 pointerdown 解锁音频（自动播放策略要求用户手势）"
        )
        assert "addEventListener('touchend', primeAudio" in js and "addEventListener('keydown', primeAudio" in js, (
            "必须额外兑底 touchend/keydown 解锁：老 iOS 无 PointerEvent，"
            "纯键盘环境可能无指针事件"
        )

    def test_tc_tpl_24_non_running_badge_centered_enlarged(self):
        """TC-TPL-24: 非 running 状态角标放大居中覆盖监控画面；running 回左上角

        契约：
        1. dashboard.css 提供 .camera-overlay.camera-state-center：铺满画面
           （inset 0）+ flex 居中，且内部 .camera-state 字体明显放大；
        2. dashboard.js updateCameraBadge 按 state.toLowerCase() !== 'running'
           切换 .camera-state-center 类：
           - running → 移除（回到 .camera-overlay 默认 top/left 8px 小角标）
           - 其它状态 → 加上（放大居中）
        """
        css = (TEMPLATES_DIR.parent / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")
        js_path = TEMPLATES_DIR.parent / "static" / "js" / "dashboard.js"
        js = js_path.read_text(encoding="utf-8")

        # 1. CSS 居中铺满 + 放大
        center_idx = css.index(".camera-overlay.camera-state-center {")
        center_block = css[center_idx:center_idx + 300]
        assert "display: flex" in center_block, (
            ".camera-state-center 必须用 flex 居中状态角标"
        )
        assert "top: 0" in center_block and "bottom: 0" in center_block, (
            ".camera-state-center 必须铺满整个监控画面（top/right/bottom/left 0）"
        )
        font_idx = css.index(".camera-overlay.camera-state-center .camera-state")
        font_block = css[font_idx:font_idx + 200]
        assert "font-size" in font_block, (
            "非 running 居中状态的字体必须放大（font-size 覆盖默认 0.75rem）"
        )

        # 2. JS 按 running 切换类
        badge_idx = js.index("function updateCameraBadge")
        badge_block = js[badge_idx:badge_idx + 1200]
        assert "camera-state-center" in badge_block, (
            "updateCameraBadge 必须切换 .camera-state-center 类"
        )
        assert "classList.toggle" in badge_block and "'running'" in badge_block, (
            "必须用 classList.toggle('camera-state-center', state !== 'running')："
            "running 回左上角默认位置，其它状态放大居中"
        )

    def test_tc_tpl_24_single_source_grid_columns(self):
        """TC-TPL-24: 摄像头网格列数只有一个来源（P3-14）

        修复前：sortable.js 的 GRID_COLS、dashboard.css 的 repeat(3, 1fr)
        与 @media 里的 1fr 各写一份，窄屏时 JS 按 3 列锁定位置而 CSS 只有
        1 列，首列被压成 2px。修复后：
        1. CSS 只消费 var(--camera-cols)，媒体查询不再覆盖 .camera-grid 列模板；
        2. JS 的 refreshGridCols() 是唯一写入方，阈值只保留 MOBILE_MAX_WIDTH。
        """
        css = (TEMPLATES_DIR.parent / "static" / "css" / "dashboard.css").read_text(
            encoding="utf-8"
        )
        js = (TEMPLATES_DIR.parent / "static" / "js" / "sortable.js").read_text(
            encoding="utf-8"
        )

        grid_block = css[
            css.index(".camera-grid {") : css.index(".camera-grid.layout-ready")
        ]
        assert "repeat(var(--camera-cols)" in grid_block, (
            "camera-grid 列模板必须消费 CSS 变量 --camera-cols"
        )
        assert "repeat(3, 1fr)" not in grid_block, (
            "camera-grid 不得再硬编码 3 列"
        )

        media_block = css[css.index("@media (max-width: 768px)") :]
        assert ".camera-grid {" not in media_block, (
            "媒体查询不得再覆盖 .camera-grid 的列模板，窄屏降级交给 JS"
        )

        assert "const GRID_COLS = 3" not in js, (
            "拖拽逻辑不得再使用硬编码列数常量"
        )
        assert "MOBILE_MAX_WIDTH" in js and "refreshGridCols" in js, (
            "必须由 refreshGridCols() 按 MOBILE_MAX_WIDTH 写入唯一列数"
        )
        assert "Math.floor(nextIdx / GRID_COLS)" in js, (
            "位置锁定必须读取运行时 GRID_COLS"
        )

    def test_tc_tpl_25_unknown_printer_refresh_banner(self):
        """TC-TPL-25: status_batch 出现页面未知的 serial 时提示刷新（P2-19）

        运行中新增打印机后，旧页面没有对应卡片，更新会全部静默。
        前端必须检测“数据里有、DOM 里没有”的 serial 并提示刷新，
        而不是要求用户自己猜为什么新机器不动。
        """
        js = (
            TEMPLATES_DIR.parent / "static" / "js" / "dashboard.js"
        ).read_text(encoding="utf-8")
        css = (
            TEMPLATES_DIR.parent / "static" / "css" / "dashboard.css"
        ).read_text(encoding="utf-8")

        assert "function maybeShowUnknownPrinterBanner" in js, (
            "必须提供未知 serial 的检测与提示函数"
        )
        all_idx = js.index("function updateAllPrinters")
        all_block = js[all_idx:all_idx + 900]
        assert "maybeShowUnknownPrinterBanner(serial)" in all_block, (
            "updateAllPrinters 必须对每个 serial 检查页面是否已有卡片"
        )
        assert "location.reload()" in js, "提示必须提供刷新入口"
        assert ".unknown-printer-banner" in css, "提示条需要有可见样式"
