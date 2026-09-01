"""
pytest 共享配置：模拟 AstrBot 的插件包加载环境。

messaging.py / reminders / services 等模块使用相对导入
（from ..constants import ...），直接 import 会失败。
本文件在测试收集前把插件目录注册为 schedule_assistant 包，
使测试能以包路径导入被测模块。
"""

import os
import sys
import types

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# 预导入并注册相对导入依赖的兄弟模块
import constants as _constants_mod

_pkg = types.ModuleType("schedule_assistant")
_pkg.__path__ = [_PLUGIN_DIR]
sys.modules.setdefault("schedule_assistant", _pkg)
sys.modules["schedule_assistant.constants"] = _constants_mod
