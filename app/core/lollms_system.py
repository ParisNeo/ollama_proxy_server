import asyncio
import datetime
import logging
from typing import Any, Optional
from sqlalchemy import select, delete
from sqlalchemy.dialects.sqlite import insert
from app.database.models import User, UserToolData
from app.database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

class LollmsSystem:
    """
    Standard Host Interface for LoLLMs Tools.
    This object is injected into tool functions as the 'lollms' parameter.
    """
    def __init__(self, user: User, library_name: str):
        # Standard user metadata
        self.user = {
            "id": user.id,
            "username": user.username,
            "is_admin": user.is_admin,
            "token_prefix": "op_..." # Abstracted
        }
        self.library_name = library_name
        self._volatile = {}

    def set(self, key: str, value: Any, persistent: bool = True):
        """Standard setter for per-user tool data."""
        if not persistent:
            self._volatile[key] = value
            return

        # Handle async DB call in a sync-friendly way for tool developers
        async def _set():
            async with AsyncSessionLocal() as db:
                # Upsert logic for SQLite / Agnostic
                stmt = insert(UserToolData).values(
                    user_id=self.user["id"],
                    library_name=self.library_name,
                    key=key,
                    value=value,
                    is_persistent=True
                ).on_conflict_do_update(
                    index_elements=['user_id', 'library_name', 'key'],
                    set_={"value": value, "updated_at": datetime.datetime.utcnow()}
                )
                await db.execute(stmt)
                await db.commit()
        
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_set())
            else:
                loop.run_until_complete(_set())
        except RuntimeError:
            asyncio.run(_set())

    def get(self, key: str, default: Any = None) -> Any:
        """Standard getter for per-user tool data."""
        if key in self._volatile:
            return self._volatile[key]

        async def _get():
            async with AsyncSessionLocal() as db:
                res = await db.execute(
                    select(UserToolData.value)
                    .filter(UserToolData.user_id == self.user["id"])
                    .filter(UserToolData.library_name == self.library_name)
                    .filter(UserToolData.key == key)
                )
                return res.scalar()

        try:
            val = asyncio.run(_get())
            return val if val is not None else default
        except:
            return default

    def delete(self, key: str):
        """Standard deleter."""
        self._volatile.pop(key, None)
        async def _del():
            async with AsyncSessionLocal() as db:
                await db.execute(
                    delete(UserToolData)
                    .filter(UserToolData.user_id == self.user["id"])
                    .filter(UserToolData.library_name == self.library_name)
                    .filter(UserToolData.key == key)
                )
                await db.commit()
        asyncio.run(_del())