"""try_lock 非阻塞抢锁的语义测试（回归：wait_for(timeout=0) 恒超时）"""

import asyncio

from schedule_assistant.async_utils import try_lock


class TestTryLock:
    def test_free_lock_acquires_and_releases(self):
        async def run():
            lock = asyncio.Lock()
            async with try_lock(lock) as acquired:
                assert acquired is True
                assert lock.locked()
            assert not lock.locked()

        asyncio.run(run())

    def test_held_lock_skips(self):
        """锁被占用时跳过本次执行（acquired=False），不阻塞等待"""

        async def run():
            lock = asyncio.Lock()
            async with try_lock(lock) as first:
                assert first is True
                async with try_lock(lock) as second:
                    assert second is False
                assert lock.locked()  # 外层仍持有，跳过者不释放他人锁

        asyncio.run(run())

    def test_reusable_after_release(self):
        async def run():
            lock = asyncio.Lock()
            for _ in range(2):
                async with try_lock(lock) as acquired:
                    assert acquired is True
            assert not lock.locked()

        asyncio.run(run())
