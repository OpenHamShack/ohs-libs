//! The decoder host (RADIO-SDR-PLAN §3.4-3.5): decoder sessions run on
//! worker threads of their own, fed by the engine's receive thread through
//! bounded rings, with results collected on a queue the engine polls.
//!
//! - **`feed` never blocks on decoding.** It copies the samples into the
//!   session's ring (2 s) under a lock held only for the copy, and returns.
//!   A full ring drops its oldest block and counts it, so a slow decoder
//!   can never stall receive: the engine's own rule for feeds.
//! - **No calls into Python from these threads.** Results go on a queue and
//!   the engine's supervisor polls it (10 Hz). Calling Python from a foreign
//!   thread is what shack_packet's librtlsdr binding avoided as fragile at
//!   shutdown on every platform.
//! - **One session on one thread at a time.** More workers decode more
//!   sessions at once; a session's state is never shared between threads.
//! - **A panic is one session's error**, caught around each block
//!   (`catch_unwind`): that session goes to `error` and nothing else does.
//!   The one thing a thread cannot contain is a crash inside a dlopen'd
//!   codec, which is why each new codec file is first probed in a child
//!   process (codecs.rs).
//! - **The CPU budget.** Each worker measures its own thread's CPU time per
//!   block, charged to the session. Over `cpu_max` (a fraction of one core)
//!   in the last two seconds, the newest session that is not pinned (the
//!   mode decoder is) is paused, state `budget`, and resumed, oldest first,
//!   when the others leave room for what it used. This copies NB's
//!   `skimBudget` and Radio's `cwSkimCpu` idea.
//! - **`close()` joins within a second.** Workers check for it between
//!   blocks, and a block is at most a quarter second of samples.
//! - **Sound has a ring of its own.** A decoder's `Output::Audio` never
//!   joins the results queue: 48 kHz stereo would crowd out every text
//!   result, and a client that polls slowly would lose both. It goes to the
//!   session's audio ring (`ring_s` seconds, the oldest dropped and counted),
//!   which the app pulls with `audio()` at its own pace, converted to its
//!   own rate and channels if it asks: Radio takes 12 kHz mono for its
//!   ordinary audio stream. The same ring serves every decoder that makes
//!   sound (DAB now; WFM, FreeDV and digital voice later).
//! - **`configure()` is live.** New parameters are checked against the
//!   decoder's catalogue entry at once (a refusal is the caller's), merged
//!   into the session's, and handed to the decoder by its worker before its
//!   next block, so the engine never waits for a block being decoded. DAB's
//!   `service` changes this way without losing the ensemble.
//! - **Files a decoder makes are the host's to keep.** A decoder hands over
//!   a finished picture's bytes (`ImageEvent::Done`) and never touches the
//!   filesystem; the host names it after the session, keeps the last
//!   `files_keep` in memory and, given `files_dir`, writes each there
//!   (whole or not at all: a temporary name, then a rename), so the app's
//!   `api/decoders/files?id=` is `file(id)` whichever it is. The result
//!   says the name and the path, without the bytes, and a `meta` from the
//!   same block whose `file` named it names it as the host keeps it.

use crate::catalogue::{self, Kind};
use crate::decoder::{
    check_params, DecodeError, Decoder, Fields, ImageEvent, Input, InputKind, Output, Params, Sink, Status, Value,
};
use crate::dsp::{AudioToIq, Resampler};
use rustfft::num_complex::Complex32;
use std::collections::{BTreeMap, VecDeque};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// The budget's window.
const WINDOW: Duration = Duration::from_secs(2);
/// A paused session is not resumed sooner than this after the last change.
const SETTLE: Duration = Duration::from_millis(1500);
/// The most a worker hands a decoder at once, in seconds of samples.
const CHUNK_S: f64 = 0.25;

#[derive(Clone, Debug)]
pub struct HostConfig {
    /// Worker threads, 1-4 (`decodeThreads`).
    pub threads: usize,
    /// Fraction of one core all sessions may use; 0 = no cap (`decodeCpuMax`).
    pub cpu_max: f64,
    /// Seconds of samples a session's ring holds.
    pub ring_s: f64,
    /// Sessions at most, mode decoder included.
    pub max_sessions: usize,
    /// Results held for `poll` at most; the oldest go first.
    pub max_results: usize,
    /// Offer the test decoders (testing module).
    pub testing: bool,
    /// Where decoders' files (SSTV pictures) are written; None keeps them
    /// in memory only.
    pub files_dir: Option<PathBuf>,
    /// Files kept in memory, newest; the oldest go first.
    pub files_keep: usize,
}

impl Default for HostConfig {
    fn default() -> Self {
        HostConfig {
            threads: 1,
            cpu_max: 0.25,
            ring_s: 2.0,
            max_sessions: 17,
            max_results: 20_000,
            testing: false,
            files_dir: None,
            files_keep: 16,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Starting,
    Running,
    NoSignal,
    Synced,
    Outside,
    Budget,
    Error,
    Stopped,
}

impl State {
    pub fn code(self) -> &'static str {
        match self {
            State::Starting => "starting",
            State::Running => "running",
            State::NoSignal => "nosignal",
            State::Synced => "synced",
            State::Outside => "outside",
            State::Budget => "budget",
            State::Error => "error",
            State::Stopped => "stopped",
        }
    }
    /// Being fed and decoded.
    fn active(self) -> bool {
        matches!(self, State::Starting | State::Running | State::NoSignal | State::Synced)
    }
}

/// Why a host call was refused: codes the app words.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HostError {
    UnknownDecoder,
    UnknownSession,
    BadParams(&'static str),
    BadInput(&'static str),
    InputUnsupported,
    TooManySessions,
    Closed,
}

impl HostError {
    pub fn code(&self) -> &'static str {
        match self {
            HostError::UnknownDecoder => "unknown_decoder",
            HostError::UnknownSession => "unknown_session",
            HostError::BadParams(_) => "bad_params",
            HostError::BadInput(_) => "bad_input",
            HostError::InputUnsupported => "input_unsupported",
            HostError::TooManySessions => "too_many_sessions",
            HostError::Closed => "closed",
        }
    }
    /// The finer reason, where there is one.
    pub fn detail(&self) -> Option<&'static str> {
        match self {
            HostError::BadParams(c) | HostError::BadInput(c) => Some(c),
            _ => None,
        }
    }
}

