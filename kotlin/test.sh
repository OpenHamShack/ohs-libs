#!/bin/sh
# Builds and tests the Kotlin libraries with the toolchain on shack
# (~/android-toolchain: JDK 17 and Gradle 9.8.1; this repository carries no
# Gradle wrapper, because the wrapper would download a Gradle the build
# hosts already have). Usage: kotlin/test.sh [gradle args...]
set -eu
TC=${SHACK_ANDROID_TOOLCHAIN:-$HOME/android-toolchain}
export JAVA_HOME="$TC/jdk17"
export PATH="$JAVA_HOME/bin:$PATH"
cd "$(dirname "$0")"
exec "$TC/gradle-9.8.1/bin/gradle" --no-daemon "${@:-test}"
