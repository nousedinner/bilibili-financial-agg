package com.finapp.appb.ui.navigation

enum class Screen(val route: String) {
    SETUP("setup"),
    FEED("feed"),
    VIDEO_DETAIL("video/{bvid}"),
    BLOGGERS("bloggers"),
    DAILY("daily"),
    SETTINGS("settings")
}
