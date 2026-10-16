"""Idempotent schema upgrade for both the legacy database and fresh installs."""
from sqlalchemy import inspect, text
from src.models import Base

VERSION = 8


def upgrade(connection):
    current_ver = 0
    # #7: 拒绝高版本数据库被旧程序覆盖
    inspector = inspect(connection)
    if "schema_version" in inspector.get_table_names():
        current_ver = connection.execute(text("SELECT MAX(version) FROM schema_version")).scalar()
        if current_ver and current_ver > VERSION:
            raise RuntimeError(
                f"Database version {current_ver} is newer than program version {VERSION}. "
                "Cannot downgrade. Deploy the correct version first.")
    Base.metadata.create_all(connection)
    # create_all 后重建 Inspector，避免复用旧缓存导致重复加列
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
    # #5: B站avid列——重试时恢复评论抓取所需
    if "aid" not in columns:
        connection.execute(text("ALTER TABLE videos ADD COLUMN aid BIGINT"))
    # #1: 错误类型列——SQL精确过滤重试资格
    if "error_type" not in columns:
        connection.execute(text("ALTER TABLE videos ADD COLUMN error_type VARCHAR(20)"))
    # Only pre-v6 installations require legacy repair. V7 never turns legacy
    # NULL/status rows into work or resets historical retry counters.
    if not current_ver or current_ver < 6:
        # #1: 回填现有失败记录的error_type——每次upgrade都执行（幂等，WHERE error_type IS NULL保证不重复）
        # permanent: 404或"啥都木有"
        connection.execute(text("""
            UPDATE videos SET error_type = 'permanent'
            WHERE fetch_status = 'failed' AND error_type IS NULL
            AND (error_message LIKE '%404%' OR error_message LIKE '%啥都木有%')
        """))
        # incomplete_info: duration=0且非字幕问题
        connection.execute(text("""
            UPDATE videos SET error_type = 'incomplete_info'
            WHERE fetch_status = 'failed' AND error_type IS NULL
            AND (duration = 0 OR duration IS NULL)
            AND (error_message NOT LIKE '%transcript%' AND error_message NOT LIKE '%subtitle%')
        """))
        # llm_failed
        connection.execute(text("""
            UPDATE videos SET error_type = 'llm_failed'
            WHERE fetch_status = 'failed' AND error_type IS NULL
            AND error_message LIKE '%LLM analysis failed%'
        """))
        # transcript
        connection.execute(text("""
            UPDATE videos SET error_type = 'transcript'
            WHERE fetch_status = 'failed' AND error_type IS NULL
            AND (error_message LIKE '%transcript%' OR error_message LIKE '%subtitle%')
        """))
        # 剩余未分类的标记为unknown
        connection.execute(text("""
            UPDATE videos SET error_type = 'unknown'
            WHERE fetch_status = 'failed' AND error_type IS NULL
        """))

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

    # V8 changes representation only; existing phone keys remain valid. SQL
    # parameters contain digests, never the original key.
    if not current_ver or current_ver < 8:
        from src.api_keys import key_digest, is_key_digest
        rows = connection.execute(text("SELECT id, api_key FROM api_users")).fetchall()
        for user_id, old_key in rows:
            if not is_key_digest(old_key):
                connection.execute(text("UPDATE api_users SET api_key=:digest WHERE id=:id"),
                                   {"digest": key_digest(old_key), "id": user_id})

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
        or "aid" not in videos_cols
        or "error_type" not in videos_cols
        or "retry_count" not in dirty_cols):
        raise RuntimeError("Unsupported schema; run python -m scripts.manage_db upgrade")
    # create_all cannot evolve an existing table. Verify the actual columns,
    # primary keys and required indexes before the application starts.
    from sqlalchemy import String, Text, Boolean, Integer, Enum, UniqueConstraint
    from src.api_keys import is_key_digest
    for table_name, table in Base.metadata.tables.items():
        actual = {column["name"]: column for column in inspector.get_columns(table_name)}
        absent = set(table.columns.keys()) - set(actual)
        if absent:
            raise RuntimeError(f"Unsupported schema: {table_name} missing {sorted(absent)}")
        for column in table.columns:
            expected = column.type.dialect_impl(connection.dialect)
            observed = actual[column.name]["type"]
            if not column.nullable and not column.primary_key and actual[column.name].get("nullable", True):
                raise RuntimeError(f"Unsupported schema nullability: {table_name}.{column.name}")
            if isinstance(expected, Enum) and hasattr(observed, "enums") and not set(expected.enums).issubset(observed.enums):
                raise RuntimeError(f"Unsupported schema enum: {table_name}.{column.name}")
            if isinstance(expected, Boolean) and isinstance(observed, Integer):
                continue
            if expected._type_affinity is not observed._type_affinity:
                raise RuntimeError(f"Unsupported schema type: {table_name}.{column.name}")
            if isinstance(expected, String) and not isinstance(expected, Text):
                if expected.length and observed.length and observed.length < expected.length:
                    raise RuntimeError(f"Unsupported schema width: {table_name}.{column.name}")
        if connection.dialect.name == "mysql" and table_name in ("transcripts", "transcript_checkpoints"):
            if str(actual["full_text"]["type"]).upper() != "LONGTEXT":
                raise RuntimeError(f"Unsupported schema: {table_name}.full_text must be LONGTEXT")
        primary = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or [])
        if primary != tuple(column.name for column in table.primary_key):
            raise RuntimeError(f"Unsupported schema primary key: {table_name}")
        indexes = inspector.get_indexes(table_name) + [dict(value,unique=True) for value in inspector.get_unique_constraints(table_name)]
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                columns = tuple(column.name for column in constraint.columns)
                if not any(tuple(value.get("column_names") or []) == columns and value.get("unique") for value in indexes):
                    raise RuntimeError(f"Unsupported schema unique key: {table_name}.{columns}")
        for index in table.indexes:
            columns = tuple(column.name for column in index.columns)
            if not any(tuple(value.get("column_names") or []) == columns
                       and (not index.unique or value.get("unique")) for value in indexes):
                raise RuntimeError(f"Unsupported schema index: {table_name}.{index.name}")
    if any(not is_key_digest(row[0]) for row in connection.execute(text("SELECT api_key FROM api_users"))):
        raise RuntimeError("Connection key migration required; run python -m scripts.manage_db upgrade")
