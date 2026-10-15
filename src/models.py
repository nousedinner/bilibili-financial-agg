"""Database table definitions — SQLAlchemy models matching DESIGN.md v6."""

from sqlalchemy import (
    Column, BigInteger, Integer, String, Text, Float, Boolean,
    DateTime, Date, Enum, JSON, ForeignKey, Index,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import DeclarativeBase
from datetime import datetime


def _localnow():
    """返回当前时间（naive，匹配MySQL CST时区）。"""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Blogger(Base):
    __tablename__ = "bloggers"

    mid = Column(BigInteger, primary_key=True)
    name = Column(String(100), nullable=False)
    tags = Column(JSON)
    enabled = Column(Boolean, default=True)
    added_at = Column(DateTime, default=_localnow)


class Video(Base):
    __tablename__ = "videos"

    bvid = Column(String(20), primary_key=True)
    mid = Column(BigInteger, nullable=False, index=True)
    title = Column(String(500))
    duration = Column(Integer)
    aid = Column(BigInteger)  # #5: B站avid，重试时恢复评论抓取
    error_type = Column(String(20))  # #1: 持久化错误类型，SQL精确过滤重试资格
    publish_time = Column(DateTime, index=True)
    view_count = Column(Integer, default=0)
    content_type = Column(Enum("video", "dynamic", name="content_type"), default="video")
    dyn_id = Column(String(50))
    fetch_status = Column(Enum("pending", "ok", "failed", name="fetch_status"), default="pending")
    analysis_status = Column(Enum("pending", "processing", "completed", "failed", name="analysis_status"), default="pending")
    error_message = Column(Text)
    retry_count = Column(Integer, default=0)
    fetched_at = Column(DateTime, default=_localnow)


class Transcript(Base):
    __tablename__ = "transcripts"

    bvid = Column(String(20), primary_key=True)
    source = Column(Enum("cc_subtitle", "ai_subtitle", "asr", name="transcript_source"), nullable=False)
    full_text = Column(Text)
    segment_count = Column(Integer)
    created_at = Column(DateTime, default=_localnow)


class Summary(Base):
    __tablename__ = "summaries"

    bvid = Column(String(20), primary_key=True)
    summary = Column(Text)
    key_points = Column(JSON)
    sentiment = Column(Enum("bullish", "bearish", "neutral", name="sentiment"))
    sentiment_score = Column(Float)
    risk_warnings = Column(JSON)
    data_citations = Column(JSON)
    tags = Column(JSON)
    created_at = Column(DateTime, default=_localnow)


class CommentAnalysis(Base):
    __tablename__ = "comment_analysis"

    ref_id = Column(String(50), primary_key=True)
    ref_type = Column(Enum("video", "dynamic", name="ref_type"), default="video", primary_key=True)
    total_count = Column(Integer)
    fetched_count = Column(Integer)
    sentiment_bullish = Column(Float)
    sentiment_bearish = Column(Float)
    sentiment_neutral = Column(Float)
    hot_comments = Column(JSON)
    keywords = Column(JSON)
    fetched_at = Column(DateTime, default=_localnow)


class DanmakuAnalysis(Base):
    __tablename__ = "danmaku_analysis"

    bvid = Column(String(20), primary_key=True)
    total_count = Column(Integer)
    sampled_count = Column(Integer)
    sentiment_bullish = Column(Float)
    sentiment_bearish = Column(Float)
    sentiment_neutral = Column(Float)
    keywords = Column(JSON)
    fetched_at = Column(DateTime, default=_localnow)


class Dynamic(Base):
    __tablename__ = "dynamics"

    dyn_id = Column(String(50), primary_key=True)
    mid = Column(BigInteger, nullable=False, index=True)
    content = Column(Text)
    publish_time = Column(DateTime, index=True)
    summary = Column(Text)
    sentiment = Column(Enum("bullish", "bearish", "neutral", name="dynamic_sentiment"))
    tags = Column(JSON)
    fetched_at = Column(DateTime, default=_localnow)


class FetchWatermark(Base):
    __tablename__ = "fetch_watermark"

    mid = Column(BigInteger, primary_key=True)
    last_bvid = Column(String(20))
    last_publish_time = Column(DateTime)
    last_dyn_id = Column(String(50))
    updated_at = Column(DateTime, default=_localnow)


class ApiUser(Base):
    """API用户表 — 鉴权用。"""
    __tablename__ = "api_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    api_key = Column(String(128), unique=True, nullable=False)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_localnow)


class DailyDigest(Base):
    __tablename__ = "daily_digests"

    digest_date = Column(Date, primary_key=True)
    content = Column(JSON)
    created_at = Column(DateTime, default=_localnow)


class PendingDynamic(Base):
    __tablename__ = "pending_dynamics"
    dyn_id = Column(String(50), primary_key=True)
    mid = Column(BigInteger, nullable=False, index=True)
    payload = Column(JSON, nullable=False)
    retry_count = Column(Integer, default=0, nullable=False)
    error_message = Column(Text)


class DirtyDigest(Base):
    __tablename__ = "dirty_digests"
    digest_date = Column(Date, primary_key=True)
    error_message = Column(Text)
    retry_count = Column(Integer, default=0)  # Issue #10: 失败计数


class FetchJob(Base):
    __tablename__ = "fetch_jobs"
    id = Column(String(36), primary_key=True)
    kind = Column(String(30), nullable=False)
    status = Column(String(20), nullable=False)
    started_at = Column(DateTime, default=_localnow)
    finished_at = Column(DateTime)
    result = Column(JSON)
    error_message = Column(Text)


# Workflow tables intentionally have no backfill from videos. A missing row is
# legacy data, never pending work. Public Transcript/Summary remain a published pair.
class WorkBatch(Base):
    __tablename__ = "work_batches"
    id = Column(String(36), primary_key=True)
    origin = Column(String(24), nullable=False)
    mid = Column(BigInteger)
    since = Column(DateTime, nullable=False)
    until = Column(DateTime, nullable=False)
    selected_bvids = Column(JSON, nullable=False, default=list)
    max_videos = Column(Integer, nullable=False)
    max_pages = Column(Integer, nullable=False)
    spent_pages = Column(Integer, nullable=False, default=0)
    max_audio_seconds = Column(Integer, nullable=False, default=3600)
    spent_audio_seconds = Column(Integer, nullable=False, default=0)
    status = Column(String(24), nullable=False, default="discovering")
    created_at = Column(DateTime, nullable=False, default=_localnow)


class VideoWork(Base):
    __tablename__ = "video_work"
    bvid = Column(String(20), primary_key=True)
    mid = Column(BigInteger, nullable=False, index=True)
    cid = Column(BigInteger)
    batch_id = Column(String(36), ForeignKey("work_batches.id"), nullable=False)
    origin = Column(String(24), nullable=False)
    last_trigger = Column(String(24), nullable=False, default="incremental")
    payload = Column(JSON, nullable=False)
    publish_time = Column(DateTime, nullable=False)
    admitted_at = Column(DateTime, nullable=False, default=_localnow)
    expires_at = Column(DateTime, nullable=False)
    state = Column(String(24), nullable=False, default="pending", index=True)
    reason = Column(String(40))
    credential_version = Column(Integer)
    next_attempt_at = Column(DateTime)
    transcript_version = Column(String(64))
    published_version = Column(String(64))
    generation = Column(BigInteger)
    updated_at = Column(DateTime, nullable=False, default=_localnow)


class WorkStage(Base):
    __tablename__ = "work_stages"
    bvid = Column(String(20), ForeignKey("video_work.bvid"), primary_key=True)
    stage = Column(String(24), primary_key=True)
    cid = Column(BigInteger)
    state = Column(String(24), nullable=False, default="pending")
    reason = Column(String(40))
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime)
    credential_version = Column(Integer)
    input_version = Column(String(64))
    result = Column(JSON)
    updated_at = Column(DateTime, nullable=False, default=_localnow)


