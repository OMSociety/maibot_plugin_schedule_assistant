"""Notion 服务 - 封装 NotionClient，添加格式化功能"""

from datetime import datetime

from ..notion_client import NotionClient


class NotionService:
    """Notion 服务，封装 NotionClient，提供格式化输出（复用 NotionClient 内部缓存）"""

    def __init__(self, notion_client: NotionClient | None):
        self.notion = notion_client

    @staticmethod
    def format_ddl(ddl_str: str) -> str:
        """格式化截止日期显示

        Args:
            ddl_str: ISO 格式的截止日期字符串

        Returns:
            格式化后的截止日期描述，如 "今天截止"、"还剩2天"
        """
        if not ddl_str:
            return ""
        try:
            due = datetime.fromisoformat(ddl_str.replace("Z", "+00:00"))
            due_local = due.astimezone().replace(tzinfo=None)
            diff = (due_local.date() - datetime.now().date()).days
            if diff < 0:
                return f"已逾期{-diff}天"
            elif diff == 0:
                return "今天截止"
            elif diff == 1:
                return "还剩1天"
            else:
                return f"还剩{diff}天"
        except Exception:
            return ""

    async def get_pending_tasks(self) -> list[dict]:
        """获取未完成任务列表（使用 NotionClient 内部缓存）"""
        if not self.notion:
            return []
        return await self.notion.get_pending_transactions()
