"""Async database connection — SQLAlchemy + aiomysql."""

import os
from sqlalchemy import URL
from src.config import get_env
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from src.models import Base

DB_USER = os.environ.get("DB_USER", "zzybili")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_HOST = os.environ.get("DB_HOST", "mysql")
DB_PORT = os.environ.get("DB_PORT", "3306")
DB_NAME = os.environ.get("DB_NAME", "fin_agg")

DATABASE_URL = URL.create("mysql+aiomysql", username=DB_USER, password=DB_PASSWORD,
    host=DB_HOST, port=int(DB_PORT), database=DB_NAME, query={"charset": "utf8mb4"})

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    from src.migrations import upgrade, verify
    async with engine.begin() as conn:
        if get_env("AUTO_MIGRATE", "false").lower() == "true":
            await conn.run_sync(upgrade)
        else:
            await conn.run_sync(verify)
    print("[db] Schema verified")


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
