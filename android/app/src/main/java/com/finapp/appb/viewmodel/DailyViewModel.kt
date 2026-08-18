package com.finapp.appb.viewmodel

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.finapp.appb.FinApp
import com.finapp.appb.data.api.DailyContent
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

class DailyViewModel(application: Application) : AndroidViewModel(application) {
    private val app = application as FinApp
    private val repo = app.repository

    private val _summaries = MutableStateFlow<List<DailyContent>>(emptyList())
    val summaries: StateFlow<List<DailyContent>> = _summaries

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error

    private val _expandedDate = MutableStateFlow<String?>(null)
    val expandedDate: StateFlow<String?> = _expandedDate

    init {
        loadAll()
    }

    fun loadAll() {
        viewModelScope.launch {
            _isLoading.value = true
            _error.value = null

            val datesResult = repo.getDailyDates()
            datesResult.onSuccess { dates ->
                if (dates.isEmpty()) {
                    _summaries.value = emptyList()
                    _isLoading.value = false
                    return@launch
                }

                // 并发加载所有日期的详情
                val details = dates.map { date ->
                    async {
                        repo.getDailyDetail(date).getOrNull()
                    }
                }.awaitAll()

                _summaries.value = details.filterNotNull().sortedByDescending { it.date }
                Log.d("DailyVM", "Loaded ${_summaries.value.size} summaries")
            }.onFailure {
                _error.value = it.message ?: "加载失败"
            }

            _isLoading.value = false
        }
    }

    fun toggleExpand(date: String) {
        _expandedDate.value = if (_expandedDate.value == date) null else date
    }
}
