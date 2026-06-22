"""
Hybrid Bilibili Executor integrating LotteryAutoScript logic.

This provides a bridge to LotteryAutoScript for mature Bilibili lottery features
while keeping DPMS gates, event store, etc.
"""
import asyncio
import subprocess
import json
from pathlib import Path
from typing import Dict, Any

from ...services.real_run_gate import real_run_gate
from ...event_store import record_event

async def run_lotteryautoscript_task(task: Dict[str, Any], account: Dict[str, Any]):
    """Execute via LotteryAutoScript subprocess."""
    if not real_run_gate.allow(task, 'bilibili'):
        await record_event('gate_blocked', task)
        return {'status': 'gated'}

    # Prepare config from DPMS
    config = prepare_las_config(task, account)
    config_path = write_temp_config(config)

    try:
        result = await asyncio.create_subprocess_exec(
            'node', 'path/to/lotteryautoscript/main.js', 'start',
            env={**os.environ, 'LAS_CONFIG': str(config_path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await result.communicate()

        await record_event('lottery_execution', {'output': stdout.decode(), 'task_id': task['id']})
        return {'status': 'success', 'output': stdout.decode()}
    except Exception as e:
        await record_event('execution_error', {'error': str(e)})
        return {'status': 'error'}

def prepare_las_config(task, account):
    # Map DPMS task to LAS my_config
    return {
        'accounts': [account.get('cookie')],
        'blacklist': task.get('blacklist', []),
        # ... more mappings
    }

def write_temp_config(config):
    path = Path('/tmp/las_config.json')
    path.write_text(json.dumps(config))
    return path
