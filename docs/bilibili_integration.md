## Bilibili Integration with LotteryAutoScript

The LotteryAutoScript-inspired integration is now a Python **direct-API engine**
in the worker, not the old "hybrid" stub.

→ See **[docs/bilibili_api_engine.md](bilibili_api_engine.md)** for the engine
(`worker/app/bilibili/`), the worker API execution channel
(`worker/app/adapters/bilibili_api_channel.py`), the autonomous dispatch loop
(`core/app/services/auto_dispatch.py`), and the live self-test
(`worker/tools/bilibili_api_selftest.py`).

The earlier `core/app/adapters/bilibili/hybrid_executor.py` (a broken,
never-wired Node shell-out stub) was removed once the real engine landed.
