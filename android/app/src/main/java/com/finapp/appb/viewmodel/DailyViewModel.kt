package com.finapp.appb.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.finapp.appb.FinApp
import com.finapp.appb.data.api.DailyContent
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

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

            try {
                // 1. Load cached details immediately
                val cachedDates = try { repo.getCachedDailyDates() } catch (_: Exception) { emptyList() }
                if (cachedDates.isNotEmpty()) {
                    val cachedItems = cachedDates.mapNotNull { date ->
                        try { repo.getCachedDailyDetail(date) } catch (_: Exception) { null }
                    }
                    if (cachedItems.isNotEmpty()) {
                        _summaries.value = cachedItems
                        _isLoading.value = false
                    }
                }

                // 2. Fetch date list from network
                val datesResult = repo.getDailyDates()
                datesResult.onSuccess { dates ->
                    if (dates.isEmpty()) {
                        if (_summaries.value.isEmpty()) _summaries.value = emptyList()
                        _isLoading.value = false
                        return@launch
                    }

                    // 3. Pre-load missing details from network (safe, isolated)
                    withContext(Dispatchers.IO) {
                        try { repo.preloadDailyDetails(dates) } catch (_: Exception) {}
                    }

                    // 4. Reload all from cache (now complete)
                    val allDetails = dates.mapNotNull { date ->
                        try { repo.getCachedDailyDetail(date) } catch (_: Exception) { null }
                    }.sortedByDescending { it.date }
                    if (allDetails.isNotEmpty()) _summaries.value = allDetails
                }.onFailure {
                    // Network failed but we already showed cache above
                    if (_summaries.value.isEmpty()) {
                        _error.value = it.message ?: "加载失败"
                    }
                }
            } catch (e: Exception) {
                if (_summaries.value.isEmpty()) {
                    _error.value = e.message ?: "加载失败"
                }
            }

            _isLoading.value = false
        }
    }
}
