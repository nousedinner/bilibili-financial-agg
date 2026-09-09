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

    private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        appScope.launch {
            if (repository.ensureInitialized()) repository.preloadBloggers()
        }
    }
}