/// A result, as `poll` hands it out.
#[derive(Clone, Debug)]
pub struct Item {
    pub session: String,
    pub rev: u64,
    pub t: f64,
    pub body: Body,
}

#[derive(Clone, Debug)]
pub enum Body {
    Output(Output),
    State { state: State, why: Option<String> },
}

/// A run of sound pulled from a session, all at one rate and channel count.
#[derive(Clone, Debug, PartialEq)]
pub struct AudioChunk {
    /// UTC seconds of the first frame, from the samples.
    pub t: f64,
    pub rate: u32,
    pub channels: u8,
    /// Interleaved when stereo.
    pub pcm: Vec<f32>,
}

impl AudioChunk {
    pub fn frames(&self) -> usize {
        self.pcm.len() / self.channels.max(1) as usize
    }
}

/// What `status` says about a session.
#[derive(Clone, Debug)]
pub struct SessionInfo {
    pub id: String,
    pub decoder: &'static str,
    pub input: InputKind,
    pub rate: f64,
    pub params: Params,
    pub pinned: bool,
    pub state: State,
    pub why: Option<String>,
    /// UTC seconds of the last state change.
    pub since: f64,
    pub rev: u64,
    /// Share of one core over the budget's window.
    pub cpu: f64,
    pub fed: u64,
    pub dropped: u64,
    pub decoder_status: Status,
    /// The decoder's latest `meta` result (DAB's ensemble and services), so
    /// a view opened late, or a client that missed it, has it at once.
    pub meta: Option<Fields>,
    /// Frames of sound waiting in the ring, and dropped from it unpulled.
    pub audio_queued: usize,
    pub audio_dropped: u64,
}

/// A file a decoder made (an SSTV picture), as `files()` lists it.
#[derive(Clone, Debug, PartialEq)]
pub struct FileInfo {
    /// The id `file()` takes: the session's id, then the decoder's name.
    pub name: String,
    /// Empty for a file found on disk from before this host started.
    pub session: String,
    pub decoder: String,
    /// UTC seconds, from the samples (a file found on disk: its mtime).
    pub t: f64,
    pub size: usize,
    pub path: Option<PathBuf>,
}

struct Saved {
    info: FileInfo,
    bytes: Arc<Vec<u8>>,
}

/// A name the host will read or write: letters, digits, `_`, `-` and
/// `.`, not starting with a dot, so an id from a client can never leave
/// the files folder.
pub fn safe_file_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 160
        && !name.starts_with('.')
        && name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-' || b == b'.')
}

/// Write whole or not at all.
fn write_file(dir: &Path, name: &str, bytes: &[u8]) -> Option<PathBuf> {
    std::fs::create_dir_all(dir).ok()?;
    let tmp = dir.join(format!(".{name}.part"));
    let path = dir.join(name);
    std::fs::write(&tmp, bytes).ok()?;
    match std::fs::rename(&tmp, &path) {
        Ok(()) => Some(path),
        Err(_) => {
            let _ = std::fs::remove_file(&tmp);
            None
        }
    }
}

#[derive(Clone, Debug)]
pub struct HostStats {
    pub sessions: usize,
    pub threads: usize,
    pub cpu: f64,
    pub cpu_max: f64,
    pub queued: usize,
    /// Results the queue had to drop because nobody polled.
    pub lost: u64,
}

enum Samples {
    Iq(Vec<Complex32>),
    Audio(Vec<f32>),
}

impl Samples {
    fn len(&self) -> usize {
        match self {
            Samples::Iq(v) => v.len(),
            Samples::Audio(v) => v.len(),
        }
    }
}

struct Block {
    samples: Samples,
    t0: f64,
    index: u64,
}

/// A session's sound, waiting to be pulled, and the converter that keeps
/// its state across pulls.
struct AudioRing {
    chunks: VecDeque<AudioChunk>,
    frames: usize,
    cap_s: f64,
    dropped: u64,
    conv: Option<Convert>,
}

/// Rate and channel conversion for one stream: (from rate, from channels,
/// to rate, to channels), one resampler per output channel.
struct Convert {
    key: (u32, u8, u32, u8),
    rs: Vec<Resampler<f32>>,
}

impl AudioRing {
    fn push(&mut self, t: f64, pcm: Vec<f32>, rate: u32, channels: u8) {
        if rate == 0 || channels == 0 || pcm.is_empty() {
            return;
        }
        let ch = channels as usize;
        let frames = pcm.len() / ch;
        // Join a run of the same format that ends where this one starts.
        match self.chunks.back_mut() {
            Some(last) if last.rate == rate && last.channels == channels => last.pcm.extend_from_slice(&pcm[..frames * ch]),
            _ => self.chunks.push_back(AudioChunk { t, rate, channels, pcm: pcm[..frames * ch].to_vec() }),
        }
        self.frames += frames;
        // Over the ring: the oldest go, counted in frames.
        let cap = (self.cap_s * rate as f64) as usize;
        while self.frames > cap {
            let Some(front) = self.chunks.front_mut() else { break };
            let fch = front.channels as usize;
            let excess = self.frames - cap;
            let have = front.frames();
            if have <= excess {
                self.frames -= have;
                self.dropped += have as u64;
                self.chunks.pop_front();
            } else {
                front.pcm.drain(..excess * fch);
                front.t += excess as f64 / front.rate as f64;
                self.frames -= excess;
                self.dropped += excess as u64;
            }
        }
    }

