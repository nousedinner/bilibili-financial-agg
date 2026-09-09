package com.finapp.appb.data.repository

import com.finapp.appb.data.api.*
import com.finapp.appb.data.local.AppDatabase
import com.finapp.appb.data.local.FeedCacheEntity
import com.finapp.appb.data.local.UserPreferences
import com.finapp.appb.data.local.VideoDetailCacheEntity
import com.google.gson.Gson
import kotlinx.coroutines.flow.first
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

class FinRepository(
    private val prefs: UserPreferences,
    private val database: AppDatabase
) {
    private var currentBaseUrl: String = ""
    private var currentApiKey: String = ""
    private var api: FinApiService? = null
    private val feedCacheDao = database.feedCacheDao()
    private val videoDetailCacheDao = database.videoDetailCacheDao()
    private val gson = Gson()

    // In-memory cache
    private var cachedBloggers: List<Blogger>? = null
    private var lastBloggersFetch: Long = 0
    private var cachedFeedItems: List<FeedItem>? = null

    suspend fun ensureInitialized(): Boolean {
        val url = prefs.baseUrl.first()
        val key = prefs.apiKey.first()
        if (url.isBlank() || key.isBlank()) return false
        if (api == null || url != currentBaseUrl || key != currentApiKey) {
            rebuildApi(url, key)
        }
        return true
    }

    private fun rebuildApi(baseUrl: String, apiKey: String) {
        currentBaseUrl = baseUrl
        currentApiKey = apiKey

        val logging = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC
        }

        val client = OkHttpClient.Builder()
            .addInterceptor { chain ->
                val req = chain.request().newBuilder()
                    .addHeader("X-API-Key", apiKey)
                    .build()
                chain.proceed(req)
            }
            .addInterceptor(logging)
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .build()

        val fixedUrl = if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/"

        api = Retrofit.Builder()
            .baseUrl(fixedUrl)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(FinApiService::class.java)
    }

    private fun requireApi(): FinApiService {
        return api ?: throw IllegalStateException("API not initialized")
    }

    // ── Health (no auth) ──
    suspend fun healthCheck(baseUrl: String): Boolean {
        return try {
            val logging = HttpLoggingInterceptor().apply {
                level = HttpLoggingInterceptor.Level.BASIC
            }
            val client = OkHttpClient.Builder()
                .addInterceptor(logging)
                .connectTimeout(10, TimeUnit.SECONDS)
                .build()
            val fixedUrl = if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/"
            val tempApi = Retrofit.Builder()
                .baseUrl(fixedUrl)
                .client(client)
                .addConverterFactory(GsonConverterFactory.create())
                .build()
                .create(FinApiService::class.java)
            tempApi.health().isSuccessful
        } catch (e: Exception) {
            false
        }
    }

    // ── Bootstrap ──
    suspend fun bootstrap(baseUrl: String, username: String): Result<UserResponse> {
        return try {
            val logging = HttpLoggingInterceptor().apply {
                level = HttpLoggingInterceptor.Level.BASIC
            }
            val client = OkHttpClient.Builder()
                .addInterceptor(logging)
                .connectTimeout(15, TimeUnit.SECONDS)
                .build()
            val fixedUrl = if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/"
            val tempApi = Retrofit.Builder()
                .baseUrl(fixedUrl)
                .client(client)
                .addConverterFactory(GsonConverterFactory.create())
                .build()
                .create(FinApiService::class.java)
            val resp = tempApi.bootstrap(BootstrapRequest(username))
            if (resp.isSuccessful && resp.body()?.code == 0) {
                Result.success(resp.body()!!.data)
            } else {
                Result.failure(Exception(resp.errorBody()?.string() ?: "创建失败"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    // ── Feed with Cache ──
    suspend fun getFeedPage(before: Double? = null, limit: Int = 50): Result<FeedPageData> {
        return try {
            val resp = requireApi().getFeedPage(before, limit)
            if (resp.isSuccessful) {
                Result.success(resp.body()!!.data)
            } else {
                // API 失败，回退到 Room 缓存
                val cached = getFeedFromCache()
                if (cached.isNotEmpty()) {
                    Result.success(FeedPageData(items = cached, total = cached.size, hasMore = false))
                } else {
                    Result.failure(Exception("加载失败: ${resp.code()}"))
                }
            }
        } catch (e: Exception) {
            // 网络异常，回退到 Room 缓存
            val cached = getFeedFromCache()
            if (cached.isNotEmpty()) {
                Result.success(FeedPageData(items = cached, total = cached.size, hasMore = false))
            } else {
                Result.failure(e)
            }
        }
    }

    suspend fun getFeed(limit: Int = 100, blogger: Long? = null): Result<List<FeedItem>> {
        if (cachedFeedItems != null && blogger == null) {
            return Result.success(cachedFeedItems!!)
        }

        return try {
            val resp = requireApi().getFeed(limit, blogger)
            if (resp.isSuccessful) {
                val items = resp.body()!!.data.items
                if (blogger == null) {
                    cachedFeedItems = items
                }
                cacheFeedItems(items)
                Result.success(items)
            } else {
                Result.failure(Exception("加载失败: ${resp.code()}"))
            }
        } catch (e: Exception) {
            val cached = getFeedFromCache()
            if (cached.isNotEmpty()) {
                Result.success(cached)
            } else {
                Result.failure(e)
            }
        }
    }

    suspend fun getFeedFromCache(): List<FeedItem> {
        return try {
            feedCacheDao.getAll().map { entity ->
                FeedItem(
                    type = entity.type,
                    bvid = entity.bvid.takeIf { it.isNotEmpty() && it != "null" },
                    dynId = null,
                    title = entity.title,
                    sentiment = entity.sentiment,
                    summary = entity.summary,
                    publishTime = entity.publishTime,
                    mid = entity.mid,
                    viewCount = entity.viewCount,
                    duration = entity.duration,
                    sentimentScore = entity.sentimentScore
                )
            }
        } catch (e: Exception) {
            emptyList()
        }
    }

    suspend fun cacheFeedItems(items: List<FeedItem>) {
        try {
            val entities = items.map { item ->
                FeedCacheEntity(
                    bvid = item.bvid ?: item.dynId ?: "unknown_${System.currentTimeMillis()}",
                    type = item.type,
                    title = item.title,
                    summary = item.summary,
                    sentiment = item.sentiment,
                    sentimentScore = item.sentimentScore,
                    publishTime = item.publishTime,
                    mid = item.mid,
                    viewCount = item.viewCount,
                    duration = item.duration
                )
            }
            feedCacheDao.insertAll(entities)
        } catch (e: Exception) {
            // Ignore cache errors
        }
    }

    suspend fun checkForUpdates(): Boolean {
        return try {
            val resp = requireApi().status()
            if (resp.isSuccessful) {
                true
            } else {
                false
            }
        } catch (e: Exception) {
            false
        }
    }

    // ── Bloggers with Memory Cache ──
    suspend fun getBloggers(forceRefresh: Boolean = false): Result<List<Blogger>> {
        // Return cached if available and not forcing refresh
        if (!forceRefresh && cachedBloggers != null) {
            return Result.success(cachedBloggers!!)
        }

        return try {
            val resp = requireApi().getBloggers()
            if (resp.isSuccessful) {
                val bloggers = resp.body()!!.data
                cachedBloggers = bloggers
                lastBloggersFetch = System.currentTimeMillis()
                Result.success(bloggers)
            } else {
                Result.failure(Exception("加载失败: ${resp.code()}"))
            }
        } catch (e: Exception) {
            // Return cached if available on error
            if (cachedBloggers != null) {
                Result.success(cachedBloggers!!)
            } else {
                Result.failure(e)
            }
        }
    }

    suspend fun preloadBloggers() {
        getBloggers()
    }

    suspend fun addBloggerByName(name: String): Result<AddBloggerResponse> {
        return try {
            val resp = requireApi().addBloggerByName(name)
            if (resp.isSuccessful) {
                // Invalidate cache
                cachedBloggers = null
                Result.success(resp.body()!!.data)
            } else {
                Result.failure(Exception("添加失败: ${resp.code()}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun deleteBlogger(mid: Long): Result<Any> {
        return try {
            val resp = requireApi().deleteBlogger(mid)
            if (resp.isSuccessful) {
                // Invalidate cache
                cachedBloggers = null
                Result.success(Any())
            } else {
                Result.failure(Exception("删除失败: ${resp.code()}"))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    // ── Video Detail ──
    suspend fun getVideoDetail(bvid: String): Result<VideoDetail> {
        return try {
            val resp = requireApi().getVideoDetail(bvid)
            if (resp.isSuccessful) {
                val detail = resp.body()!!.data
                // 成功时写入缓存
                try {
                    val json = gson.toJson(detail)
                    videoDetailCacheDao.insert(VideoDetailCacheEntity(bvid = bvid, json = json))
                } catch (_: Exception) { }
                Result.success(detail)
            } else {
                // API 失败，回退到缓存
                val cached = getVideoDetailFromCache(bvid)
                if (cached != null) Result.success(cached)
                else Result.failure(Exception("加载失败: ${resp.code()}"))
            }
        } catch (e: Exception) {
            // 网络异常，回退到缓存
            val cached = getVideoDetailFromCache(bvid)
            if (cached != null) Result.success(cached)
            else Result.failure(e)
        }
    }

    private suspend fun getVideoDetailFromCache(bvid: String): VideoDetail? {
        return try {
            val entity = videoDetailCacheDao.getByBvid(bvid) ?: return null
            gson.fromJson(entity.json, VideoDetail::class.java)
        } catch (_: Exception) {
            null
        }
    }

    // ── Daily ──
    suspend fun getDailyDates(): Result<List<String>> {
        return try {
            val resp = requireApi().getDailyDates()
            if (resp.isSuccessful) Result.success(resp.body()!!.data.dates)
            else Result.failure(Exception("加载失败: ${resp.code()}"))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun getDailyDetail(date: String): Result<DailyContent> {
        return try {
            val resp = requireApi().getDailyDetail(date)
            if (resp.isSuccessful) Result.success(resp.body()!!.data)
            else Result.failure(Exception("加载失败: ${resp.code()}"))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    // ── Status ──
    suspend fun getStatus(): Result<SystemStatus> {
        return try {
            val resp = requireApi().status()
            if (resp.isSuccessful) Result.success(resp.body()!!.data)
            else Result.failure(Exception("加载失败: ${resp.code()}"))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    // ── Operations ──
    suspend fun triggerFetch(): Result<String> {
        return try {
            val resp = requireApi().triggerFetch()
            if (resp.isSuccessful) Result.success("已触发")
            else Result.failure(Exception("触发失败: ${resp.code()}"))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    // ── Sync Bloggers ──
    suspend fun syncBloggers(): Result<SyncResponse> {
        return try {
            val resp = requireApi().syncBloggers()
            if (resp.isSuccessful) Result.success(resp.body()!!.data)
            else Result.failure(Exception("同步失败: ${resp.code()}"))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun batchAddBloggers(mids: String, names: String): Result<BatchAddResponse> {
        return try {
            val resp = requireApi().batchAddBloggers(mids, names)
            if (resp.isSuccessful) Result.success(resp.body()!!.data)
            else Result.failure(Exception("添加失败: ${resp.code()}"))
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
}
