"""Idempotent schema upgrade for both the legacy database and fresh installs."""
from sqlalchemy import inspect, text
from src.models import Base

VERSION = 3


def upgrade(connection):
    Base.metadata.create_all(connection)
    # #3: create_all 后重建 Inspector，避免复用旧缓存导致重复加列
    inspector = inspect(connection)

    # ── videos 表 ──
    columns = {c["name"] for c in inspector.get_columns("videos")}
    if "retry_count" not in columns:
        connection.execute(text("ALTER TABLE videos ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"))
    if "analysis_status" not in columns:
        if connection.dialect.name == "mysql":
            connection.execute(text("ALTER TABLE videos ADD COLUMN analysis_status ENUM('pending','processing','completed','failed') NOT NULL DEFAULT 'pending'"))
        else:
            connection.execute(text("ALTER TABLE videos ADD COLUMN analysis_status VARCHAR(20) NOT NULL DEFAULT 'pending'"))

    # #2: analysis_status 数据修复——独立执行，不受分支条件限制
    connection.execute(text("""
        UPDATE videos SET analysis_status = 'completed'
        WHERE fetch_status = 'ok'
        AND analysis_status != 'completed'
        AND bvid IN (SELECT bvid FROM transcripts WHERE full_text IS NOT NULL AND full_text != '')
        AND bvid IN (SELECT bvid FROM summaries WHERE summary IS NOT NULL AND summary != '')
    """))

    # ── dirty_digests 表 ──
    if "dirty_digests" in inspector.get_table_names():
        dirty_cols = {c["name"] for c in inspector.get_columns("dirty_digests")}
        if "retry_count" not in dirty_cols:
            connection.execute(text("ALTER TABLE dirty_digests ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"))
    else:
        # create_all 已建表，但 retry_count 可能由 Base 定义；二次确认
        inspector2 = inspect(connection)
        dirty_cols2 = {c["name"] for c in inspector2.get_columns("dirty_digests")}
        if "retry_count" not in dirty_cols2:
            connection.execute(text("ALTER TABLE dirty_digests ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"))

    # ── transcripts full_text → LONGTEXT ──
    if connection.dialect.name == "mysql":
        insp = inspect(connection)
        full_text = next((c for c in insp.get_columns("transcripts") if c["name"] == "full_text"), None)
        if full_text and str(full_text["type"]).upper() != "LONGTEXT":
            connection.execute(text("ALTER TABLE transcripts MODIFY full_text LONGTEXT"))

    # ── schema_version ──
    connection.execute(text("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"))
    connection.execute(text("DELETE FROM schema_version"))
    connection.execute(text("INSERT INTO schema_version (version) VALUES (:version)"), {"version": VERSION})


def verify(connection):
    inspector = inspect(connection)
    missing = set(Base.metadata.tables) - set(inspector.get_table_names())
    if missing or "schema_version" not in inspector.get_table_names():
        raise RuntimeError("Database upgrade required: python -m scripts.manage_db upgrade")
    version = connection.execute(text("SELECT MAX(version) FROM schema_version")).scalar()
    videos_cols = {c["name"] for c in inspector.get_columns("videos")}
    dirty_cols = {c["name"] for c in inspector.get_columns("dirty_digests")} if "dirty_digests" in inspector.get_table_names() else set()
    if (version != VERSION
        or "retry_count" not in videos_cols
        or "analysis_status" not in videos_cols
        or "retry_count" not in dirty_cols):
        raise RuntimeError("Unsupported schema; run python -m scripts.manage_db upgrade")
