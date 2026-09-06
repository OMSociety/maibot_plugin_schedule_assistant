"""ScheduleStore 纯逻辑测试：键归一化、旧键迁移、CRUD 往返、Apple 同步去重。

ScheduleStore 底层是注入目录下的 JSON 文件，全部逻辑不依赖 SDK，
直接用 tmp_path 建真实文件做往返验证。
"""

import asyncio

from schedule_assistant.schedule_store import (
    ScheduleItem,
    ScheduleStore,
    _bare_user_id,
)


class TestBareUserId:
    """存储键统一口径：platform:id → 裸 ID"""

    def test_prefixed(self):
        assert _bare_user_id("qq:123456") == "123456"

    def test_telegram_prefixed(self):
        assert _bare_user_id("telegram:abcdef") == "abcdef"

    def test_bare_unchanged(self):
        assert _bare_user_id("123456") == "123456"

    def test_empty(self):
        assert _bare_user_id("") == ""
        assert _bare_user_id(None) == ""


class TestMigrateLegacyKeys:
    """旧版 platform:id 存储键迁移为裸 ID 键（含 _users 列表归一化）"""

    def test_migrates_prefixed_keys(self):
        data = {
            "schedules_qq:123": [{"id": "a"}],
            "habits_qq:123": [{"id": "b"}],
            "water_last_qq:123": "2026-01-01",
        }
        changed = ScheduleStore._migrate_legacy_keys(data)
        assert changed is True
        assert data["schedules_123"] == [{"id": "a"}]
        assert data["habits_123"] == [{"id": "b"}]
        assert data["water_last_123"] == "2026-01-01"
        # 旧键一律删除（新键已存在时也不保留旧键）
        assert "schedules_qq:123" not in data

    def test_existing_bare_key_not_overwritten(self):
        data = {
            "schedules_123": [{"id": "new"}],
            "schedules_qq:123": [{"id": "old"}],
        }
        changed = ScheduleStore._migrate_legacy_keys(data)
        assert changed is True
        assert data["schedules_123"] == [{"id": "new"}]
        assert "schedules_qq:123" not in data

    def test_no_colon_keys_untouched(self):
        data = {"schedules_123": [], "_users": ["123"]}
        assert ScheduleStore._migrate_legacy_keys(data) is False

    def test_users_list_normalized(self):
        data = {"_users": ["qq:123", "456", "qq:123"]}
        changed = ScheduleStore._migrate_legacy_keys(data)
        assert changed is True
        assert data["_users"] == ["123", "456"]

    def test_user_platform_prefix_migrated(self):
        data = {"user_platform_qq:123": "qq"}
        changed = ScheduleStore._migrate_legacy_keys(data)
        assert changed is True
        assert data["user_platform_123"] == "qq"
        assert "user_platform_qq:123" not in data


class TestScheduleItem:
    """序列化与字段过滤"""

    def test_from_dict_filters_unknown_fields(self):
        item = ScheduleItem.from_dict({"id": "x", "title": "t", "unknown_field": 1})
        assert item.id == "x"
        assert item.title == "t"
        assert not hasattr(item, "unknown_field")

    def test_from_dict_missing_id_gets_generated(self):
        item = ScheduleItem.from_dict({"title": "t"})
        assert item.id

    def test_roundtrip(self):
        item = ScheduleItem(type="schedule", title="看电影", time="2026-09-08 19:00")
        revived = ScheduleItem.from_dict(item.to_dict())
        assert revived == item


class TestStoreRoundTrip:
    """存储 CRUD 往返（tmp_path 真实文件）"""

    def _store(self, tmp_path):
        store = ScheduleStore()
        store.set_data_dir(tmp_path)
        return store

    def test_add_and_list_with_prefixed_uid(self, tmp_path):
        """user_id 带 platform: 前缀时应归一化为裸 ID 存储键（B2-A 修复回归）"""
        store = self._store(tmp_path)
        item = ScheduleItem(type="schedule", title="早课", time="2026-09-08 08:00")
        asyncio.run(store.add_item("qq:123456", item))

        raw = (tmp_path / "schedule_data.json").read_text(encoding="utf-8")
        assert "schedules_123456" in raw
        assert "schedules_qq:123456" not in raw

        items = asyncio.run(store.list_all_items("123456"))
        assert [i.id for i in items] == [item.id]

    def test_update_and_remove(self, tmp_path):
        store = self._store(tmp_path)
        item = ScheduleItem(type="schedule", title="旧标题", time="2026-09-08 08:00")
        asyncio.run(store.add_item("123", item))

        item.title = "新标题"
        assert asyncio.run(store.update_item("123", item)) is True
        assert asyncio.run(store.list_all_items("123"))[0].title == "新标题"

        assert asyncio.run(store.remove_item("123", item.id)) is True
        assert asyncio.run(store.list_all_items("123")) == []
        assert asyncio.run(store.remove_item("123", "missing")) is False

    def test_habit_add_replaces_same_title(self, tmp_path):
        """习惯按标题去重：同 title 重复添加只保留最新"""
        store = self._store(tmp_path)
        asyncio.run(store.add_item("u1", ScheduleItem(type="habit", title="喝水")))
        asyncio.run(
            store.add_item("u1", ScheduleItem(type="habit", title="喝水", time="09:00"))
        )
        items = asyncio.run(store.list_all_items("u1"))
        assert len(items) == 1
        assert items[0].time == "09:00"

    def test_get_all_users_dedup(self, tmp_path):
        store = self._store(tmp_path)
        asyncio.run(store.add_item("qq:1", ScheduleItem(title="a")))
        asyncio.run(store.add_item("1", ScheduleItem(title="b")))
        asyncio.run(store.add_item("2", ScheduleItem(title="c")))
        assert asyncio.run(store.get_all_users()) == ["1", "2"]


