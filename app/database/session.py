from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.config import settings

from sqlalchemy.pool import QueuePool

engine = create_async_engine(
    settings.DATABASE_URL, 
    pool_pre_ping=True,
    # SQLite works best with a dedicated pool size in high-concurrency gateway scenarios
    pool_size=20,
    max_overflow=10,
    pool_timeout=30,
    # Use a faster checkout for SQLite
    pool_recycle=3600,
    connect_args={
        "timeout": 30,
        "check_same_thread": False # Required for aiosqlite/multithreading
    }
)

# --- SPACE OPTIMIZATION ---
from sqlalchemy import event

@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    # Limit journal size to 2MB to prevent disk exhaustion during big transactions
    cursor.execute("PRAGMA journal_size_limit = 2097152")
    # Synchronous NORMAL is faster and uses less disk IO/journaling
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.close()

AsyncSessionLocal = async_sessionmaker(
    autocommit=False, autoflush=False, bind=engine, class_=AsyncSession, expire_on_commit=False
)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session