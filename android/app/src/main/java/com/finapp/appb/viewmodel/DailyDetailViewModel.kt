package com.finapp.appb.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.finapp.appb.FinApp
import com.finapp.appb.data.api.DailyContent
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

class DailyDetailViewModel(application: Application) : AndroidViewModel(application) {
    private val repo = (application as FinApp).repository

    private val _detail = MutableStateFlow<DailyContent?>(null)
    val detail: StateFlow<DailyContent?> = _detail

    private val _isLoading = MutableStateFlow(true)
    val isLoading: StateFlow<Boolean> = _isLoading

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error

    fun loadDetail(date: String) {
        viewModelScope.launch {
            _isLoading.value = true
            _error.value = null
            // 1. Show cache immediately
            val cached = try { repo.getCachedDailyDetail(date) } catch (_: Exception) { null }
            if (cached != null) {
                _detail.value = cached
                _isLoading.value = false
            }
            // 2. Fetch from network
            val result = repo.refreshDailyDetail(date)
            result.onSuccess { _detail.value = it }
                .onFailure {
                    if (cached == null) _error.value = it.message ?: "加载失败"
                }
            _isLoading.value = false
        }
    }
}
