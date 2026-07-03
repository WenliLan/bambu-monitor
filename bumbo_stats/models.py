"""数据模型 - Pydantic 模型定义"""

from datetime import datetime, date
from typing import Optional
from enum import Enum

from pydantic import BaseModel


class PrinterState(str, Enum):
    """打印机状态枚举"""
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSE = "PAUSE"
    FINISH = "FINISH"
    FAILED = "FAILED"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


class HMSError(BaseModel):
    """HMS 健康监控错误"""
    code: int = 0          # 错误码，如 0x0B01（堵嘴）
    severity: str = ""    # "error" 或 "warning"
    message: str = ""     # 友好描述


class PrinterStatus(BaseModel):
    """打印机实时状态"""
    serial: str
    name: str
    state: PrinterState = PrinterState.UNKNOWN
    nozzle_temp: float = 0.0
    nozzle_target_temp: float = 0.0
    bed_temp: float = 0.0
    bed_target_temp: float = 0.0
    chamber_temp: float = 0.0
    progress: int = 0               # 0-100
    remaining_time: int = 0         # 分钟
    current_layer: int = 0
    total_layers: int = 0
    gcode_file: str = ""
    print_speed: int = 0
    wifi_signal: str = ""
    light_on: bool = False
    last_updated: Optional[datetime] = None
    hms_errors: list[HMSError] = []   # HMS 错误列表
    ams_status: Optional[dict] = None  # AMS 耗材状态


class DailyUsage(BaseModel):
    """每日使用统计"""
    printer_serial: str
    printer_name: str = ""
    date: date
    total_print_seconds: int = 0
    session_count: int = 0
    utilization_percent: float = 0.0  # 计算值: seconds / 86400 * 100


class PrintSession(BaseModel):
    """单次打印任务记录"""
    id: int
    printer_serial: str
    gcode_file: str
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    status: str = "running"  # running, completed, cancelled, failed


class MonthlyUsagePoint(BaseModel):
    """月度折线图数据点"""
    date: date
    utilization_percent: float


class MonthlyUsage(BaseModel):
    """单台打印机月度使用数据"""
    printer_serial: str
    printer_name: str
    points: list[MonthlyUsagePoint]


class TodayUsageItem(BaseModel):
    """今日产能饱和度条目"""
    printer_serial: str
    printer_name: str
    total_print_seconds: int
    utilization_percent: float
    print_time_display: str  # 如 "17h 21m"
