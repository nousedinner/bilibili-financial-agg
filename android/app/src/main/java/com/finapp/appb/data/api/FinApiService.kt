package com.finapp.appb.data.api

import retrofit2.Response
import retrofit2.http.*

interface FinApiService {

    // ── System ──
    @GET("api/health")
    suspend fun health(): Response<Map<String, String>>

    @GET("api/status")
    suspend fun status(): Response<ApiResponse<SystemStatus>>

    // ── User ──
    @POST("api/bootstrap")
    suspend fun bootstrap(@Body request: BootstrapRequest): Response<ApiResponse<UserResponse>>

    // ── Bloggers ──
    @GET("api/bloggers")
    suspend fun getBloggers(): Response<ApiResponse<List<Blogger>>>

    @POST("api/bloggers")
    suspend fun addBloggerByName(@Query("name") name: String): Response<ApiResponse<AddBloggerResponse>>

    @POST("api/bloggers")
    suspend fun addBloggerByMid(
        @Query("mid") mid: Long,
        @Query("name") name: String
    ): Response<ApiResponse<AddBloggerResponse>>

    @POST("api/bloggers/sync")
    suspend fun syncBloggers(): Response<ApiResponse<SyncResponse>>

    @POST("api/bloggers/sync/add")
    suspend fun batchAddBloggers(
        @Query("mids") mids: String,
        @Query("names") names: String
    ): Response<ApiResponse<BatchAddResponse>>

    @DELETE("api/bloggers/{mid}")
    suspend fun deleteBlogger(@Path("mid") mid: Long): Response<ApiResponse<Any>>

    // ── Videos ──
    @GET("api/videos")
    suspend fun getVideos(
        @Query("page") page: Int = 1,
        @Query("limit") limit: Int = 20,
        @Query("blogger") blogger: Long? = null,
        @Query("sentiment") sentiment: String? = null
    ): Response<ApiResponse<VideoListResponse>>

    @GET("api/videos/{bvid}")
    suspend fun getVideoDetail(@Path("bvid") bvid: String): Response<ApiResponse<VideoDetail>>

    // ── Feed ──
    @GET("api/feed")
    suspend fun getFeed(
        @Query("limit") limit: Int = 50,
        @Query("blogger") blogger: Long? = null
    ): Response<ApiResponse<FeedResponse>>

    @GET("api/feed")
    suspend fun getFeedPage(
        @Query("before") before: Double? = null,
        @Query("limit") limit: Int = 50
    ): Response<ApiResponse<FeedPageData>>

    // ── Daily ──
    @GET("api/daily")
    suspend fun getDailyDates(): Response<ApiResponse<DailyDatesResponse>>

    @GET("api/daily/{date}")
    suspend fun getDailyDetail(@Path("date") date: String): Response<ApiResponse<DailyContent>>

    // ── Operations ──
    @POST("api/fetch/trigger")
    suspend fun triggerFetch(): Response<ApiResponse<ApiMessage>>
}
