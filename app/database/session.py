from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL, 
    pool_pre_ping=True,
    connect_args={"timeout": 30} # Wait longer if disk is slow
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