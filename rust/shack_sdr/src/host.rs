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

use crate::catalogue::{self, Kind};
use crate::decoder::{DecodeError, Decoder, Input, InputKind, Output, Params, Sink, Status};
use crate::dsp::AudioToIq;
use rustfft::num_complex::Complex32;
use std::collections::{BTreeMap, VecDeque};
use std::panic::{catch_unwind, AssertUnwindSafe};
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
}

impl Default for HostConfig {
    fn default() -> Self {
        HostConfig { threads: 1, cpu_max: 0.25, ring_s: 2.0, max_sessions: 17, max_results: 20_000, testing: false }
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

struct Inbox {
    blocks: VecDeque<Block>,
    samples: usize,
    cap: usize,
    accepting: bool,
    fed: u64,
    dropped: u64,
}

struct Work {
    decoder: Box<dyn Decoder>,
    adapter: Option<AudioToIq>,
    /// Reset the decoder before the next block (after a pause).
    reset: bool,
    status: Status,
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
}

struct Session {
    id: String,
    seq: u64,
    kind: Kind,
    input: InputKind,
    rate: f64,
    params: Params,
    pinned: bool,
    inbox: Mutex<Inbox>,
    work: Mutex<Work>,
    ctl: Mutex<Ctl>,
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
                lock(&s.work).reset = true;
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
            params,
            pinned,
            inbox: Mutex::new(Inbox { blocks: VecDeque::new(), samples: 0, cap, accepting: true, fed: 0, dropped: 0 }),
            work: Mutex::new(Work { decoder: dec, adapter, reset: false, status }),
            ctl: Mutex::new(Ctl {
                state: State::Starting,
                why: None,
                since: now_utc(),
                rev: 0,
                cpu: VecDeque::new(),
                paused_share: 0.0,
                removed: false,
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
        // The decoder's own status was copied after its last block, so this
        // never waits for a worker that is decoding.
        let decoder_status = lock(&s.work).status.clone();
        let mut c = lock(&s.ctl);
        let cpu = window_share(&mut c.cpu, Instant::now());
        Ok(SessionInfo {
            id: s.id.clone(),
            decoder: s.kind.info.id,
            input: s.input,
            rate: s.rate,
            params: s.params.clone(),
            pinned: s.pinned,
            state: c.state,
            why: c.why.clone(),
            since: c.since,
            rev: c.rev,
            cpu,
            fed,
            dropped,
            decoder_status,
        })
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
        lock(&s.work).reset = true;
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
            lock(&s.work).reset = true;
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
        let r = catch_unwind(AssertUnwindSafe(|| -> Result<(), DecodeError> {
            if w.reset {
                w.reset = false;
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
    if let Some(st) = status.clone() {
        work.status = st;
    }
    // Results, numbered.
    let items: Vec<Item> = {
        let mut c = lock(&s.ctl);
        c.cpu.push_back((Instant::now(), spent));
        if c.removed {
            Vec::new()
        } else {
            sink.take()
                .into_iter()
                .map(|(t, out)| {
                    c.rev += 1;
                    Item { session: s.id.clone(), rev: c.rev, t, body: Body::Output(out) }
                })
                .collect()
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
            }
        };
    }
    pub static ECHO: Info = info!("test_echo");
    pub static BURN: Info = info!("test_burn");
    pub static SLEEP: Info = info!("test_sleep");
    pub static PANIC: Info = info!("test_panic");

    /// Says how many samples each block had and where it started; `ms` of
    /// CPU burnt (test_burn) or of sleep (test_sleep) per block; test_panic
    /// panics on block `after`.
    pub struct Test {
        info: &'static Info,
        ms: f64,
        after: u64,
        blocks: u64,
    }

    impl Decoder for Test {
        fn info(&self) -> &'static Info {
            self.info
        }
        fn configure(&mut self, p: &Params) -> Result<(), DecodeError> {
            check_params(p, PARAMS)?;
            self.ms = p.num("ms").unwrap_or(0.0);
            self.after = p.num("after").unwrap_or(0.0) as u64;
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
        Box::new(Test { info, ms: 0.0, after: 0, blocks: 0 })
    }

    pub fn kinds() -> Vec<Kind> {
        vec![
            Kind { info: &ECHO, make: || make(&ECHO) },
            Kind { info: &BURN, make: || make(&BURN) },
            Kind { info: &SLEEP, make: || make(&SLEEP) },
            Kind { info: &PANIC, make: || make(&PANIC) },
        ]
    }
}
