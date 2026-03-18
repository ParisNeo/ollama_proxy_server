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
    sender: str = "anon"
    timestamp: float = 0.0
    ttft: float = 0.0      # Time To First Token (ms)
    tps: float = 0.0       # Tokens Per Second
    token_count: int = 0   # Total tokens processed
    error_message: Optional[str] = None # Detailed error

class EventManager:
    def __init__(self):
        # Using a set of queues to handle multiple UI subscribers
        self.subscribers: set[asyncio.Queue] = set()

    async def subscribe(self) -> AsyncGenerator[str, None]:
        """
        Subscribes a client to the event stream. 
        Yields formatted SSE strings.
        """
        queue = asyncio.Queue()
        self.subscribers.add(queue)
        try:
            # Send initial connection event
            yield ": connected\n\n"
            while True:
                data = await queue.get()
                yield f"data: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            logger.debug("SSE subscription cancelled.")
            raise
        finally:
            self.subscribers.remove(queue)

    def emit(self, event: ProxyEvent):
        if not self.subscribers:
            return
            
        data = {
            "type": event.event_type,
            "id": event.request_id,
            "model": event.model,
            "server": event.server,
            "sender": event.sender,
            "ts": event.timestamp or asyncio.get_event_loop().time(),
            "ttft": event.ttft,
            "tps": event.tps,
            "tokens": event.token_count,
            "error": event.error_message
        }
        
        for queue in self.subscribers:
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                # If a queue is full, the client is likely lagging; skip it
                pass

# Instantiate the singleton for use across the application
event_manager = EventManager()