class TranscriptCheckpoint(Base):
    __tablename__ = "transcript_checkpoints"
    version = Column(String(64), primary_key=True)
    bvid = Column(String(20), nullable=False, index=True)
    cid = Column(BigInteger, nullable=False)
    source = Column(String(24), nullable=False)
    full_text = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    segment_count = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=_localnow)


class ASRSegment(Base):
    __tablename__ = "asr_segments"
    run_key = Column(String(64), primary_key=True)
    ordinal = Column(Integer, primary_key=True)
    bvid = Column(String(20), nullable=False, index=True)
    cid = Column(BigInteger, nullable=False)
    input_hash = Column(String(64), nullable=False)
    start_seconds = Column(Float, nullable=False)
    end_seconds = Column(Float, nullable=False)
    model = Column(String(100), nullable=False)
    text = Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=_localnow)


class WorkerOwner(Base):
    __tablename__ = "worker_owner"
    id = Column(Integer, primary_key=True)
    owner = Column(String(36), nullable=False)
    generation = Column(BigInteger, nullable=False)
    lease_until = Column(DateTime, nullable=False)


class AuthState(Base):
    __tablename__ = "bili_auth_state"
    id = Column(Integer, primary_key=True)
    credential_version = Column(Integer, nullable=False)
    state = Column(String(16), nullable=False)
    last_confirmed = Column(String(16))
    reason = Column(String(40))
    checked_at = Column(DateTime)
    last_valid_at = Column(DateTime)
    incident_id = Column(String(36))
    recovered_at = Column(DateTime)


