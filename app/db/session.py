# app/db/session.py
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from app.core.config import settings

# Using the local docker-compose credentials
DATABASE_URL = settings.database_url

# pool_size and max_overflow handle connection pooling, critical for the Event Loop.
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
)

# async_sessionmaker generates session factory instances 
AsyncSessionLocal = async_sessionmaker(
    bind=engine, 
    class_=AsyncSession, 
    expire_on_commit=False
)

async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI Dependency for database sessions.
    The 'yield' statement temporarily pauses execution, handing the session to the controller.
    Once the HTTP response is sent, execution resumes to execute the 'finally' block, safely closing the connection.
    This replaces Laravel's automatic end-of-request PDO cleanup.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()