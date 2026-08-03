"""Shared Worker execution infrastructure selected by platform modules.

This module deliberately contains no platform registry and no platform
selection.  A platform descriptor may opt into the browser-observation
infrastructure by referencing this callable, while the task runner remains
unaware of the descriptor's executor label.
"""

from __future__ import annotations


async def execute_browser_observation_shadow(
    task: dict,
    adapter,
    pool,
    *,
    runtime,
):
    """Use injected browser lifecycle infrastructure without a back-import."""

    return await runtime.execute_browser_observation_shadow(
        task,
        adapter,
        pool,
    )


async def execute_browser_observation_probe(
    binding: dict,
    pool,
    *,
    runtime,
):
    """Invoke shared read-only selector probing without central dispatch."""

    return await runtime.execute_browser_observation_probe(binding, pool)


async def execute_browser_real_task(
    task: dict,
    adapter,
    pool,
    *,
    runtime,
):
    """Invoke the shared fenced browser mutation lifecycle."""

    return await runtime.execute_real_task(task, adapter, pool)