    /// Up to `max_s` seconds, oldest first, each run converted if asked.
    fn pull(&mut self, max_s: f64, rate: Option<u32>, channels: Option<u8>) -> Vec<AudioChunk> {
        let mut out = Vec::new();
        let mut left = max_s;
        while left > 0.0 {
            let Some(front) = self.chunks.front_mut() else { break };
            let ch = front.channels as usize;
            let src_rate = front.rate as f64;
            let n = front.frames().min((left * src_rate).round() as usize);
            if n == 0 {
                break;
            }
            let pcm: Vec<f32> = front.pcm.drain(..n * ch).collect();
            let chunk = AudioChunk { t: front.t, rate: front.rate, channels: front.channels, pcm };
            front.t += n as f64 / front.rate as f64;
            if front.pcm.is_empty() {
                self.chunks.pop_front();
            }
            self.frames -= n;
            left -= n as f64 / src_rate;
            out.push(self.convert(chunk, rate, channels));
        }
        out
    }

    fn convert(&mut self, c: AudioChunk, rate: Option<u32>, channels: Option<u8>) -> AudioChunk {
        let to_rate = rate.unwrap_or(c.rate);
        let to_ch = channels.unwrap_or(c.channels).clamp(1, 2);
        if to_rate == c.rate && to_ch == c.channels {
            return c;
        }
        let key = (c.rate, c.channels, to_rate, to_ch);
        if self.conv.as_ref().map(|v| v.key) != Some(key) {
            let rs = (0..to_ch).map(|_| Resampler::new(c.rate as f64, to_rate as f64, 0.9, 32)).collect();
            self.conv = Some(Convert { key, rs });
        }
        let conv = self.conv.as_mut().unwrap();
        let ch = c.channels as usize;
        let frames = c.frames();
        // Channels first: stereo to mono is the mean, mono to stereo a copy.
        let planes: Vec<Vec<f32>> = match (ch, to_ch) {
            (2, 1) => vec![(0..frames).map(|i| 0.5 * (c.pcm[2 * i] + c.pcm[2 * i + 1])).collect()],
            (1, 2) => vec![c.pcm.clone(), c.pcm.clone()],
            (2, 2) => vec![(0..frames).map(|i| c.pcm[2 * i]).collect(), (0..frames).map(|i| c.pcm[2 * i + 1]).collect()],
            _ => vec![c.pcm.clone()],
        };
        let mut outs: Vec<Vec<f32>> = Vec::new();
        for (plane, r) in planes.iter().zip(conv.rs.iter_mut()) {
            let mut o = Vec::new();
            if to_rate == c.rate {
                o.extend_from_slice(plane);
            } else {
                r.process(plane, &mut o);
            }
            outs.push(o);
        }
        let n = outs.iter().map(|o| o.len()).min().unwrap_or(0);
        let pcm = if to_ch == 2 { (0..n).flat_map(|i| [outs[0][i], outs[1][i]]).collect() } else { outs[0][..n].to_vec() };
        AudioChunk { t: c.t, rate: to_rate, channels: to_ch, pcm }
    }
}

struct Inbox {
    blocks: VecDeque<Block>,
    samples: usize,
    cap: usize,
    accepting: bool,
    fed: u64,
    dropped: u64,
}

/// What only the worker decoding the session touches. Its lock is held for
/// a whole chunk of decoding, so nothing the engine calls ever takes it:
/// the decoder's status is copied out to `Ctl` after each chunk, and a
/// reset is asked for through `Session::reset`.
struct Work {
    decoder: Box<dyn Decoder>,
    adapter: Option<AudioToIq>,
}

struct Ctl {
    state: State,
    why: Option<String>,
    since: f64,
    rev: u64,
    /// (when, cpu) for each block decoded in the window.
    cpu: VecDeque<(Instant, Duration)>,
    /// The share it used when it was paused, for deciding when it fits again.
    paused_share: f64,
    removed: bool,
    /// The decoder's status after its last chunk.
    status: Status,
    /// Its latest `meta` result.
    meta: Option<Fields>,
}

struct Session {
    id: String,
    seq: u64,
    kind: Kind,
    input: InputKind,
    rate: f64,
    params: Mutex<Params>,
    pinned: bool,
    inbox: Mutex<Inbox>,
    work: Mutex<Work>,
    ctl: Mutex<Ctl>,
    /// Reset the decoder before its next block (a retune, a pause ended).
    reset: AtomicBool,
    /// Parameters to hand the decoder before its next block (`configure`).
    pending: Mutex<Option<Params>>,
    audio: Mutex<AudioRing>,
}

struct Shared {
    cfg: HostConfig,
    sessions: Mutex<BTreeMap<String, Arc<Session>>>,
    results: Mutex<VecDeque<Item>>,
    lost: AtomicU64,
    wake: Mutex<u64>,
    wake_cv: Condvar,
    stop: AtomicBool,
    next: AtomicU64,
    salt: u64,
    budget: Mutex<Instant>,
    files: Mutex<VecDeque<Saved>>,
}

pub struct Host {
    shared: Arc<Shared>,
    workers: Mutex<Vec<JoinHandle<()>>>,
}

/// A poisoned lock means a panic while it was held; every holder here
/// leaves the data consistent, so carry on with it.
fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn now_utc() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn window_share(cpu: &mut VecDeque<(Instant, Duration)>, now: Instant) -> f64 {
    while cpu.front().is_some_and(|(t, _)| now.duration_since(*t) > WINDOW) {
        cpu.pop_front();
    }
    cpu.iter().map(|(_, d)| d.as_secs_f64()).sum::<f64>() / WINDOW.as_secs_f64()
}

