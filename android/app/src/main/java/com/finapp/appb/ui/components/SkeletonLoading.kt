package com.finapp.appb.ui.components

import androidx.compose.animation.core.*
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

/** Skeleton shimmer 基色，跟随主题 surfaceVariant */
private fun shimmerBrush(): Brush {
    val base = Color(0xFFE0E0E0)
    val highlight = Color(0xFFF5F5F5)
    return Brush.linearGradient(
        colors = listOf(base, highlight, base),
        start = Offset.Zero,
        end = Offset.Infinite
    )
}

@Composable
fun SkeletonCard(modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition()
    val translateAnim = transition.animateFloat(
        initialValue = 0f, targetValue = 1000f,
        animationSpec = infiniteRepeatable(tween(1200, easing = LinearEasing))
    )
    val brush = Brush.linearGradient(
        colors = listOf(Color(0xFFE0E0E0), Color(0xFFF5F5F5), Color(0xFFE0E0E0)),
        start = Offset(translateAnim.value - 200f, 0f),
        end = Offset(translateAnim.value, 0f)
    )

    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 8.dp)
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Box(modifier = Modifier.size(14.dp).clip(RoundedCornerShape(7.dp)).background(brush))
            Box(modifier = Modifier.height(14.dp).width(80.dp).clip(RoundedCornerShape(4.dp)).background(brush))
        }
        Box(modifier = Modifier.height(18.dp).fillMaxWidth(0.85f).clip(RoundedCornerShape(4.dp)).background(brush))
        Box(modifier = Modifier.height(12.dp).fillMaxWidth().clip(RoundedCornerShape(4.dp)).background(brush))
        Box(modifier = Modifier.height(12.dp).fillMaxWidth(0.6f).clip(RoundedCornerShape(4.dp)).background(brush))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Box(modifier = Modifier.height(22.dp).width(56.dp).clip(RoundedCornerShape(11.dp)).background(brush))
            Box(modifier = Modifier.height(22.dp).width(56.dp).clip(RoundedCornerShape(11.dp)).background(brush))
        }
    }
}

@Composable
fun SkeletonDetail(modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition()
    val translateAnim = transition.animateFloat(
        initialValue = 0f, targetValue = 1000f,
        animationSpec = infiniteRepeatable(tween(1200, easing = LinearEasing))
    )
    val brush = Brush.linearGradient(
        colors = listOf(Color(0xFFE0E0E0), Color(0xFFF5F5F5), Color(0xFFE0E0E0)),
        start = Offset(translateAnim.value - 200f, 0f),
        end = Offset(translateAnim.value, 0f)
    )

    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Box(modifier = Modifier.height(24.dp).fillMaxWidth(0.7f).clip(RoundedCornerShape(4.dp)).background(brush))
        Box(modifier = Modifier.height(14.dp).fillMaxWidth(0.5f).clip(RoundedCornerShape(4.dp)).background(brush))
        Spacer(modifier = Modifier.height(8.dp))
        repeat(3) {
            Box(modifier = Modifier.height(14.dp).fillMaxWidth().clip(RoundedCornerShape(4.dp)).background(brush))
        }
        Spacer(modifier = Modifier.height(8.dp))
        Box(modifier = Modifier.height(120.dp).fillMaxWidth().clip(RoundedCornerShape(8.dp)).background(brush))
    }
}
