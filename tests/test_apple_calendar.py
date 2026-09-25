"""Apple CalDAV 客户端的纯逻辑测试（无网络）。

覆盖三处对外不变量：
1. 写操作成败只看 HTTP 状态码 —— <500 时响应体一律非 None，正文分不出
   201/204 与 401/403（create_event 返回 uid / delete_event 返回 True 都必须
   以 2xx 为准，否则 4xx 会被当成功）；
2. XML 解析失败后的降级提取正则必须真能匹配（双重转义写成字面反斜杠即永久失效）；
3. _clean_href 的相对路径分支必须能提取 href。

_async_request / _request_with_status 用替身替换，不上网。
"""

import asyncio
import re
from datetime import datetime
from urllib.parse import urljoin

import pytest

from schedule_assistant.apple_calendar import AppleCalendar

CAL_ID = "11111111-2222-3333-4444-555555555555"
BASE_URL = "https://caldav.icloud.com/1234567890/calendars"
PUT_URL = f"{BASE_URL}/{CAL_ID}/some-uid.ics"


class FakeResponse:
    """最小响应替身：只保留状态码与正文"""

    def __init__(self, status, text=""):
        self.status = status
        self._text = text

    async def text(self, encoding=None, errors=None):
        return self._text


def make_calendar(monkeypatch, status, body="", list_calendars=True):
    """构造已完成发现的 AppleCalendar，请求走替身，记录调用参数

    只猴补传输层：`_discovered=True` 让 _discover() 直接短路，
    _list_calendars() 则走 PROPFIND 替身响应拿到测试日历（list_calendars=False
    时返回空列表，用于「无可写日历」分支）。
    """
    cal = AppleCalendar(username="me@example.com", app_password="pw")
    cal._discovered = True
    cal._principal_url = "https://caldav.icloud.com/1234567890/principal/"
    cal._caldav_base_url = BASE_URL
    cal._caldav_base_domain = "caldav.icloud.com"
    calls = []

    async def fake_request(url, method="GET", data=None, headers=None, **kw):
        calls.append(
            {
                "url": url,
                "method": method,
                "data": data,
                "headers": dict(headers or {}),
                "aware": data and b"BEGIN:VCALENDAR" in data,
            }
        )
        if method == "PROPFIND" and list_calendars:
            return (
                207,
                f'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">'
                f"<D:response><D:href>/{CAL_ID}/</D:href></D:response>"
                f"</D:multistatus>",
            )
        return status, body

    monkeypatch.setattr(cal, "_request_with_status", fake_request, raising=False)
    return cal, calls


def sync(coro):
    return asyncio.run(coro)


class TestCleanHref:
    """_clean_href 自己负责的清洗：空白 / 零宽空格 / 路径尾段截取"""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # 相对路径 + 首尾空白：提取不到就必须靠后续清洗兜住
            ("  /1234567890/principal/  ", "/1234567890/principal/"),
            # 零宽空格由 replace 去掉
            ("/1234567890/calendars/\u200b", "/1234567890/calendars/"),
            # 路径后跟空格：以路径尾段为返回值，空格必须被截掉
            ("/1/calendars/ x", "/1/calendars/"),
            # 绝对 URL 走第一条分支
            (
                "https://caldav.icloud.com/1234567890/calendars/",
                "https://caldav.icloud.com/1234567890/calendars/",
            ),
        ],
    )
    def test_extracts_href(self, raw, expected):
        assert AppleCalendar._clean_href(raw) == expected

    def test_empty_href(self):
        assert AppleCalendar._clean_href("") == ""
        assert AppleCalendar._clean_href(None) == ""


