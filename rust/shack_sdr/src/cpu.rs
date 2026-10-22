//! The CPU time of the calling thread, for the host's budget
//! (RADIO-SDR-PLAN §3.4): CLOCK_THREAD_CPUTIME_ID on Linux and macOS,
//! GetThreadTimes on Windows. Wall time would charge a decoder for the
//! moments the scheduler gave its core to something else, which on a busy
//! N150 is most of them.

use std::time::Duration;

#[cfg(unix)]
pub fn thread_time() -> Duration {
    let mut ts = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    // SAFETY: a valid out-pointer to a timespec; the clock id is a constant
    // both Linux and macOS define.
    let r = unsafe { libc::clock_gettime(libc::CLOCK_THREAD_CPUTIME_ID, &mut ts) };
    if r != 0 {
        return Duration::ZERO;
    }
    Duration::new(ts.tv_sec as u64, ts.tv_nsec as u32)
}

#[cfg(windows)]
pub fn thread_time() -> Duration {
    #[repr(C)]
    #[derive(Default, Clone, Copy)]
    struct FileTime {
        lo: u32,
        hi: u32,
    }
    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentThread() -> isize;
        fn GetThreadTimes(h: isize, c: *mut FileTime, e: *mut FileTime, k: *mut FileTime, u: *mut FileTime) -> i32;
    }
    let (mut c, mut e, mut k, mut u) = (FileTime::default(), FileTime::default(), FileTime::default(), FileTime::default());
    // SAFETY: GetCurrentThread's pseudo-handle needs no closing; four valid
    // out-pointers.
    let ok = unsafe { GetThreadTimes(GetCurrentThread(), &mut c, &mut e, &mut k, &mut u) };
    if ok == 0 {
        return Duration::ZERO;
    }
    let t100 = |f: FileTime| ((f.hi as u64) << 32) | f.lo as u64;
    Duration::from_nanos((t100(k) + t100(u)) * 100)
}

#[cfg(not(any(unix, windows)))]
pub fn thread_time() -> Duration {
    Duration::ZERO
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_work_not_sleep() {
        let t0 = thread_time();
        std::thread::sleep(Duration::from_millis(50));
        let slept = thread_time() - t0;
        let t1 = thread_time();
        let mut x = 0u64;
        let start = std::time::Instant::now();
        while start.elapsed() < Duration::from_millis(50) {
            x = x.wrapping_mul(6364136223846793005).wrapping_add(1);
        }
        std::hint::black_box(x);
        let worked = thread_time() - t1;
        assert!(slept < Duration::from_millis(10), "{slept:?}");
        assert!(worked > Duration::from_millis(30), "{worked:?}");
    }
}
