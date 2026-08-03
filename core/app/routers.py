from fastapi import FastAPI

from app.api import (
    accounts,
    capacity,
    events,
    experiments,
    governance,
    knowledge,
    learning,
    lotteries,
    metrics,
    notify,
    orchestration,
    proxies,
    risk_intel,
    scheduling,
    semantic,
    throughput,
    transitions,
    update,
    xiaohongshu_targets,
)


API_ROUTERS = (
    (accounts.router, "/api/accounts", ["accounts"]),
    (lotteries.router, "/api/lotteries", ["lotteries"]),
    (update.router, "/api/update", ["update"]),
    (notify.router, "/api/notify", ["notify"]),
    (proxies.router, "/api/proxies", ["proxies"]),
    (metrics.router, "/api/metrics", ["metrics"]),
    (events.router, "/api/events", ["events"]),
    (knowledge.router, "/api/knowledge", ["knowledge"]),
    (experiments.router, "/api/experiments", ["experiments"]),
    (risk_intel.router, "/api/risk", ["risk-intel"]),
    (learning.router, "/api/learning", ["learning"]),
    (governance.router, "/api/governance", ["governance"]),
    (transitions.router, "/api/transitions", ["transitions"]),
    (semantic.router, "/api/semantic", ["semantic"]),
    (scheduling.router, "/api/scheduling", ["scheduling"]),
    (capacity.router, "/api/capacity", ["capacity"]),
    (orchestration.router, "/api/orchestration", ["orchestration"]),
    (throughput.router, "/api/throughput", ["throughput"]),
    (
        xiaohongshu_targets.router,
        "/api/xiaohongshu-targets",
        ["xiaohongshu-targets"],
    ),
)


def include_api_routers(app: FastAPI) -> None:
    for router, prefix, tags in API_ROUTERS:
        app.include_router(router, prefix=prefix, tags=tags)
