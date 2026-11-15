"""Apple iCloud CalDAV 日历同步模块（MaiBot 插件版）
支持：日历发现 / PROPFIND读取事件 / 创建&删除事件 / 时区正确处理"""

import asyncio
import base64
import html
import ipaddress
import logging
import re
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone, tzinfo
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import aiohttp

logger = logging.getLogger(__name__)

__all__ = ["AppleCalendar"]

# CalDAV 发现入口。默认入口对中国大陆区账号同样可用，分区主机由 Apple 服务端
# 在 principal / calendar-home-set 里下发；.com.cn 仅作为被 .com 拒绝认证时的
# 备用入口（配置项 calendar_sync.apple_caldav_host 可覆盖两者）。
CALDAV_ENTRY_URL = "https://caldav.icloud.com/"
CALDAV_CN_ENTRY_URL = "https://caldav.icloud.com.cn/"


def _escape_ics_text(text: str) -> str:
    """按 RFC 5545 转义 TEXT 值（反斜杠、分号、逗号、换行）。"""
    if not text:
        return ""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


class AppleCalendar:
    """Apple iCloud / CalDAV 日历客户端"""

    def __init__(
        self,
        username: str | None = None,
        app_password: str | None = None,
        webcal_urls: list[str] | None = None,
        calendar_id: str | None = None,
        caldav_host: str | None = None,
        timezone: tzinfo | None = None,
    ):
        self.username = username
        self.app_password = app_password
        self.webcal_urls = webcal_urls or []
        self._tz = timezone or ZoneInfo("Asia/Shanghai")
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
        self._caldav_entry_url = self._normalize_entry_url(caldav_host)
        self._last_discover_reason: str | None = None
        self._calendars_logged = False

    def _now(self) -> datetime:
        return datetime.now(self._tz) if self._tz else datetime.now()

    def _local_tz(self) -> tzinfo:
        return self._tz or datetime.now().astimezone().tzinfo or timezone.utc

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
        """异步 HTTP 请求（aiohttp），带重试；只返回响应体文本。

        仅需正文的读取路径用这个包装；写路径要判成败必须用
        _request_with_status（<500 时响应体同样非 None，正文无法区分
        201/204 与 401/403）。
        """
        _status, text = await self._request_with_status(
            url,
            method=method,
            data=data,
            headers=headers,
            timeout=timeout,
            retries=retries,
        )
        return text

    async def _request_with_status(
        self,
        url: str,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict | None = None,
        timeout: int = 30,
        retries: int = 3,
    ) -> tuple[int, str | None]:
        """异步 HTTP 请求（aiohttp），带重试；返回 (状态码, 响应体)。

        网络失败（ClientError / TimeoutError）返回 (0, None) —— 状态码 0
        不是任何合法 HTTP 状态，调用方可据此把「请求没发出去」与
        「服务端返回 4xx」区分开。
        """
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
                        allow_redirects=False,
                    ) as resp,
                ):
                    if 300 <= resp.status < 400:
                        logger.warning(
                            f"[AppleCalendar] 拒绝重定向 {resp.status} {url}"
                        )
                        return 0, None
                    if resp.status >= 500 and attempt < retries - 1:
                        await asyncio.sleep(1 * (attempt + 1))
                        last_error = aiohttp.ClientResponseError(
                            resp.request_info, resp.history, status=resp.status
                        )
                        continue
                    text = await resp.text(encoding="utf-8", errors="replace")
                    return resp.status, text
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
        logger.warning(
            f"[AppleCalendar] 请求异常 {method} {url}: "
            f"{type(last_error).__name__}: {last_error}"
        )
        return 0, None

    @staticmethod
    def _clean_href(raw: str) -> str:
        href = html.unescape((raw or "").strip())
        href = href.replace("\u200b", "")
        href = re.sub(r"[\r\n\t]", "", href)
        href = href.strip("'" + "<>\\")
        for splitter in ('">', "'>", "<", ">"):
            if splitter in href:
                href = href.split(splitter, 1)[0]
        m = re.search(r"(https?://[^\s<>'\"]+|/[^\s<>'\"]+)", href)
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

    @staticmethod
    def _normalize_entry_url(host: str | None) -> str:
        """把配置里的主机整理成可打 PROPFIND 的入口 URL（留空用默认入口）。"""
        text = (host or "").strip()
        if not text:
            return CALDAV_ENTRY_URL
        if "://" not in text:
            text = "https://" + text
        return text.rstrip("/") + "/"

    @staticmethod
    def _extract_calendar_names(xml_text: str) -> dict[str, str]:
        """从日历集合的 multistatus 里取 href → displayname。"""
        if not xml_text:
            return {}
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return {}
        names: dict[str, str] = {}
        for elem in root.iter():
            if not elem.tag.endswith("response"):
                continue
            href: str | None = None
            displayname = ""
            for child in elem.iter():
                if child.tag.endswith("href") and child.text and href is None:
                    href = AppleCalendar._clean_href(child.text)
                elif child.tag.endswith("displayname"):
                    displayname = (child.text or "").strip()
            if href:
                names.setdefault(href, displayname)
        return names

    def _describe_http_failure(self, status: int | None, body: str | None) -> str:
        """把发现失败的状态码翻译成用户能照着做的一句话。"""
        if status in (None, 0):
            return "无法连接 Apple 服务器（网络不通或被拦截），请检查本机网络与代理"
        if status == 401:
            return (
                "Apple 未接受凭据（HTTP 401），"
                "请确认 Apple ID 邮箱与 App 专用密码都已填写"
            )
        if status == 403:
            return (
                "Apple 拒绝认证（HTTP 403）：App 专用密码错误或已失效，"
                "需在 appleid.apple.com 重新生成「App 专用密码」，不能用 iCloud 登录密码"
            )
        snippet = (body or "").strip().replace("\n", " ")[:200]
        if snippet:
            return f"PROPFIND 返回 HTTP {status}: {snippet}"
        return f"PROPFIND 返回 HTTP {status}"

    def _report_discover_failure(self, reason: str) -> None:
        """报告发现失败：同一原因每实例只报一次 ERROR，重复的降为 DEBUG。"""
        if reason == self._last_discover_reason:
            logger.debug(f"[AppleCalendar] CalDAV 发现失败：{reason}")
            return
        self._last_discover_reason = reason
        logger.error(f"[AppleCalendar] CalDAV 发现失败：{reason}")

    async def _discover(self) -> bool:
        """发现 principal URL 和 calendar home set URL"""
        if self._discovered or not self.username or not self.app_password:
            return bool(self._discovered)
        async with self._discover_lock:
            if self._discovered:
                return True
            headers = {
                "Authorization": self._auth_header(),
                "Content-Type": "text/xml",
            }
            body1 = b'<?xml version="1.0" encoding="UTF-8"?><D:propfind xmlns:D="DAV:"><D:prop><D:current-user-principal/></D:prop></D:propfind>'
            status1, resp1 = await self._request_with_status(
                self._caldav_entry_url,
                method="PROPFIND",
                data=body1,
                headers=headers,
            )
            if status1 == 403 and self._caldav_entry_url == CALDAV_ENTRY_URL:
                cn_status, cn_resp = await self._request_with_status(
                    CALDAV_CN_ENTRY_URL,
                    method="PROPFIND",
                    data=body1,
                    headers=headers,
                )
                if cn_status == 207:
                    self._caldav_entry_url = CALDAV_CN_ENTRY_URL
                    status1, resp1 = cn_status, cn_resp
                    logger.info(
                        "[AppleCalendar] caldav.icloud.com 拒绝凭据，"
                        f"已改用中国大陆区入口 {CALDAV_CN_ENTRY_URL}"
                    )
                else:
                    self._report_discover_failure(
                        self._describe_http_failure(cn_status, cn_resp)
                        + f"（已同时尝试中国大陆区入口 {CALDAV_CN_ENTRY_URL}）"
                    )
                    return False
            if status1 != 207:
                self._report_discover_failure(
                    self._describe_http_failure(status1, resp1)
                )
                return False
            principal_href = self._extract_href(resp1 or "", "current-user-principal")
            if not principal_href:
                # XML 解析失败时的降级提取（iCloud principal 形如 /<dsid>/principal/）。
                # 捕获组已含前导斜杠，直接用作绝对路径：再补一个 "/" 会拼成
                # "//<dsid>/principal"，被 urljoin 当网络路径引用、host 变成 dsid
                m = re.search(r"(/\d+/\w+)/?$", resp1 or "")
                principal_href = m.group(1) if m else None
            if not principal_href:
                self._report_discover_failure("响应中没有 principal URL（HTTP 207）")
                return False
            self._principal_url = self._to_absolute_url(
                self._caldav_entry_url.rstrip("/"), principal_href
            )
            if not self._principal_url:
                self._report_discover_failure("principal URL 组装失败")
                return False
            logger.debug(f"[AppleCalendar] principal URL: {self._principal_url}")
            body2 = b'<?xml version="1.0" encoding="UTF-8"?><D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><C:calendar-home-set/></D:prop></D:propfind>'
            status2, resp2 = await self._request_with_status(
                self._principal_url,
                method="PROPFIND",
                data=body2,
                headers=headers,
            )
            if status2 != 207:
                self._report_discover_failure(
                    self._describe_http_failure(status2, resp2)
                )
                return False
            cal_home_href = self._extract_href(resp2 or "", "calendar-home-set")
            if not cal_home_href:
                # 降级提取：先找绝对 URL，再找 /<dsid>/calendars/ 相对路径。
                # 同理，捕获组已含前导斜杠，不再补 "/"（补了会变成 //<dsid>/calendars/）
                m = re.search(r"https?://[^\s<>\"']+/calendars/", resp2 or "")
                if m:
                    cal_home_href = m.group(0).rstrip("/")
                else:
                    m = re.search(r"/(\d+/calendars/?)", resp2 or "")
                    if m:
                        cal_home_href = "/" + m.group(1).rstrip("/")
            if not cal_home_href:
                self._report_discover_failure(
                    "响应中没有 calendar home set URL（HTTP 207）"
                )
                return False
            self._caldav_base_url = self._to_absolute_url(
                self._principal_url, cal_home_href
            )
            if not self._caldav_base_url:
                self._report_discover_failure("calendar home set URL 组装失败")
                return False
            self._caldav_base_domain = urlparse(self._caldav_base_url).netloc
            self._discovered = True
            self._last_discover_reason = None
            logger.info(
                f"[AppleCalendar] CalDAV 已连接: "
                f"{self._caldav_entry_url.rstrip('/')} (base={self._caldav_base_url})"
            )
            return True

    async def _caldav_fetch_with_status(
        self, cal_url: str, days: int = 30
    ) -> tuple[list[dict], str | None]:
        """纯异步 CalDAV 抓取：PROPFIND → 并发拉 .ics → 解析。

        返回 (事件列表, 未完整原因)。原因非 None 表示这次列出的文件没有全部
        读到，调用方不得据此判定事件已在 Apple 端删除。
        """
        body = b'<?xml version="1.0" encoding="UTF-8"?><D:propfind xmlns:D="DAV:"><D:prop><D:href/></D:prop></D:propfind>'
        status, resp = await self._request_with_status(
            cal_url.rstrip("/") + "/",
            method="PROPFIND",
            data=body,
            headers={
                "Authorization": self._auth_header(),
                "Content-Type": "text/xml",
                "Depth": "1",
            },
        )
        if status != 207:
            return [], (
                "抓取日历事件失败（status="
                f"{status if status is not None else '无响应'}）"
            )
        ics_urls: list[str] = []
        seen_urls: set[str] = set()
        for m in re.findall(r"<(?:D:)?href[^>]*>([^<]+)</(?:D:)?href>", resp or ""):
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
            return [], None
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
        failed = 0
        for ics_data in results:
            if ics_data:
                events.extend(self._parse_vevents(ics_data))
            else:
                failed += 1
        if failed:
            return events, f"{failed}/{len(ics_urls)} 个事件文件未拉到"
        return events, None

    async def _list_calendars_with_status(self) -> tuple[list[dict], str | None]:
        """列出所有日历，带缓存；返回 (日历列表, 未完整原因)。"""
        now_ts = time.monotonic()
        if (
            self._calendars_cache
            and (now_ts - getattr(self, "_calendars_ts", 0))
            < self._calendars_cache_ttl_seconds
        ):
            return list(self._calendars_cache), None
        if not await self._discover():
            return [], "CalDAV 未连接"
        status, resp = await self._request_with_status(
            self._caldav_base_url + "/",
            method="PROPFIND",
            data=b'<?xml version="1.0" encoding="UTF-8"?><D:propfind xmlns:D="DAV:"><D:prop><D:href/><D:displayname/></D:prop></D:propfind>',
            headers={
                "Authorization": self._auth_header(),
                "Content-Type": "text/xml",
                "Depth": "1",
            },
        )
        if status != 207:
            reason = (
                f"列出日历失败（status={status if status is not None else '无响应'}）"
            )
            logger.warning(f"[AppleCalendar] {reason}")
            return [], reason
        names = AppleCalendar._extract_calendar_names(resp or "")
        calendars = []
        seen_uuids = set()  # 去重
        for m in re.findall(r"<(?:D:)?href[^>]*>([^<]+)</(?:D:)?href>", resp or ""):
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
                    {
                        "href": href,
                        "url": cal_url,
                        "id": cal_uuid,
                        "name": names.get(href, ""),
                    }
                )
        self._calendars = calendars
        self._calendars_cache = list(calendars)
        self._calendars_ts = time.monotonic()
        logger.debug(f"[AppleCalendar] 发现 {len(calendars)} 个日历")
        if calendars and not self._calendars_logged:
            self._calendars_logged = True
            logger.info(
                "[AppleCalendar] 可用日历（apple_calendar_id 可填下列 UUID）:\n"
                + "\n".join(
                    f"  {c['name'] or '(未命名)'} → {c['id']}" for c in calendars
                )
            )
        return calendars, None

    def _parse_vevents(self, ical_data: str) -> list[dict]:
        """解析 VEVENT，正确处理 UTC 和本地时区

        iCloud ICS 格式支持:
        - DTSTART:20260422T073500Z           (UTC时间，带Z后缀)
        - DTSTART;TZID=Asia/Shanghai:...     (本地时间，带TZID)
        - DTSTART;VALUE=DATE:20260202        (全天事件)
        """
        events = []
        local_tz = self._local_tz()

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

    @staticmethod
    def _is_safe_webcal_url(url) -> bool:
        """校验 WebCal 订阅地址是否安全（仅 http/https + 公网主机）。

        防 SSRF：拒绝 localhost / 内网 / 环回 / 链路本地 / 云元数据等地址。
        主机为 IP 字面量时直接校验；否则按主机名后缀与已知元数据主机做黑名单。
        域名解析（rebinding）风险应在部署侧/README 中另行提示。

        Args:
            url: 待校验的 WebCal 地址（允许 webcal:// 前缀）

        Returns:
            True 表示可安全请求。
        """
        if not url or not isinstance(url, str):
            return False
        http_url = url.replace("webcal://", "https://").strip()
        try:
            parsed = urlparse(http_url)
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        if not host:
            return False
        host_lower = host.lower()

        # 本地/内网主机名黑名单
        if host_lower in ("localhost", "localhost.localdomain", "broadcasthost"):
            return False
        if host_lower.endswith((".local", ".localhost", ".internal", ".home.arpa")):
            return False
        if host_lower in (
            "metadata.google.internal",
            "metadata.azure.internal",
            "instance-data",
            "instance-data.ec2.internal",
            "169.254.169.254.nip.io",
        ):
            return False

        # IP 字面量：直接校验
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return True  # 域名，已过主机名黑名单
        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )

    async def fetch_webcal_with_status(
        self, url: str, days: int = 30
    ) -> tuple[list[dict], str | None]:
        """读取一个 WebCal 订阅；返回 (事件, 未完整原因)。

        原因串不含订阅地址与异常文本，避免把私有订阅链接写进日志。
        """
        events: list[dict] = []
        # 防 SSRF：WebCal 地址必须为公网 https/http，拒绝内网/本地/云元数据地址
        if not self._is_safe_webcal_url(url):
            logger.warning("[AppleCalendar] 拒绝不安全的 WebCal 地址")
            return events, "WebCal 地址不安全，已拒绝读取"
        http_url = url.replace("webcal://", "https://")
        status, text = await self._request_with_status(
            http_url, headers={"User-Agent": "Mozilla/5.0"}
        )
        if status != 200:
            reason = (
                "WebCal 读取失败（status="
                f"{status if status is not None else '无响应'}）"
            )
            logger.warning(f"[AppleCalendar] {reason}")
            return events, reason
        events = self._parse_vevents(text or "")
        logger.debug(f"[AppleCalendar] WebCal 读取成功: {len(events)} 个事件")
        return events, None

    def _cleanup_expired_cache(self):
        """清理过期的缓存（只保留今天的缓存）"""
        today = self._now().strftime("%Y%m%d")
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

    async def get_all_events(self, days: int = 1) -> tuple[list[dict], list[str]]:
        """获取所有日历事件（带缓存，去重）。

        返回 (事件列表, 未完整原因列表)。原因列表非空表示这一轮没有把所有
        事件读全（某日历 PROPFIND 失败、某个 .ics 没拉到、WebCal 读取失败），
        调用方不得据此判定事件已在 Apple 端删除。
        """
        # 缓存键包含日期，避免跨天返回错误数据
        today = self._now().strftime("%Y%m%d")
        cache_key = f"{today}_{int(days if days else 1)}"

        # 清理过期缓存
        self._cleanup_expired_cache()

        now_ts = time.monotonic()
        cached = self._events_cache.get(cache_key)
        if cached and (now_ts - cached.get("ts", 0)) < self._events_cache_ttl_seconds:
            logger.debug(
                f"[AppleCalendar] 使用缓存 key={cache_key}, 事件数={len(cached.get('events', []))}"
            )
            return list(cached.get("events", [])), list(cached.get("issues", []))

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
                return list(cached.get("events", [])), list(cached.get("issues", []))

            # 缓存不存在或已过期，重新获取
            all_events: dict[str, dict] = {}  # 用 dict 做去重，key 为 uid
            issues: list[str] = []

            if self.username and self.app_password:
                calendars, list_issue = await self._list_calendars_with_status()
                if list_issue:
                    issues.append(list_issue)
                logger.debug(f"[AppleCalendar] 准备获取 {len(calendars)} 个日历的事件")
                for cal in calendars:
                    cal_events, fetch_issue = await self._caldav_fetch_with_status(
                        cal["url"], days
                    )
                    if fetch_issue:
                        issues.append(
                            f"{cal.get('name') or cal.get('id', '?')}: {fetch_issue}"
                        )
                    for evt in cal_events:
                        uid = evt.get("uid")
                        if uid:
                            all_events[uid] = evt  # 去重
                    logger.debug(
                        f"[AppleCalendar] 日历 {cal.get('id', '?')} 获取到 {len(cal_events)} 个事件"
                    )

            for url in self.webcal_urls:
                webcal_events, webcal_issue = await self.fetch_webcal_with_status(
                    url, days
                )
                if webcal_issue:
                    issues.append(webcal_issue)
                for evt in webcal_events:
                    uid = evt.get("uid")
                    if uid:
                        all_events[uid] = evt
                logger.debug(
                    f"[AppleCalendar] WebCal 获取到 {len(webcal_events)} 个事件"
                )

            events_list = list(all_events.values())
            self._events_cache[cache_key] = {
                "ts": time.monotonic(),
                "events": events_list,
                "issues": list(issues),
            }
            detail = f"（不完整：{'；'.join(issues)}）" if issues else ""
            logger.info(
                f"[AppleCalendar] get_all_events(days={days}, cache_key={cache_key}) "
                f"返回 {len(events_list)} 个事件（去重后）{detail}"
            )
            return events_list, list(issues)

    def _resolve_calendar_id(
        self, calendars: list[dict], calendar_id: str | None
    ) -> str:
        """解析写入用日历 UID：显式参数 > 配置值 > 第一个日历。

        create_event / update_event / delete_event 共用同一口径（脱胎于原
        create_event 的解析段，含「配置值即当作 UID」的既有语义），保证同一
        条目始终落在同一个日历资源下。
        """
        resolved_id = calendar_id or self._calendar_id
        if not resolved_id:
            resolved_id = calendars[0]["id"]
            logger.debug(
                f"[AppleCalendar] 未找到指定日历，使用第一个: {resolved_id[:8]}..."
            )
        return resolved_id

    async def _upsert_event(
        self,
        uid: str,
        summary: str,
        start: datetime,
        end: datetime | None,
        calendar_id: str | None,
        description: str,
        all_day: bool,
    ) -> str | None:
        """按 UID 写入 VEVENT（CalDAV PUT）。

        同一 UID 落在同一 .ics 资源路径上：资源不存在时为新建（201），
        已存在时为更新（204）—— 更新既有事件必须沿用原 UID，绝不能新生成
        （新 UID 会变成第二条事件，旧条目还会被同步判为「Apple 已删除」）。
        成功（2xx）返回 uid，4xx/5xx/网络失败返回 None。
        """
        resolved_id = self._resolve_calendar_id(
            (await self._list_calendars_with_status())[0], calendar_id
        )
        cal_url = f"{self._caldav_base_url}/{resolved_id}/"
        tz = self._local_tz()
        tzid = getattr(tz, "key", None) or "UTC"
        start_aware = start if start.tzinfo else start.replace(tzinfo=tz)
        end_aware = end or (start + timedelta(hours=1))
        if end_aware.tzinfo is None:
            end_aware = end_aware.replace(tzinfo=tz)
        created = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if all_day:
            # 全天事件：VALUE=DATE，DTEND 为独占次日（RFC 5545）
            dtstart_line = f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}"
            dtend_fmt = (end or (start + timedelta(days=1))).strftime("%Y%m%d")
            dtend_line = f"DTEND;VALUE=DATE:{dtend_fmt}"
        else:
            dtstart_fmt = start_aware.astimezone(tz).strftime("%Y%m%dT%H%M%S")
            dtend_fmt = end_aware.astimezone(tz).strftime("%Y%m%dT%H%M%S")
            dtstart_line = f"DTSTART;TZID={tzid}:{dtstart_fmt}"
            dtend_line = f"DTEND;TZID={tzid}:{dtend_fmt}"
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{created}",
            dtstart_line,
            dtend_line,
            f"SUMMARY:{_escape_ics_text(summary)}",
        ]
        esc_desc = _escape_ics_text(description)
        if esc_desc:
            lines.append(f"DESCRIPTION:{esc_desc}")
        lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")
        vevent = ("\r\n".join(lines) + "\r\n").encode()
        event_url = f"{cal_url}{uid}.ics"
        status, _body = await self._request_with_status(
            event_url,
            method="PUT",
            data=vevent,
            headers={
                "Authorization": self._auth_header(),
                "Content-Type": "text/calendar",
            },
        )
        if 200 <= status < 300:
            logger.info(f"[AppleCalendar] 写入事件成功: {summary} (UID={uid})")
            return uid
        logger.warning(
            f"[AppleCalendar] 写入事件失败: {summary} (UID={uid}, HTTP {status})"
        )
        return None

    async def create_event(
        self,
        summary: str,
        start: datetime,
        end: datetime | None = None,
        calendar_id: str | None = None,
        description: str = "",
        all_day: bool = False,
    ) -> str | None:
        if not await self._discover():
            logger.warning("[AppleCalendar] CalDAV 未连接，无法创建事件")
            return None
        if not (await self._list_calendars_with_status())[0]:
            logger.warning("[AppleCalendar] 未找到可写日历")
            return None
        return await self._upsert_event(
            uid=str(uuid.uuid4()),
            summary=summary,
            start=start,
            end=end,
            calendar_id=calendar_id,
            description=description,
            all_day=all_day,
        )

    async def update_event(
        self,
        uid: str,
        summary: str,
        start: datetime,
        end: datetime | None = None,
        calendar_id: str | None = None,
        description: str = "",
        all_day: bool = False,
    ) -> bool:
        """按既有 UID 更新 Apple 事件（同 UID 的 CalDAV PUT 即覆盖原资源）"""
        if not uid:
            logger.warning("[AppleCalendar] 缺少 UID，无法更新事件")
            return False
        if not await self._discover():
            logger.warning("[AppleCalendar] CalDAV 未连接，无法更新事件")
            return False
        if not (await self._list_calendars_with_status())[0]:
            logger.warning("[AppleCalendar] 未找到可写日历")
            return False
        updated_uid = await self._upsert_event(
            uid=uid,
            summary=summary,
            start=start,
            end=end,
            calendar_id=calendar_id,
            description=description,
            all_day=all_day,
        )
        return updated_uid is not None

    async def delete_event(self, uid: str, calendar_id: str | None = None) -> bool:
        if not await self._discover():
            logger.warning("[AppleCalendar] CalDAV 未连接，无法删除事件")
            return False
        calendars = (await self._list_calendars_with_status())[0]
        if not calendars:
            logger.warning("[AppleCalendar] 未找到可写日历")
            return False
        resolved_id = self._resolve_calendar_id(calendars, calendar_id)
        cal_url = f"{self._caldav_base_url}/{resolved_id}/"
        event_url = f"{cal_url}{uid}.ics"
        status, _body = await self._request_with_status(
            event_url, method="DELETE", headers={"Authorization": self._auth_header()}
        )
        # DELETE 成功由状态码判定：204 无正文，401/403 也可能是非空正文
        if 200 <= status < 300:
            logger.info(f"[AppleCalendar] 删除事件成功: UID={uid}")
            return True
        logger.warning(f"[AppleCalendar] 删除事件失败: UID={uid} HTTP {status}")
        return False

    async def close(self):
        pass
