"""Run only on an isolated MySQL instance restored from a production dump."""
import asyncio
import hashlib
import json
import os
import time
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from src.db import engine
from src import migrations
from src.api_keys import key_digest


async def fingerprint(connection, tables=None):
    if tables is None:
        tables = (await connection.execute(text("SHOW TABLES"))).scalars().all()
    result = {}
    for table in tables:
        if table == 'schema_version':
            continue
        rows = (await connection.execute(text(f'SELECT * FROM `{table}`'))).all()
        canonical = sorted(json.dumps(list(row), default=str, ensure_ascii=False, sort_keys=True) for row in rows)
        result[table] = {'rows': len(rows), 'sha256': hashlib.sha256('\n'.join(canonical).encode()).hexdigest()}
    return result


async def structure(connection):
    return [list(row) for row in (await connection.execute(text(
        "SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,EXTRA "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME,ORDINAL_POSITION"))).all()]


async def main():
    assert (engine.url.database or '').startswith('fin_agg_audit_'), 'Refusing non-audit schema'
    assert engine.url.host == '127.0.0.1', 'Use the dedicated isolated MySQL network namespace'
    assert os.environ.get('XIAOMI_API_KEY') == 'TEST_ONLY' and not os.environ.get('SESSDATA')
    async with engine.connect() as connection:
        before = await fingerprint(connection)
        shape_before = await structure(connection)
        for sql in ['SELECT COUNT(*) FROM videos v JOIN video_work w ON v.bvid=w.bvid',
                    'SELECT COUNT(*) FROM videos v JOIN work_stages s ON v.bvid=s.bvid']:
            try:
                await connection.execute(text(sql))
            except Exception as exc:
                assert '1267' in str(exc), type(exc).__name__
            else:
                raise AssertionError('Expected restored production collation conflict')
    started = time.monotonic()
    async with engine.begin() as connection:
        await connection.run_sync(migrations.upgrade)
        await connection.run_sync(migrations.verify)
    duration = time.monotonic() - started
    async with engine.begin() as connection:
        after = await fingerprint(connection, before)
        assert before == after, 'Existing table data changed'
        shape_after = [row for row in await structure(connection) if row[0] in before or row[0] == 'schema_version']
        assert shape_before == shape_after, 'Column definitions changed unexpectedly'
        joins = [(await connection.execute(text(sql))).scalar_one() for sql in [
            'SELECT COUNT(*) FROM videos v JOIN video_work w ON v.bvid=w.bvid',
            'SELECT COUNT(*) FROM videos v JOIN work_stages s ON v.bvid=s.bvid']]
        await connection.run_sync(migrations.upgrade)
        await connection.run_sync(migrations.verify)
        # Table default remains correct, but one existing column drifts.
        await connection.execute(text('ALTER TABLE videos MODIFY title VARCHAR(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci'))
        try:
            await connection.run_sync(migrations.verify)
        except RuntimeError as exc:
            assert 'collation' in str(exc)
        else:
            raise AssertionError('Column-level drift was not detected')
        await connection.run_sync(migrations.upgrade)
        await connection.run_sync(migrations.verify)
        assert before == await fingerprint(connection, before)
    # Fresh database starts with a deliberately different default.
    async with engine.begin() as connection:
        await connection.execute(text('CREATE DATABASE fin_agg_audit_fresh CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci'))
    fresh = create_async_engine(engine.url.set(database='fin_agg_audit_fresh'))
    async with fresh.begin() as connection:
        await connection.run_sync(migrations.upgrade)
        await connection.run_sync(migrations.verify)
        await connection.execute(text('ALTER TABLE api_users CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci'))
        await connection.execute(text('INSERT INTO api_users(username,api_key,enabled) VALUES (:name,:key,1)'),
            [{'name': 'probe', 'key': key_digest('first')}, {'name': 'probe ', 'key': key_digest('second')}])
    async with fresh.begin() as connection:
        try:
            await connection.run_sync(migrations.upgrade)
        except RuntimeError as exc:
            assert 'unique-key conflict' in str(exc), str(exc)
        else:
            raise AssertionError('Target collation collision not rejected')
        await connection.execute(text("DELETE FROM api_users WHERE BINARY username = BINARY 'probe '"))
        await connection.run_sync(migrations.upgrade)
        await connection.run_sync(migrations.verify)
    await fresh.dispose()
    await engine.dispose()
    print(json.dumps({'production_copy_tables_unchanged': len(before), 'table_fingerprints': before,
        'schema': migrations.VERSION, 'migration_seconds': round(duration, 3), 'join_counts': joins,
        'fresh_install': True, 'idempotent': True, 'column_drift_repaired': True,
        'unique_collision_rejected': True, 'paid_calls': 0}, ensure_ascii=True))


if __name__ == '__main__':
    asyncio.run(main())
