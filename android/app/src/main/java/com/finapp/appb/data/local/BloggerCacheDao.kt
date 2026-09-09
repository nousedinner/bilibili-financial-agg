package com.finapp.appb.data.local

import androidx.room.*

@Dao
interface BloggerCacheDao {
    @Query("SELECT * FROM blogger_cache ORDER BY cachedAt DESC")
    suspend fun getAll(): List<BloggerCacheEntity>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertAll(items: List<BloggerCacheEntity>)

    @Query("DELETE FROM blogger_cache")
    suspend fun clear()

    @Query("DELETE FROM blogger_cache WHERE cachedAt < :before")
    suspend fun deleteOlderThan(before: Long)
}
