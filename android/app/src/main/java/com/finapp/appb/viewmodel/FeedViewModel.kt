package com.finapp.appb.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.finapp.appb.FinApp
import com.finapp.appb.data.api.*
import com.finapp.appb.data.repository.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

class FeedViewModel(application: Application) : AndroidViewModel(application) {
    private val repo = (application as FinApp).repository
    private val _items = MutableStateFlow<List<FeedItem>>(emptyList())
    val items: StateFlow<List<FeedItem>> = _items
    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading
    private val _isRefreshing = MutableStateFlow(false)
    val isRefreshing: StateFlow<Boolean> = _isRefreshing
    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error
    private val _authError = MutableStateFlow(false)
    val authError: StateFlow<Boolean> = _authError
    private val _bloggerNames = MutableStateFlow<Map<Long, String>>(emptyMap())
    val bloggerNames: StateFlow<Map<Long, String>> = _bloggerNames
    private var cursor: FeedCursor? = null
    private var hasMore = false
    private var pageFailed = false
    private var generation = 0
    private var job: Job? = null

    init { refresh() }
    fun loadFeed() = refresh()
    fun dismissAuthError() { _authError.value = false }
    fun retry() { if (pageFailed) { pageFailed = false; loadMore() } else refresh() }

    fun refresh() {
        val token = ++generation
        job?.cancel()
        _isLoading.value = true
        _isRefreshing.value = _items.value.isNotEmpty()
        _error.value = null
        _authError.value = false
        pageFailed = false
        hasMore = false
        cursor = null
        job = viewModelScope.launch {
            try {
                val result = repo.getFeedPage()
                ensureActive()
                if (token != generation) return@launch
                result.onSuccess { data ->
                    _items.value = data.items
                    cursor = data.nextCursor
                    hasMore = data.hasMore && cursor?.before != null && cursor?.beforeId != null
                    if (data.fromCache) _error.value = "网络暂不可用，当前显示缓存内容；下拉可重试"
                }.onFailure { failed(it) }
                repo.getBloggers().onSuccess { _bloggerNames.value = it.associate { b -> b.mid to b.name } }
            } finally {
                if (token == generation) { _isLoading.value = false; _isRefreshing.value = false }
            }
        }
    }

    fun loadMore() {
        val next = cursor ?: return
        if (!hasMore || _isLoading.value || pageFailed) return
        _isLoading.value = true
        _error.value = null
        val token = generation
        job = viewModelScope.launch {
            try {
                val result = repo.getFeedPage(before = next.before, beforeId = next.beforeId)
                ensureActive()
                if (token != generation) return@launch
                result.onSuccess { data ->
                    _items.value = mergeFeedItems(_items.value, data.items)
                    cursor = data.nextCursor
                    hasMore = data.hasMore && cursor != null && cursor != next && cursor?.beforeId != null
                }.onFailure { pageFailed = true; failed(it) }
            } finally {
                if (token == generation) _isLoading.value = false
            }
        }
    }

    private fun failed(error: Throwable) {
        _error.value = error.message ?: "加载失败，请重试"
        if (error.isAuthError()) {
            _items.value = emptyList()
            _authError.value = true
            hasMore = false
        }
    }
}