class TestDiscoverFallback:
    """XML 解析失败时 _discover 的降级提取（正则写坏即永久失效的回归点）

    断言精确 URL（host 必须是 caldav.icloud.com）：降级分支把提取到的相对路径
    交给 _to_absolute_url 组装，任何多补的前导斜杠都会让 URL 变成 "//<dsid>/…"
    这种网络路径引用，urljoin 随之把 <dsid> 当成 host。
    """

    DSID = "1234567890"
    CAL_ID = "137"  # iCloud calendar home 的 <dsid>/calendars 段

    def test_principal_extracted_from_broken_xml(self, monkeypatch):
        """降级分支能从坏 XML 里提取出 principal 路径并组装成正确 URL"""
        cal = AppleCalendar(username="me@example.com", app_password="pw")
        bodies = {
            "https://caldav.icloud.com/": f"broken <<< /{self.DSID}/principal/",
        }
        urls = []

        async def fake_request(url, method="GET", *args, **kw):
            urls.append(url)
            text = bodies.get(url)
            return (0, None) if text is None else (207, text)

        monkeypatch.setattr(cal, "_request_with_status", fake_request, raising=False)
        assert sync(cal._discover()) is False  # principal 拿到后中止（无 home set 响应）
        assert urls == [
            "https://caldav.icloud.com/",
            f"https://caldav.icloud.com/{self.DSID}/principal",
        ]

    def test_calendar_home_from_relative_path(self, monkeypatch):
        """相对 calendar home（无绝对 URL）走第二条正则，host 全程保持 caldav.icloud.com"""
        cal = AppleCalendar(username="me@example.com", app_password="pw")
        first = f"broken <<< /{self.DSID}/principal/"
        second = f"broken <<< /{self.CAL_ID}/calendars/"
        urls = []

        async def fake_request(url, method="GET", *args, **kw):
            urls.append(url)
            return (207, first) if len(urls) == 1 else (207, second)

        monkeypatch.setattr(cal, "_request_with_status", fake_request, raising=False)
        assert sync(cal._discover()) is True  # home set 齐了就发现成功
        assert urls == [
            "https://caldav.icloud.com/",
            f"https://caldav.icloud.com/{self.DSID}/principal",
        ]
        assert cal._principal_url == (
            f"https://caldav.icloud.com/{self.DSID}/principal"
        )
        # base 由降级正则提取出的相对 calendar home 绝对化，host 不变
        # （绝对路径 "/137/calendars" 会替换 principal 路径，这正是原实现的拼接口径）
        assert cal._caldav_base_url == (
            f"https://caldav.icloud.com/{self.CAL_ID}/calendars"
        )
        assert cal._caldav_base_domain == "caldav.icloud.com"

    def test_calendar_home_absolute_url(self, monkeypatch):
        """响应里带绝对 calendar home URL 时，_discover 走完全流程返回 True"""
        cal = AppleCalendar(username="me@example.com", app_password="pw")
        home = f"https://p{self.CAL_ID}-caldav.icloud.com/{self.CAL_ID}/calendars/"
        first = f"broken <<< /{self.DSID}/principal/"
        second = f"broken <<< {home}"  # 尾段即 /calendars/，绝对 URL 正则命中
        urls = []

        async def fake_request(url, method="GET", *args, **kw):
            urls.append(url)
            return (207, first) if len(urls) == 1 else (207, second)

        monkeypatch.setattr(cal, "_request_with_status", fake_request, raising=False)
        assert sync(cal._discover()) is True
        assert cal._caldav_base_url == home.rstrip("/")
        assert cal._caldav_base_domain == f"p{self.CAL_ID}-caldav.icloud.com"
        assert len(urls) == 2

    def test_broken_xml_without_any_path_fails_discovery(self, monkeypatch):
        """响应里没有任何可提取路径时正常失败（降级分支不能瞎猜）"""
        cal = AppleCalendar(username="me@example.com", app_password="pw")

        async def fake_request(url, method="GET", *args, **kw):
            return 207, "broken xml with no path at all"

        monkeypatch.setattr(cal, "_request_with_status", fake_request, raising=False)
        assert sync(cal._discover()) is False


class TestRequestWithStatus:
    """请求层：失败返回状态码 0，成功返回真实状态码"""

    def test_network_failure_is_status_zero(self, monkeypatch):
        import aiohttp

        class BoomSession:
            async def __aenter__(self):
                raise aiohttp.ClientError("boom")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(aiohttp, "ClientSession", lambda: BoomSession())
        cal = AppleCalendar(username="u", app_password="p")
        status, text = sync(cal._request_with_status("https://x/", retries=1))
        assert status == 0
        assert text is None


