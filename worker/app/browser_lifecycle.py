
from datetime import datetime, timedelta



class BrowserLifecycle:

    HARD_TTL = timedelta(hours=6)

    MAX_MEMORY_MB = 700

    MAX_CONTEXTS = 20



    def __init__(self, browser_id: str):

        self.browser_id = browser_id

        self.created_at = datetime.now()

        self.context_count = 0



    def should_recycle(self, current_memory_mb: float) -> str:

        age = datetime.now() - self.created_at

        if age >= self.HARD_TTL: return "hard_ttl"

        if current_memory_mb > self.MAX_MEMORY_MB: return "memory"

        if self.context_count >= self.MAX_CONTEXTS: return "context_count"

        return "none"
