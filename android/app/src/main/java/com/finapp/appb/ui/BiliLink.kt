package com.finapp.appb.ui

import android.content.Context
import android.content.Intent
import android.net.Uri

object BiliLink {
    private const val PKG_DOMESTIC = "tv.danmaku.bili"

    fun openVideo(context: Context, bvid: String) {
        val uri = Uri.parse("https://www.bilibili.com/video/$bvid")
        openWithFallback(context, uri)
    }

    fun openSpace(context: Context, mid: Long) {
        val uri = Uri.parse("https://space.bilibili.com/$mid")
        openWithFallback(context, uri)
    }

    fun openDynamic(context: Context, dynId: String) {
        val uri = Uri.parse("https://t.bilibili.com/$dynId")
        openWithFallback(context, uri)
    }

    private fun openWithFallback(context: Context, uri: Uri) {
        try {
            context.startActivity(Intent(Intent.ACTION_VIEW, uri).apply {
                setPackage(PKG_DOMESTIC)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            })
            return
        } catch (_: Exception) {}
        try {
            context.startActivity(Intent(Intent.ACTION_VIEW, uri).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            })
        } catch (_: Exception) {}
    }
}
