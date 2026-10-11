plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.compose.compiler)
  alias(libs.plugins.kotlin.serialization)
  alias(libs.plugins.google.services) apply false
  alias(libs.plugins.ksp)
}


// Apply private Firebase resource generation only when owner-provided config is present.
if (file("google-services.json").isFile) {
    apply(plugin = "com.google.gms.google-services")
}

fun secretValue(name: String): String? =
    providers.gradleProperty(name).orElse(providers.environmentVariable(name)).orNull?.takeIf { it.isNotBlank() }

fun secretFile(name: String): java.io.File? =
    secretValue(name)?.let { file(it) }

fun configValue(name: String, defaultValue: String): String =
    secretValue(name) ?: defaultValue

android {
    namespace = "com.amretra.megasmoves"
    compileSdk = 36
    defaultConfig {
        applicationId = "com.amretra.megasmoves"
        minSdk = 24
        targetSdk = 36
        // Prefer VERSION_CODE/VERSION_NAME from VPS publish (auto-bumped). Floor keeps sideload/Play monotonic.
        versionCode = System.getenv("VERSION_CODE")?.toIntOrNull() ?: 546
        versionName = System.getenv("VERSION_NAME") ?: "1.0.546"
    }

    signingConfigs {
        create("megasTesting") {
            val keystoreFile = secretFile("MEGAS_MOVES_TESTING_KEYSTORE") ?: file("keystores/megas-moves-testing.keystore")
            storeFile = keystoreFile
            storePassword = secretValue("MEGAS_MOVES_TESTING_STORE_PASSWORD") ?: ""
            keyAlias = secretValue("MEGAS_MOVES_TESTING_KEY_ALIAS") ?: "megas-moves-testing"
            keyPassword = secretValue("MEGAS_MOVES_TESTING_KEY_PASSWORD") ?: ""
        }
        create("megasUpload") {
            val uploadKeystore = secretFile("MEGAS_MOVES_UPLOAD_KEYSTORE") ?: file("keystores/megas-moves-upload.keystore")
            val storePass = secretValue("MEGAS_MOVES_UPLOAD_STORE_PASSWORD")
            if (uploadKeystore.exists() && !storePass.isNullOrBlank()) {
                storeFile = uploadKeystore
                storePassword = storePass
                keyAlias = secretValue("MEGAS_MOVES_UPLOAD_KEY_ALIAS") ?: "megas-moves-upload"
                keyPassword = secretValue("MEGAS_MOVES_UPLOAD_KEY_PASSWORD") ?: storePass
            } else {
                val debugConfig = getByName("debug")
                storeFile = debugConfig.storeFile
                storePassword = debugConfig.storePassword
                keyAlias = debugConfig.keyAlias
                keyPassword = debugConfig.keyPassword
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            buildConfigField("String", "BASE_URL", "\"${configValue("MEGAS_MOVES_RELEASE_BASE_URL", "https://megasmoves.io")}\"")
            signingConfig = signingConfigs.getByName("megasUpload")
        }
        create("previewDebug") {
            initWith(getByName("debug"))
            signingConfig = signingConfigs.getByName("megasTesting")
            buildConfigField("String", "BASE_URL", "\"${configValue("MEGAS_MOVES_TESTING_BASE_URL", "https://megasmoves.io")}\"")
            matchingFallbacks.add("debug")
        }
        debug {
            signingConfig = signingConfigs.getByName("debug")
            buildConfigField("String", "BASE_URL", "\"https://megasmoves.io\"")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
        isCoreLibraryDesugaringEnabled = true
    }

    buildFeatures {
        compose = true
        aidl = false
        buildConfig = true
        shaders = false
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }

    lint {
        disable += "InvalidFragmentVersionForActivityResult"
    }

    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }
}

androidComponents {
    onVariants { variant ->
        variant.outputs.forEach { output ->
            val buildType = variant.buildType ?: "debug"
            // Keep artifact naming aligned with defaultConfig when release env is absent.
            val version = System.getenv("VERSION_NAME")?.takeIf { it.isNotBlank() } ?: "1.0.546"
            val fileName = when (buildType) {
                "previewDebug" -> "megas-moves-preview-debug-v$version.apk"
                else -> "megas-moves-${buildType}-v$version.apk"
            }
            output.outputFileName.set(fileName)
        }
    }
}

kotlin {
    jvmToolchain(21)
}

dependencies {
    coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.5")

    val composeBom = platform(libs.androidx.compose.bom)
    implementation(composeBom)
    androidTestImplementation(composeBom)

    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.fragment.ktx)
    implementation(libs.androidx.biometric)

    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)

    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    debugImplementation(libs.androidx.compose.ui.tooling)
    androidTestImplementation(libs.androidx.compose.ui.test.junit4)
    debugImplementation(libs.androidx.compose.ui.test.manifest)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver)
    testImplementation(libs.org.json)

    androidTestImplementation(libs.androidx.test.core)
    androidTestImplementation(libs.androidx.test.ext.junit)
    androidTestImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.androidx.test.espresso.core)

    implementation(libs.androidx.navigation3.ui)
    implementation(libs.androidx.navigation3.runtime)
    implementation(libs.androidx.lifecycle.viewmodel.navigation3)

    implementation(libs.play.billing)

    implementation(libs.androidx.compose.icons.extended)

    implementation(libs.androidx.room.runtime)
    implementation(libs.androidx.room.ktx)
    ksp(libs.androidx.room.compiler)

    implementation(libs.androidx.datastore.preferences)
    implementation(libs.androidx.work.runtime.ktx)
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")

    implementation(libs.okhttp)
    implementation(libs.okhttp.logging)

    implementation(platform(libs.firebase.bom))
    implementation(libs.firebase.auth)
    implementation(libs.firebase.messaging)
    implementation(libs.play.integrity)
    implementation(libs.play.app.update)
    implementation(libs.play.app.update.ktx)
    implementation(libs.androidx.credentials)
    implementation(libs.androidx.credentials.play.services)
    implementation(libs.googleid)
}