package com.finapp.appb.viewmodel

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.finapp.appb.FinApp
import com.finapp.appb.data.api.DailyContent
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

    // 按日期缓存已加载的详情
    private val _details = mutableMapOf<String, DailyContent>()

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

                // 只加载日期列表，创建空壳 DailyContent 占位
                _summaries.value = dates.map { date ->
                    _details[date] ?: DailyContent(date = date)
                }
                Log.d("DailyVM", "Loaded ${dates.size} date entries (details lazy)")
            }.onFailure {
                _error.value = it.message ?: "加载失败"
            }

            _isLoading.value = false
        }
    }

    fun toggleExpand(date: String) {
        if (_expandedDate.value == date) {
            _expandedDate.value = null
        } else {
            _expandedDate.value = date
            // 展开时如果还没有该日期的详情数据，加载之
            if (_details[date] == null) {
                viewModelScope.launch {
                    val detail = repo.getDailyDetail(date).getOrNull()
                    if (detail != null) {
                        _details[date] = detail
                        // 更新 summaries 中对应项
                        _summaries.value = _summaries.value.map {
                            if (it.date == date) detail else it
                        }
                    }
                }
            }
        }
    }
}