class TestSyncFromAppleCalendar:
    """Apple 日历同步：重复 UID 去重且不误删、空列表不删除"""

    def _evt(self, uid, title="事件", start="2026-09-10T10:00:00"):
        return {"uid": uid, "summary": title, "start": start}

    def test_empty_events_no_deletion(self, tmp_path):
        """空事件列表（可能为同步失败）不触发删除"""
        store = ScheduleStore()
        store.set_data_dir(tmp_path)
        item = ScheduleItem(
            type="schedule", title="已有", time="2026-09-10 10:00", apple_uid="u1"
        )
        asyncio.run(store.add_item("u", item))
        stats = asyncio.run(store.sync_from_apple_calendar("u", []))
        assert stats == {"added": 0, "updated": 0, "deleted": 0}
        assert len(asyncio.run(store.list_all_items("u"))) == 1

    def test_duplicate_uid_added_once_and_kept(self, tmp_path):
        """Apple 返回重复 RRULE 实例：同一 UID 只添加一次，且不因重复被误删"""
        store = ScheduleStore()
        store.set_data_dir(tmp_path)
        events = [self._evt("u1"), self._evt("u1")]
        stats = asyncio.run(store.sync_from_apple_calendar("u", events))
        assert stats["added"] == 1
        assert stats["deleted"] == 0
        items = asyncio.run(store.list_all_items("u"))
        assert len(items) == 1
        assert items[0].apple_uid == "u1"

    def test_repeated_sync_idempotent(self, tmp_path):
        """同一批事件同步两次：第二次无新增、无删除"""
        store = ScheduleStore()
        store.set_data_dir(tmp_path)
        events = [self._evt("u1"), self._evt("u2")]
        asyncio.run(store.sync_from_apple_calendar("u", events))
        stats = asyncio.run(store.sync_from_apple_calendar("u", events))
        assert stats["added"] == 0
        assert stats["updated"] == 0
        assert stats["deleted"] == 0
        assert len(asyncio.run(store.list_all_items("u"))) == 2

    def test_changed_event_updated(self, tmp_path):
        store = ScheduleStore()
        store.set_data_dir(tmp_path)
        asyncio.run(store.sync_from_apple_calendar("u", [self._evt("u1")]))
        stats = asyncio.run(
            store.sync_from_apple_calendar("u", [self._evt("u1", title="改名了")])
        )
        assert stats["updated"] == 1
        assert asyncio.run(store.list_all_items("u"))[0].title == "改名了"

    def test_missing_from_apple_deleted(self, tmp_path):
        """本地有 apple_uid 而本次同步没有的事件应删除；无 apple_uid 的不动"""
        store = ScheduleStore()
        store.set_data_dir(tmp_path)
        asyncio.run(store.add_item("u", ScheduleItem(title="a", apple_uid="gone")))
        asyncio.run(store.add_item("u", ScheduleItem(title="b")))
        stats = asyncio.run(store.sync_from_apple_calendar("u", [self._evt("keep")]))
        assert stats["deleted"] == 1
        titles = {i.title for i in asyncio.run(store.list_all_items("u"))}
        assert titles == {"b", "事件"}


class TestClearExpiredOverrides:
    """临时覆盖清理：非今日的覆盖移除，今日的保留"""

    def test_stale_override_removed(self, tmp_path):
        from datetime import datetime, timedelta

        store = ScheduleStore()
        store.set_data_dir(tmp_path)
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        asyncio.run(
            store.add_item(
                "u",
                ScheduleItem(
                    type="habit",
                    title="喝水",
                    temp_override=f"{yesterday} 09:00",
                ),
            )
        )
        asyncio.run(
            store.add_item(
                "u",
                ScheduleItem(
                    type="habit",
                    title="锻炼",
                    temp_override=f"{today} 09:00",
                ),
            )
        )
        asyncio.run(store.clear_expired_overrides("u"))
        items = {i.title: i for i in asyncio.run(store.list_all_items("u"))}
        assert items["喝水"].temp_override is None
        assert items["锻炼"].temp_override == f"{today} 09:00"
