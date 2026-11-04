"""Apple CalDAV 客户端的读路径测试：诊断文案、入口主机、日历清单、读取完整性。

与 test_apple_calendar.py 共用替身口径（只替换 _request_with_status /
_aiohttp_request，不上网）。
"""

import asyncio
import time

import pytest

from schedule_assistant import apple_calendar as apple_module
from schedule_assistant.apple_calendar import AppleCalendar

DSID = "1234567890"
UUID_A = "D6B0E7A1-1111-2222-3333-444455556666"
UUID_B = "0A1B2C3D-AAAA-BBBB-CCCC-DDDDEEEEFFFF"
BASE_URL = f"https://caldav.icloud.com/{DSID}/calendars"

PRINCIPAL_XML = (
    '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"><D:response>'
    f"<D:href>/{DSID}/principal/</D:href><D:propstat><D:prop>"
    "<D:current-user-principal>"
    f"<D:href>/{DSID}/principal/</D:href>"
    "</D:current-user-principal></D:prop></D:propstat></D:response></D:multistatus>"
)
HOME_XML = (
    '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:" '
    'xmlns:C="urn:ietf:params:xml:ns:caldav"><D:response>'
    f"<D:href>/{DSID}/principal/</D:href><D:propstat><D:prop>"
    "<C:calendar-home-set>"
    f"<D:href>/{DSID}/calendars/</D:href>"
    "</C:calendar-home-set></D:prop></D:propstat></D:response></D:multistatus>"
)
CALENDARS_XML = (
    '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">'
    f"<D:response><D:href>/{DSID}/calendars/{UUID_A}/</D:href><D:propstat>"
    "<D:prop><D:displayname>工作日</D:displayname></D:prop></D:propstat>"
    "</D:response>"
    f"<D:response><D:href>/{DSID}/calendars/{UUID_B}/</D:href><D:propstat>"
    "<D:prop><D:displayname>中国节假日</D:displayname></D:prop></D:propstat>"
    "</D:response></D:multistatus>"
)
ICS_LIST_XML = (
    '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">'
    f"<D:response><D:href>/{DSID}/calendars/{UUID_A}/a.ics</D:href></D:response>"
    f"<D:response><D:href>/{DSID}/calendars/{UUID_A}/b.ics</D:href></D:response>"
    "</D:multistatus>"
)
ICS_BODY = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:ics-a\r\n"
    "SUMMARY:组会\r\nDTSTART:20300101T120000Z\r\nDTEND:20300101T130000Z\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def sync(coro):
    return asyncio.run(coro)


def make_calendar(monkeypatch, script):
    """按顺序返回 (status, text) 的发现流程替身，记录请求 URL。"""
    cal = AppleCalendar(username="me@example.com", app_password="pw")
    urls = []
    replies = list(script)

    async def fake_request(url, method="GET", data=None, headers=None, **kw):
        urls.append(url)
        assert replies, f"未预期的请求: {url}"
        return replies.pop(0)

    monkeypatch.setattr(cal, "_request_with_status", fake_request, raising=False)
    return cal, urls


def make_discovered(monkeypatch):
    """已发现完成的实例，读路径请求全部走替身。"""
    cal = AppleCalendar(username="me@example.com", app_password="pw")
    cal._discovered = True
    cal._caldav_base_url = BASE_URL
    cal._caldav_base_domain = "caldav.icloud.com"
    return cal


def error_texts(caplog):
    return [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]


