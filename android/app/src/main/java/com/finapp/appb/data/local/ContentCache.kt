package com.finapp.appb.data.local

import androidx.room.withTransaction

interface ContentCache {
    val feed: FeedCacheDao
    val details: VideoDetailCacheDao
    val bloggers: BloggerCacheDao
    val daily: DailyCacheDao
    suspend fun <T> transaction(block: suspend () -> T): T
}

class RoomContentCache(private val database: AppDatabase) : ContentCache {
    override val feed = database.feedCacheDao()
    override val details = database.videoDetailCacheDao()
    override val bloggers = database.bloggerCacheDao()
    override val daily = database.dailyCacheDao()
    override suspend fun <T> transaction(block: suspend () -> T): T = database.withTransaction(block)
}
