package com.finapp.appb.data.local

import kotlinx.coroutines.flow.Flow

data class ConnectionConfig(val url: String, val key: String, val username: String)

interface ConnectionStore {
    val config: Flow<ConnectionConfig>
    val cacheScope: Flow<String>
    suspend fun saveConfig(url: String, key: String, user: String)
    suspend fun setCacheScope(scope: String)
    suspend fun clearConfig()
}
