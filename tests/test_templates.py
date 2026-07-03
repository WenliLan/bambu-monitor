"""测试前端模板渲染

测试用例:
  TC-TPL-01: base.html 包含必要的 CDN 引用
  TC-TPL-02: dashboard.html 渲染包含所有区域
  TC-TPL-03: 打印机配置正确传递到模板
"""

from pathlib import Path
import pytest
from jinja2 import Environment, FileSystemLoader


TEMPLATES_DIR = Path(__file__).parent.parent / "bumbo_stats" / "templates"


@pytest.fixture
def env():
    return Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))


class TestBaseTemplate:
    def test_tc_tpl_01_base_has_cdn(self):
        """TC-TPL-01: base.html 包含 HTMX 和 Chart.js"""
        content = (TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
        assert "htmx.org" in content
        assert "chart.js" in content.lower() or "chart.umd" in content.lower()
        assert "dashboard.css" in content



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