class AuthIncident(Base):
    __tablename__ = "auth_incidents"
    id = Column(String(36), primary_key=True)
    credential_version = Column(Integer, nullable=False)
    opened_at = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime)
    last_reminded_at = Column(DateTime, nullable=False)


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"
    id = Column(String(100), primary_key=True)
    incident_id = Column(String(36), nullable=False, index=True)
    kind = Column(String(24), nullable=False)
    credential_version = Column(Integer, nullable=False)
    payload = Column(JSON, nullable=False)
    state = Column(String(24), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=_localnow)
    submitted_at = Column(DateTime)
    acknowledged_at = Column(DateTime)
    error_code = Column(String(40))


class AppDevice(Base):
    __tablename__ = "app_device"
    id = Column(Integer, primary_key=True)
    token = Column(Text, nullable=False)
    platform = Column(String(24), nullable=False, default="android")
    notification_enabled = Column(Boolean, nullable=False, default=False)
    verified_at = Column(DateTime)
    updated_at = Column(DateTime, nullable=False, default=_localnow)


class DailyUsage(Base):
    __tablename__ = "daily_usage"
    day = Column(Date, primary_key=True)
    audio_seconds = Column(Float, nullable=False, default=0)
    llm_calls = Column(Integer, nullable=False, default=0)


class DiscoveryScan(Base):
    __tablename__ = "discovery_scans"
    id = Column(String(36), ForeignKey("work_batches.id"), primary_key=True)
    mid = Column(BigInteger, nullable=False, index=True)
    since = Column(DateTime, nullable=False)
    until = Column(DateTime, nullable=False)
    state = Column(String(24), nullable=False)
    newest_bvid = Column(String(20))
    newest_publish_time = Column(DateTime)
    gap_from = Column(DateTime)
    gap_until = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=_localnow)


class LoginSession(Base):
    __tablename__ = "bili_login_sessions"
    id = Column(String(36), primary_key=True)
    nonce_hash = Column(String(64), nullable=False)
    qrcode_key = Column(String(128), nullable=False)
    credential_version = Column(Integer, nullable=False)
    state = Column(String(24), nullable=False, default="pending")
    expires_at = Column(DateTime, nullable=False)
    last_polled_at = Column(DateTime)
