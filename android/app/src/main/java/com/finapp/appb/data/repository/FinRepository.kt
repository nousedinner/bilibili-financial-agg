package com.finapp.appb.data.repository

import com.finapp.appb.data.api.*
import com.finapp.appb.data.local.*
import com.google.gson.Gson
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import okhttp3.OkHttpClient
import retrofit2.Response
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

class ApiException(val status: Int, message: String) : IOException(message)
fun Throwable.isAuthError() = this is ApiException && status in listOf(401, 403)
fun Throwable.canUseCache() = this is IOException && (this !is ApiException || status >= 500)
fun FeedItem.stableId(): String? = when (type) {
    "video" -> bvid?.takeIf { it.isNotBlank() }?.let { "video:$it" }
    "dynamic" -> dynId?.takeIf { it.isNotBlank() }?.let { "dynamic:$it" }
    else -> null
}
fun mergeFeedItems(old: List<FeedItem>, next: List<FeedItem>): List<FeedItem> =
    (next + old).filter { it.stableId() != null }.distinctBy { it.stableId() }
        .sortedWith(compareByDescending<FeedItem> { it.publishTime }.thenByDescending { it.stableId() })

class FinRepository(private val prefs: ConnectionStore, private val cache: ContentCache) {
    constructor(prefs: UserPreferences, database: AppDatabase) : this(prefs, RoomContentCache(database))
    private data class Session(val scope: String, val client: OkHttpClient, val api: FinApiService)
    private val mutex = Mutex()
    private var current: Session? = null
    private var cachedBloggers: List<Blogger>? = null
    private var bloggersAt = 0L
    private val feedDao = cache.feed
    private val detailDao = cache.details
    private val gson = Gson()
    private val maxAge = TimeUnit.DAYS.toMillis(7)

    private fun fingerprint(url: String, key: String): String = MessageDigest.getInstance("SHA-256")
        .digest((url.trim().trimEnd('/') + "/\n" + key).toByteArray()).joinToString("") { "%02x".format(it) }

