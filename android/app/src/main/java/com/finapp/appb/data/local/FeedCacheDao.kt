package com.finapp.appb.data.local

import androidx.room.*

@Dao
interface FeedCacheDao {
    @Query("SELECT * FROM feed_cache ORDER BY publishTime DESC")
    suspend fun getAll(): List<FeedCacheEntity>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertAll(items: List<FeedCacheEntity>)

    @Query("DELETE FROM feed_cache")
    suspend fun clear()

    @Query("SELECT COUNT(*) FROM feed_cache")
    suspend fun count(): Int
}
