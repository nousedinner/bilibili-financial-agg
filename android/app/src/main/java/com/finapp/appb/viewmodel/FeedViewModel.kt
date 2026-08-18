package com.finapp.appb.viewmodel

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.finapp.appb.FinApp
import com.finapp.appb.data.api.FeedItem
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

class FeedViewModel(application: Application) : AndroidViewModel(application) {
    private val app = application as FinApp
    private val repo = app.repository

    private val _items = MutableStateFlow<List<FeedItem>>(emptyList())
    val items: StateFlow<List<FeedItem>> = _items

    private val _isLoading = MutableStateFlow(true)
    val isLoading: StateFlow<Boolean> = _isLoading

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error

    private val _isRefreshing = MutableStateFlow(false)
    val isRefreshing: StateFlow<Boolean> = _isRefreshing

    private val _authError = MutableStateFlow(false)
    val authError: StateFlow<Boolean> = _authError

    private val _bloggerNames = MutableStateFlow<Map<Long, String>>(emptyMap())
    val bloggerNames: StateFlow<Map<Long, String>> = _bloggerNames

    private var currentPage = 1
    private var hasMore = true
    private val pageSize = 50
    private var lastRefreshTime = 0L

    init {
        Log.d("FeedVM", "init called")
        loadFeed()
    }

    fun loadFeed() {
        Log.d("FeedVM", "loadFeed called, items=${_items.value.size}")
        viewModelScope.launch {
            val hasDataInMemory = _items.value.isNotEmpty()

            if (!hasDataInMemory) {
                _isLoading.value = true
            }
            _error.value = null
            _authError.value = false

            loadBloggerNames()

            if (hasDataInMemory) {
                Log.d("FeedVM", "Has data in memory, delayed background refresh")
                launch {
                    delay(1000) // Wait 1 second before background refresh
                    refreshInBackground()
                }
                return@launch
            }

            val cached = repo.getFeed(limit = pageSize)
            if (cached.isSuccess && cached.getOrNull()?.isNotEmpty() == true) {
                _items.value = cached.getOrNull()!!
                _isLoading.value = false
                Log.d("FeedVM", "Loaded from cache: ${_items.value.size} items")
            }

            var ready = false
            repeat(15) {
                if (repo.ensureInitialized()) { ready = true; return@repeat }
                delay(200)
            }
            if (!ready) {
                if (_items.value.isEmpty()) {
                    val roomCache = repo.getFeedFromCache()
                    if (roomCache.isNotEmpty()) {
                        _items.value = roomCache
                    } else {
                        _error.value = "API 未就绪，请重启 APP"
                    }
                }
                _isLoading.value = false
                return@launch
            }

            val result = repo.getFeedPage(page = 1, limit = pageSize)
            result.onSuccess { data ->
                _items.value = data.items
                hasMore = data.hasMore
                currentPage = 1
                repo.cacheFeedItems(data.items)
                Log.d("FeedVM", "Loaded from API: ${data.items.size} items")
            }.onFailure {
                val msg = it.message ?: ""
                if (msg.contains("401")) {
                    _authError.value = true
                    _error.value = "密码已失效，请重新配置"
                } else if (_items.value.isEmpty()) {
                    val roomCache = repo.getFeedFromCache()
                    if (roomCache.isNotEmpty()) {
                        _items.value = roomCache
                    } else {
                        _error.value = msg
                    }
                }
            }
            _isLoading.value = false
        }
    }

    private suspend fun refreshInBackground() {
        val now = System.currentTimeMillis()
        if (now - lastRefreshTime < 5000) {
            Log.d("FeedVM", "Skip refresh - too soon (${now - lastRefreshTime}ms)")
            return
        }
        lastRefreshTime = now

        if (!repo.ensureInitialized()) return

        Log.d("FeedVM", "Background refresh starting, current items=${_items.value.size}")
        val result = repo.getFeedPage(page = 1, limit = pageSize)
        result.onSuccess { data ->
            Log.d("FeedVM", "Background refresh got ${data.items.size} items, hasMore=${data.hasMore}")
            val sameData = data.items == _items.value
            Log.d("FeedVM", "Data same=$sameData")
            if (!sameData) {
                Log.d("FeedVM", ">>> UPDATING _items.value - this triggers recomposition")
                _items.value = data.items
                hasMore = data.hasMore
                currentPage = 1
                repo.cacheFeedItems(data.items)
            } else {
                Log.d("FeedVM", "Data same, skip update")
            }
        }.onFailure {
            Log.d("FeedVM", "Background refresh failed: ${it.message}")
        }
    }

    fun loadMore() {
        if (!hasMore || _isLoading.value) return

        viewModelScope.launch {
            _isLoading.value = true
            val nextPage = currentPage + 1
            val result = repo.getFeedPage(page = nextPage, limit = pageSize)
            result.onSuccess { data ->
                val currentItems = _items.value.toMutableList()
                currentItems.addAll(data.items)
                _items.value = currentItems
                currentPage = nextPage
                hasMore = data.hasMore
                repo.cacheFeedItems(data.items)
            }.onFailure {
                // Silently fail
            }
            _isLoading.value = false
        }
    }

    fun refresh() {
        viewModelScope.launch {
            _isRefreshing.value = true
            _error.value = null
            _authError.value = false
            currentPage = 1
            hasMore = true
            loadBloggerNames()
            val result = repo.getFeedPage(page = 1, limit = pageSize)
            result.onSuccess { data ->
                _items.value = data.items
                hasMore = data.hasMore
                repo.cacheFeedItems(data.items)
            }.onFailure {
                val msg = it.message ?: ""
                if (msg.contains("401")) {
                    _authError.value = true
                    _error.value = "密码已失效，请重新配置"
                } else {
                    _error.value = msg
                }
            }
            _isRefreshing.value = false
        }
    }

    private suspend fun loadBloggerNames() {
        try {
            val result = repo.getBloggers()
            result.onSuccess { bloggers ->
                _bloggerNames.value = bloggers.associate { it.mid to it.name }
            }
        } catch (e: Exception) {
            // Ignore
        }
    }
}
