import asyncio
from typing import AsyncGenerator, Optional
from dataclasses import dataclass
import json
import logging

logger = logging.getLogger(__name__)

@dataclass
class ProxyEvent:
    event_type: str # 'received', 'assigned', 'active', 'completed', 'error'
    request_id: str
    model: str = "unknown"
    server: str = "none"
    sender: str = "system"
    timestamp: float = 0.0
    ttft: float = 0.0      # Time To First Token (ms)
    tps: float = 0.0       # Tokens Per Second
    token_count: int = 0   # Total tokens processed
    prompt_tokens: int = 0 # Input tokens
    request_type: str = "REQ" # CHAT, GEN, TAGS, etc
    error_message: Optional[str] = None # Detailed error

class EventManager:
    def __init__(self):
        # Using a set of queues to handle multiple UI subscribers
        self.subscribers: set[asyncio.Queue] = set()
        # Authoritative server-side state
        self.active_requests: dict[str, dict] = {}
        self.recent_completions: list[dict] = []
        self.max_history = 10

    def get_snapshot(self) -> dict:
        """Returns the current reality of the system."""
        return {
            "active": list(self.active_requests.values()),
            "history": self.recent_completions
        }

    async def subscribe(self) -> AsyncGenerator[str, None]:
        """
        Subscribes a client and immediately sends the current snapshot.
        """
        queue = asyncio.Queue()
        self.subscribers.add(queue)
        try:
            # 1. Send the current 'Reality' snapshot first
            snapshot = self.get_snapshot()
            yield f"data: {json.dumps({'type': 'snapshot', 'data': snapshot})}\n\n"
            
            # 2. Start streaming live updates
            while True:
                data = await queue.get()
                yield f"data: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            self.subscribers.remove(queue)

    def emit(self, event: ProxyEvent):
        import time
        # Use time.time() if loop is not accessible (from thread)
        try:
            now = asyncio.get_event_loop().time()
        except RuntimeError:
            now = time.time()

        data = {
            "type": event.event_type,
            "id": event.request_id,
            "model": event.model,
            "server": event.server,
            "sender": event.sender,
            "ts": event.timestamp or now,
            "ttft": event.ttft,
            "tps": event.tps,
            "tokens": event.token_count,
            "prompt_tokens": event.prompt_tokens,
            "req_type": event.request_type,
            "error": event.error_message
        }

        # Update Server-Side State
        if event.event_type in ("received", "assigned", "active"):
            # Update or Create entry in active registry
            if event.request_id not in self.active_requests:
                self.active_requests[event.request_id] = data
            else:
                self.active_requests[event.request_id].update(data)
        
        elif event.event_type in ("completed", "error"):
            # Move from active to history
            finished_req = self.active_requests.pop(event.request_id, data)
            finished_req.update(data) # Merge final stats
            self.recent_completions.insert(0, finished_req)
            self.recent_completions = self.recent_completions[:self.max_history]

        # Broadcast to all connected UIs
        if self.subscribers:
            for queue in self.subscribers:
                try:
                    queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass

# Instantiate the singleton for use across the application
event_manager = EventManager()