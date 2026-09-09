package com.finapp.appb.ui.screens

import android.widget.Toast
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.pulltorefresh.rememberPullToRefreshState
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.finapp.appb.data.api.Blogger
import com.finapp.appb.data.api.SyncFollowing
import com.finapp.appb.ui.BiliLink
import com.finapp.appb.ui.friendlyErrorMessage
import com.finapp.appb.ui.theme.*
import com.finapp.appb.viewmodel.BloggerViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun BloggerScreen(viewModel: BloggerViewModel) {
    val context = LocalContext.current
    val bloggers by viewModel.bloggers.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val error by viewModel.error.collectAsState()
    val toast by viewModel.toast.collectAsState()
    val newFollowings by viewModel.newFollowings.collectAsState()
    val selectedMids by viewModel.selectedMids.collectAsState()
    val syncLoading by viewModel.syncLoading.collectAsState()
    var showAddDialog by remember { mutableStateOf(false) }
    var showSyncSheet by remember { mutableStateOf(false) }

    // Show sync sheet when new followings arrive
    LaunchedEffect(newFollowings) {
        if (newFollowings.isNotEmpty()) {
            showSyncSheet = true
        }
    }

    LaunchedEffect(toast) {
        toast?.let {
            Toast.makeText(context, it, Toast.LENGTH_SHORT).show()
            viewModel.clearToast()
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("博主管理", fontWeight = FontWeight.Bold) },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.background),
                actions = {
                    IconButton(onClick = { showAddDialog = true }) {
                        Icon(Icons.Default.Add, "添加博主")
                    }
                    IconButton(onClick = { viewModel.syncBloggers() }, enabled = !syncLoading) {
                        if (syncLoading) {
                            CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
                        } else {
                            Icon(Icons.Default.Refresh, "同步关注")
                        }
                    }
                }
            )
        }
    ) { padding ->
        val refreshState = rememberPullToRefreshState()
        PullToRefreshBox(
            isRefreshing = isLoading,
            onRefresh = { viewModel.refreshBloggers() },
            state = refreshState,
            modifier = Modifier.fillMaxSize().padding(padding)
        ) {
            // Main blogger list - always scrollable
            when {
                isLoading && bloggers.isEmpty() -> Box(
                    modifier = Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center
                ) { CircularProgressIndicator() }
                error != null && bloggers.isEmpty() -> Box(
                    modifier = Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(friendlyErrorMessage(error), color = MaterialTheme.colorScheme.error)
                        Spacer(modifier = Modifier.height(12.dp))
                        Button(onClick = { viewModel.loadBloggers() }) { Text("重试") }
                    }
                }
                bloggers.isEmpty() -> Box(
                    modifier = Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("👤", fontSize = 48.sp)
                        Spacer(modifier = Modifier.height(12.dp))
                        Text("暂无博主", fontSize = 16.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Spacer(modifier = Modifier.height(8.dp))
                        Text("点击右上角 + 添加或同步", fontSize = 13.sp, color = TextSecondary)
                    }
                }
                else -> LazyColumn(
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = PaddingValues(16.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    items(bloggers, key = { it.mid }) { blogger ->
                        BloggerCard(blogger = blogger, onDelete = { viewModel.deleteBlogger(blogger.mid) })
                    }
                }
            }
        }
    }

    // Sync Bottom Sheet
    if (showSyncSheet && newFollowings.isNotEmpty()) {
        SyncBottomSheet(
            followings = newFollowings,
            selectedMids = selectedMids,
            onToggle = { viewModel.toggleSelection(it) },
            onSelectAll = { viewModel.selectAll() },
            onClear = { viewModel.clearSelection() },
            onAdd = {
                viewModel.addSelected()
                showSyncSheet = false
            },
            onDismiss = {
                showSyncSheet = false
                viewModel.clearSync()
            }
        )
    }

    // Add blogger dialog
    if (showAddDialog) {
        AddBloggerDialog(
            onDismiss = { showAddDialog = false },
            onConfirm = { name ->
                viewModel.addBloggerByName(name)
                showAddDialog = false
            }
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SyncBottomSheet(
    followings: List<SyncFollowing>,
    selectedMids: Set<Long>,
    onToggle: (Long) -> Unit,
    onSelectAll: () -> Unit,
    onClear: () -> Unit,
    onAdd: () -> Unit,
    onDismiss: () -> Unit
) {
    val sheetState = rememberModalBottomSheetState()

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        shape = RoundedCornerShape(topStart = 20.dp, topEnd = 20.dp)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 20.dp)
                .padding(bottom = 32.dp)
        ) {
            // Header
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    "发现 ${followings.size} 个新关注",
                    fontWeight = FontWeight.Bold,
                    fontSize = 18.sp
                )
                IconButton(onClick = onDismiss, modifier = Modifier.size(32.dp)) {
                    Icon(Icons.Default.Close, "关闭", modifier = Modifier.size(20.dp))
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            // Select all / clear
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(onClick = onSelectAll) { Text("全选", fontSize = 13.sp) }
                    TextButton(onClick = onClear) { Text("清除", fontSize = 13.sp) }
                }
                Text(
                    "已选 ${selectedMids.size}/${followings.size}",
                    fontSize = 13.sp,
                    color = TextSecondary
                )
            }

            Spacer(modifier = Modifier.height(8.dp))

            // Followings list
            LazyColumn(
                modifier = Modifier.heightIn(max = 350.dp),
                verticalArrangement = Arrangement.spacedBy(2.dp)
            ) {
                items(followings, key = { it.mid }) { f ->
                    val isSelected = selectedMids.contains(f.mid)
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clickable { onToggle(f.mid) }
                            .padding(vertical = 8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Checkbox(
                            checked = isSelected,
                            onCheckedChange = { onToggle(f.mid) }
                        )
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                f.name,
                                fontSize = 15.sp,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis
                            )
                            if (!f.sign.isNullOrBlank()) {
                                Text(
                                    f.sign,
                                    fontSize = 12.sp,
                                    color = TextSecondary,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis
                                )
                            }
                        }
                    }
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            // Add button
            Button(
                onClick = onAdd,
                modifier = Modifier.fillMaxWidth().height(50.dp),
                shape = RoundedCornerShape(12.dp),
                enabled = selectedMids.isNotEmpty()
            ) {
                Text("添加选中的 ${selectedMids.size} 个博主", fontSize = 15.sp)
            }
        }
    }
}