impl Shared {
    fn push(&self, items: Vec<Item>) {
        if items.is_empty() {
            return;
        }
        let mut q = lock(&self.results);
        for it in items {
            q.push_back(it);
        }
        while q.len() > self.cfg.max_results {
            q.pop_front();
            self.lost.fetch_add(1, Ordering::Relaxed);
        }
    }

    /// Change a session's state, queueing a `state` result when it changed.
    fn set_state(&self, s: &Session, state: State, why: Option<String>) {
        let item = {
            let mut c = lock(&s.ctl);
            if c.removed || (c.state == state && c.why == why) {
                return;
            }
            c.state = state;
            c.why = why.clone();
            c.since = now_utc();
            c.rev += 1;
            Item { session: s.id.clone(), rev: c.rev, t: c.since, body: Body::State { state, why } }
        };
        self.push(vec![item]);
    }

    /// Keep a decoder's finished file; the event its result carries.
    fn keep_file(&self, s: &Session, t: f64, file: &str, bytes: Vec<u8>) -> ImageEvent {
        let mut name = format!("{}_{}", s.id, file);
        name.retain(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-' | '.'));
        let path = self.cfg.files_dir.as_ref().and_then(|d| write_file(d, &name, &bytes));
        let info = FileInfo {
            name: name.clone(),
            session: s.id.clone(),
            decoder: s.kind.info.id.to_string(),
            t,
            size: bytes.len(),
            path: path.clone(),
        };
        let mut files = lock(&self.files);
        files.push_back(Saved { info, bytes: Arc::new(bytes) });
        while files.len() > self.cfg.files_keep.max(1) {
            files.pop_front();
        }
        ImageEvent::Done { file: name, png: Vec::new(), path: path.map(|p| p.to_string_lossy().into_owned()) }
    }

    fn wake(&self) {
        *lock(&self.wake) += 1;
        self.wake_cv.notify_one();
    }

    fn snapshot(&self) -> Vec<Arc<Session>> {
        lock(&self.sessions).values().cloned().collect()
    }

    /// Pause the newest unpinned session while over the cap; resume the
    /// oldest paused one when the rest leave room for it.
    fn budget(&self) {
        if self.cfg.cpu_max <= 0.0 {
            return;
        }
        let mut last = lock(&self.budget);
        let now = Instant::now();
        if now.duration_since(*last) < Duration::from_millis(250) {
            return;
        }
        let sessions = self.snapshot();
        let mut used = 0.0;
        for s in &sessions {
            let mut c = lock(&s.ctl);
            let share = window_share(&mut c.cpu, now);
            if c.state.active() || c.state == State::Outside {
                used += share;
            }
        }
        if used > self.cfg.cpu_max {
            let newest = sessions
                .iter()
                .filter(|s| !s.pinned && lock(&s.ctl).state.active())
                .max_by_key(|s| s.seq);
            if let Some(s) = newest {
                let share = {
                    let mut c = lock(&s.ctl);
                    let sh = window_share(&mut c.cpu, now);
                    c.paused_share = sh;
                    c.cpu.clear();
                    sh
                };
                let _ = share;
                {
                    let mut ib = lock(&s.inbox);
                    ib.accepting = false;
                    ib.blocks.clear();
                    ib.samples = 0;
                }
                self.set_state(s, State::Budget, Some("cpu_budget".into()));
                *last = now;
            }
            return;
        }
        if now.duration_since(*last) < SETTLE {
            return;
        }
        let oldest = sessions.iter().filter(|s| lock(&s.ctl).state == State::Budget).min_by_key(|s| s.seq);
        if let Some(s) = oldest {
            let need = lock(&s.ctl).paused_share;
            if used + need < 0.9 * self.cfg.cpu_max {
                s.reset.store(true, Ordering::SeqCst);
                lock(&s.inbox).accepting = true;
                self.set_state(s, State::Starting, None);
                *last = now;
            }
        }
    }
}

impl Host {
    pub fn new(cfg: HostConfig) -> Host {
        let threads = cfg.threads.clamp(1, 4);
        let salt = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.subsec_nanos() as u64).unwrap_or(0);
        let shared = Arc::new(Shared {
            cfg: HostConfig { threads, ..cfg },
            sessions: Mutex::new(BTreeMap::new()),
            results: Mutex::new(VecDeque::new()),
            lost: AtomicU64::new(0),
            wake: Mutex::new(0),
            wake_cv: Condvar::new(),
            stop: AtomicBool::new(false),
            next: AtomicU64::new(1),
            salt,
            budget: Mutex::new(Instant::now()),
            files: Mutex::new(VecDeque::new()),
        });
        let workers = (0..threads)
            .map(|i| {
                let sh = shared.clone();
                std::thread::Builder::new()
                    .name(format!("shack-sdr-{i}"))
                    .spawn(move || worker(sh, i))
                    .expect("spawn a decoder thread")
            })
            .collect();
        Host { shared, workers: Mutex::new(workers) }
    }

    pub fn config(&self) -> &HostConfig {
        &self.shared.cfg
    }

    /// The bytes of a file a decoder made, by its name: from memory, else
    /// from the files folder; None for an unknown or unsafe name.
    pub fn file(&self, name: &str) -> Option<Vec<u8>> {
        if !safe_file_name(name) {
            return None;
        }
        if let Some(f) = lock(&self.shared.files).iter().find(|f| f.info.name == name) {
            return Some(f.bytes.as_ref().clone());
        }
        std::fs::read(self.shared.cfg.files_dir.as_ref()?.join(name)).ok()
    }

