package com.finapp.appb.data.local

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase

@Database(
    entities = [
        FeedCacheEntity::class,
        VideoDetailCacheEntity::class,
        BloggerCacheEntity::class,
        DailyCacheEntity::class
    ],
    version = 3,
    exportSchema = false
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun feedCacheDao(): FeedCacheDao
    abstract fun videoDetailCacheDao(): VideoDetailCacheDao
    abstract fun bloggerCacheDao(): BloggerCacheDao
    abstract fun dailyCacheDao(): DailyCacheDao

    companion object {
        @Volatile
        private var INSTANCE: AppDatabase? = null

        fun getDatabase(context: Context): AppDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "fin_app_db"
                )
                    .fallbackToDestructiveMigration() // 缓存数据，丢失可接受
                    .build()
                INSTANCE = instance
                instance
            }
        }
    }
}
