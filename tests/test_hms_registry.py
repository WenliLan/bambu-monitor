"""测试 HMS 错误码注册表和通知消息格式化

测试用例覆盖:
  TC-HMS-01: lookup_hms_error 正确查找 Wiki 8 位码
  TC-HMS-02: lookup_hms_error 未知码返回默认未知信息
  TC-HMS-03: _build_markdown 正确渲染错误分类和建议（Wiki 码）
  TC-HMS-04: 多个错误合并显示
  TC-HMS-05: 无原始消息时使用注册表中文名
  TC-HMS-06: lookup_hms_error 对所有 131 个 Wiki 码返回完整字段
  TC-HMS-07: 飞书消息正确渲染
  TC-HMS-08: 非 error 事件不渲染错误区域
  TC-HMS-09: 短码格式 (XXXX_YYYY) + 严重级别 emoji (修复后新增)
  TC-HMS-10: severity=fatal/serious/common 渲染对应颜色 emoji
  TC-HMS-11: 未填 attr/raw_code 时不崩溃（默认 0）
"""

import pytest

from bumbo_stats.models import HMSError, lookup_hms_error, HMS_ERROR_CODES
from bumbo_stats.printer.wecom import WeComNotifier, EVENT_ERROR
from bumbo_stats.printer.feishu import FeishuNotifier


# ---------------------------------------------------------------------------
# TC-HMS-01~02: 错误码注册表查找
# ---------------------------------------------------------------------------

class TestHMSErrorRegistry:
    def test_tc_hms_01_wiki_codes(self):
        """TC-HMS-01: Wiki 8 位码正确查找（用实际存在于注册表中的码）"""
        cases = [
            # (code, expected_cn_fragment, expected_category)
            (0x03000200, "右挤出机喷嘴温控异常", "系统"),       # 右挤出机 nozzle temp
            (0x03001800, "Z轴电机转动受阻", "系统"),           # Z轴卡住
            (0x03001A00, "喷嘴被耗材包裹或打印板放歪", "系统"),  # 喷嘴包裹/打印板歪
            (0x07FE2000, "左/右挤出机外部挂载的耗材用完", "AMS"),
            (0x03002000, "X轴回零异常", "系统"),             # X轴回零
            (0x0C000200, "喷嘴太低", "视觉"),                 # 视觉：喷嘴太低
        ]
        for code, exp_cn_frag, exp_cat in cases:
            info = lookup_hms_error(code)
            assert exp_cn_frag in info["cn"], \
                f"0x{code:08X}: expected '{exp_cn_frag}' in cn='{info['cn']}'"
            assert info["category"] == exp_cat

    def test_tc_hms_02_unknown_code(self):
        """TC-HMS-02: 未知错误码返回默认未知信息"""
        info = lookup_hms_error(0xFFFF)
        assert info["cn"] == "未知错误"
        assert info["category"] == "其他"
        assert info["en"] == "Unknown Error"
        assert info["suggestion"] == "请查看完整错误信息"

    def test_tc_hms_06_all_wiki_codes_have_fields(self):
        """TC-HMS-06: 所有 131 个 Wiki 码都有完整字段"""
        for code, info in HMS_ERROR_CODES.items():
            assert "cn" in info, f"0x{code:08X} missing cn"
            assert "en" in info, f"0x{code:08X} missing en"
            assert "category" in info, f"0x{code:08X} missing category"
            assert "suggestion" in info, f"0x{code:08X} missing suggestion"


# ---------------------------------------------------------------------------
# TC-HMS-03~05: 通知消息格式化
# ---------------------------------------------------------------------------

class TestHMSErrorMessageFormatting:
    def _make_status(self):
        return type("MockStatus", (), {
            "gcode_file": "test.gcode",
            "current_layer": 50,
            "total_layers": 100,
            "progress": 50,
        })()

    def test_tc_hms_03_build_markdown_renders(self):
        """TC-HMS-03: _build_markdown 正确渲染 Wiki 码的错误分类和处理建议"""
        errors = [
            HMSError(code=0x03000200, severity="error", message=""),
            HMSError(code=0x03001A00, severity="error", message=""),
        ]
        md = WeComNotifier._build_markdown("P1", EVENT_ERROR, self._make_status(), errors)
        assert "右挤出机喷嘴温控异常" in md
        assert "喷嘴被耗材包裹或打印板放歪" in md
        assert "💡" in md  # 有建议
        assert "📋" in md  # 有原始消息
        assert "🔢" in md  # 有错误码
        assert "0x03000200" in md
        assert "0x03001A00" in md

    def test_tc_hms_04_multiple_errors_merged(self):
        """TC-HMS-04: 多个错误合并为一条通知，每个错误独立渲染"""
        errors = [
            HMSError(code=0x03000200, severity="error", message="nozzle temp error"),
            HMSError(code=0x03002000, severity="error", message="X axis homing failed"),
            HMSError(code=0x07FE2000, severity="error", message="filament empty"),
        ]
        md = WeComNotifier._build_markdown("P1", EVENT_ERROR, self._make_status(), errors)
        assert md.count("💡") == 3
        assert md.count("📋") == 3
        assert md.count("🔢") == 3
        assert "右挤出机喷嘴温控异常" in md
        assert "X轴回零异常" in md
        assert "左/右挤出机外部挂载的耗材用完" in md

    def test_tc_hms_05_no_original_message_uses_registry_name(self):
        """TC-HMS-05: 无原始消息时使用注册表中文名"""
        errors = [HMSError(code=0x03000200, severity="error", message="")]
        md = WeComNotifier._build_markdown("P1", EVENT_ERROR, self._make_status(), errors)
        assert "右挤出机喷嘴温控异常" in md
        assert "原始: (无)" in md

    def test_tc_hms_07_feishu_build_message_renders(self):
        """TC-HMS-07: 飞书 _build_message 正确渲染 Wiki 错误码"""
        errors = [HMSError(code=0x07FE2000, severity="error", message="filament empty")]
        msg = FeishuNotifier._build_message("P1", EVENT_ERROR, self._make_status(), errors)
        assert "左/右挤出机外部挂载的耗材用完" in msg
        assert "AMS" in msg
        assert "0x07FE2000" in msg
        assert "原始: filament empty" in msg

    def test_tc_hms_08_non_error_event_no_error_section(self):
        """TC-HMS-08: 非 error 事件不渲染错误区域"""
        for event in ["layer3", "completed"]:
            md = WeComNotifier._build_markdown("P1", event, self._make_status(), None)
            assert "💡" not in md
            assert "📋" not in md


