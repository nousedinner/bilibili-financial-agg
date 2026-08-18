package com.finapp.appb.data.local

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "feed_cache")
data class FeedCacheEntity(
    @PrimaryKey
    val bvid: String,  // Use bvid or dyn_id as primary key
    val type: String?,
    val title: String?,
    val summary: String?,
    val sentiment: String?,
    val sentimentScore: Double?,
    val publishTime: String?,
    val mid: Long?,
    val viewCount: Int?,
    val duration: Int?,
    val cachedAt: Long = System.currentTimeMillis()
)
