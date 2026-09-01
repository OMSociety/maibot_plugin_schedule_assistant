"""Apple iCloud CalDAV 日历同步模块（MaiBot 插件版）
支持：日历发现 / PROPFIND读取事件 / 创建&删除事件 / 时区正确处理"""

import asyncio
import base64
import html
import logging
import re
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import aiohttp

logger = logging.getLogger(__name__)

__all__ = ["AppleCalendar"]


class AppleCalendar:
    """Apple iCloud / CalDAV 日历客户端"""

    def __init__(
        self,
        username: str | None = None,
        app_password: str | None = None,
        webcal_urls: list[str] | None = None,
        calendar_id: str | None = None,
    ):
        self.username = username
        self.app_password = app_password
        self.webcal_urls = webcal_urls or []
        self._principal_url: str | None = None
        self._caldav_base_url: str | None = None
        self._caldav_base_domain: str | None = None
        self._calendars: list[dict] | None = None
        self._discovered = False
        self._discover_lock = asyncio.Lock()
        self._fetch_lock = asyncio.Lock()
        self._events_cache: dict[int, dict] = {}
        self._events_cache_ttl_seconds = 300
        self._calendars_cache: list[dict] = []
        self._calendars_cache_ttl_seconds = 300
        self._last_ics_discovery_log_ts = 0.0
        self._last_ics_discovery_count: int | None = None
        self._calendar_id = calendar_id

    def _auth_header(self) -> str:
        creds = f"{self.username}:{self.app_password}"
        return "Basic " + base64.b64encode(creds.encode()).decode()

    async def _aiohttp_request(
        self,
        url: str,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict | None = None,
        timeout: int = 30,
        retries: int = 3,
    ) -> str | None:
        """异步 HTTP 请求（aiohttp），带重试"""
        headers = dict(headers or {})
        headers.setdefault("User-Agent", "curl/7.88.1")
        last_error = None
        for attempt in range(retries):
            try:
                async with (
                    aiohttp.ClientSession() as session,
                    session.request(
                        method,
                        url,
                        data=data,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as resp,
                ):
                    if resp.status >= 500 and attempt < retries - 1:
                        await asyncio.sleep(1 * (attempt + 1))
                        last_error = aiohttp.ClientResponseError(
                            resp.request_info, resp.history, status=resp.status
                        )
                        continue
                    return await resp.text(encoding="utf-8", errors="replace")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
        logger.debug(
            f"[AppleCalendar] 请求异常 {url}: {type(last_error).__name__}: {last_error}"
        )
        return None

    async def _async_request(
        self,
        url: str,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict | None = None,
        timeout: int = 30,
        retries: int = 3,
    ) -> str | None:
        """向后兼容别名：直接委托给 _aiohttp_request"""
        return await self._aiohttp_request(
            url,
            method=method,
            data=data,
            headers=headers,
            timeout=timeout,
            retries=retries,
        )

    @staticmethod
    def _clean_href(raw: str) -> str:
        href = html.unescape((raw or "").strip())
        href = href.replace("\u200b", "")
        href = re.sub(r"[\r\n\t]", "", href)
        href = href.strip("'" + "<>\\")
        for splitter in ('">', "'>", "<", ">"):
            if splitter in href:
                href = href.split(splitter, 1)[0]
        m = re.search("(https?://[^\\s<>'\\\"]+|/^\\s<>'\\\"]+)", href)
        href = m.group(1) if m else href
        href = re.sub(r"\s+", "", href)
        return href

    @staticmethod
    def _extract_href(xml_text: str, parent_tag_suffix: str) -> str | None:
        if not xml_text:
            return None
        try:
            root = ET.fromstring(xml_text)
            for elem in root.iter():
                if elem.tag.endswith(parent_tag_suffix):
                    for child in elem.iter():
                        if child.tag.endswith("href") and child.text:
                            href = AppleCalendar._clean_href(child.text)
                            if href:
                                return href
        except ET.ParseError:
            pass
        return None

    @staticmethod
    def _to_absolute_url(base: str, href: str) -> str | None:
        href = AppleCalendar._clean_href(href)
        if not href:
            return None
        if href.startswith(("http://", "https://")):
            candidate = href.rstrip("/")
        else:
            candidate = urljoin(base.rstrip("/") + "/", href).rstrip("/")
        parsed = urlparse(candidate)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        return candidate

    async def _discover(self) -> bool:
        """发现 principal URL 和 calendar home set URL"""
        if self._discovered or not self.username or not self.app_password:
            return bool(self._discovered)
        async with self._discover_lock:
            if self._discovered:
                return True
            body1 = b'<?xml version="1.0" encoding="UTF-8"?><D:propfind xmlns:D="DAV:"><D:prop><D:current-user-principal/></D:prop></D:propfind>'
            resp1 = await self._async_request(
                "https://caldav.icloud.com/",
                method="PROPFIND",
                data=body1,
                headers={
                    "Authorization": self._auth_header(),
                    "Content-Type": "text/xml",
                },
            )
            if not resp1:
                logger.debug("[AppleCalendar] CalDAV 发现失败，未配置 Apple 日历")
                return False
            principal_href = self._extract_href(resp1, "current-user-principal")
            if not principal_href:
                m = re.search(r"(/\\d+/\\w+)/?$", resp1)
                principal_href = "/" + m.group(1) if m else None
            if not principal_href:
                logger.debug("[AppleCalendar] 无法解析 principal URL")
                return False
            self._principal_url = self._to_absolute_url(
                "https://caldav.icloud.com", principal_href
            )
            if not self._principal_url:
                logger.debug("[AppleCalendar] principal URL 组装失败")
                return False
            logger.debug(f"[AppleCalendar] principal URL: {self._principal_url}")
            body2 = b'<?xml version="1.0" encoding="UTF-8"?><D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><C:calendar-home-set/></D:prop></D:propfind>'
            resp2 = await self._async_request(
                self._principal_url,
                method="PROPFIND",
                data=body2,
                headers={
                    "Authorization": self._auth_header(),
                    "Content-Type": "text/xml",
                },
            )
            if not resp2:
                logger.debug("[AppleCalendar] principal URL 无响应，跳过日历发现")
                return False
            cal_home_href = self._extract_href(resp2, "calendar-home-set")
            if not cal_home_href:
                m = re.search(r"https?://[^\\s<>\"']+/calendars/", resp2)
                if m:
                    cal_home_href = m.group(0).rstrip("/")
                else:
                    m = re.search(r"/(\\d+/calendars/?)", resp2)
                    if m:
                        cal_home_href = "/" + m.group(1).rstrip("/")
            if not cal_home_href:
                logger.debug("[AppleCalendar] 无法解析 calendar home set URL")
                return False
            self._caldav_base_url = self._to_absolute_url(
                self._principal_url, cal_home_href
            )
            if not self._caldav_base_url:
                logger.debug("[AppleCalendar] calendar home set URL 组装失败")
                return False
            self._caldav_base_domain = urlparse(self._caldav_base_url).netloc
            self._discovered = True
            logger.debug(
                f"[AppleCalendar] CalDAV 发现成功: base={self._caldav_base_url}"
            )
            return True

    async def _caldav_fetch(self, cal_url: str, days: int = 30) -> list[dict]:
        """纯异步 CalDAV 抓取：PROPFIND → 并发拉 .ics → 解析"""
        body = b'<?xml version="1.0" encoding="UTF-8"?><D:propfind xmlns:D="DAV:"><D:prop><D:href/></D:prop></D:propfind>'
        resp = await self._async_request(
            cal_url.rstrip("/") + "/",
            method="PROPFIND",
            data=body,
            headers={
                "Authorization": self._auth_header(),
                "Content-Type": "text/xml",
                "Depth": "1",
            },
        )
        if not resp:
            return []
        ics_urls: list[str] = []
        seen_urls: set[str] = set()
        for m in re.findall(r"<(?:D:)?href[^>]*>([^<]+)</(?:D:)?href>", resp):
            href = m.strip()
            if href.endswith(".ics"):
                if href.startswith("/"):
                    ics_url = f"https://{self._caldav_base_domain}{href}"
                elif href.startswith("https://"):
                    ics_url = href
                else:
                    ics_url = f"{cal_url.rstrip('/')}/{href}"
                if ics_url not in seen_urls:
                    seen_urls.add(ics_url)
                    ics_urls.append(ics_url)
        if not ics_urls:
            return []
        now_ts = time.monotonic()
        current_count = len(ics_urls)
        if (
            self._last_ics_discovery_count != current_count
            or (now_ts - self._last_ics_discovery_log_ts) >= 300
        ):
            logger.debug(f"[AppleCalendar] 发现 {current_count} 个事件文件")
            self._last_ics_discovery_count = current_count
            self._last_ics_discovery_log_ts = now_ts

        async def _fetch_one(url: str) -> str | None:
            return await self._aiohttp_request(
                url, headers={"Authorization": self._auth_header()}, timeout=10
            )

        results = await asyncio.gather(*[_fetch_one(u) for u in ics_urls])
        events: list[dict] = []
        for ics_data in results:
            if ics_data:
                events.extend(self._parse_vevents(ics_data))
        return events

    async def _list_calendars(self) -> list[dict]:
        """列出所有日历，带缓存"""
        now_ts = time.monotonic()
        if (
            self._calendars_cache
            and (now_ts - getattr(self, "_calendars_ts", 0))
            < self._calendars_cache_ttl_seconds
        ):
            return list(self._calendars_cache)
        if not await self._discover():
            return []
        resp = await self._async_request(
            self._caldav_base_url + "/",
            method="PROPFIND",
            data=b'<?xml version="1.0" encoding="UTF-8"?><D:propfind xmlns:D="DAV:"><D:prop><D:href/></D:prop></D:propfind>',
            headers={
                "Authorization": self._auth_header(),
                "Content-Type": "text/xml",
                "Depth": "1",
            },
        )
        if not resp:
            return []
        calendars = []
        seen_uuids = set()  # 去重
        for m in re.findall(r"<(?:D:)?href[^>]*>([^<]+)</(?:D:)?href>", resp):
            href = AppleCalendar._clean_href(m.strip())
            if not href:
                continue
            # 检查是否是 UUID 日历（大小写不敏感）
            uuid_match = re.search(
                r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
                href,
            )
            if uuid_match:
                cal_uuid = uuid_match.group(1)
                if cal_uuid in seen_uuids:
                    continue
                seen_uuids.add(cal_uuid)
                # 构建日历 URL（优先使用绝对 URL）
                if href.startswith("http"):
                    cal_url = href.rstrip("/")
                else:
                    # 相对路径，使用 base_url 拼接
                    cal_url = f"{self._caldav_base_url.rstrip('/')}/{cal_uuid}"
                calendars.append(
                    {"href": href, "url": cal_url, "id": cal_uuid, "name": ""}
                )
        self._calendars = calendars
        self._calendars_cache = list(calendars)
        self._calendars_ts = time.monotonic()
        logger.debug(f"[AppleCalendar] 发现 {len(calendars)} 个日历")
        return calendars

    def _parse_vevents(self, ical_data: str) -> list[dict]:
        """解析 VEVENT，正确处理 UTC 和本地时区

        iCloud ICS 格式支持:
        - DTSTART:20260422T073500Z           (UTC时间，带Z后缀)
        - DTSTART;TZID=Asia/Shanghai:...     (本地时间，带TZID)
        - DTSTART;VALUE=DATE:20260202        (全天事件)
        """
        events = []
        local_tz = datetime.now().astimezone().tzinfo

        for ev in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", ical_data, re.DOTALL):
            summary_m = re.search(r"SUMMARY:([^\r\n]+)", ev)
            uid_m = re.search(r"UID:([^\r\n]+)", ev)
            desc_m = re.search(r"DESCRIPTION:([^\r\n]*)", ev)

            summary = summary_m.group(1).strip() if summary_m else "无标题"
            uid = uid_m.group(1).strip() if uid_m else str(uuid.uuid4())
            description = desc_m.group(1).replace("\\n", "\n").strip() if desc_m else ""

            # 解析 DTSTART
            dtstart_line = re.search(r"DTSTART[^\r\n]*", ev)
            dtstart_all_day = False
            start_time = None

            if dtstart_line:
                line = dtstart_line.group(0)

                # 全天事件检测
                if "VALUE=DATE" in line or re.search(r":\d{8}$", line):
                    dtstart_all_day = True
                    date_match = re.search(r":(\d{8})(?:T\d{6})?$", line)
                    if date_match:
                        start_time = datetime.strptime(date_match.group(1), "%Y%m%d")
                else:
                    # 提取时区信息
                    tzid_match = re.search(r"TZID=([^:]+)", line)
                    # 提取时间值
                    value_match = re.search(r":(\d{8}T\d{6})", line)

                    if value_match:
                        time_str = value_match.group(1)
                        naive = datetime.strptime(time_str, "%Y%m%dT%H%M%S")
                        if tzid_match:
                            try:
                                from dateutil import tz as dateutil_tz

                                tz_name = tzid_match.group(1)
                                tz_obj = dateutil_tz.gettz(tz_name)
                                if tz_obj:
                                    aware = naive.replace(tzinfo=tz_obj)
                                    start_time = aware.astimezone(local_tz).replace(
                                        tzinfo=None
                                    )
                                else:
                                    start_time = naive
                            except Exception:
                                start_time = naive
                        elif line.rstrip().endswith("Z"):
                            utc = naive.replace(tzinfo=timezone.utc)
                            start_time = utc.astimezone(local_tz).replace(tzinfo=None)
                        else:
                            start_time = naive

            # 解析 DTEND
            dtend_line = re.search(r"DTEND[^\r\n]*", ev)
            end_time = None

            if dtend_line and not dtstart_all_day:
                line = dtend_line.group(0)
                tzid_match = re.search(r"TZID=([^:]+)", line)
                value_match = re.search(r":(\d{8}T\d{6})", line)
                if value_match:
                    time_str = value_match.group(1)
                    naive = datetime.strptime(time_str, "%Y%m%dT%H%M%S")

                    if tzid_match:
                        try:
                            from dateutil import tz as dateutil_tz

                            tz_name = tzid_match.group(1)
                            tz_obj = dateutil_tz.gettz(tz_name)
                            if tz_obj:
                                aware = naive.replace(tzinfo=tz_obj)
                                end_time = aware.astimezone(local_tz).replace(
                                    tzinfo=None
                                )
                            else:
                                end_time = naive
                        except Exception:
                            end_time = naive
                    elif line.rstrip().endswith("Z"):
                        utc = naive.replace(tzinfo=timezone.utc)
                        end_time = utc.astimezone(local_tz).replace(tzinfo=None)
                    else:
                        end_time = naive

            if start_time:
                events.append(
                    {
                        "uid": uid,
                        "summary": summary,
                        "description": description,
                        "start": start_time.isoformat(),
                        "end": end_time.isoformat() if end_time else None,
                        "all_day": dtstart_all_day,
                    }
                )

        return events

    async def fetch_webcal_async(self, url: str, days: int = 30) -> list[dict]:
        events = []
        try:
            http_url = url.replace("webcal://", "https://")
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    http_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp,
            ):
                ical_data = await resp.text()
            events = self._parse_vevents(ical_data)
            logger.debug(f"[AppleCalendar] WebCal 读取成功: {len(events)} 个事件")
        except Exception as e:
            logger.warning(f"[AppleCalendar] WebCal 读取失败: {e}")
        return events

    def _cleanup_expired_cache(self):
        """清理过期的缓存（只保留今天的缓存）"""
        today = datetime.now().strftime("%Y%m%d")
        # 清理所有不以今天日期开头的缓存键（包括旧格式的整数键）
        expired_keys = [
            k for k in list(self._events_cache.keys()) if not str(k).startswith(today)
        ]
        if expired_keys:
            for k in expired_keys:
                del self._events_cache[k]
            logger.debug(
                f"[AppleCalendar] 清理了 {len(expired_keys)} 个过期缓存键: {expired_keys}"
            )

    async def get_all_events(self, days: int = 1) -> list[dict]:
        """获取所有日历事件（带缓存，去重）"""
        # 缓存键包含日期，避免跨天返回错误数据
        today = datetime.now().strftime("%Y%m%d")
        cache_key = f"{today}_{int(days if days else 1)}"

        # 清理过期缓存
        self._cleanup_expired_cache()

        now_ts = time.monotonic()
        cached = self._events_cache.get(cache_key)
        if cached and (now_ts - cached.get("ts", 0)) < self._events_cache_ttl_seconds:
            logger.debug(
                f"[AppleCalendar] 使用缓存 key={cache_key}, 事件数={len(cached.get('events', []))}"
            )
            return list(cached.get("events", []))

        async with self._fetch_lock:
            # 双重检查：加锁后再检查缓存（可能被其他协程填充）
            now_ts = time.monotonic()
            cached = self._events_cache.get(cache_key)
            if (
                cached
                and (now_ts - cached.get("ts", 0)) < self._events_cache_ttl_seconds
            ):
                logger.debug(
                    f"[AppleCalendar] 使用缓存(锁内) key={cache_key}, 事件数={len(cached.get('events', []))}"
                )
                return list(cached.get("events", []))

            # 缓存不存在或已过期，重新获取
            all_events: dict[str, dict] = {}  # 用 dict 做去重，key 为 uid

            if self.username and self.app_password:
                calendars = await self._list_calendars()
                logger.debug(f"[AppleCalendar] 准备获取 {len(calendars)} 个日历的事件")
                for cal in calendars:
                    cal_events = await self._caldav_fetch(cal["url"], days)
                    for evt in cal_events:
                        uid = evt.get("uid")
                        if uid:
                            all_events[uid] = evt  # 去重
                    logger.debug(
                        f"[AppleCalendar] 日历 {cal.get('id', '?')} 获取到 {len(cal_events)} 个事件"
                    )

            for url in self.webcal_urls:
                webcal_events = await self.fetch_webcal_async(url, days)
                for evt in webcal_events:
                    uid = evt.get("uid")
                    if uid:
                        all_events[uid] = evt
                logger.debug(
                    f"[AppleCalendar] WebCal {url} 获取到 {len(webcal_events)} 个事件"
                )

            events_list = list(all_events.values())
            self._events_cache[cache_key] = {
                "ts": time.monotonic(),
                "events": events_list,
            }
            logger.info(
                f"[AppleCalendar] get_all_events(days={days}, cache_key={cache_key}) 返回 {len(events_list)} 个事件（去重后）"
            )
            return events_list

    async def create_event(
        self,
        summary: str,
        start: datetime,
        end: datetime | None = None,
        calendar_id: str | None = None,
        description: str = "",
    ) -> str | None:
        if not await self._discover():
            logger.error("[AppleCalendar] CalDAV 未连接，无法创建事件")
            return None
        calendars = await self._list_calendars()
        if not calendars:
            logger.warning("[AppleCalendar] 未找到可写日历")
            return None

        # 优先级：传入参数 > 配置的 calendar_id > 第一个日历
        resolved_id = calendar_id or self._calendar_id
        if not resolved_id:
            # 尝试按名称匹配日历
            for c in calendars:
                if (
                    c.get("name")
                    and (self._calendar_id or calendar_id)
                    and self._calendar_id
                    and self._calendar_id in c.get("name", "")
                ):
                    resolved_id = c["id"]
                    break
            if not resolved_id:
                resolved_id = calendars[0]["id"]
                logger.debug(
                    f"[AppleCalendar] 未找到指定日历，使用第一个: {resolved_id[:8]}..."
                )
        cal_url = f"{self._caldav_base_url}/{resolved_id}/"
        uid = str(uuid.uuid4())
        dtstart_fmt = start.strftime("%Y%m%dT%H%M%S")
        dtend_fmt = (end or (start + timedelta(hours=1))).strftime("%Y%m%dT%H%M%S")
        created = datetime.now().strftime("%Y%m%dT%H%M%S")
        vevent = f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:{created}\r\nDTSTART;TZID=Asia/Shanghai:{dtstart_fmt}\r\nDTEND;TZID=Asia/Shanghai:{dtend_fmt}\r\nSUMMARY:{summary}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n".encode()
        event_url = f"{cal_url}{uid}.ics"
        resp = await self._async_request(
            event_url,
            method="PUT",
            data=vevent,
            headers={
                "Authorization": self._auth_header(),
                "Content-Type": "text/calendar",
            },
        )
        if resp is not None:
            logger.info(f"[AppleCalendar] 创建事件成功: {summary} (UID={uid})")
            return uid
        logger.error("[AppleCalendar] 创建事件失败（请检查网络）")
        return None

    async def delete_event(self, uid: str, calendar_id: str | None = None) -> bool:
        if not await self._discover():
            return False
        calendars = await self._list_calendars()
        if not calendars:
            return False
        resolved_id = calendar_id or self._calendar_id or calendars[0]["id"]
        cal_url = f"{self._caldav_base_url}/{resolved_id}/"
        event_url = f"{cal_url}{uid}.ics"
        resp = await self._async_request(
            event_url, method="DELETE", headers={"Authorization": self._auth_header()}
        )
        if resp is not None:
            logger.info(f"[AppleCalendar] 删除事件成功: UID={uid}")
            return True
        return False

    async def close(self):
        pass

    async def get_late_night_events(self) -> list[dict]:
        events = await self.get_all_events(days=1)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        late_night_end = today + timedelta(hours=6)
        late_night = []
        for e in events:
            start_str = e.get("start", "")
            if not start_str:
                continue
            try:
                start = datetime.fromisoformat(start_str)
                if start.hour == 0 and start.minute == 0 and start.second == 0:
                    continue
                if today <= start < late_night_end:
                    late_night.append(e)
            except ValueError:
                continue
        return late_night
