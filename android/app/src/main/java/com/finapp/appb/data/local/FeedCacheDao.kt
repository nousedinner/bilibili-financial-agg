package com.finapp.appb.data.local

import androidx.room.*

@Dao
interface FeedCacheDao {
    @Query("SELECT * FROM feed_cache ORDER BY publishTime DESC, bvid DESC LIMIT 500")
    suspend fun getAll(): List<FeedCacheEntity>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertAll(items: List<FeedCacheEntity>)

    @Query("DELETE FROM feed_cache")
    suspend fun clear()

    @Query("DELETE FROM feed_cache WHERE cachedAt < :before")
    suspend fun deleteOlderThan(before: Long)

    @Query("DELETE FROM feed_cache WHERE bvid NOT IN (SELECT bvid FROM feed_cache ORDER BY publishTime DESC, bvid DESC LIMIT 500)")
    suspend fun trim()

    @Query("SELECT COUNT(*) FROM feed_cache")
    suspend fun count(): Int
}
