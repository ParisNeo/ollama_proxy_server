"""Database session management for Ollama Proxy Server."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
ASYNC_SESSION_LOCAL = async_sessionmaker(autocommit=False, autoflush=False, bind=engine, class_=AsyncSession)


async def get_db():
    """Get database session for dependency injection."""
    async with ASYNC_SESSION_LOCAL() as session:
        yield session


# Export with the old name for backward compatibility
ASYNC_SESSION_LOCAL_OLD = ASYNC_SESSION_LOCAL
