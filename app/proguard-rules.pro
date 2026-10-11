# JNI resolves these methods by their declared Java names.
-keepclasseswithmembernames class com.amretra.megasmoves.core.audio.NativeAudioProcessor {
    native <methods>;
}

# Preserve generic signatures and annotations consumed by Firebase and AndroidX.
-keepattributes Signature,*Annotation*
