"""Database table definitions — SQLAlchemy models matching DESIGN.md v6."""

from sqlalchemy import (
    Column, BigInteger, Integer, String, Text, Float, Boolean,
    DateTime, Date, Enum, JSON, ForeignKey, Index,
)
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
