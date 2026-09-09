package com.finapp.appb.data.api

import com.google.gson.annotations.SerializedName

// ── Generic Response ──
data class ApiResponse<T>(
    val code: Int,
    val data: T
)

data class ApiMessage(
    val message: String
)

// ── User / Auth ──
data class BootstrapRequest(val username: String)
data class UserResponse(
    val username: String,
    @SerializedName("api_key") val apiKey: String
)

// ── System Status ──
data class SystemStatus(
    val bloggers: Int,
    val videos: Int,
    val failed: Int,
    @SerializedName("last_fetch") val lastFetch: String?,
    @SerializedName("cookie_valid") val cookieValid: Boolean,
    @SerializedName("cookie_expire") val cookieExpire: String,
    @SerializedName("last_job") val lastJob: TaskStatus? = null
)

// ── Blogger ──
data class Blogger(
    val mid: Long,
    val name: String,
    val tags: List<String>?,
    @SerializedName("added_at") val addedAt: String
)

// ── Feed ──
data class FeedPageData(
    val items: List<FeedItem>,
    val total: Int?,
    @SerializedName("has_more") val hasMore: Boolean,
    @SerializedName("next_cursor") val nextCursor: FeedCursor? = null,
    @Transient val fromCache: Boolean = false
)

data class FeedCursor(
    val before: Double? = null,
    @SerializedName("before_id") val beforeId: String? = null
)

data class FeedItem(
    val type: String?,
    val bvid: String?,
    @SerializedName("dyn_id") val dynId: String?,
    val title: String?,
    val sentiment: String?,
    val summary: String?,
    @SerializedName("publish_time") val publishTime: String?,
    val mid: Long?,
    @SerializedName("view_count") val viewCount: Int?,
    val duration: Int?,
    @SerializedName("sentiment_score") val sentimentScore: Double?
)

// ── Video List ──
data class VideoListResponse(
    val items: List<VideoItem>,
    val total: Int,
    val page: Int,
    @SerializedName("has_more") val hasMore: Boolean
)

data class VideoItem(
    val bvid: String,
    val mid: Long,
    val title: String,
    val duration: Int,
    @SerializedName("publish_time") val publishTime: String,
    @SerializedName("view_count") val viewCount: Int,
    val sentiment: String,
    @SerializedName("sentiment_score") val sentimentScore: Double,
    val summary: String
)

// ── Video Detail ──
data class VideoDetail(
    val bvid: String?,
    val mid: Long?,
    val title: String?,
    val duration: Int?,
    @SerializedName("publish_time") val publishTime: String?,
    @SerializedName("view_count") val viewCount: Int?,
    val summary: VideoSummary?,
    val transcript: Transcript?,
    val comments: CommentsAnalysis?,
    val danmaku: DanmakuAnalysis?,
    @Transient val fromCache: Boolean = false
)

data class VideoSummary(
    val text: String?,
    @SerializedName("key_points") val keyPoints: List<String>?,
    val sentiment: String?,
    @SerializedName("sentiment_score") val sentimentScore: Double?,
    @SerializedName("risk_warnings") val riskWarnings: List<String>?,
    @SerializedName("data_citations") val dataCitations: List<String>?,
    val tags: List<String>?
)

data class Transcript(
    val source: String?,
    val text: String?
)

data class CommentsAnalysis(
    val total: Int?,
    val sentiment: SentimentDistribution?,
    @SerializedName("hot_comments") val hotComments: List<String>?,
    val keywords: List<String>?
)

data class SentimentDistribution(
    val bullish: Double?,
    val bearish: Double?,
    val neutral: Double?
)

data class DanmakuAnalysis(
    val total: Int?,
    val sampled: Int?,
    val sentiment: SentimentDistribution?,
    val keywords: List<String>?
)

// ── Add Blogger Response ──
data class AddBloggerResponse(
    val mid: Long?,
    val name: String?,
    val message: String?
)

// ── Sync Response ──
data class SyncResponse(
    @SerializedName("total_followings") val totalFollowings: Int,
    @SerializedName("already_tracked") val alreadyTracked: Int,
    @SerializedName("new_count") val newCount: Int,
    @SerializedName("new") val newFollowings: List<SyncFollowing>?
)

data class SyncFollowing(
    val mid: Long,
    val name: String,
    val sign: String?
)

// ── Batch Add Response ──
data class BatchAddResponse(
    val added: List<BatchAddItem>?
)

data class BatchAddItem(
    val mid: Long,
    val name: String,
    val action: String?
)

// ── Daily ──
data class DailyDatesResponse(
    val dates: List<String>
)

data class DailyContent(
    val date: String? = null,
    val summary: String? = null,
    val bloggers: List<String>? = null,
    val consensus: List<String>? = null,
    @SerializedName("key_topics") val keyTopics: List<String>? = null,
    val differences: List<String>? = null,
    @SerializedName("overall_sentiment") val overallSentiment: String? = null,
    @SerializedName("sentiment_score") val sentimentScore: Double? = null
)


data class BloggerInput(val mid: Long, val name: String)

data class TaskStatus(val id: String, val kind: String, val status: String, val error: String?)