    private fun build(url: String, key: String): Session {
        val normalized = url.trim().trimEnd('/') + "/"
        val scope = fingerprint(normalized, key)
        val client = OkHttpClient.Builder().connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS).callTimeout(40, TimeUnit.SECONDS)
            .followRedirects(false).followSslRedirects(false)
            .addInterceptor { chain -> chain.proceed(chain.request().newBuilder().header("X-API-Key", key).build()) }
            .build()
        val api = Retrofit.Builder().baseUrl(normalized).client(client)
            .addConverterFactory(GsonConverterFactory.create()).build().create(FinApiService::class.java)
        return Session(scope, client, api)
    }

    private suspend fun clearCache() {
        cachedBloggers = null
        bloggersAt = 0
        cache.transaction { feedDao.clear(); detailDao.clear() }
    }

    private suspend fun initialized(): Session? {
        val config = prefs.config.first()
        if (config.url.isBlank() || config.key.isBlank()) {
            current?.client?.dispatcher?.cancelAll()
            current = null
            clearCache()
            return null
        }
        if (current?.scope == fingerprint(config.url, config.key)) return current
        val candidate = build(config.url, config.key)
        current?.client?.dispatcher?.cancelAll()
        current = null
        if (prefs.cacheScope.first() != candidate.scope) {
            clearCache()
            prefs.setCacheScope(candidate.scope)
        }
        current = candidate
        feedDao.deleteOlderThan(System.currentTimeMillis() - maxAge)
        detailDao.deleteOlderThan(System.currentTimeMillis() - maxAge)
        return candidate
    }

    suspend fun ensureInitialized(): Boolean = try {
        mutex.withLock { initialized() != null }
    } catch (e: CancellationException) { throw e } catch (_: Exception) { false }

    suspend fun connect(url: String, key: String, username: String): Result<Unit> = try {
        require(key.isNotBlank()) { "请输入 API Key" }
        val candidate = build(url, key.trim())
        val bloggers = payload(candidate.api.getBloggers())
        withContext(NonCancellable) {
            mutex.withLock {
                current?.client?.dispatcher?.cancelAll()
                current = null
                clearCache()
                prefs.saveConfig(url.trim().trimEnd('/') + "/", key.trim(), username.trim())
                prefs.setCacheScope(candidate.scope)
                current = candidate
                cachedBloggers = bloggers
                bloggersAt = System.currentTimeMillis()
            }
        }
        Result.success(Unit)
    } catch (e: CancellationException) { throw e } catch (e: Exception) { Result.failure(e) }

    suspend fun logout() = withContext(NonCancellable) {
        mutex.withLock {
            current?.client?.dispatcher?.cancelAll()
            current = null
            prefs.clearConfig()
            clearCache()
        }
    }

    private fun checkSession(session: Session) {
        if (current !== session) throw CancellationException("Connection changed")
    }

    private fun <T> payload(response: Response<ApiResponse<T>>): T {
        if (!response.isSuccessful) throw ApiException(response.code(), when (response.code()) {
            401, 403 -> "认证已失效，请重新配置"
            409 -> "已有任务正在运行，请稍后查看状态"
            else -> "请求失败 (${response.code()})"
        })
        val body = response.body() ?: throw IOException("服务器返回空响应")
        if (body.code != 0) throw ApiException(400, "服务器未接受请求 (${body.code})")
        return body.data ?: throw IOException("服务器返回缺少数据")
    }

    private suspend fun <T> request(
        call: suspend (FinApiService) -> Response<ApiResponse<T>>,
        save: suspend (T) -> Unit = {},
        fallback: (suspend () -> T?)? = null
    ): Result<T> {
        var session: Session? = null
        return try {
            val active = mutex.withLock { initialized() ?: throw ApiException(401, "请先配置连接") }
            session = active
            val data = payload(call(active.api))
            mutex.withLock { checkSession(active); save(data) }
            Result.success(data)
        } catch (e: CancellationException) { throw e } catch (e: Exception) {
            mutex.withLock {
                session?.let { checkSession(it) }
                if (e.isAuthError()) {
                    current?.client?.dispatcher?.cancelAll()
                    current = null
                    clearCache()
                }
                val cached = if (session != null && e.canUseCache()) fallback?.invoke() else null
                if (cached != null) Result.success(cached) else Result.failure(e)
            }
        }
    }

    suspend fun getFeedPage(before: Double? = null, limit: Int = 50, beforeId: String? = null): Result<FeedPageData> =
        request(call = { it.getFeedPage(before, limit, beforeId) }, save = { data ->
            cache.transaction {
                if (before == null) feedDao.clear()
                feedDao.insertAll(data.items.mapNotNull { item -> item.stableId()?.let { id ->
                    FeedCacheEntity(id, item.type, item.title, item.summary, item.sentiment, item.sentimentScore,
                        item.publishTime, item.mid, item.viewCount, item.duration)
                } })
                feedDao.deleteOlderThan(System.currentTimeMillis() - maxAge)
                feedDao.trim()
            }
        }, fallback = if (before == null) ({
            val items = feedDao.getAll().filter { it.cachedAt >= System.currentTimeMillis() - maxAge }.map { e ->
                val id = e.bvid.substringAfter(':')
                FeedItem(e.type, id.takeIf { e.type == "video" }, id.takeIf { e.type == "dynamic" }, e.title,
                    e.sentiment, e.summary, e.publishTime, e.mid, e.viewCount, e.duration, e.sentimentScore)
            }
            if (items.isEmpty()) null else FeedPageData(items, items.size, false, fromCache = true)
        }) else null)

    suspend fun getBloggers(forceRefresh: Boolean = false): Result<List<Blogger>> {
        try {
            mutex.withLock {
                initialized()
                if (!forceRefresh && cachedBloggers != null && System.currentTimeMillis() - bloggersAt < 60_000)
                    return Result.success(cachedBloggers!!)
            }
        } catch (e: CancellationException) { throw e } catch (e: Exception) { return Result.failure(e) }
        return request({ it.getBloggers() }, save = { cachedBloggers = it; bloggersAt = System.currentTimeMillis() })
    }

    private suspend fun invalidateContent() {
        // Reject reads started before a blogger was disabled or added.
        current?.client?.dispatcher?.cancelAll()
        current = current?.copy()
        clearCache()
    }

    suspend fun preloadBloggers() { getBloggers() }
    suspend fun addBloggerByName(name: String) = request({ it.addBloggerByName(name) }, save = { invalidateContent() })
    suspend fun deleteBlogger(mid: Long) = request({ it.deleteBlogger(mid) }, save = { invalidateContent() })
    suspend fun batchAddBloggers(bloggers: List<BloggerInput>) = request({ it.batchAddBloggers(bloggers) }, save = { invalidateContent() })
    suspend fun syncBloggers() = request({ it.syncBloggers() })
    suspend fun getDailyDates(): Result<List<String>> = request({ it.getDailyDates() }).map { it.dates }
    suspend fun getDailyDetail(date: String) = request({ it.getDailyDetail(date) })
    suspend fun getStatus() = request({ it.status() })
    suspend fun triggerFetch(): Result<String> = request({ it.triggerFetch() }).map { it.message }

    suspend fun getVideoDetail(bvid: String): Result<VideoDetail> = request({ it.getVideoDetail(bvid) }, save = {
        detailDao.insert(VideoDetailCacheEntity(bvid, gson.toJson(it)))
        detailDao.deleteOlderThan(System.currentTimeMillis() - maxAge)
        detailDao.trim()
    }, fallback = {
        detailDao.getByBvid(bvid)?.takeIf { it.cachedAt >= System.currentTimeMillis() - maxAge }?.let {
            gson.fromJson(it.json, VideoDetail::class.java).copy(fromCache = true)
        }
    })
}
