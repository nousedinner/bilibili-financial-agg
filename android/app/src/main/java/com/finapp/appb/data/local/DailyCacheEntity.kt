package com.finapp.appb.data.local

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "daily_cache")
data class DailyCacheEntity(
    @PrimaryKey
    val date: String,         // "2026-09-09"
    val json: String,         // serialized DailyContent
    val cachedAt: Long = System.currentTimeMillis()
)