class TestHMSErrorShortCodeAndSeverity:
    """TC-HMS-09~11: 修复后新增强制显示的信息

    历史 bug: 原渲染只显示 8 位 hex 错误码，不显示严重级别。
    修复后: 追加短码 (XXXX_YYYY) + 级别 emoji + 级别名。
    短码格式: (attr >> 16) & 0xFFFF 拼接 (raw_code & 0xFFFF)
    """

    def _make_status(self):
        return type("MockStatus", (), {
            "gcode_file": "test.gcode",
            "current_layer": 50,
            "total_layers": 100,
            "progress": 50,
        })()

    def test_tc_hms_09_short_code_and_severity(self):
        """TC-HMS-09: wecom 渲染包含短码 + 级别"""
        # attr=0x03000200, raw_code=0x00010200
        # 短码 = (0x03000200 >> 16) & 0xFFFF = 0x0300
        #     + "_" + (0x00010200 & 0xFFFF) = 0x0200
        # 期望: "0300_0200" + severity="fatal" + emoji "🔴"
        err = HMSError(
            code=0x03000200, severity="fatal", message="nozzle temp",
            attr=0x03000200, raw_code=0x00010200,
        )
        md = WeComNotifier._build_markdown("P1", EVENT_ERROR, self._make_status(), [err])
        assert "0300_0200" in md
        assert "fatal" in md
        assert "🔴" in md
        # 保留原有字段
        assert "右挤出机喷嘴温控异常" in md
        assert "0x03000200" in md

    def test_tc_hms_10_severity_emoji_mapping(self):
        """TC-HMS-10: 不同 severity 对应不同 emoji"""
        cases = [
            ("fatal", "🔴"),
            ("serious", "🟡"),
            ("common", "🔵"),
            ("error", "🔴"),     # 兼容老数据
            ("warning", "🟡"),   # 兼容老数据
        ]
        for sev, emoji in cases:
            err = HMSError(
                code=0x03000200, severity=sev, message="",
                attr=0x03000200, raw_code=0x00010200,
            )
            md = WeComNotifier._build_markdown("P1", EVENT_ERROR, self._make_status(), [err])
            assert emoji in md, f"severity={sev} 应渲染 {emoji}"
            assert sev in md

    def test_tc_hms_11_missing_attr_raw_code_no_crash(self):
        """TC-HMS-11: 未填 attr/raw_code 时不崩溃，短码退化为 0000_XXXX"""
        # 不传 attr/raw_code，验证默认 0 不导致 KeyError/FormatError
        err = HMSError(code=0x03000200, severity="error", message="")
        md = WeComNotifier._build_markdown("P1", EVENT_ERROR, self._make_status(), [err])
        # 默认 attr=0 raw_code=0 -> 短码 "0000_0000"
        assert "0000_0000" in md
        # 原有字段仍在
        assert "0x03000200" in md
        assert "右挤出机喷嘴温控异常" in md

    def test_tc_hms_12_feishu_short_code(self):
        """TC-HMS-12: 飞书渲染同样包含短码 + 级别 emoji"""
        err = HMSError(
            code=0x07FE2000, severity="serious", message="filament empty",
            attr=0x07FE2000, raw_code=0x00020300,
        )
        msg = FeishuNotifier._build_message("P1", EVENT_ERROR, self._make_status(), [err])
        # 短码: (0x07FE2000 >> 16) = 0x07FE, (0x00020300 & 0xFFFF) = 0x0300
        assert "07FE_0300" in msg
        # 严重级别
        assert "serious" in msg
        assert "🟡" in msg
        # 原有字段
        assert "0x07FE2000" in msg
        assert "左/右挤出机外部挂载的耗材用完" in msg
        assert "AMS" in msg
        assert "原始: filament empty" in msg