    /// Every file it can give, oldest first: those made since it started,
    /// then any others in the files folder (from before a restart).
    pub fn files(&self) -> Vec<FileInfo> {
        let mut out: Vec<FileInfo> = lock(&self.shared.files).iter().map(|f| f.info.clone()).collect();
        if let Some(dir) = &self.shared.cfg.files_dir {
            let mut disk: Vec<FileInfo> = std::fs::read_dir(dir)
                .into_iter()
                .flatten()
                .flatten()
                .filter_map(|e| {
                    let name = e.file_name().into_string().ok()?;
                    if !safe_file_name(&name) || out.iter().any(|f| f.name == name) {
                        return None;
                    }
                    let md = e.metadata().ok()?;
                    let t = md.modified().ok()?.duration_since(UNIX_EPOCH).ok()?.as_secs_f64();
                    Some(FileInfo {
                        name,
                        session: String::new(),
                        decoder: String::new(),
                        t,
                        size: md.len() as usize,
                        path: Some(e.path()),
                    })
                })
                .collect();
            disk.sort_by(|a, b| a.t.total_cmp(&b.t));
            disk.extend(out);
            out = disk;
        }
        out
    }

    /// Start a session. `centre_hz` is where, in audio fed to an IQ-only
    /// decoder, the signal's centre sits (RTTY's tone pair); ignored for IQ.
    /// `pinned`: never paused by the budget (the mode decoder).
    pub fn start(
        &self,
        decoder: &str,
        params: Params,
        input: InputKind,
        rate: f64,
        centre_hz: f64,
        pinned: bool,
    ) -> Result<String, HostError> {
        if self.shared.stop.load(Ordering::SeqCst) {
            return Err(HostError::Closed);
        }
        let kind = catalogue::find(decoder, self.shared.cfg.testing).ok_or(HostError::UnknownDecoder)?;
        if !(rate.is_finite() && rate > 0.0) {
            return Err(HostError::BadInput("rate"));
        }
        if input == InputKind::Iq && rate < kind.info.min_rate_hz {
            return Err(HostError::BadInput("rate"));
        }
        let adapter = match (input, kind.info.inputs.contains(&input)) {
            (_, true) => None,
            (InputKind::Audio, false) if kind.info.inputs.contains(&InputKind::Iq) => {
                if !(centre_hz > 0.0 && centre_hz < rate / 2.0) {
                    return Err(HostError::BadInput("centre"));
                }
                Some(AudioToIq::new(centre_hz, rate))
            }
            // IQ to discriminator audio arrives with the FM-based modes (M2).
            _ => return Err(HostError::InputUnsupported),
        };
        let mut dec = (kind.make)();
        dec.configure(&params).map_err(|e| match e {
            DecodeError::BadInput(c) => HostError::BadParams(c),
            other => HostError::BadParams(other.code()),
        })?;
        let status = dec.status();
        let mut sessions = lock(&self.shared.sessions);
        if sessions.len() >= self.shared.cfg.max_sessions {
            return Err(HostError::TooManySessions);
        }
        let seq = self.shared.next.fetch_add(1, Ordering::SeqCst);
        let id = format!("ds_{:04x}", (seq.wrapping_mul(0x9e37) ^ self.shared.salt) & 0xffff);
        let id = if sessions.contains_key(&id) { format!("{id}{seq}") } else { id };
        let cap = (self.shared.cfg.ring_s * rate).ceil() as usize;
        let s = Arc::new(Session {
            id: id.clone(),
            seq,
            kind,
            input,
            rate,
            params: Mutex::new(params),
            pinned,
            inbox: Mutex::new(Inbox { blocks: VecDeque::new(), samples: 0, cap, accepting: true, fed: 0, dropped: 0 }),
            work: Mutex::new(Work { decoder: dec, adapter }),
            ctl: Mutex::new(Ctl {
                state: State::Starting,
                why: None,
                since: now_utc(),
                rev: 0,
                cpu: VecDeque::new(),
                paused_share: 0.0,
                removed: false,
                status,
                meta: None,
            }),
            reset: AtomicBool::new(false),
            pending: Mutex::new(None),
            audio: Mutex::new(AudioRing {
                chunks: VecDeque::new(),
                frames: 0,
                cap_s: self.shared.cfg.ring_s.max(0.5),
                dropped: 0,
                conv: None,
            }),
        });
        sessions.insert(id.clone(), s);
        Ok(id)
    }

    fn session(&self, id: &str) -> Result<Arc<Session>, HostError> {
        lock(&self.shared.sessions).get(id).cloned().ok_or(HostError::UnknownSession)
    }

    fn feed_block(&self, id: &str, samples: Samples, t0: f64, index: u64) -> Result<(), HostError> {
        let s = self.session(id)?;
        let kind = match samples {
            Samples::Iq(_) => InputKind::Iq,
            Samples::Audio(_) => InputKind::Audio,
        };
        if kind != s.input {
            return Err(HostError::BadInput("input_kind"));
        }
        let n = samples.len();
        if n == 0 {
            return Ok(());
        }
        {
            let mut ib = lock(&s.inbox);
            if !ib.accepting {
                return Ok(()); // paused, outside, stopped or failed: fed nothing
            }
            ib.fed += n as u64;
            ib.blocks.push_back(Block { samples, t0, index });
            ib.samples += n;
            // Over the ring: the oldest blocks go, counted.
            while ib.samples > ib.cap && ib.blocks.len() > 1 {
                let b = ib.blocks.pop_front().unwrap();
                ib.samples -= b.samples.len();
                ib.dropped += b.samples.len() as u64;
            }
        }
        self.shared.wake();
        Ok(())
    }

    /// Copy complex samples into the session's ring and return.
    pub fn feed_iq(&self, id: &str, x: &[Complex32], t0: f64, index: u64) -> Result<(), HostError> {
        self.feed_block(id, Samples::Iq(x.to_vec()), t0, index)
    }

    /// Copy real audio into the session's ring and return.
    pub fn feed_audio(&self, id: &str, x: &[f32], t0: f64, index: u64) -> Result<(), HostError> {
        self.feed_block(id, Samples::Audio(x.to_vec()), t0, index)
    }

