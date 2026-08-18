package com.finapp.appb

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.finapp.appb.ui.navigation.Screen
import com.finapp.appb.ui.screens.*
import com.finapp.appb.ui.theme.FinAppTheme
import com.finapp.appb.viewmodel.*

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            FinAppTheme {
                FinAppRoot()
            }
        }
    }
}

@Composable
fun FinAppRoot() {
    val context = LocalContext.current
    val app = context.applicationContext as FinApp
    val navController = rememberNavController()

    var isConfigured by remember { mutableStateOf(false) }
    var configChecked by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        isConfigured = app.repository.ensureInitialized()
        configChecked = true
    }

    if (!configChecked) {
        Box(modifier = Modifier.fillMaxSize(), contentAlignment = androidx.compose.ui.Alignment.Center) {
            CircularProgressIndicator()
        }
        return
    }

    val startDestination = if (isConfigured) Screen.FEED.route else Screen.SETUP.route

    val navBackStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = navBackStackEntry?.destination?.route

    val showBottomBar = currentRoute in listOf(Screen.FEED.route, Screen.BLOGGERS.route, Screen.DAILY.route)

    // 【修复关键】把 NavHost 移到 Scaffold 外面
    // 原因：Scaffold 重组时（showBottomBar 变化触发）会连带重建内部的 NavHost，
    // 导致 NavBackStackEntry 销毁 → ViewModel 被清除 → init 重复调用
    // 修复后：NavHost 不受 Scaffold 重组影响，ViewModel 生命周期稳定
    Box(modifier = Modifier.fillMaxSize()) {
        // NavHost 在外层，不受 Scaffold showBottomBar 变化影响
        NavHost(
            navController = navController,
            startDestination = startDestination,
            modifier = Modifier
                .fillMaxSize()
                .padding(bottom = if (showBottomBar) 80.dp else 0.dp) // NavigationBar 高度
        ) {
            composable(Screen.SETUP.route) {
                SetupScreen(
                    onSetupComplete = {
                        navController.navigate(Screen.FEED.route) {
                            popUpTo(Screen.SETUP.route) { inclusive = true }
                        }
                    }
                )
            }

            composable(Screen.FEED.route) {
                val feedVm: FeedViewModel = viewModel()
                FeedScreen(
                    viewModel = feedVm,
                    onVideoClick = { bvid ->
                        navController.navigate("video/$bvid")
                    },
                    onSettingsClick = {
                        navController.navigate(Screen.SETTINGS.route)
                    }
                )
            }

            composable(
                route = Screen.VIDEO_DETAIL.route,
                arguments = listOf(navArgument("bvid") { type = NavType.StringType }),
                enterTransition = { fadeIn(animationSpec = tween(150)) },
                exitTransition = { fadeOut(animationSpec = tween(150)) },
                popEnterTransition = { fadeIn(animationSpec = tween(150)) },
                popExitTransition = { fadeOut(animationSpec = tween(150)) }
            ) { backStackEntry ->
                val bvid = backStackEntry.arguments?.getString("bvid") ?: return@composable
                val detailVm: VideoDetailViewModel = viewModel()
                VideoDetailScreen(
                    bvid = bvid,
                    viewModel = detailVm,
                    onBack = { navController.popBackStack() }
                )
            }

            composable(Screen.BLOGGERS.route) {
                val bloggerVm: BloggerViewModel = viewModel()
                BloggerScreen(viewModel = bloggerVm)
            }

            composable(Screen.DAILY.route) {
                val dailyVm: DailyViewModel = viewModel()
                DailyScreen(viewModel = dailyVm)
            }

            composable(Screen.SETTINGS.route) {
                val settingsVm: SettingsViewModel = viewModel()
                SettingsScreen(
                    viewModel = settingsVm,
                    onLogout = {
                        navController.navigate(Screen.SETUP.route) {
                            popUpTo(0) { inclusive = true }
                        }
                    }
                )
            }
        }

        // 底部导航栏独立渲染，只控制显示/隐藏，不影响 NavHost
        if (showBottomBar) {
            NavigationBar(
                containerColor = MaterialTheme.colorScheme.surface.copy(alpha = 0.85f),
                modifier = Modifier.align(androidx.compose.ui.Alignment.BottomCenter)
            ) {
                NavigationBarItem(
                    selected = currentRoute == Screen.FEED.route,
                    onClick = {
                        if (currentRoute != Screen.FEED.route) {
                            navController.navigate(Screen.FEED.route) {
                                popUpTo(Screen.FEED.route) { inclusive = true }
                            }
                        }
                    },
                    icon = { Text("📰") },
                    label = { Text("资讯") }
                )
                NavigationBarItem(
                    selected = currentRoute == Screen.BLOGGERS.route,
                    onClick = {
                        if (currentRoute != Screen.BLOGGERS.route) {
                            navController.navigate(Screen.BLOGGERS.route) {
                                popUpTo(Screen.FEED.route)
                            }
                        }
                    },
                    icon = { Text("👤") },
                    label = { Text("博主") }
                )
                NavigationBarItem(
                    selected = currentRoute == Screen.DAILY.route,
                    onClick = {
                        if (currentRoute != Screen.DAILY.route) {
                            navController.navigate(Screen.DAILY.route) {
                                popUpTo(Screen.FEED.route)
                            }
                        }
                    },
                    icon = { Text("📅") },
                    label = { Text("日历") }
                )
            }
        }
    }
}
