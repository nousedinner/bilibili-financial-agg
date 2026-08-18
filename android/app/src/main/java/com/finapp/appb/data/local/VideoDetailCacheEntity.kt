package com.finapp.appb.data.local

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "video_detail_cache")
data class VideoDetailCacheEntity(
    @PrimaryKey
    val bvid: String,
    val json: String,  // 序列化后的 VideoDetail JSON
    val cachedAt: Long = System.currentTimeMillis()
)
