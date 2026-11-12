plugins {
    id("com.android.library")
}

android {
    namespace = "com.amretra.megasmoves.androidmcp"
    compileSdk = 36
    defaultConfig {
        minSdk = 24
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }
}

kotlin { jvmToolchain(21) }

androidComponents {
    onVariants { variant ->
        variant.androidTest?.sources?.kotlin?.addStaticSourceDirectory("src/test/java")
    }
}

dependencies {
    implementation(libs.okhttp)
    androidTestImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.androidx.test.ext.junit)
    androidTestImplementation(libs.okhttp.mockwebserver)
    androidTestImplementation("com.squareup.okhttp3:okhttp-tls:4.12.0")
    testImplementation(libs.junit)
    testImplementation(libs.org.json)
    testImplementation(libs.okhttp.mockwebserver)
    testImplementation("com.squareup.okhttp3:okhttp-tls:4.12.0")
}
