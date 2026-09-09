package com.finapp.appb

import com.finapp.appb.data.api.*
import com.finapp.appb.data.local.*
import com.finapp.appb.data.repository.*
import com.google.gson.Gson
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.*
import org.junit.After
import org.junit.Before
import org.junit.Test
import java.io.IOException
import java.util.concurrent.TimeUnit

private class MemoryPreferences : ConnectionStore {
    override val config = MutableStateFlow(ConnectionConfig("", "", ""))
    override val cacheScope = MutableStateFlow("")
    override suspend fun saveConfig(url: String, key: String, user: String) { config.value = ConnectionConfig(url, key, user) }
    override suspend fun setCacheScope(scope: String) { cacheScope.value = scope }
    override suspend fun clearConfig() { config.value = ConnectionConfig("", "", ""); cacheScope.value = "" }
}

private class MemoryCache : ContentCache {
    val feeds = linkedMapOf<String, FeedCacheEntity>()
    val videos = linkedMapOf<String, VideoDetailCacheEntity>()
    override val feed = object : FeedCacheDao {
        override suspend fun getAll() = feeds.values.sortedByDescending { it.publishTime }
        override suspend fun insertAll(items: List<FeedCacheEntity>) { items.forEach { feeds[it.bvid] = it } }
        override suspend fun clear() { feeds.clear() }
        override suspend fun count() = feeds.size
        override suspend fun deleteOlderThan(before: Long) { feeds.entries.removeAll { it.value.cachedAt < before } }
        override suspend fun trim() {}
    }
    override val details = object : VideoDetailCacheDao {
        override suspend fun getByBvid(bvid: String) = videos[bvid]
        override suspend fun insert(item: VideoDetailCacheEntity) { videos[item.bvid] = item }
        override suspend fun clear() { videos.clear() }
        override suspend fun deleteOlderThan(before: Long) { videos.entries.removeAll { it.value.cachedAt < before } }
        override suspend fun trim() {}
    }
    override suspend fun <T> transaction(block: suspend () -> T): T = block()
}

class RepositoryTest {
    private lateinit var server: MockWebServer
    private lateinit var prefs: MemoryPreferences
    private lateinit var cache: MemoryCache
    private lateinit var repo: FinRepository
    private val page = """{"code":0,"data":{"items":[{"type":"dynamic","dyn_id":"123","summary":"Cached dynamic","publish_time":"2026-09-09T12:00:00"}],"total":2,"has_more":true,"next_cursor":{"before":1788926400,"before_id":"dynamic:123"}}}"""

    @Before fun setup() {
        server = MockWebServer(); server.start()
        prefs = MemoryPreferences(); cache = MemoryCache(); repo = FinRepository(prefs, cache)
    }
    @After fun cleanup() { server.shutdown() }
    private fun response(body: String) = MockResponse().setHeader("Content-Type", "application/json").setBody(body)
    private suspend fun connect() {
        server.enqueue(response("""{"code":0,"data":[]}"""))
        repo.connect(server.url("/").toString(), "test-key", "test-user").getOrThrow()
        assertEquals("test-key", server.takeRequest(5, TimeUnit.SECONDS)!!.getHeader("X-API-Key"))
    }

    @Test fun compoundCursorRoundTripsAndDynamicCacheKeepsId() = runBlocking {
        connect(); server.enqueue(response(page))
        val first = repo.getFeedPage().getOrThrow()
        server.takeRequest(5, TimeUnit.SECONDS)
        assertEquals("dynamic:123", first.nextCursor!!.beforeId)
        server.enqueue(MockResponse().setResponseCode(503))
        val more = repo.getFeedPage(first.nextCursor.before, beforeId = first.nextCursor.beforeId)
        assertTrue(more.isFailure)
        val request = server.takeRequest(5, TimeUnit.SECONDS)!!
        assertEquals("dynamic:123", request.requestUrl!!.queryParameter("before_id"))
        server.enqueue(MockResponse().setResponseCode(503))
        val offline = repo.getFeedPage().getOrThrow()
        assertTrue(offline.fromCache)
        assertFalse(offline.hasMore)
        assertNull(offline.items.single().bvid)
        assertEquals("123", offline.items.single().dynId)
    }

    @Test fun authenticationFailureNeverReturnsOrKeepsCache() = runBlocking {
        connect(); server.enqueue(response(page)); repo.getFeedPage().getOrThrow()
        server.enqueue(MockResponse().setResponseCode(401))
        val failed = repo.getFeedPage()
        assertTrue(failed.exceptionOrNull()!!.isAuthError())
        assertTrue(cache.feeds.isEmpty())
    }

    @Test fun authFailureInvalidatesEarlierSuccessfulResponse() = runBlocking {
        connect()
        server.enqueue(response(page).setBodyDelay(2, TimeUnit.SECONDS))
        val earlier = async { repo.getFeedPage() }
        withContext(Dispatchers.IO) { server.takeRequest(5, TimeUnit.SECONDS) }
        server.enqueue(MockResponse().setResponseCode(401))
        assertTrue(repo.getStatus().exceptionOrNull()!!.isAuthError())
        try { earlier.await(); fail("Late data must not recreate cache after 401") }
        catch (_: CancellationException) {}
        assertTrue(cache.feeds.isEmpty())
    }

