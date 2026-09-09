package com.finapp.appb.data.local

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "fin_settings")

class UserPreferences(private val context: Context) : ConnectionStore {

    companion object {
        private val API_BASE_URL = stringPreferencesKey("api_base_url")
        private val API_KEY = stringPreferencesKey("api_key")
        private val CACHE_SCOPE = stringPreferencesKey("cache_scope")
        private val USERNAME = stringPreferencesKey("username")
    }

    override val config: Flow<ConnectionConfig> = context.dataStore.data.map {
        ConnectionConfig(it[API_BASE_URL] ?: "", it[API_KEY] ?: "", it[USERNAME] ?: "")
    }
    override val cacheScope: Flow<String> = context.dataStore.data.map { it[CACHE_SCOPE] ?: "" }
    override suspend fun setCacheScope(scope: String) { context.dataStore.edit { it[CACHE_SCOPE] = scope } }

    val baseUrl: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[API_BASE_URL] ?: "https://home.cancanneed.top/fin-api/"
    }

    val apiKey: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[API_KEY] ?: ""
    }

    val username: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[USERNAME] ?: ""
    }

    override suspend fun saveConfig(url: String, key: String, user: String) {
        context.dataStore.edit { prefs ->
            prefs[API_BASE_URL] = url
            prefs[API_KEY] = key
            prefs[USERNAME] = user
        }
    }

    override suspend fun clearConfig() {
        context.dataStore.edit { it.clear() }
    }
}