    /// Up to `max` results, oldest first. Never blocks on decoding.
    pub fn poll(&self, max: usize) -> Vec<Item> {
        let mut q = lock(&self.shared.results);
        let n = max.min(q.len());
        q.drain(..n).collect()
    }

    pub fn status(&self, id: &str) -> Result<SessionInfo, HostError> {
        let s = self.session(id)?;
        let (fed, dropped) = {
            let ib = lock(&s.inbox);
            (ib.fed, ib.dropped)
        };
        // The decoder's own status as copied after its last chunk: this
        // never waits for a worker that is decoding.
        let (audio_queued, audio_dropped) = {
            let a = lock(&s.audio);
            (a.frames, a.dropped)
        };
        let params = lock(&s.params).clone();
        let mut c = lock(&s.ctl);
        let decoder_status = c.status.clone();
        let cpu = window_share(&mut c.cpu, Instant::now());
        Ok(SessionInfo {
            id: s.id.clone(),
            decoder: s.kind.info.id,
            input: s.input,
            rate: s.rate,
            params,
            pinned: s.pinned,
            state: c.state,
            why: c.why.clone(),
            since: c.since,
            rev: c.rev,
            cpu,
            fed,
            dropped,
            decoder_status,
            meta: c.meta.clone(),
            audio_queued,
            audio_dropped,
        })
    }

    /// Change a running session's parameters: checked now against the
    /// decoder's entry, applied by its worker before its next block.
    /// Parameters not named keep their values.
    pub fn configure(&self, id: &str, params: Params) -> Result<(), HostError> {
        let s = self.session(id)?;
        check_params(&params, s.kind.info.params).map_err(|e| HostError::BadParams(e.code()))?;
        let merged = {
            let p = lock(&s.params);
            let mut m = p.clone();
            for (k, v) in params.0 {
                m.0.insert(k, v);
            }
            m
        };
        // What only the decoder can judge (a DAB SId that is not hex) is
        // judged now, by a fresh one, so the refusal is the caller's and not
        // lost on the worker.
        (s.kind.make)().configure(&merged).map_err(|e| HostError::BadParams(e.code()))?;
        *lock(&s.params) = merged.clone();
        *lock(&s.pending) = Some(merged);
        self.shared.wake();
        Ok(())
    }

    /// The session's sound: up to `max_s` seconds of it, oldest first, as runs
    /// of one format each, converted to `rate` and `channels` when given
    /// (the conversion keeps its state from one pull to the next). Empty
    /// when there is none; never waits.
    pub fn audio(&self, id: &str, max_s: f64, rate: Option<u32>, channels: Option<u8>) -> Result<Vec<AudioChunk>, HostError> {
        let s = self.session(id)?;
        if rate == Some(0) || channels.is_some_and(|c| c == 0 || c > 2) {
            return Err(HostError::BadInput("audio_format"));
        }
        let out = lock(&s.audio).pull(max_s, rate, channels);
        Ok(out)
    }

    pub fn sessions(&self) -> Vec<String> {
        lock(&self.shared.sessions).keys().cloned().collect()
    }

    pub fn stats(&self) -> HostStats {
        let now = Instant::now();
        let sessions = self.shared.snapshot();
        let cpu = sessions.iter().map(|s| window_share(&mut lock(&s.ctl).cpu, now)).sum();
        HostStats {
            sessions: sessions.len(),
            threads: self.shared.cfg.threads,
            cpu,
            cpu_max: self.shared.cfg.cpu_max,
            queued: lock(&self.shared.results).len(),
            lost: self.shared.lost.load(Ordering::Relaxed),
        }
    }

    /// Forget the session's signal state (a retune).
    pub fn reset(&self, id: &str) -> Result<(), HostError> {
        let s = self.session(id)?;
        {
            let mut ib = lock(&s.inbox);
            ib.blocks.clear();
            ib.samples = 0;
        }
        s.reset.store(true, Ordering::SeqCst);
        Ok(())
    }

    /// The engine says the session's frequency left (or came back into) the
    /// receiver's span: `outside` is fed nothing (RADIO-SDR-PLAN §4.3).
    pub fn set_outside(&self, id: &str, outside: bool) -> Result<(), HostError> {
        let s = self.session(id)?;
        let state = lock(&s.ctl).state;
        if outside && state.active() {
            {
                let mut ib = lock(&s.inbox);
                ib.accepting = false;
                ib.blocks.clear();
                ib.samples = 0;
            }
            self.shared.set_state(&s, State::Outside, Some("outside_span".into()));
        } else if !outside && state == State::Outside {
            s.reset.store(true, Ordering::SeqCst);
            lock(&s.inbox).accepting = true;
            self.shared.set_state(&s, State::Starting, None);
        }
        Ok(())
    }

    pub fn stop(&self, id: &str) -> Result<(), HostError> {
        let s = lock(&self.shared.sessions).remove(id).ok_or(HostError::UnknownSession)?;
        {
            let mut ib = lock(&s.inbox);
            ib.accepting = false;
            ib.blocks.clear();
            ib.samples = 0;
        }
        self.shared.set_state(&s, State::Stopped, None);
        lock(&s.ctl).removed = true;
        Ok(())
    }

    /// Stop the workers and join them, within one second. True if every
    /// worker was joined; one still inside a decoder is left to finish on
    /// its own (it holds no hardware).
    pub fn close(&self) -> bool {
        self.shared.stop.store(true, Ordering::SeqCst);
        self.shared.wake_cv.notify_all();
        let deadline = Instant::now() + Duration::from_secs(1);
        let mut workers = lock(&self.workers);
        let mut all = true;
        for h in workers.drain(..) {
            while !h.is_finished() && Instant::now() < deadline {
                self.shared.wake_cv.notify_all();
                std::thread::sleep(Duration::from_millis(2));
            }
            if h.is_finished() {
                let _ = h.join();
            } else {
                all = false;
            }
        }
        let ids: Vec<String> = self.sessions();
        for id in ids {
            let _ = self.stop(&id);
        }
        all
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        self.close();
    }
}

