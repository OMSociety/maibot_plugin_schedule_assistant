"""
pytest 共享配置：模拟 MaiBot 的插件包加载环境，
并给未安装的运行依赖（aiohttp / apscheduler）装上最小替身。

plugin.py / messaging.py / reminders / services 等模块使用相对导入
（from ..constants import ...），直接 import 会失败。
本文件在测试收集前把插件目录注册为 schedule_assistant 包，
使测试能以包路径导入被测模块。

替身只提供导入与构造所需的最小接口，具体逻辑由各测试自行 stub。
maibot_sdk 不在此替身：test_config_i18n.py 依赖「SDK 缺失即整文件 skip」
的判定，若在这里伪装成已安装，会让该测试带着假 SDK 跑真逻辑。

注意：替身只在真依赖缺失时注册，装有真依赖的环境不受影响。
"""

import os
import sys
import types

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


def _stub_runtime_deps() -> None:
    """给未安装的第三方运行依赖装最小替身"""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        aiohttp_mod = types.ModuleType("aiohttp")

        class _ClientError(Exception):
            pass

        class _ClientResponseError(_ClientError):
            def __init__(self, *a, status=None, **kw):
                super().__init__(str(status))
                self.status = status

        aiohttp_mod.ClientError = _ClientError
        aiohttp_mod.ClientResponseError = _ClientResponseError
        aiohttp_mod.ClientSession = type("ClientSession", (), {})
        aiohttp_mod.ClientTimeout = type(
            "ClientTimeout", (), {"__init__": lambda self, **kw: None}
        )
        sys.modules["aiohttp"] = aiohttp_mod

    try:
        import apscheduler.schedulers.asyncio  # noqa: F401
        import apscheduler.triggers.cron  # noqa: F401
    except ImportError:
        root = types.ModuleType("apscheduler")
        root.__path__ = []
        schedulers = types.ModuleType("apscheduler.schedulers")
        schedulers.__path__ = []
        asyncio_mod = types.ModuleType("apscheduler.schedulers.asyncio")
        asyncio_mod.AsyncIOScheduler = type(
            "AsyncIOScheduler",
            (),
            {
                "__init__": lambda self, **kw: None,
                "add_job": lambda self, *a, **kw: None,
                "start": lambda self, *a, **kw: None,
                "shutdown": lambda self, *a, **kw: None,
            },
        )
        triggers = types.ModuleType("apscheduler.triggers")
        triggers.__path__ = []
        cron = types.ModuleType("apscheduler.triggers.cron")
        cron.CronTrigger = type(
            "CronTrigger", (), {"__init__": lambda self, **kw: None}
        )
        root.schedulers = schedulers
        root.triggers = triggers
        sys.modules["apscheduler"] = root
        sys.modules["apscheduler.schedulers"] = schedulers
        sys.modules["apscheduler.schedulers.asyncio"] = asyncio_mod
        sys.modules["apscheduler.triggers"] = triggers
        sys.modules["apscheduler.triggers.cron"] = cron


_stub_runtime_deps()

# 预导入并注册相对导入依赖的兄弟模块
import constants as _constants_mod  # noqa: E402 - 必须在 sys.path 注入之后导入

_pkg = types.ModuleType("schedule_assistant")
_pkg.__path__ = [_PLUGIN_DIR]
sys.modules.setdefault("schedule_assistant", _pkg)
sys.modules["schedule_assistant.constants"] = _constants_mod
