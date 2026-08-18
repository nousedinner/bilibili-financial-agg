package com.finapp.appb.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.finapp.appb.FinApp
import com.finapp.appb.data.api.VideoDetail
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

class VideoDetailViewModel(application: Application) : AndroidViewModel(application) {
    private val app = application as FinApp
    private val repo = app.repository

    private val _detail = MutableStateFlow<VideoDetail?>(null)
    val detail: StateFlow<VideoDetail?> = _detail

    private val _isLoading = MutableStateFlow(true)
    val isLoading: StateFlow<Boolean> = _isLoading

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error

    fun loadDetail(bvid: String) {
        viewModelScope.launch {
            _isLoading.value = true
            _error.value = null
            val result = repo.getVideoDetail(bvid)
            result.onSuccess { _detail.value = it }
                .onFailure { _error.value = it.message ?: "加载失败" }
            _isLoading.value = false
        }
    }
}
