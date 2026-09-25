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
    """HMS 健康监控错误

    Bambu 实际 MQTT 字段 (hms[] 数组每条 entry) 结构:
      attr  : 32 bit, [Module ID 8][Module No 8][Part ID 8][Part No 8]
              例如 0x03000200 = module 03(MC) part 02(右挤出机喷嘴)
              用于在 HMS_ERROR_CODES 注册表中查找中文描述
      code  : 32 bit, [Alert level 16][Error code ID 16]
              高 16 位是告警严重级别:
                0x0001 = fatal   (Error 红, 任务停止, 报警前消除)
                0x0002 = serious (Warning 黄, 任务暂停, 恢复前消除)
                0x0003 = common  (Notification 蓝, 仅提示)
              低 16 位是错误码 ID (Bambu Wiki 0xCCCC 4 位)
    历史字段含义 (修复前, 已废弃):
      code    : 原意为 8 位 hex 错误码, 实际写入了 attr 值
      severity: 原意为 "error"/"warning" 字符串, 实际一直为空
    """
    code: int = 0          # 错误码 (现指 attr 字段, 用于 lookup_hms_error)
    severity: str = ""     # "fatal" / "serious" / "common" / "error" / "warning"
    message: str = ""      # 原始 MQTT 描述 (兼容 err_msg / msg 字段)
    attr: int = 0          # 原始 attr 字段 (4 byte, module+part 定位)
    raw_code: int = 0      # 原始 code 字段 (4 byte, alert level 高 16 + error id 低 16)

# ---------------------------------------------------------------------------
# HMS 错误码注册表（来源：Bambu Lab Wiki，131 个官方错误码）
# ---------------------------------------------------------------------------
# Wiki URL 格式: /hmscode/CCCC-HHHH-TTTT-IIII → code = 0xCCCCHHHH
# 类别前缀:
#   0x03xxxx - 工具头/系统（温度/电机/传感器/通信）
#   0x05xxxx - 存储/视频服务/AMS模块
#   0x07xxxx - AMS 辅助电机/耗材/切刀/干燥箱
#   0x0Cxxxx - 视觉/AI 识别
#   0x12xxxx - AMS 耗材/送丝
#   0x18xxxx - AMS-HT 高温干燥箱
#   0x1Axxxx - 热端挂架/工具头/AMS 综合
# ---------------------------------------------------------------------------

