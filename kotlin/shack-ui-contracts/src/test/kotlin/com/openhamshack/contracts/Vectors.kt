package com.openhamshack.contracts

import com.openhamshack.client.Json
import java.io.File

/** Reads spec/ from the repository, never from a copy (see the root
 *  build file): the vectors are the contract between the languages. */
object Vectors {
    val spec = File(System.getProperty("shack.spec"))

    fun file(path: String) = File(spec, "vectors/$path")

    @Suppress("UNCHECKED_CAST")
    fun cases(path: String): List<Map<String, Any?>> =
        Json.parseObject(file(path).readText())["cases"] as List<Map<String, Any?>>

    @Suppress("UNCHECKED_CAST")
    fun head(path: String): Map<String, Any?> = Json.parseObject(file(path).readText())
}

@Suppress("UNCHECKED_CAST")
fun Any?.map() = this as Map<String, Any?>

@Suppress("UNCHECKED_CAST")
fun Any?.list() = this as List<Any?>

fun Any?.dbl() = (this as Number).toDouble()
