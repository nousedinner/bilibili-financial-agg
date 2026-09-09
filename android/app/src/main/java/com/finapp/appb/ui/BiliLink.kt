package com.finapp.appb.ui

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri

/**
 * 统一B站跳转工具：国内版 → 国际版 → 浏览器
 *
 * 国内版(tv.danmaku.bili)支持 bilibili:// 深链接
 * 国际版(com.bilibili.app.in)只支持 https:// web URL
 */
object BiliLink {
    private const val PKG_DOMESTIC = "tv.danmaku.bili"
    private const val PKG_INTERNATIONAL = "com.bilibili.app.in"

    /** 在B站APP打开视频 */
    fun openVideo(context: Context, bvid: String) {
        val webUrl = "https://www.bilibili.com/video/$bvid"
        val domesticUri = Uri.parse("bilibili://video/$bvid")
        val webUri = Uri.parse(webUrl)

        // 1. 国内版 — 支持 bilibili:// 深链接
        if (isAppInstalled(context, PKG_DOMESTIC)) {
            try {
                context.startActivity(Intent(Intent.ACTION_VIEW, domesticUri).apply {
                    setPackage(PKG_DOMESTIC)
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                })
                return
            } catch (_: Exception) {}
        }
        // 2. 国际版 — 只支持 https:// web URL
        if (isAppInstalled(context, PKG_INTERNATIONAL)) {
            try {
                context.startActivity(Intent(Intent.ACTION_VIEW, webUri).apply {
                    setPackage(PKG_INTERNATIONAL)
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                })
                return
            } catch (_: Exception) {}
        }
        // 3. 浏览器兜底
        try {
            context.startActivity(Intent(Intent.ACTION_VIEW, webUri).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            })
        } catch (_: Exception) {}
    }

    /** 在B站APP打开UP主空间 */
    fun openSpace(context: Context, mid: Long) {
        val webUrl = "https://space.bilibili.com/$mid"
        val domesticUri = Uri.parse("bilibili://space/$mid")
        val webUri = Uri.parse(webUrl)

        // 1. 国内版
        if (isAppInstalled(context, PKG_DOMESTIC)) {
            try {
                context.startActivity(Intent(Intent.ACTION_VIEW, domesticUri).apply {
                    setPackage(PKG_DOMESTIC)
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                })
                return
            } catch (_: Exception) {}
        }
        // 2. 国际版 — 只支持 https:// web URL
        if (isAppInstalled(context, PKG_INTERNATIONAL)) {
            try {
                context.startActivity(Intent(Intent.ACTION_VIEW, webUri).apply {
                    setPackage(PKG_INTERNATIONAL)
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                })
                return
            } catch (_: Exception) {}
        }
        // 3. 浏览器兜底
        try {
            context.startActivity(Intent(Intent.ACTION_VIEW, webUri).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            })
        } catch (_: Exception) {}
    }

    private fun isAppInstalled(context: Context, packageName: String): Boolean = try {
        context.packageManager.getPackageInfo(packageName, 0)
        true
    } catch (_: PackageManager.NameNotFoundException) { false }
}
