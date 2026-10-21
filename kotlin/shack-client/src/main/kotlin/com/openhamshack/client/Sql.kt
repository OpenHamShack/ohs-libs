package com.openhamshack.client

import java.io.Closeable
import java.sql.Connection
import java.sql.DriverManager

/**
 * The few SQLite operations the store needs, as an interface, so the same
 * store runs on Android's own SQLite (`android.database.sqlite`, in the app:
 * no bundled native library, and the system's WAL and locking) and on the
 * JVM over sqlite-jdbc for the vector tests.
 *
 * Values bound and returned are `String`, `Long`, `Double`, `ByteArray` and
 * `null` (an Android `Cursor` gives exactly those). An Android adapter is a
 * page of code over `SQLiteDatabase.rawQuery` / `execSQL`, with
 * [immediate] as `beginTransactionNonExclusive()` (BEGIN IMMEDIATE) +
 * `setTransactionSuccessful()` + `endTransaction()`, and [backupTo] as a
 * `PRAGMA wal_checkpoint(TRUNCATE)` followed by a file copy (Android 9's
 * SQLite 3.22 predates VACUUM INTO, and the platform has no backup API).
 */
interface SqlConnection : Closeable {
    /** Run one statement that returns no rows. */
    fun exec(sql: String, vararg args: Any?)

    /** Run a query; every row as an array of column values. */
    fun query(sql: String, vararg args: Any?): List<Array<Any?>>

    /** Run [block] inside BEGIN IMMEDIATE ... COMMIT, rolling back if it
     *  throws. IMMEDIATE because every write transaction here also advances
     *  the HLC kept in `meta`: two processes taking the write lock at the
     *  start never mint the same stamp (SYNC.md §2.2, T18). */
    fun <T> immediate(block: () -> T): T

    /** A consistent copy of the whole database at [path] (the archive of
     *  "use the controller's log": SYNC.md §7). */
    fun backupTo(path: String)
}

/** Opens a connection to the store file; the store calls it once. */
fun interface SqlOpener {
    fun open(path: String): SqlConnection
}

/**
 * The JVM driver, over any JDBC SQLite driver on the classpath (the tests
 * use org.xerial:sqlite-jdbc). Only java.sql is used, so this file compiles
 * without the driver and costs an Android app nothing.
 */
class JdbcSqlite(path: String) : SqlConnection {
    private val conn: Connection = DriverManager.getConnection("jdbc:sqlite:$path")

    init {
        conn.autoCommit = true
        // busy_timeout before WAL: switching the journal mode needs the lock
        // another connection may be holding.
        exec("PRAGMA busy_timeout=5000")
        query("PRAGMA journal_mode=WAL")
    }

    private fun bind(st: java.sql.PreparedStatement, args: Array<out Any?>) {
        args.forEachIndexed { n, v ->
            when (v) {
                null -> st.setNull(n + 1, java.sql.Types.NULL)
                is String -> st.setString(n + 1, v)
                is Int -> st.setLong(n + 1, v.toLong())
                is Long -> st.setLong(n + 1, v)
                is Double -> st.setDouble(n + 1, v)
                is Boolean -> st.setLong(n + 1, if (v) 1 else 0)
                is ByteArray -> st.setBytes(n + 1, v)
                else -> st.setString(n + 1, v.toString())
            }
        }
    }

    override fun exec(sql: String, vararg args: Any?) {
        conn.prepareStatement(sql).use { st ->
            bind(st, args)
            st.execute()
        }
    }

    override fun query(sql: String, vararg args: Any?): List<Array<Any?>> {
        conn.prepareStatement(sql).use { st ->
            bind(st, args)
            val hasRows = st.execute()
            if (!hasRows) return emptyList()
            st.resultSet.use { rs ->
                val n = rs.metaData.columnCount
                val out = ArrayList<Array<Any?>>()
                while (rs.next()) {
                    out.add(Array(n) { c ->
                        when (val v = rs.getObject(c + 1)) {
                            is Int -> v.toLong()
                            is Float -> v.toDouble()
                            else -> v
                        }
                    })
                }
                return out
            }
        }
    }

    private var depth = 0

    override fun <T> immediate(block: () -> T): T {
        // Reentrant: a store method that runs inside another's transaction
        // joins it rather than failing on a nested BEGIN.
        if (depth > 0) {
            depth++
            try { return block() } finally { depth-- }
        }
        exec("BEGIN IMMEDIATE")
        depth = 1
        try {
            val v = block()
            exec("COMMIT")
            return v
        } catch (e: Throwable) {
            try { exec("ROLLBACK") } catch (_: Exception) {}
            throw e
        } finally {
            depth = 0
        }
    }

    override fun backupTo(path: String) {
        // sqlite-jdbc's own statement for sqlite3_backup.
        conn.createStatement().use { it.executeUpdate("backup to '" + path.replace("'", "''") + "'") }
    }

    /** Replace this database with the copy at [path] (the inverse of
     *  [backupTo]; the tests' controller restores a backup with it). */
    fun restoreFrom(path: String) {
        conn.createStatement().use { it.executeUpdate("restore from '" + path.replace("'", "''") + "'") }
    }

    override fun close() = conn.close()

    companion object {
        val OPENER = SqlOpener { JdbcSqlite(it) }
    }
}
