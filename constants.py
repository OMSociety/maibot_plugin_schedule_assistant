"""Schedule Assistant 插件常量定义

定义插件范围内使用的常量，包括存储键名和默认配置值。
"""

# ==================== 数据存储配置 ====================
# Preference 键名

# 数据键名（存储在 preference value 中的键）
SCHEDULES_KEY = "schedules"  # 单次日程列表键名
HABITS_KEY = "habits"  # 重复习惯列表键名
WATER_LAST_KEY = "water_last"  # 上次喝水时间键名

# ==================== 默认提醒时间 ====================
DEFAULT_BATH_TIME = "22:00"  # 默认洗澡时间
DEFAULT_SLEEP_TIME = "23:00"  # 默认睡觉时间
DEFAULT_WATER_START = "09:30"  # 默认喝水提醒开始时间
DEFAULT_WATER_END = "21:30"  # 默认喝水提醒结束时间
DEFAULT_WATER_INTERVAL = 90  # 默认喝水提醒间隔（分钟）

# ==================== LLM 播报特例指令 ====================
# 追加在人格 system_prompt 末尾，覆盖聊天场景中的字数/分段约束，
# 确保 LLM 输出完整 markdown（否则人格的"极简回复"会压制表格渲染）。
# 早安播报 / 习惯提醒 / 日程提醒三处共用同一份，集中定义避免重复。
BROADCAST_MD_OVERRIDE = (
    "【播报任务特例】本条消息是定时播报，不是即时聊天回复。"
    "请忽略聊天场景中关于字数限制、段落数量、回复极简的要求，"
    "完整输出全部播报内容，必须使用 markdown 排版"
    "（#### 小标题、**粗体**、表格等），语气保持原有风格。"
)

# ==================== 日志前缀 ====================
LOG_PREFIX = "[ScheduleAssistant]"
