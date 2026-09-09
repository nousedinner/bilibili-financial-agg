package com.finapp.appb.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.pulltorefresh.rememberPullToRefreshState
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.finapp.appb.data.repository.stableId
import com.finapp.appb.ui.BiliLink
import com.finapp.appb.ui.components.SkeletonCard
import com.finapp.appb.ui.components.VideoCard
import com.finapp.appb.ui.friendlyErrorMessage
import com.finapp.appb.viewmodel.FeedViewModel
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FeedScreen(
    viewModel: FeedViewModel,
    onVideoClick: (String) -> Unit,
    onSettingsClick: () -> Unit
) {
    val items by viewModel.items.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val isRefreshing by viewModel.isRefreshing.collectAsState()
    val error by viewModel.error.collectAsState()
    val bloggerNames by viewModel.bloggerNames.collectAsState()
    val authError by viewModel.authError.collectAsState()
    val listState = rememberLazyListState()

    // Load more when scrolling to bottom
    val shouldLoadMore by remember {
        derivedStateOf {
            val lastVisibleItem = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
            lastVisibleItem >= items.size - 5 && !isLoading
        }
    }

    LaunchedEffect(shouldLoadMore) {
        if (shouldLoadMore && items.isNotEmpty()) {
            viewModel.loadMore()
        }
    }

    if (authError) {
        AlertDialog(
            onDismissRequest = { viewModel.dismissAuthError() },
            title = { Text("认证失败") },
            text = { Text("密码已失效，请重新配置 API 密码") },
            confirmButton = {
                TextButton(onClick = { viewModel.dismissAuthError(); onSettingsClick() }) {
                    Text("去设置")
                }
            },
            dismissButton = {
                TextButton(onClick = { viewModel.dismissAuthError() }) {
                    Text("取消")
                }
            }
        )
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("消息聚合", fontWeight = FontWeight.Bold) },
                actions = {
                    IconButton(onClick = onSettingsClick) {
                        Icon(Icons.Default.Settings, "设置")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background
                )
            )
        }
    ) { padding ->
        val refreshState = rememberPullToRefreshState()
        PullToRefreshBox(
            isRefreshing = isRefreshing,
            onRefresh = { viewModel.refresh() },
            state = refreshState,
            modifier = Modifier.fillMaxSize().padding(padding)
        ) {
            when {
                isLoading && items.isEmpty() -> LazyColumn(
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = PaddingValues(vertical = 8.dp)
                ) {
                    items(5) { SkeletonCard() }
                }
                error != null && items.isEmpty() -> Box(
                    modifier = Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("😢", fontSize = 48.sp)
                        Spacer(modifier = Modifier.height(12.dp))
                        Text(friendlyErrorMessage(error), color = MaterialTheme.colorScheme.error)
                        Spacer(modifier = Modifier.height(16.dp))
                        Button(onClick = { viewModel.loadFeed() }) {
                            Text("重试")
                        }
                    }
                }
                items.isEmpty() -> Box(
                    modifier = Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("📭", fontSize = 48.sp)
                        Spacer(modifier = Modifier.height(12.dp))
                        Text("暂无内容", fontSize = 16.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
                else -> LazyColumn(
                    state = listState,
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = PaddingValues(vertical = 8.dp)
                ) {
                    if (error != null) {
                        item(key = "feed-error") {
                            Column(Modifier.padding(16.dp)) {
                                Text(error!!, color = MaterialTheme.colorScheme.error)
                                TextButton(onClick = { viewModel.retry() }, enabled = !isLoading) { Text("重试") }
                            }
                        }
                    }
                    items(
                        items = items,
                        key = { it.stableId() ?: "unknown:${it.publishTime}" }
                    ) { item ->
                        val context = androidx.compose.ui.platform.LocalContext.current
                        VideoCard(
                            item = item,
                            bloggerNames = bloggerNames,
                            sentimentScore = item.sentimentScore ?: 0.0,
                            onClick = {
                                if (item.type == "video" && item.bvid != null) {
                                    onVideoClick(item.bvid)
                                } else if (item.mid != null) {
                                    BiliLink.openSpace(context, item.mid)
                                }
                            }
                        )
                    }

                    // Loading indicator at bottom
                    if (isLoading && items.isNotEmpty()) {
                        item {
                            Box(
                                modifier = Modifier.fillMaxWidth().padding(16.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                CircularProgressIndicator(modifier = Modifier.size(24.dp))
                            }
                        }
                    }
                }
            }
        }
    }
}
