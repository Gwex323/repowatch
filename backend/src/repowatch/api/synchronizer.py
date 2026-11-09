"""只读同步随 API 启停；退出前等待有界读取完成。"""

import asyncio


async def synchronize(service, stop):
    while not stop.is_set():
        worked = await asyncio.to_thread(service.tick)
        if not worked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                pass
