package com.finapp.appb.data.local

import androidx.room.*

@Dao
interface VideoDetailCacheDao {
    @Query("SELECT * FROM video_detail_cache WHERE bvid = :bvid")
    suspend fun getByBvid(bvid: String): VideoDetailCacheEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(item: VideoDetailCacheEntity)

    @Query("DELETE FROM video_detail_cache")
    suspend fun clear()

    @Query("DELETE FROM video_detail_cache WHERE bvid NOT IN (SELECT bvid FROM video_detail_cache ORDER BY cachedAt DESC LIMIT 100)")
    suspend fun trim()

    @Query("DELETE FROM video_detail_cache WHERE cachedAt < :before")
    suspend fun deleteOlderThan(before: Long)
}
