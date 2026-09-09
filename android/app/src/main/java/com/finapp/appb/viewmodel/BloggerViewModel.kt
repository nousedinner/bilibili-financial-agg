package com.finapp.appb.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.finapp.appb.FinApp
import com.finapp.appb.data.api.Blogger
import com.finapp.appb.data.api.SyncFollowing
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

class BloggerViewModel(application: Application) : AndroidViewModel(application) {
    private val app = application as FinApp
    private val repo = app.repository

    private val _bloggers = MutableStateFlow<List<Blogger>>(emptyList())
    val bloggers: StateFlow<List<Blogger>> = _bloggers

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error

    private val _toast = MutableStateFlow<String?>(null)
    val toast: StateFlow<String?> = _toast

    // Sync state
    private val _newFollowings = MutableStateFlow<List<SyncFollowing>>(emptyList())
    val newFollowings: StateFlow<List<SyncFollowing>> = _newFollowings

    private val _syncLoading = MutableStateFlow(false)
    val syncLoading: StateFlow<Boolean> = _syncLoading

    private val _selectedMids = MutableStateFlow<Set<Long>>(emptySet())
    val selectedMids: StateFlow<Set<Long>> = _selectedMids

    init {
        loadBloggers()
    }

    fun loadBloggers() {
        viewModelScope.launch {
            _isLoading.value = true
            _error.value = null
            val result = repo.getBloggers()
            result.onSuccess { _bloggers.value = it }
                .onFailure { _error.value = it.message ?: "加载失败" }
            _isLoading.value = false
        }
    }

    fun refreshBloggers() {
        viewModelScope.launch {
            _isLoading.value = true
            _error.value = null
            val result = repo.getBloggers(forceRefresh = true)
            result.onSuccess { _bloggers.value = it }
                .onFailure {
                    val msg = it.message ?: "加载失败"
                    _error.value = if (msg.contains("429")) "请求过于频繁，请稍后再试" else msg
                }
            _isLoading.value = false
        }
    }

    fun addBloggerByName(name: String) {
        viewModelScope.launch {
            _toast.value = "正在搜索并添加..."
            val result = repo.addBloggerByName(name)
            result.onSuccess { resp ->
                _toast.value = resp.message ?: "添加成功"
                refreshBloggers()
            }.onFailure {
                _toast.value = "添加失败: ${it.message}"
            }
        }
    }

    fun syncBloggers() {
        viewModelScope.launch {
            _syncLoading.value = true
            _error.value = null
            val result = repo.syncBloggers()
            result.onSuccess { resp ->
                val newFollowings = resp.newFollowings ?: emptyList()
                _toast.value = "找到 ${resp.newCount} 个未追踪的关注"
                _newFollowings.value = newFollowings
            }.onFailure {
                _toast.value = "同步失败: ${it.message}"
            }
            _syncLoading.value = false
        }
    }

    fun toggleSelection(mid: Long) {
        val current = _selectedMids.value.toMutableSet()
        if (current.contains(mid)) current.remove(mid) else current.add(mid)
        _selectedMids.value = current
    }

    fun selectAll() {
        _selectedMids.value = _newFollowings.value.map { it.mid }.toSet()
    }

    fun clearSelection() {
        _selectedMids.value = emptySet()
    }

    fun addSelected() {
        viewModelScope.launch {
            val selected = _selectedMids.value
            if (selected.isEmpty()) {
                _toast.value = "请先选择要添加的博主"
                return@launch
            }
            val followings = _newFollowings.value
            val inputs = followings.filter { it.mid in selected }.map {
                com.finapp.appb.data.api.BloggerInput(it.mid, it.name)
            }
            if (inputs.size != selected.size) {
                _toast.value = "关注列表已变化，请重新同步"
                return@launch
            }
            val result = repo.batchAddBloggers(inputs)
            result.onSuccess { resp ->
                val addedCount = resp.added?.size ?: 0
                _toast.value = "已添加 $addedCount 个博主"
                _selectedMids.value = emptySet()
                _newFollowings.value = emptyList()
                refreshBloggers()
            }.onFailure {
                _toast.value = "添加失败: ${it.message}"
            }
        }
    }

    fun deleteBlogger(mid: Long) {
        viewModelScope.launch {
            val result = repo.deleteBlogger(mid)
            result.onSuccess {
                _toast.value = "已删除"
                refreshBloggers()
            }.onFailure { _toast.value = "删除失败: ${it.message}" }
        }
    }

    fun clearSync() {
        _newFollowings.value = emptyList()
        _selectedMids.value = emptySet()
    }

    fun clearToast() { _toast.value = null }
}
