// ohs-libs Kotlin: the client side OpenHamShack Remote needs (SYNC.md §9.2,
// I18N.md §4). Plain Kotlin/JVM modules with no Android dependency, so the
// shared vectors in ../spec run on any JDK and an Android app takes the jars
// as they are; the Android-specific pieces (android.database.sqlite, the
// Keystore) plug in through interfaces (SqlOpener, PairingVault) in the app.
rootProject.name = "ohs-libs-kotlin"

pluginManagement {
    repositories {
        gradlePluginPortal()
        mavenCentral()
    }
}

dependencyResolutionManagement {
    repositories {
        mavenCentral()
    }
}

include("shack-client", "shack-i18n", "shack-ui-contracts")