class TestDiscoverDiagnostics:
    """发现失败的诊断：说明真实原因、同因只报一次、状态码优先于正文"""

    def test_403_empty_body_reports_actionable_reason(self, monkeypatch, caplog):
        cal, urls = make_calendar(monkeypatch, [(403, ""), (403, "")])
        with caplog.at_level("ERROR"):
            assert sync(cal._discover()) is False
        errors = error_texts(caplog)
        assert len(errors) == 1
        assert "403" in errors[0] and "App 专用密码" in errors[0]
        assert "未配置 Apple 日历" not in errors[0]
        # 默认入口被拒后应当再试一次国区入口
        assert urls == [apple_module.CALDAV_ENTRY_URL, apple_module.CALDAV_CN_ENTRY_URL]

    def test_403_with_principal_body_is_not_treated_as_success(self, monkeypatch):
        """4xx 正文里即使含 principal 路径，也只认状态码（fail-closed）"""
        cal, urls = make_calendar(monkeypatch, [(403, PRINCIPAL_XML)])
        cal._caldav_entry_url = "https://p01-caldav.icloud.com.cn/"
        assert sync(cal._discover()) is False
        assert len(urls) == 1  # 指定了入口就不再回退
        assert cal._principal_url is None

    def test_same_reason_reports_error_once(self, monkeypatch, caplog):
        cal, _urls = make_calendar(
            monkeypatch, [(403, ""), (403, ""), (403, ""), (403, "")]
        )
        with caplog.at_level("DEBUG"):
            assert sync(cal._discover()) is False
            assert sync(cal._discover()) is False
        errors = error_texts(caplog)
        assert len(errors) == 1
        assert len([r for r in caplog.records if r.levelname == "DEBUG"]) >= 1

    def test_network_failure_mentions_network(self, monkeypatch, caplog):
        cal, _urls = make_calendar(monkeypatch, [(0, None)])
        with caplog.at_level("ERROR"):
            assert sync(cal._discover()) is False
        assert "网络" in error_texts(caplog)[0]

    def test_207_without_principal_reports_parse_failure(self, monkeypatch, caplog):
        cal, _urls = make_calendar(monkeypatch, [(207, "无路径的响应")])
        with caplog.at_level("ERROR"):
            assert sync(cal._discover()) is False
        assert "响应中没有 principal URL" in error_texts(caplog)[0]

    def test_success_after_failure_clears_reason(self, monkeypatch, caplog):
        cal, urls = make_calendar(
            monkeypatch,
            [(403, ""), (403, ""), (207, PRINCIPAL_XML), (207, HOME_XML)],
        )
        with caplog.at_level("INFO"):
            assert sync(cal._discover()) is False
            assert cal._last_discover_reason is not None
            assert sync(cal._discover()) is True
        assert cal._last_discover_reason is None
        assert cal._caldav_base_url == BASE_URL
        assert urls[2] == apple_module.CALDAV_ENTRY_URL

    def test_create_event_pairs_reason_error_with_skip_warning(
        self, monkeypatch, caplog
    ):
        from datetime import datetime

        cal, _urls = make_calendar(monkeypatch, [(403, ""), (403, "")])
        with caplog.at_level("WARNING"):
            uid = sync(cal.create_event("开会", datetime(2030, 1, 1, 12, 0)))
        assert uid is None
        errors = error_texts(caplog)
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert len(errors) == 1 and "CalDAV 发现失败" in errors[0]
        assert len(warnings) == 1 and "无法创建事件" in warnings[0]


class TestCaldavHostConfig:
    """入口主机：可配置、留空用默认、仅默认入口 403 时回退国区"""

    @pytest.mark.parametrize(
        "host,expected",
        [
            (None, apple_module.CALDAV_ENTRY_URL),
            ("", apple_module.CALDAV_ENTRY_URL),
            ("   ", apple_module.CALDAV_ENTRY_URL),
            ("caldav.icloud.com.cn", "https://caldav.icloud.com.cn/"),
            (
                "https://p01-caldav.icloud.com.cn",
                "https://p01-caldav.icloud.com.cn/",
            ),
            ("https://p01-caldav.icloud.com.cn/", "https://p01-caldav.icloud.com.cn/"),
        ],
    )
    def test_entry_url_is_normalized(self, host, expected):
        assert AppleCalendar._normalize_entry_url(host) == expected

    def test_configured_host_is_used(self, monkeypatch):
        cal, urls = make_calendar(monkeypatch, [(207, PRINCIPAL_XML), (207, HOME_XML)])
        cal._caldav_entry_url = AppleCalendar._normalize_entry_url(
            "p01-caldav.icloud.com.cn"
        )
        assert sync(cal._discover()) is True
        assert urls == [
            "https://p01-caldav.icloud.com.cn/",
            f"https://p01-caldav.icloud.com.cn/{DSID}/principal",
        ]
        assert cal._principal_url == (
            f"https://p01-caldav.icloud.com.cn/{DSID}/principal"
        )

    def test_default_entry_403_falls_back_to_cn(self, monkeypatch, caplog):
        cal, urls = make_calendar(
            monkeypatch, [(403, ""), (207, PRINCIPAL_XML), (207, HOME_XML)]
        )
        with caplog.at_level("INFO"):
            assert sync(cal._discover()) is True
        assert cal._caldav_entry_url == apple_module.CALDAV_CN_ENTRY_URL
        assert len(urls) == 3
        assert urls[2].startswith(f"https://caldav.icloud.com.cn/{DSID}/principal")
        assert any("中国大陆区" in r.getMessage() for r in caplog.records)

    def test_configured_host_403_does_not_fall_back(self, monkeypatch):
        cal, urls = make_calendar(monkeypatch, [(403, "")])
        cal._caldav_entry_url = AppleCalendar._normalize_entry_url(
            "p01-caldav.icloud.com.cn"
        )
        assert sync(cal._discover()) is False
        assert len(urls) == 1

    def test_both_entries_403_reports_both_tried(self, monkeypatch, caplog):
        cal, urls = make_calendar(monkeypatch, [(403, ""), (403, "")])
        with caplog.at_level("ERROR"):
            assert sync(cal._discover()) is False
        errors = error_texts(caplog)
        assert len(errors) == 1
        assert "403" in errors[0] and "中国大陆区" in errors[0]
        assert len(urls) == 2