    @Test fun deletingBloggerInvalidatesInFlightFeedAndCache() = runBlocking {
        connect()
        server.enqueue(response(page).setBodyDelay(2, TimeUnit.SECONDS))
        val earlier = async { repo.getFeedPage() }
        withContext(Dispatchers.IO) { server.takeRequest(5, TimeUnit.SECONDS) }
        server.enqueue(response("""{"code":0,"data":{"message":"disabled"}}"""))
        assertTrue(repo.deleteBlogger(123).isSuccess)
        try { earlier.await(); fail("Late feed must not restore disabled content") }
        catch (_: CancellationException) {}
        assertTrue(cache.feeds.isEmpty())
    }

    @Test fun logoutWaitsForStorageAndRejectsLateResponse() = runBlocking {
        connect()
        server.enqueue(response(page).setBodyDelay(2, TimeUnit.SECONDS))
        val pending = async { repo.getFeedPage() }
        withContext(Dispatchers.IO) { assertNotNull(server.takeRequest(5, TimeUnit.SECONDS)) }
        repo.logout()
        try { pending.await(); fail("Previous session request must be cancelled") }
        catch (_: CancellationException) {}
        assertEquals("", prefs.config.value.key)
        assertTrue(cache.feeds.isEmpty())
        assertFalse(repo.ensureInitialized())
    }

    @Test fun switchingServerClearsOldAccountCache() = runBlocking {
        connect(); server.enqueue(response(page)); repo.getFeedPage().getOrThrow()
        val second = MockWebServer(); second.start()
        try {
            second.enqueue(response("""{"code":0,"data":[]}"""))
            repo.connect(second.url("/").toString(), "other-key", "other").getOrThrow()
            assertTrue(cache.feeds.isEmpty())
            second.enqueue(MockResponse().setResponseCode(503))
            assertTrue(repo.getFeedPage().isFailure)
            assertEquals("other-key", prefs.config.value.key)
        } finally { second.shutdown() }
    }

    @Test fun invalidConnectionDoesNotOverwriteExistingConfiguration() = runBlocking {
        connect(); val original = prefs.config.value
        server.enqueue(MockResponse().setResponseCode(401))
        assertTrue(repo.connect(server.url("/").toString(), "bad-key", "bad").isFailure)
        assertEquals(original, prefs.config.value)
    }

    @Test fun expiredCacheCannotBeUsedOffline() = runBlocking {
        connect(); server.enqueue(response(page)); repo.getFeedPage().getOrThrow()
        cache.feeds.replaceAll { _, item -> item.copy(cachedAt = 0) }
        server.enqueue(MockResponse().setResponseCode(503))
        assertTrue(repo.getFeedPage().isFailure)
    }

    @Test fun cancellationPropagatesWithoutReturningCache() = runBlocking {
        connect(); server.enqueue(response(page)); repo.getFeedPage().getOrThrow()
        server.takeRequest(5, TimeUnit.SECONDS)
        server.enqueue(response(page).setBodyDelay(2, TimeUnit.SECONDS))
        val pending = async { repo.getFeedPage() }
        withContext(Dispatchers.IO) { server.takeRequest(5, TimeUnit.SECONDS) }
        pending.cancelAndJoin()
        assertTrue(pending.isCancelled)
        assertEquals(1, cache.feeds.size)
    }

    @Test fun loadedFirstPageReplacesDisabledAndStaleRows() = runBlocking {
        connect(); server.enqueue(response(page)); repo.getFeedPage().getOrThrow()
        server.enqueue(response("""{"code":0,"data":{"items":[],"total":0,"has_more":false}}"""))
        assertTrue(repo.getFeedPage().getOrThrow().items.isEmpty())
        assertTrue(cache.feeds.isEmpty())
    }

    @Test fun duplicatePagesMergeUsingTypeAndId() {
        val dynamic = Gson().fromJson("""{"type":"dynamic","dyn_id":"123","publish_time":"2026-09-09T12:00:00"}""", FeedItem::class.java)
        val video = dynamic.copy(type = "video", bvid = "123", dynId = null)
        val merged = mergeFeedItems(listOf(dynamic, video), listOf(dynamic.copy(summary = "updated")))
        assertEquals(2, merged.size)
        assertEquals("updated", merged.single { it.type == "dynamic" }.summary)
        assertEquals("video:123", merged.first().stableId())
    }

    @Test fun cachePolicyAndDailyPlaceholderRemainExplicit() {
        assertTrue(IOException("offline").canUseCache())
        assertFalse(ApiException(401, "auth").canUseCache())
        assertFalse(ApiException(404, "missing").canUseCache())
        assertFalse(CancellationException().canUseCache())
        val placeholder = DailyContent(date = "2026-09-09")
        assertNull(placeholder.overallSentiment)
        assertNull(placeholder.sentimentScore)
        assertNull(placeholder.summary)
    }
}
