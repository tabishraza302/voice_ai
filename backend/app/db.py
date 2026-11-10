import os

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import AsyncAdaptedQueuePool


class Base(DeclarativeBase):
    pass


database_url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://voice:voice@localhost:5432/voice")
engine = create_async_engine(
    database_url,
    poolclass=AsyncAdaptedQueuePool,
    pool_size=10,
    max_overflow=10,
    pool_timeout=10,
    pool_pre_ping=True,
    pool_recycle=300,
)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def get_db():
    async with Session() as session:
        yield session
