package com.finapp.appb.data.local

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "blogger_cache")
data class BloggerCacheEntity(
    @PrimaryKey
    val mid: Long,
    val name: String,
    val tags: String?,       // JSON array stored as string
    val addedAt: String?,
    val cachedAt: Long = System.currentTimeMillis()
)
