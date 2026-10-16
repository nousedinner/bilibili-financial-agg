package com.finapp.appb

import android.app.Application
import com.finapp.appb.data.local.AppDatabase
import com.finapp.appb.data.local.UserPreferences
import com.finapp.appb.data.repository.FinRepository
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class FinApp : Application() {
    val preferences by lazy { UserPreferences(this) }
    val database by lazy { AppDatabase.getDatabase(this) }
    val repository by lazy { FinRepository(preferences, database) }

    // App scope for background work (not tied to any ViewModel)
    val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        appScope.launch { com.finapp.appb.data.local.removeLegacyManagementKey(this@FinApp) }
        com.finapp.appb.notifications.AuthorizationNotifications.start(this)
        // Only initialize session; data loading is handled by ViewModels (cache-first)
    }
}
