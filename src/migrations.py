"""Idempotent schema upgrade for both the legacy database and fresh installs."""
from sqlalchemy import inspect, text
from src.models import Base

VERSION = 2


def upgrade(connection):
    inspector = inspect(connection)
    if "schema_version" in inspector.get_table_names():
        current = connection.execute(text("SELECT MAX(version) FROM schema_version")).scalar()
        if current is not None and current > VERSION:
            raise RuntimeError("Refusing to downgrade a newer database schema")
    Base.metadata.create_all(connection)
    columns = {c["name"] for c in inspect(connection).get_columns("videos")}
    if "retry_count" not in columns:
        connection.execute(text("ALTER TABLE videos ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"))
    if "analysis_status" not in columns:
        if connection.dialect.name == "mysql":
            connection.execute(text("ALTER TABLE videos ADD COLUMN analysis_status ENUM('pending','processing','completed','failed') NOT NULL DEFAULT 'pending'"))
        else:
            connection.execute(text("ALTER TABLE videos ADD COLUMN analysis_status VARCHAR(20) NOT NULL DEFAULT 'pending'"))
        # Issue #9: 迁移恢复已完成视频的 analysis_status
        connection.execute(text("""
            UPDATE videos SET analysis_status = 'completed'
            WHERE fetch_status = 'ok'
            AND bvid IN (SELECT bvid FROM transcripts WHERE full_text IS NOT NULL AND full_text != '')
            AND bvid IN (SELECT bvid FROM summaries WHERE summary IS NOT NULL AND summary != '')
        """))
    # Issue #10: DirtyDigest retry_count 列
    dirty_cols = {c["name"] for c in inspect(connection).get_columns("dirty_digests")} if "dirty_digests" in inspector.get_table_names() else set()
    if "retry_count" not in dirty_cols:
        connection.execute(text("ALTER TABLE dirty_digests ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"))
    if connection.dialect.name == "mysql":
        full_text = next(c for c in inspect(connection).get_columns("transcripts") if c["name"] == "full_text")
        if str(full_text["type"]).upper() != "LONGTEXT":
            connection.execute(text("ALTER TABLE transcripts MODIFY full_text LONGTEXT"))
    connection.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"))
    connection.execute(text("DELETE FROM schema_version"))
    connection.execute(text("INSERT INTO schema_version (version) VALUES (:version)"), {"version": VERSION})


def verify(connection):
    inspector = inspect(connection)
    missing = set(Base.metadata.tables) - set(inspector.get_table_names())
    if missing or "schema_version" not in inspector.get_table_names():
        raise RuntimeError("Database upgrade required: python -m scripts.manage_db upgrade")
    version = connection.execute(text("SELECT MAX(version) FROM schema_version")).scalar()
    if version != VERSION or "retry_count" not in {c["name"] for c in inspector.get_columns("videos")} or "analysis_status" not in {c["name"] for c in inspector.get_columns("videos")}:
        raise RuntimeError("Unsupported schema; run python -m scripts.manage_db upgrade")