/// Take up to CHUNK_S of the session's samples, or nothing if another
/// worker has it.
fn take(s: &Session) -> Vec<Block> {
    let mut ib = lock(&s.inbox);
    let limit = (CHUNK_S * s.rate).max(1.0) as usize;
    let mut out = Vec::new();
    let mut n = 0;
    while n < limit {
        let Some(b) = ib.blocks.pop_front() else { break };
        n += b.samples.len();
        ib.samples -= b.samples.len();
        out.push(b);
    }
    out
}

fn worker(sh: Arc<Shared>, _i: usize) {
    let mut turn = 0usize;
    loop {
        if sh.stop.load(Ordering::SeqCst) {
            return;
        }
        let seen = *lock(&sh.wake);
        let sessions = sh.snapshot();
        let mut did = false;
        // Round robin over the sessions, from a different one each pass.
        let n = sessions.len();
        for k in 0..n {
            if sh.stop.load(Ordering::SeqCst) {
                return;
            }
            let s = &sessions[(turn + k) % n];
            // try_lock: a session another worker is decoding is left to it.
            let Ok(mut work) = s.work.try_lock() else { continue };
            let blocks = take(s);
            if blocks.is_empty() {
                continue;
            }
            did = true;
            decode(&sh, s, &mut work, blocks);
        }
        turn = turn.wrapping_add(1);
        if did {
            sh.budget();
            continue;
        }
        let g = lock(&sh.wake);
        if *g == seen && !sh.stop.load(Ordering::SeqCst) {
            let _ = sh.wake_cv.wait_timeout(g, Duration::from_millis(100));
        }
        sh.budget();
    }
}

fn decode(sh: &Shared, s: &Session, work: &mut Work, blocks: Vec<Block>) {
    let t_cpu = crate::cpu::thread_time();
    let mut sink = Sink::new();
    let mut failure: Option<String> = None;
    let mut needs: Option<String> = None;
    for b in blocks {
        let w = &mut *work;
        let pending = lock(&s.pending).take();
        let r = catch_unwind(AssertUnwindSafe(|| -> Result<(), DecodeError> {
            if let Some(p) = pending {
                // Checked against the entry already; a decoder that still
                // refuses keeps its old settings, as a bad start would.
                let _ = w.decoder.configure(&p);
            }
            if s.reset.swap(false, Ordering::SeqCst) {
                w.decoder.reset();
                if let Some(a) = w.adapter.as_mut() {
                    a.reset();
                }
            }
            match (&b.samples, w.adapter.as_mut()) {
                (Samples::Audio(x), Some(a)) => {
                    let mut iq = Vec::with_capacity(x.len());
                    a.process(x, &mut iq);
                    w.decoder.feed(Input::Iq { samples: &iq, rate: s.rate, t0: b.t0, index: b.index }, &mut sink)
                }
                (Samples::Audio(x), None) => {
                    w.decoder.feed(Input::Audio { samples: x, rate: s.rate, t0: b.t0, index: b.index }, &mut sink)
                }
                (Samples::Iq(x), _) => {
                    w.decoder.feed(Input::Iq { samples: x, rate: s.rate, t0: b.t0, index: b.index }, &mut sink)
                }
            }
        }));
        match r {
            Ok(Ok(())) => {}
            Ok(Err(DecodeError::NeedsCodec(c))) => needs = Some(format!("needs_codec:{}", c.code())),
            Ok(Err(DecodeError::Codec(..))) => {} // counted by the decoder; not fatal
            Ok(Err(e)) => {
                failure = Some(e.code().to_string());
                break;
            }
            Err(_) => {
                failure = Some("panic".to_string());
                break;
            }
        }
    }
    let spent = crate::cpu::thread_time().saturating_sub(t_cpu);
    let status = if failure.is_none() {
        catch_unwind(AssertUnwindSafe(|| work.decoder.status())).ok()
    } else {
        None
    };
    // Results, numbered.
    let items: Vec<Item> = {
        let mut c = lock(&s.ctl);
        if let Some(st) = status.clone() {
            c.status = st;
        }
        c.cpu.push_back((Instant::now(), spent));
        if c.removed {
            Vec::new()
        } else {
            let mut items = Vec::new();
            // Files kept in this block: (the decoder's name, the host's).
            let mut renamed: Vec<(String, String)> = Vec::new();
            for (t, out) in sink.take() {
                match out {
                    // Sound goes to its own ring, not the results.
                    Output::Audio { pcm, rate, channels } => lock(&s.audio).push(t, pcm, rate, channels),
                    // A file: kept (and written) here; the result names it.
                    Output::Image { event: ImageEvent::Done { file, png, .. } } => {
                        let event = sh.keep_file(s, t, &file, png);
                        if let ImageEvent::Done { file: kept, .. } = &event {
                            renamed.push((file, kept.clone()));
                        }
                        c.rev += 1;
                        items.push(Item { session: s.id.clone(), rev: c.rev, t, body: Body::Output(Output::Image { event }) });
                    }
                    mut other => {
                        if let Output::Meta { fields } = &mut other {
                            // A meta naming a file just handed over names it
                            // as the host keeps it, so a client can fetch it.
                            for (k, v) in fields.iter_mut() {
                                if let (true, Value::Text(name)) = (k == "file", &*v) {
                                    if let Some((_, kept)) = renamed.iter().find(|(from, _)| from == name) {
                                        *v = Value::Text(kept.clone());
                                    }
                                }
                            }
                            c.meta = Some(fields.clone());
                        }
                        c.rev += 1;
                        items.push(Item { session: s.id.clone(), rev: c.rev, t, body: Body::Output(other) });
                    }
                }
            }
            items
        }
    };
    sh.push(items);
    if let Some(code) = failure {
        {
            let mut ib = lock(&s.inbox);
            ib.accepting = false;
            ib.blocks.clear();
            ib.samples = 0;
        }
        sh.set_state(s, State::Error, Some(code));
        return;
    }
    let state = lock(&s.ctl).state;
    if state.active() {
        let next = match (needs, status.map(|st| st.sync)) {
            (Some(why), _) => (State::Running, Some(why)),
            (None, Some(true)) => (State::Synced, None),
            (None, Some(false)) => (State::NoSignal, None),
            (None, None) => (State::Running, None),
        };
        sh.set_state(s, next.0, next.1);
    }
}

