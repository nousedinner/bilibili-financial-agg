"""Named-lock ownership plus transaction fencing and checks before paid calls."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, text
from src.lifecycle import now, OwnershipLost
from src.models import WorkerOwner


@dataclass(frozen=True)
class OwnershipTicket:
    guard: object
    generation: int

    async def check(self):
        await self.guard.check(self.generation)

    async def fence(self, session):
        await self.guard.fence(session, self.generation)


class OwnerGuard:
    def __init__(self, engine, sessions, *, heartbeat_seconds=20, lease_seconds=90, lock_name="fin_agg_scheduler"):
        self.engine, self.sessions = engine, sessions
        self.heartbeat_seconds, self.lease_seconds = heartbeat_seconds, lease_seconds
        self.owner, self.generation = str(uuid4()), 0
        self.connection = None
        self.lost = True
        self.closed = False
        self.monitor = None
        self.io_lock = asyncio.Lock()
        self.tasks = set()
        self.lock_name = lock_name

    def ticket(self):
        if self.lost:
            raise OwnershipLost()
        return OwnershipTicket(self, self.generation)

    async def acquire(self):
        async with self.io_lock:
            if self.connection is not None:
                await self.connection.close()
            self.connection = await self.engine.connect()
            if self.connection.dialect.name == "mysql":
                acquired = (await self.connection.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": self.lock_name})).scalar()
                if acquired != 1:
                    await self.connection.close()
                    self.connection = None
                    raise OwnershipLost()
            async with self.sessions() as session:
                row = (await session.execute(select(WorkerOwner).where(WorkerOwner.id == 1).with_for_update())).scalar_one_or_none()
                if row is None:
                    row = WorkerOwner(id=1, owner=self.owner, generation=1, lease_until=now())
                    session.add(row)
                else:
                    row.generation += 1
                    row.owner = self.owner
                row.lease_until = now() + timedelta(seconds=self.lease_seconds)
                await session.commit()
                self.generation = row.generation
            self.lost = False

    async def start(self):
        await self.acquire()
        self.monitor = asyncio.create_task(self._monitor())

    async def fence(self, session, generation=None):
        expected = self.generation if generation is None else generation
        if self.lost or expected != self.generation:
            raise OwnershipLost()
        row = (await session.execute(select(WorkerOwner).where(WorkerOwner.id == 1)
            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if row is None or row.owner != self.owner or row.generation != expected or row.lease_until <= now():
            self._lose()
            raise OwnershipLost()

    async def check(self, generation=None):
        expected = self.generation if generation is None else generation
        if self.lost or expected != self.generation:
            raise OwnershipLost()
        try:
            async with self.io_lock:
                if self.connection is None:
                    raise OwnershipLost()
                if self.connection.dialect.name == "mysql":
                    result = await asyncio.wait_for(self.connection.execute(text(
                        "SELECT IS_USED_LOCK(:name) = CONNECTION_ID()"), {"name": self.lock_name}), timeout=10)
                    if result.scalar() != 1:
                        raise OwnershipLost()
                else:
                    await self.connection.execute(text("SELECT 1"))
            async with self.sessions() as session:
                await self.fence(session, expected)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._lose()
            raise OwnershipLost() from None

    def _lose(self):
        self.lost = True
        current = asyncio.current_task()
        for task in list(self.tasks):
            if task is not current:
                task.cancel()

    async def _monitor(self):
        while not self.closed:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                if self.lost:
                    # Tickets capture a generation, so late old tasks stay fenced out.
                    if any(not task.done() for task in self.tasks):
                        continue
                    await self.acquire()
                else:
                    await self.check()
                    async with self.sessions() as session:
                        await self.fence(session)
                        row = await session.get(WorkerOwner, 1)
                        row.lease_until = now() + timedelta(seconds=self.lease_seconds)
                        await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._lose()

    async def close(self):
        self.closed = True
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.connection:
            try:
                if self.connection.dialect.name == "mysql" and not self.connection.invalidated:
                    await self.connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": self.lock_name})
            except Exception:
                pass
            finally:
                await self.connection.close()
        self.lost = True
