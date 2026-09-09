package com.finapp.appb.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.finapp.appb.FinApp
import com.finapp.appb.data.api.SystemStatus
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

class SettingsViewModel(application: Application) : AndroidViewModel(application) {
    private val app = application as FinApp
    private val repo = app.repository
    private val prefs = app.preferences

    private val _status = MutableStateFlow<SystemStatus?>(null)
    val status: StateFlow<SystemStatus?> = _status

    private val _isTriggering = MutableStateFlow(false)
    val isTriggering: StateFlow<Boolean> = _isTriggering

    private val _toast = MutableStateFlow<String?>(null)
    val toast: StateFlow<String?> = _toast

    val baseUrl = prefs.baseUrl
    val apiKey = prefs.apiKey
    val username = prefs.username

    fun loadStatus() {
        viewModelScope.launch {
            val result = repo.getStatus()
            result.onSuccess { _status.value = it }
        }
    }

    fun triggerFetch() {
        viewModelScope.launch {
            _isTriggering.value = true
            val result = repo.triggerFetch()
            result.onSuccess { _toast.value = "已触发抓取" }
                .onFailure { _toast.value = "触发失败: ${it.message}" }
            _isTriggering.value = false
        }
    }

    fun logout() {
        repo.reset()
        viewModelScope.launch { prefs.clearConfig() }
    }

    fun clearToast() { _toast.value = null }
}