/// Decoders for the host's own tests, offered only by a host made with
/// `testing` (and so never in an app's catalogue).
pub mod testing {
    use crate::catalogue::Kind;
    use crate::decoder::*;
    use std::time::{Duration, Instant};

    const ANY: &[InputKind] = &[InputKind::Iq, InputKind::Audio];
    const PARAMS: &[ParamSpec] = &[
        ParamSpec { key: "ms", kind: ParamKind::Range { min: 0.0, max: 1000.0 }, default: ParamDefault::Num(0.0) },
        ParamSpec { key: "after", kind: ParamKind::Range { min: 0.0, max: 1e9 }, default: ParamDefault::Num(0.0) },
        ParamSpec { key: "level", kind: ParamKind::Range { min: 0.0, max: 1.0 }, default: ParamDefault::Num(0.5) },
        ParamSpec { key: "name", kind: ParamKind::Text, default: ParamDefault::Text("") },
    ];

    macro_rules! info {
        ($id:literal) => {
            Info {
                id: $id,
                family: Family::Amateur,
                inputs: ANY,
                rate_hz: 12_000.0,
                bandwidth_hz: 3_000.0,
                outputs: &[OutputKind::Text],
                panel: Panel::Text,
                params: PARAMS,
                needs: &[],
                needs_for: &[],
                min_rate_hz: 0.0,
            }
        };
    }
    pub static ECHO: Info = info!("test_echo");
    pub static BURN: Info = info!("test_burn");
    pub static SLEEP: Info = info!("test_sleep");
    pub static PANIC: Info = info!("test_panic");
    pub static SOUND: Info = info!("test_sound");

    /// Says how many samples each block had and where it started; `ms` of
    /// CPU burnt (test_burn) or of sleep (test_sleep) per block; test_panic
    /// panics on block `after`; test_sound makes one stereo frame of a
    /// 1 kHz tone at 48 kHz, at `level`, for each sample fed, and says
    /// `name` and its block count as meta.
    pub struct Test {
        info: &'static Info,
        ms: f64,
        after: u64,
        blocks: u64,
        level: f64,
        name: String,
        phase: u64,
    }

    impl Decoder for Test {
        fn info(&self) -> &'static Info {
            self.info
        }
        fn configure(&mut self, p: &Params) -> Result<(), DecodeError> {
            check_params(p, PARAMS)?;
            self.ms = p.num("ms").unwrap_or(0.0);
            self.after = p.num("after").unwrap_or(self.after as f64) as u64;
            self.level = p.num("level").unwrap_or(self.level);
            if let Some(n) = p.text("name") {
                self.name = n.to_string();
            }
            Ok(())
        }
        fn feed(&mut self, input: Input<'_>, out: &mut Sink) -> Result<(), DecodeError> {
            if self.info.id == "test_panic" && self.blocks >= self.after {
                panic!("test_panic, as asked");
            }
            self.blocks += 1;
            let d = Duration::from_secs_f64(self.ms / 1000.0);
            match self.info.id {
                "test_burn" => {
                    let t = crate::cpu::thread_time();
                    let wall = Instant::now();
                    let mut x = 1u64;
                    while crate::cpu::thread_time() - t < d && wall.elapsed() < 10 * d {
                        for _ in 0..1000 {
                            x = x.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                        }
                    }
                    std::hint::black_box(x);
                }
                "test_sleep" => std::thread::sleep(d),
                "test_sound" => {
                    let pcm: Vec<f32> = (0..input.len())
                        .flat_map(|_| {
                            let v = (self.level * (2.0 * std::f64::consts::PI * self.phase as f64 / 48.0).sin()) as f32;
                            self.phase += 1;
                            [v, v]
                        })
                        .collect();
                    out.push(input.t0(), Output::Audio { pcm, rate: 48_000, channels: 2 });
                    out.push(
                        input.t0(),
                        Output::Meta {
                            fields: vec![
                                ("name".into(), Value::Text(self.name.clone())),
                                ("blocks".into(), Value::Num((self.blocks) as f64)),
                                ("list".into(), Value::List(vec![Value::Map(vec![("k".into(), Value::Null)])])),
                            ],
                        },
                    );
                }
                _ => {}
            }
            out.push(
                input.t0(),
                Output::Text { text: format!("{}@{}", input.len(), input.index()), offset_hz: 0.0, snr_db: None },
            );
            Ok(())
        }
        fn reset(&mut self) {}
        fn status(&self) -> Status {
            Status { sync: true, counters: vec![("blocks", self.blocks)], ..Default::default() }
        }
    }

    fn make(info: &'static Info) -> Box<dyn Decoder> {
        Box::new(Test { info, ms: 0.0, after: 0, blocks: 0, level: 0.5, name: String::new(), phase: 0 })
    }

    pub fn kinds() -> Vec<Kind> {
        vec![
            Kind { info: &ECHO, make: || make(&ECHO) },
            Kind { info: &BURN, make: || make(&BURN) },
            Kind { info: &SLEEP, make: || make(&SLEEP) },
            Kind { info: &PANIC, make: || make(&PANIC) },
            Kind { info: &SOUND, make: || make(&SOUND) },
        ]
    }
}
