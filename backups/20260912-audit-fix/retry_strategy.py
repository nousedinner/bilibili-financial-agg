"""重试策略模块 — 按失败类型分级处理。"""

# 不同错误类型的最大重试次数
MAX_RETRIES_BY_ERROR = {
    "permanent": 0,      # 视频已删除/404，永不重试
    "incomplete_info": 2, # 视频信息不完整(duration=0)，需重新抓取
    "llm_failed": 2,      # LLM分析失败，降级重试
    "transcript": 3,      # 字幕获取失败，正常重试
    "unknown": 3,         # 未知错误，正常重试
}


def classify_error(error_message: str, duration: int = 0) -> str:
    """根据错误信息和视频元数据分类失败类型。"""
    if not error_message:
        return "unknown"
    msg = error_message.lower()
    # 视频已删除
    if "404" in msg or "啥都木有" in msg:
        return "permanent"
    # 视频信息不完整（duration=0 且不是字幕问题）
    if duration == 0 and "transcript" not in msg:
        return "incomplete_info"
    # LLM分析失败
    if "llm analysis failed" in msg:
        return "llm_failed"
    # 字幕问题
    if "transcript" in msg or "subtitle" in msg:
        return "transcript"
    return "unknown"


def max_retries_for(error_type: str) -> int:
    """获取某错误类型的最大重试次数。"""
    return MAX_RETRIES_BY_ERROR.get(error_type, 3)


def should_retry(retry_count: int, error_type: str) -> bool:
    """判断是否应该重试。"""
    return retry_count < max_retries_for(error_type)


def is_permanent(error_type: str) -> bool:
    """判断是否为永久性失败（不应重试）。"""
    return error_type == "permanent"
