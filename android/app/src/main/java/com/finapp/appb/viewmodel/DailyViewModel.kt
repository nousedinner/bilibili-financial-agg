package com.finapp.appb.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.finapp.appb.FinApp
import com.finapp.appb.data.api.DailyContent
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

    init { loadAll() }

    fun loadAll() {
        if (_isLoading.value) return
        viewModelScope.launch {
            _isLoading.value = true
            _error.value = null

            // 1. Fetch date list from network
            val datesResult = repo.getDailyDates()
            datesResult.onSuccess { dates ->
                if (dates.isEmpty()) {
                    _summaries.value = emptyList()
                    _isLoading.value = false
                    return@launch
                }

                // 2. Load cached details immediately
                val cachedDetails = dates.map { date ->
                    try { repo.getCachedDailyDetail(date) } catch (_: Exception) { null }
                }
                val cachedItems = cachedDetails.filterNotNull().sortedByDescending { it.date }
                if (cachedItems.isNotEmpty()) {
                    _summaries.value = cachedItems
                    _isLoading.value = false
                }

                // 3. Pre-load missing details from network
                repo.preloadDailyDetails(dates)

                // 4. Reload all from cache (now complete)
                val allDetails = dates.mapNotNull { date ->
                    try { repo.getCachedDailyDetail(date) } catch (_: Exception) { null }
                }.sortedByDescending { it.date }
                _summaries.value = allDetails
            }.onFailure {
                // Try loading from cache even if network fails
                try {
                    val cachedAll = repo.getAllCachedDailyDetails()
                    if (cachedAll.isNotEmpty()) {
                        _summaries.value = cachedAll
                    } else {
                        _error.value = it.message ?: "加载失败"
                    }
                } catch (_: Exception) {
                    _error.value = it.message ?: "加载失败"
                }
            }

            _isLoading.value = false
        }
    }
}