@Composable
private fun AddBloggerDialog(onDismiss: () -> Unit, onConfirm: (String) -> Unit) {
    var name by remember { mutableStateOf("") }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("添加博主") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("输入B站用户名，系统自动搜索并添加", fontSize = 13.sp, color = TextSecondary)
                OutlinedTextField(
                    value = name,
                    onValueChange = { name = it },
                    label = { Text("B站用户名") },
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = true,
                    shape = RoundedCornerShape(10.dp),
                    leadingIcon = { Icon(Icons.Default.Search, "搜索") }
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { if (name.isNotBlank()) onConfirm(name) },
                enabled = name.isNotBlank()
            ) { Text("搜索并添加") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("取消") }
        }
    )
}

@Composable
private fun BloggerCard(blogger: Blogger, onDelete: () -> Unit) {
    val context = LocalContext.current
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { BiliLink.openSpace(context, blogger.mid) },
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        Row(
            modifier = Modifier.padding(16.dp).fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(blogger.name, fontSize = 16.sp, fontWeight = FontWeight.SemiBold, color = Primary)
                Text("ID: ${blogger.mid}", fontSize = 12.sp, color = TextSecondary)
                if (!blogger.tags.isNullOrEmpty()) {
                    Spacer(modifier = Modifier.height(4.dp))
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(4.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        blogger.tags.orEmpty().take(3).forEach { tag ->
                            AssistChip(
                                onClick = {},
                                label = { Text(tag, fontSize = 11.sp) },
                                shape = RoundedCornerShape(6.dp)
                            )
                        }
                    }
                }
            }
            IconButton(onClick = onDelete) {
                Icon(Icons.Default.Close, "删除", tint = MaterialTheme.colorScheme.error)
            }
        }
    }
}