class TestCalendarsListing:
    """日历清单：displayname 与 UUID 对上，且只打印一次"""

    def _calendar(self, monkeypatch):
        cal = make_discovered(monkeypatch)

        async def fake_request(url, method="GET", data=None, headers=None, **kw):
            assert method == "PROPFIND"
            assert b"displayname" in data
            return 207, CALENDARS_XML

        monkeypatch.setattr(cal, "_request_with_status", fake_request, raising=False)
        return cal

    def test_displayname_is_attached_to_uuid(self, monkeypatch):
        cal = self._calendar(monkeypatch)
        calendars, issue = sync(cal._list_calendars_with_status())
        assert issue is None
        assert [(c["id"], c["name"]) for c in calendars] == [
            (UUID_A, "工作日"),
            (UUID_B, "中国节假日"),
        ]

    def test_uuid_listing_is_logged_once(self, monkeypatch, caplog):
        cal = self._calendar(monkeypatch)
        with caplog.at_level("INFO"):
            sync(cal._list_calendars_with_status())
            cal._calendars_cache = []
            cal._calendars_ts = 0
            sync(cal._list_calendars_with_status())
        listing = [r for r in caplog.records if "可用日历" in r.getMessage()]
        assert len(listing) == 1
        assert UUID_A in listing[0].getMessage()


class TestReadCompleteness:
    """读取完整性：读不全时把原因交回调用方，且问题随缓存一起返回"""

    def _calendar(self, monkeypatch, fetch_status=207, ics_results=None):
        cal = make_discovered(monkeypatch)
        cal._calendars_cache = [
            {"id": UUID_A, "name": "工作日", "url": f"{BASE_URL}/{UUID_A}"}
        ]
        cal._calendars_ts = time.monotonic()
        replies = {"list": fetch_status, "ics": ics_results or [ICS_BODY]}

        async def fake_request(url, method="GET", data=None, headers=None, **kw):
            if url.rstrip("/").endswith("calendars"):
                return 207, CALENDARS_XML
            return replies["list"], ICS_LIST_XML if replies["list"] == 207 else ""

        async def fake_ics(url, **kw):
            return replies["ics"].pop(0)

        monkeypatch.setattr(cal, "_request_with_status", fake_request, raising=False)
        monkeypatch.setattr(cal, "_aiohttp_request", fake_ics, raising=False)
        return cal

    def test_clean_read_has_no_issues(self, monkeypatch):
        cal = self._calendar(monkeypatch, ics_results=[ICS_BODY, ICS_BODY])
        events, issues = sync(cal.get_all_events(days=7))
        assert issues == []
        assert len(events) == 1

    def test_list_failure_is_reported(self, monkeypatch):
        cal, _urls = make_calendar(monkeypatch, [(403, ""), (403, "")])
        events, issues = sync(cal.get_all_events(days=7))
        assert events == []
        assert issues == ["CalDAV 未连接"]

    def test_calendar_fetch_failure_prefixes_calendar_name(self, monkeypatch):
        cal = self._calendar(monkeypatch, fetch_status=403)
        events, issues = sync(cal.get_all_events(days=7))
        assert events == []
        assert issues == ["工作日: 抓取日历事件失败（status=403）"]

    def test_single_ics_failure_is_reported(self, monkeypatch):
        cal = self._calendar(monkeypatch, ics_results=[ICS_BODY, None])
        events, issues = sync(cal.get_all_events(days=7))
        assert len(events) == 1
        assert issues == ["工作日: 1/2 个事件文件未拉到"]

    def test_webcal_failure_is_reported(self, monkeypatch):
        cal = self._calendar(monkeypatch, ics_results=[ICS_BODY, ICS_BODY])
        cal.webcal_urls = ["webcal://example.com/cal.ics"]
        monkeypatch.setattr(cal, "_is_safe_webcal_url", lambda _u: True, raising=False)
        monkeypatch.setattr(
            cal,
            "_request_with_status",
            _webcal_stub(cal),
            raising=False,
        )
        events, issues = sync(cal.get_all_events(days=7))
        assert len(events) == 1
        assert issues == ["WebCal 读取失败（status=403）"]

    def test_issues_are_cached(self, monkeypatch):
        cal = self._calendar(monkeypatch, fetch_status=403)
        _events1, issues1 = sync(cal.get_all_events(days=7))
        _events2, issues2 = sync(cal.get_all_events(days=7))
        assert issues1 == issues2 == ["工作日: 抓取日历事件失败（status=403）"]


def time_now():
    import time

    return time.monotonic()


def _webcal_stub(cal):
    """日历 PROPFIND 正常、WebCal 请求一律 403 的替身。"""
    inner = cal._request_with_status

    async def fake(url, method="GET", data=None, headers=None, **kw):
        if url.startswith("https://example.com/"):
            return 403, ""
        return await inner(url, method=method, data=data, headers=headers, **kw)

    return fake
