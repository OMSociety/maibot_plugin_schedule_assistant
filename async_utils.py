"""异步小工具（纯标准库）"""

import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def try_lock(lock: asyncio.Lock):
    """非阻塞抢锁：锁被占用时以 acquired=False 让出，空闲则持有至退出。

    asyncio.wait_for(lock.acquire(), timeout=0) 不能用作「尝试抢锁」：
    timeout<=0 时协程还没被调度就被判超时，空闲锁也必抛 TimeoutError。
    """
    if lock.locked():
        yield False
        return
    await lock.acquire()
    try:
        yield True
    finally:
        lock.release()
