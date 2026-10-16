import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
    id("com.google.devtools.ksp")
}

val pushProperties = Properties().apply {
    val source = file("firebase.properties")
    if (source.exists()) source.inputStream().use { load(it) }
}
fun pushValue(key: String) = "\"" + pushProperties.getProperty(key, "").replace("\\", "\\\\").replace("\"", "\\\"") + "\""
val authorizationPoc = providers.gradleProperty("authorizationPoc").getOrElse("false") == "true"

android {
    namespace = "com.finapp.appb"
    compileSdk = 35
    buildToolsVersion = "34.0.0"

    defaultConfig {
        applicationId = if (authorizationPoc) "com.finapp.appb.poc" else "com.finapp.appb"
        manifestPlaceholders["appLabel"] = if (authorizationPoc) "财经聚合验证" else "财经聚合"
        minSdk = 26
        targetSdk = 34
        versionCode = 5
        versionName = "1.1.3"
        buildConfigField("String", "FIREBASE_APP_ID", pushValue("appId"))
        buildConfigField("String", "FIREBASE_API_KEY", pushValue("apiKey"))
        buildConfigField("String", "FIREBASE_PROJECT_ID", pushValue("projectId"))
        buildConfigField("String", "FIREBASE_SENDER_ID", pushValue("senderId"))
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }
}

dependencies {
    implementation("com.google.firebase:firebase-messaging:25.1.3")
    implementation("androidx.work:work-runtime-ktx:2.10.1")
    implementation("com.google.zxing:core:3.5.3")

    // Compose BOM
    val composeBom = platform("androidx.compose:compose-bom:2024.12.01")
    implementation(composeBom)
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")

    // Core
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.activity:activity-compose:1.9.3")

    // Navigation
    implementation("androidx.navigation:navigation-compose:2.8.5")

    // ViewModel
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")

    // Network
    implementation("com.squareup.retrofit2:retrofit:2.11.0")
    implementation("com.squareup.retrofit2:converter-gson:2.11.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")

    // Serialization (for JSON parsing)
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    implementation("com.google.code.gson:gson:2.11.0")

    // DataStore
    implementation("androidx.datastore:datastore-preferences:1.1.1")

    // Room Database
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")

    // Coil for image loading
    implementation("io.coil-kt:coil-compose:2.7.0")

    // Accompanist (swipe refresh)
    implementation("com.google.accompanist:accompanist-swiperefresh:0.36.0")
}
