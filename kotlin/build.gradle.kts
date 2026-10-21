// Kotlin 2.4.21 because OpenHamShack Remote's Android build is on it
// (openhamshack-remote android/gradle/libs.versions.toml): a library compiled
// by a newer compiler than its consumer cannot be read, and an older one
// gains nothing. JVM target 17 matches the toolchain on shack and what
// Android's D8 accepts.
plugins {
    kotlin("jvm") version "2.4.21" apply false
}

val shackVersion = rootDir.resolve("../VERSION").readText().trim()

subprojects {
    group = "com.openhamshack"
    version = shackVersion

    apply(plugin = "org.jetbrains.kotlin.jvm")

    extensions.configure<org.jetbrains.kotlin.gradle.dsl.KotlinJvmProjectExtension> {
        jvmToolchain(17)
    }

    dependencies {
        "testImplementation"("org.junit.jupiter:junit-jupiter:5.11.4")
        "testRuntimeOnly"("org.junit.platform:junit-platform-launcher:1.11.4")
    }

    tasks.withType<Test>().configureEach {
        useJUnitPlatform()
        // The vectors are read from the repository's spec/ directory, never
        // copied: they are the contract between the Python, Kotlin and
        // Swift libraries, and a copy would let one drift.
        systemProperty("shack.spec", rootDir.resolve("../spec").canonicalPath)
        // Scratch databases (T16 writes 10,000 records) go under the
        // build directory, not /tmp, which on shack is a small tmpfs every
        // session shares.
        systemProperty("java.io.tmpdir", layout.buildDirectory.dir("tmp/tests").get().asFile.also { it.mkdirs() }.path)
        testLogging {
            events("failed")
            exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
        }
    }
}
