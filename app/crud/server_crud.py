from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models import OllamaServer
from app.schema.server import ServerCreate

async def get_server_by_url(db: AsyncSession, url: str) -> OllamaServer | None:
    result = await db.execute(select(OllamaServer).filter(OllamaServer.url == url))
    return result.scalars().first()

async def get_servers(db: AsyncSession, skip: int = 0, limit: int = 100) -> list[OllamaServer]:
    result = await db.execute(select(OllamaServer).order_by(OllamaServer.created_at.desc()).offset(skip).limit(limit))
    return result.scalars().all()

async def create_server(db: AsyncSession, server: ServerCreate) -> OllamaServer:
    db_server = OllamaServer(name=server.name, url=str(server.url))
    db.add(db_server)
    await db.commit()
    await db.refresh(db_server)
    return db_server

async def delete_server(db: AsyncSession, server_id: int) -> OllamaServer | None:
    result = await db.execute(select(OllamaServer).filter(OllamaServer.id == server_id))
    server = result.scalars().first()
    if server:
        await db.delete(server)
        await db.commit()
    return server