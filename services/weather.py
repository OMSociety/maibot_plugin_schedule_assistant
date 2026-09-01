"""天气服务 - 心知天气 API，带 30 分钟缓存"""

import asyncio
import time as _time

import aiohttp


class WeatherService:
    def __init__(self, config: dict):
        self.weather_api_key = config.get("weather_api_key", "")
        self.weather_city = config.get("weather_city", "北京")
        self._cache: dict = {"data": ("", ""), "timestamp": 0}
        self._CACHE_TTL = 1800

    async def fetch(self) -> tuple[str, str]:
        weather_current, weather_forecast = "", ""
        if not self.weather_api_key:
            return "未配置天气API", ""
        if (self._cache["data"][0] or self._cache["data"][1]) and (
            _time.time() - self._cache["timestamp"] < self._CACHE_TTL
        ):
            return self._cache["data"]
        try:
            async with aiohttp.ClientSession() as session:
                now_params = {
                    "key": self.weather_api_key,
                    "location": self.weather_city,
                    "language": "zh-Hans",
                    "unit": "c",
                }
                async with session.get(
                    "https://api.seniverse.com/v3/weather/now.json",
                    params=now_params,
                    timeout=20,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", [])
                        if results:
                            w = results[0].get("now", {})
                            weather_current = (
                                f"{w.get('text', '未知')}, "
                                f"{w.get('temperature', '?')}°C"
                            )
                daily_params = {
                    "key": self.weather_api_key,
                    "location": self.weather_city,
                    "language": "zh-Hans",
                    "unit": "c",
                }
                async with session.get(
                    "https://api.seniverse.com/v3/weather/daily.json",
                    params=daily_params,
                    timeout=20,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", [])
                        if results:
                            daily = results[0].get("daily", [])
                            if daily:
                                t = daily[0]
                                weather_forecast = (
                                    f"白天{t.get('text_day', '未知')} / "
                                    f"夜间{t.get('text_night', '未知')}, "
                                    f"{t.get('low', '?')}~{t.get('high', '?')}°C, "
                                    f"降水概率{t.get('precip', '0')}%"
                                )
        except asyncio.TimeoutError:
            weather_current = "获取超时"
        except Exception:
            weather_current = "获取失败"
        self._cache = {
            "data": (weather_current, weather_forecast),
            "timestamp": _time.time(),
        }
        return weather_current, weather_forecast
