package com.finapp.appb.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.finapp.appb.FinApp
import com.finapp.appb.data.api.DailyContent
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

class DailyViewModel(application: Application) : AndroidViewModel(application) {
    private val repo = (application as FinApp).repository
    private val _summaries = MutableStateFlow<List<DailyContent>>(emptyList())
    val summaries: StateFlow<List<DailyContent>> = _summaries
    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading
    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error
    private val _expandedDate = MutableStateFlow<String?>(null)
    val expandedDate: StateFlow<String?> = _expandedDate
    private val _detailLoading = MutableStateFlow<String?>(null)
    val detailLoading: StateFlow<String?> = _detailLoading
    private val _detailError = MutableStateFlow<String?>(null)
    val detailError: StateFlow<String?> = _detailError
    private var detailJob: Job? = null
    private var detailGeneration = 0

    init { loadAll() }
    fun loadAll() {
        if (_isLoading.value) return
        _isLoading.value = true
        detailGeneration++
        detailJob?.cancel()
        _detailLoading.value = null
        _detailError.value = null
        _expandedDate.value = null
        viewModelScope.launch {
            try {
                _error.value = null
                repo.getDailyDates().onSuccess { dates ->
                    _summaries.value = dates.distinct().map { DailyContent(date = it) }
                }.onFailure { _error.value = it.message ?: "日期加载失败" }
            } finally { _isLoading.value = false }
        }
    }

    fun toggleExpand(date: String) {
        detailGeneration++
        detailJob?.cancel()
        _detailLoading.value = null
        _detailError.value = null
        if (_expandedDate.value == date) _expandedDate.value = null
        else {
            _expandedDate.value = date
            loadDetail(date)
        }
    }

    fun retryDetail() { _expandedDate.value?.let { loadDetail(it) } }
    private fun loadDetail(date: String) {
        detailGeneration++
        detailJob?.cancel()
        _detailLoading.value = date
        _detailError.value = null
        val token = detailGeneration
        detailJob = viewModelScope.launch {
            try {
                repo.getDailyDetail(date).onSuccess { detail ->
                    _summaries.value = _summaries.value.map { if (it.date == date) detail else it }
                }.onFailure { _detailError.value = it.message ?: "详情加载失败" }
            } finally { if (token == detailGeneration) _detailLoading.value = null }
        }
    }
}