HMS_ERROR_CODES: dict[int, dict] = {
    0x03000100: dict(cn="热床加热过程中出现异常， 加热模块可能故障。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03000200: dict(cn="右挤出机喷嘴温控异常，加热器可能开路。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03000300: dict(cn="热端风扇转速慢。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03000400: dict(cn="部件冷却风扇转速过低或停转，可能被杂物卡住或已损坏。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03000500: dict(cn="电机驱动器过热，可能冷却风扇停转或散热片脱落。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03000600: dict(cn="A 电机断路，可能是连接器没插紧或电机故障。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03000900: dict(cn="E电机断路，可能线没插紧或电机故障。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03000A00: dict(cn="热床压力传感器1/2/3灵敏度低。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03000D00: dict(cn="Z轴电机在上升过程被卡住，请检查 Z滑块、Z同步带轮是否有异物卡住。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03000F00: dict(cn="检测到加速度计数据异常，请尝试重启机器。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001000: dict(cn="X轴共振频率与上次校准差异较大，建议完成打印后清洁X轴导轨并执行一次自动校准。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001200: dict(cn="工具头前盖脱落。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001300: dict(cn="电机-A电流传感器异常。这可能是硬件采样电路故障引起的。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001400: dict(cn="B电机电流传感器异常，可能是硬件采样电路故障。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001600: dict(cn="E电机电流传感器异常，可能是硬件采样电路故障。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001700: dict(cn="喷嘴风扇1转速偏低，可能有杂物堆积导致转速偏低。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001800: dict(cn="Z轴电机转动受阻，请检查 Z滑块、Z同步带轮是否有异物卡住或涡流传感器是否异常。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001900: dict(cn="Y轴电涡流传感器没有读到数据，可能线路断路。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001A00: dict(cn="喷嘴被耗材包裹或打印板放歪。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001B00: dict(cn="热床加速度传感器信号弱，可能是传感器脱落或已受损。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001C00: dict(cn="挤出电机驱动异常，可能是开关管短路。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001D00: dict(cn="挤出电机位置传感器异常，可能是传感器排线松动。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03001E00: dict(cn="左挤出机喷嘴温控异常，加热器可能开路。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03002000: dict(cn="X轴回零异常，可能是同步带松了。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03002100: dict(cn="辅助挤出电机断路，可能线没插紧或电机故障。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03002500: dict(cn="Z轴电机转动受阻，请检查 Z滑块、Z同步带轮是否有异物卡住。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03002600: dict(cn="喷嘴在位检测失败：左挤出机喷嘴未安装或安装不到位。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03002700: dict(cn="喷嘴偏移校准传感器信号异常，可能传感器已损坏或接线松动。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03002800: dict(cn="刀切模组力传感器出现异常。可能是力传感器线缆脱落或力传感器损坏。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03002900: dict(cn="视觉编码板图案识别异常，可能原因有：图案变形或破损、光源过曝、编码板摆放不对等", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03002A00: dict(cn="左/右侧热端冷却风扇转速偏低，可能有杂物堆积导致转速偏低。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03002D00: dict(cn="热床调平失败，可能由于热床上有异物或热床倾斜。继续打印可能损坏打印板，请清理异物或手动调平热床后重试。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03003100: dict(cn="部件冷却风扇转速偏低，可能有杂物堆积导致转速偏低。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03003500: dict(cn="MC模块散热风扇转速过低或停转，可能被杂物卡住或连接器未插好。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03003600: dict(cn="腔温加热循环风扇转速过低或停转，可能被杂物卡住或连接器未插好。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03004000: dict(cn="通过串行端口传输数据异常；软件系统可能有故障。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03004100: dict(cn="系统电压不稳定，触发断电保护功能。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03009000: dict(cn="腔温加热失败。可能是因为舱门未关闭，或者外界温度过低，或者电源散热口被堵住。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03009100: dict(cn="腔温加热器 1 温控异常,AC 板可能故障。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03009300: dict(cn="腔温异常，出风口温度传感器可能开路。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03009400: dict(cn="腔体散热可能过慢。如腔内气体无害，可开箱辅助散热。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03009500: dict(cn="激光模块模块过热。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03009600: dict(cn="前门霍尔传感器（上）异常，请检查连接排线是否松动。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03009700: dict(cn="检测到顶部防护板未安装，请按Wiki安装好后重新发起任务。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03009800: dict(cn="左侧窗霍尔传感器（上）异常，请检查连接排线是否松动。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03009900: dict(cn="右侧窗霍尔传感器（下）异常，请检查连接接线是否松动。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x03009D00: dict(cn="雕刻激光器的焦点XY标定失败，请将激光垫板回零台清洁干净后，重新执行激光模组挂载校准后，进行重试。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x0300A200: dict(cn="MC模块温度过高，可能是因为打印机背部散热受限，建议调整打印机位置或降低环境温度后再使用。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x0300A800: dict(cn="AMS电源异常，可能是AMS损坏、AMS接口短路，或AMS接入数量过多。请检查 AMS接入是否正确。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x0300AB00: dict(cn="右热端散热片温度传感器异常，为避免堵头，散热风扇会维持高转速。可在设备空闲时排查温度传感器故障。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x0300C000: dict(cn="自动排气格栅动作异常，可能卡住。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x0300C100: dict(cn="自适应风道系统无法进入冷却模式，请检查风门状态。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x0300C200: dict(cn="腔温切换风门霍尔传感器异常，可能是风门排线松动。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x0300C300: dict(cn="腔温切换风门电流传感器异常，可能是风门排线松动或硬件采样电路故障。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x0300DA00: dict(cn="检测到激光模组，请取下热端挂架上的热端，以防激光加工碎屑影响热端打印功能。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x0300E000: dict(cn="热端挂架电机三相电阻异常或不平衡。如重启打印机无法解决，建议联系售后服务。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x0300E100: dict(cn="热端挂架电机电流传感器异常，可能是采样电路故障。如重启打印机无法解决，建议联系售后服务。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x0300E300: dict(cn="热端挂架电机驱动板MOS管短路异常，如果重启打印机无法解决问题，建议联系售后服务。", en="", category="系统", suggestion="检查温度传感器和加热元件"),
    0x05000100: dict(cn="MicroSD卡空间不足，可能无法进行录像或者延时摄影，请删除MicroSD卡中部分文件。", en="", category="系统", suggestion="重启设备"),
    0x05000200: dict(cn="实时视频服务出现故障。", en="", category="系统", suggestion="重启设备"),
    0x05000300: dict(cn="AMS模块工作异常，请尝试重启设备。", en="", category="系统", suggestion="重启设备"),
    0x05000400: dict(cn="The RFID-tag on AMS1 Slot1 cannot be identified.", en="", category="系统", suggestion="重启设备"),
    0x05000500: dict(cn="AMS A 认证失败，请拔插连接线或重启打印机。", en="", category="系统", suggestion="重启设备"),
    0x05000600: dict(cn="俯视摄像头绑定失败，请检查硬件及线路连接。", en="", category="系统", suggestion="重启设备"),
    0x05010400: dict(cn="感应热端锁紧拉柄可能出现磨损，为保障热端取放功能正常，请及时更换感应热端锁紧拉柄并清洁感应热端挂架光杆。", en="", category="系统", suggestion="重启设备"),
    0x05800500: dict(cn="AMS-HT A 认证失败，请拔插连接线或重启打印机。", en="", category="系统", suggestion="重启设备"),
    0x07000100: dict(cn="AMS A 助力电机电流传感器故障。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07000200: dict(cn="AMS A 里程轮无信号,可能是里程轮线接触不良。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07001000: dict(cn="AMS1 有刷电机1打滑，可能挤出轮磨损或料线过细。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07002000: dict(cn="AMS 1号料槽为空。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07002500: dict(cn="检测到AMS在打印/进料过程中使用打印机供电烘干，建议接入电源适配器以获得最佳烘干效果。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07002600: dict(cn="感应热端加热中，AMS A 的烘干功率将被降低，请连接 AMS 电源适配器或等待热端加热完成。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07003000: dict(cn="AMS AMS RFID硬件A错误，可能是RFID射频板线断连或者RFID射频板损坏。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07003500: dict(cn="AMS A湿度传感器离线,可能是插头接触不良。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07004000: dict(cn="AMS A 缓冲器位置信号丢失，可能连接线异常或位置传感器异常。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07004500: dict(cn="切刀刀柄未松开，刀柄或刀片可能被卡住，或料线霍尔接线异常。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07005500: dict(cn="AMS初始化过程中检测到AMS A离线。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07006000: dict(cn="AMS 1号槽电机过载，料线可能缠料或料盘卡住。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07007000: dict(cn="AMS A 槽 1的耗材退出挤出机失败，可能是堵头或者耗材断在挤出机内部。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07007400: dict(cn="检测到在 AMS 初始化过程中接入了耗材变轨器。请先拔出耗材变轨器，待 AMS 初始化完成后再接入重试。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07007500: dict(cn="检测到拔出耗材变轨器时，挤出机中仍有耗材。请先重新接入耗材变轨器，手动退料后，再拔出耗材变轨器。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07008000: dict(cn="AMS A 加热器1电流传感器异常。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07008600: dict(cn="AMS A 与耗材变轨器绑定关系出错，请重新进行 AMS 初始化。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07008700: dict(cn="初始化异常：检测到变轨器在线但耗材未通过。请检查 AMS 与变轨器间的特氟龙管连接；如无需使用变轨器，请断开其与打印机的连接。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07008900: dict(cn="检测到耗材变轨器 IN-A 侧切换检测传感器异常，可能该侧排线松动，请重新插拔或更换传感器后重试。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07008A00: dict(cn="检测到耗材变轨器 IN-B 侧切换检测传感器异常，可能该侧排线松动，请重新插拔或更换传感器后重试。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07008C00: dict(cn="检测到耗材变轨器 IN-B 侧进料检测传感器异常，可能该侧排线松动，请重新插拔或更换后重试。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07008D00: dict(cn="检测到耗材变轨器 IN-A 侧线圈电阻异常，请参考Wiki更换线圈组件。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07008E00: dict(cn="检测到耗材变轨器 IN-B 侧线圈电阻异常，请参考Wiki更换线圈组件。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07008F00: dict(cn="检测到耗材变轨器 IN-B 侧动作异常，可能是异物卡住或该侧排线松动、线圈损坏，请参考Wiki排故。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07009000: dict(cn="AMS A 排气风门 1 电流传感器异常, 请联系客服更换 AMS 主板。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07009200: dict(cn="AMS A加热器1散热风扇无法启动，因为电源适配器未接。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07009400: dict(cn="AMS A 加热器 1 温度传感器离线，可能是插头接触不良。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07009600: dict(cn="AMS A烘干可能发生热失控，请关闭AMS电源。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07009800: dict(cn="AMS A 电源适配器电压过低，可能导致烘干温度偏低，请更换电源适配器。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x0700F000: dict(cn="耗材与热端匹配失败，请检查热端挂架槽位1处热端是否正确安装。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07FE2000: dict(cn="左/右挤出机外部挂载的耗材用完；请装入新的耗材。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07FE4500: dict(cn="切刀刀柄行程异常，可能电机丢步或XY未回零。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07FE6000: dict(cn="挤出机外部挂载的耗材缠绕或料盘卡住。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07FE8000: dict(cn="工具头升降电机位置霍尔传感器断路，请检查连接排线是否松动。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07FE8100: dict(cn="挤出切换动作异常，请检查是否有异物卡在工具头内。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07FEA000: dict(cn="左喷嘴冷拔超时，请点击重试按钮后拔出耗材。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07FF2000: dict(cn="外挂耗材用尽，请装入新的耗材丝。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07FF4500: dict(cn="切料传感器异常。请检查连接器是否正确插入。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x07FF7000: dict(cn="从右侧挤出机进料至工具头失败，可能是耗材未被挤出机咬上、挤出机至工具头间PTFE管松脱等。", en="", category="AMS", suggestion="检查 AMS 模块"),
    0x0C000100: dict(cn="实况摄像头校准失败，请重新标定。标定前请确保：移除打印板、摄像头画面清晰且图像正立。", en="", category="视觉", suggestion="检查网络和视觉连接"),
    0x0C000200: dict(cn="喷嘴太低。", en="", category="视觉", suggestion="检查网络和视觉连接"),
    0x0C000300: dict(cn="冲刷的耗材堆积。", en="", category="视觉", suggestion="检查网络和视觉连接"),
    0x0C000400: dict(cn="“打印后刀切”未检测到视觉标志，请重新按正确位置粘贴材料。", en="", category="视觉", suggestion="检查网络和视觉连接"),
    0x12001000: dict(cn="AMS1 1号槽电机过载。料线可能缠结或卡住。", en="", category="AMS", suggestion="检查 AMS 耗材和送丝"),
    0x12001200: dict(cn="AMS1 有刷电机1打滑，可能挤出轮磨损或料线过细。", en="", category="AMS", suggestion="检查 AMS 耗材和送丝"),
    0x12002000: dict(cn="AMS A 料槽1料线可能断在料管中。", en="", category="AMS", suggestion="检查 AMS 耗材和送丝"),
    0x12003000: dict(cn="AMS1 RFID硬件1错误，可能是RFID线圈断或者射频硬件电路损坏。", en="", category="AMS", suggestion="检查 AMS 耗材和送丝"),
    0x12004500: dict(cn="切刀刀柄未松开，刀柄或刀片可能被卡住，或料线霍尔接线异常。", en="", category="AMS", suggestion="检查 AMS 耗材和送丝"),
    0x12005100: dict(cn="AMS已禁用，请从料盘支架加载耗材。", en="", category="AMS", suggestion="检查 AMS 耗材和送丝"),
    0x12007000: dict(cn="AMS1 料线速度与长度计算错误，可能是里程轮传感器1故障。", en="", category="AMS", suggestion="检查 AMS 耗材和送丝"),
    0x12008000: dict(cn="AMS1 料槽1供料阻力大，料线可能缠绕。", en="", category="AMS", suggestion="检查 AMS 耗材和送丝"),
    0x12FF2000: dict(cn="料盘支架耗材为空，请放置新料。", en="", category="AMS", suggestion="检查 AMS 耗材和送丝"),
    0x12FF8000: dict(cn="外挂料盘供料阻力大，料线可能缠绕。", en="", category="AMS", suggestion="检查 AMS 耗材和送丝"),
    0x18002000: dict(cn="AMS-HT A 助力电机过载，AMS至耗材变轨器之间料管阻力过大。", en="", category="AMS", suggestion="检查 AMS-HT"),
    0x18002400: dict(cn="AMS-HT 前盖打开，可能影响烘干效果或使耗材受潮。", en="", category="AMS", suggestion="检查 AMS-HT"),
    0x18008600: dict(cn="AMS-HT A与耗材变轨器绑定关系出错，请重新进行AMS初始化。", en="", category="AMS", suggestion="检查 AMS-HT"),
    0x18009000: dict(cn="AMS HT A 排气风门 1 电流传感器异常, 请联系客服更换 AMS-HT 主板。", en="", category="AMS", suggestion="检查 AMS-HT"),
    0x1A000100: dict(cn="取用 1 槽位热端时检测到异常：工具头上已安装热端，请先卸下工具头上的热端后点击“继续”。", en="", category="工具头", suggestion="检查工具头和热端"),
    0x1A001000: dict(cn="工具头 XY 轴或热端挂架未完成回中，无法更换热端。", en="", category="工具头", suggestion="检查工具头和热端"),
    0x1A001100: dict(cn="感应热端挂架粗回中行程过长失败，请检查同步带状态。", en="", category="工具头", suggestion="检查工具头和热端"),
    0x1A001200: dict(cn="感应热端挂架暂未初始化，请按指示初始化后再使用挂架相关功能。", en="", category="工具头", suggestion="检查工具头和热端"),
    0x1A001300: dict(cn="热端挂架上热端已装满。请移除一个热端后继续。", en="", category="工具头", suggestion="检查工具头和热端"),
    0x1A002000: dict(cn="换热端时断电。请按 Wiki 指南恢复现场，否则可能导致打印失败。", en="", category="工具头", suggestion="检查工具头和热端"),
    0x1A002100: dict(cn="在取放热端过程中发生断电，请先检查热端是否安装在工具头上。如未安装，请从挂架位置 1 取下热端并装回工具头，然后将工具头移至热床中央。", en="", category="工具头", suggestion="检查工具头和热端"),
    0x1A00E000: dict(cn="感应热端挂架回中行程过长，可能是同步带过松，请检查并张紧同步带。", en="", category="工具头", suggestion="检查工具头和热端"),
}

# ---------------------------------------------------------------------------
# 错误码别名映射（Wiki 8 位码 <-> 旧 4 位码兼容）
# ---------------------------------------------------------------------------

def _build_4_to_8_aliases() -> dict[int, int]:
    aliases = {}
    for code8 in HMS_ERROR_CODES:
        code4 = code8 & 0xFFFF
        if code4 not in aliases:
            aliases[code4] = code8
    return aliases


_4_TO_8_ALIAS: dict[int, int] = _build_4_to_8_aliases()


def lookup_hms_error(code: int) -> dict:
    """根据错误码查找 HMS 错误定义信息

    兼容两种格式:
      - Wiki 8 位格式: 0x03000B01
      - 旧 4 位格式:   0x0B01
    """
    if code in HMS_ERROR_CODES:
        return HMS_ERROR_CODES[code]
    alias = _4_TO_8_ALIAS.get(code)
    if alias and alias in HMS_ERROR_CODES:
        return HMS_ERROR_CODES[alias]
    return dict(
        cn="未知错误",
        en="Unknown Error",
        category="其他",
        suggestion="请查看完整错误信息",
    )


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