class TestCreateEventStatus:
    """create_event 只在 2xx 返回 uid（4xx 一律 None）"""

    @pytest.mark.parametrize(
        "status,body", [(201, ""), (200, "created"), (204, "")]
    )
    def test_2xx_returns_uid(self, monkeypatch, status, body):
        cal, calls = make_calendar(monkeypatch, status, body)
        uid = sync(
            cal.create_event(
                summary="开会",
                start=datetime(2026, 9, 10, 14, 30),
                end=datetime(2026, 9, 10, 15, 30),
            )
        )
        assert uid
        put = [c for c in calls if c["method"] == "PUT"]
        assert len(put) == 1
        assert put[0]["url"] == f"{BASE_URL}/{CAL_ID}/{uid}.ics"
        assert f"UID:{uid}".encode() in put[0]["data"]

    @pytest.mark.parametrize(
        "status,body",
        [
            (401, "Unauthorized"),
            (403, "Forbidden"),
            (404, "Not Found"),
            (412, "Precondition Failed"),
        ],
    )
    def test_4xx_returns_none(self, monkeypatch, status, body):
        """401/403 的响应体同样非 None：只看正文就会把失败当成功"""
        cal, calls = make_calendar(monkeypatch, status, body)
        uid = sync(
            cal.create_event(summary="开会", start=datetime(2026, 9, 10, 14, 30))
        )
        assert uid is None
        assert [c for c in calls if c["method"] == "PUT"]

    def test_5xx_returns_none(self, monkeypatch):
        cal, _calls = make_calendar(monkeypatch, 500, "Server Error")
        assert sync(cal.create_event("开会", datetime(2026, 9, 10, 14, 30))) is None

    def test_no_calendar_returns_none(self, monkeypatch):
        cal, _calls = make_calendar(monkeypatch, 201, "", list_calendars=False)
        assert sync(cal.create_event("开会", datetime(2026, 9, 10, 14, 30))) is None


class TestDeleteEventStatus:
    """delete_event 只在 2xx 返回 True"""

    @pytest.mark.parametrize("status", [204, 200, 202])
    def test_2xx_returns_true(self, monkeypatch, status):
        cal, calls = make_calendar(monkeypatch, status)
        assert sync(cal.delete_event("some-uid")) is True
        deletes = [c for c in calls if c["method"] == "DELETE"]
        assert deletes and deletes[0]["url"] == PUT_URL

    @pytest.mark.parametrize(
        "status,body", [(401, "Unauthorized"), (403, "Forbidden"), (404, "Not Found")]
    )
    def test_4xx_returns_false(self, monkeypatch, status, body):
        cal, _calls = make_calendar(monkeypatch, status, body)
        assert sync(cal.delete_event("some-uid")) is False

    def test_5xx_returns_false(self, monkeypatch):
        cal, _calls = make_calendar(monkeypatch, 500, "Server Error")
        assert sync(cal.delete_event("some-uid")) is False


class TestUpdateEventStatus:
    """update_event：沿用既有 UID 的 PUT（不新生成 UID）"""

    def test_2xx_updates_same_uid(self, monkeypatch):
        cal, calls = make_calendar(monkeypatch, 204)
        ok = sync(
            cal.update_event(
                "existing-uid",
                summary="改过的组会",
                start=datetime(2026, 9, 11, 9, 0),
                end=datetime(2026, 9, 11, 11, 0),
            )
        )
        assert ok is True
        put = [c for c in calls if c["method"] == "PUT"][0]
        assert put["url"] == f"{BASE_URL}/{CAL_ID}/existing-uid.ics"
        payload = put["data"].decode()
        assert "UID:existing-uid" in payload
        assert "SUMMARY:改过的组会" in payload
        assert "DTSTART;TZID=Asia/Shanghai:20260911T090000" in payload

    def test_4xx_returns_false(self, monkeypatch):
        cal, _calls = make_calendar(monkeypatch, 403, "Forbidden")
        ok = sync(cal.update_event("existing-uid", "组会", datetime(2026, 9, 11, 9, 0)))
        assert ok is False

    def test_empty_uid_returns_false(self, monkeypatch):
        cal, calls = make_calendar(monkeypatch, 204)
        assert sync(cal.update_event("", "组会", datetime(2026, 9, 11, 9, 0))) is False
        assert [c for c in calls if c["method"] == "PUT"] == []


class TestCalendarResolution:
    """日历 UID 解析口径：显式参数 > 配置值 > 第一个日历"""

    CALENDARS = [
        {"id": "cal-a", "name": "个人", "url": "u-a"},
        {"id": "cal-b", "name": "工作日程", "url": "u-b"},
    ]

    def test_explicit_wins(self):
        """显式传入的 calendar_id 优先级最高（配置值不参与）"""
        cal = AppleCalendar(calendar_id="配置里写的")
        assert cal._resolve_calendar_id(self.CALENDARS, "cal-a") == "cal-a"

    def test_config_value_wins_over_first(self):
        """配置值直接当 UID 用（原实现的名称模糊匹配分支不可达：
        resolved_id = calendar_id or self._calendar_id 已经先赋成名称，
        源码里那个 for 循环的 if not resolved_id 永远不成立 —— 保持既有
        语义不动，update_event 复用同一口径）"""
        cal = AppleCalendar(calendar_id="cal-b")
        assert cal._resolve_calendar_id(self.CALENDARS, None) == "cal-b"

    def test_fallback_first(self):
        cal = AppleCalendar()
        assert cal._resolve_calendar_id(self.CALENDARS, None) == "cal-a"
