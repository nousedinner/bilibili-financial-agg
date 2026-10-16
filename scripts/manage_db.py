"""Deployment CLI; credentials come from .env, never from command arguments."""
import argparse
import asyncio
import secrets
from sqlalchemy import select
from src.db import engine, async_session
from src.migrations import upgrade, verify
from src.models import ApiUser


async def run(args):
    try:
        async with engine.begin() as connection:
            await connection.run_sync(upgrade if args.command == "upgrade" else verify)
        if args.command == "create-user":
            username = args.username.strip()
            if not 1 <= len(username) <= 50:
                raise ValueError("Username must contain 1..50 characters")
            async with async_session() as session:
                if (await session.execute(select(ApiUser).where(ApiUser.username == username))).scalar_one_or_none():
                    raise ValueError("User already exists; no key was changed")
                key = secrets.token_urlsafe(32)
                session.add(ApiUser(username=username, api_key=key, enabled=True))
                await session.commit()
                print(f"Created {username}. Store this API key securely: {key}")
        else:
            print("Database upgrade completed")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("upgrade")
    user = commands.add_parser("create-user")
    user.add_argument("username")
    asyncio.run(run(parser.parse_args()))
