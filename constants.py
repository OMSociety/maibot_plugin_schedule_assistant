"""Schedule Assistant 插件常量定义。

只保留运行时需要的键名、默认时间与日志前缀。
"""

# ==================== 数据存储 ====================
SCHEDULES_KEY = "schedules"  # 单次日程列表键名

# ==================== 默认提醒时间 ====================
DEFAULT_BATH_TIME = "22:00"
DEFAULT_SLEEP_TIME = "23:00"
DEFAULT_WATER_START = "09:30"
DEFAULT_WATER_END = "21:30"
DEFAULT_WATER_INTERVAL = 90

# ==================== 日志前缀 ====================
LOG_PREFIX = "[ScheduleAssistant]"
