package com.finapp.appb.data.local

import androidx.room.*

@Dao
interface DailyCacheDao {
    @Query("SELECT * FROM daily_cache ORDER BY date DESC")
    suspend fun getAll(): List<DailyCacheEntity>

    @Query("SELECT * FROM daily_cache WHERE date = :date")
    suspend fun getByDate(date: String): DailyCacheEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertAll(items: List<DailyCacheEntity>)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insert(item: DailyCacheEntity)

    @Query("DELETE FROM daily_cache")
    suspend fun clear()

    @Query("DELETE FROM daily_cache WHERE cachedAt < :before")
    suspend fun deleteOlderThan(before: Long)
}
