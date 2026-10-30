# Changelog

## 0.2.0 (unreleased)

First M1 cut.

- shack-ui moves to ui/2, server-driven screens (openhamshack
  docs/contract/SCREENS.md, 30/10/2026), and ui/1 goes: no client ever
  drew it. `Screen` and the `p`/`A`/`T` builders, `serve_document`,
  `LiveHub`, the validator and op applier (from the contract's fixture
  tool), the shared symbol sets in `spec/ui2/`, `spec/schemas/ui2.schema.json`
  and `spec/vectors/ui2/` for the Kotlin and Swift renderers. Radio's
  ui/1 screens (openhamshack-nb `ohsradio/screens.py`, `ohscore/server/uiv1.py`)
  stop importing on their next ohs-libs pin and move to ui/2 then.
  Its wheel is 0.3.0.dev1, not 0.2.0, so it never shares a number with
  ui/1's 0.2.0 wheel that apps already pin; tools/test.sh's lockstep
  check accepts a .devN of a later version, and the release sets it back.

- shack-rig and shack-packet, for Packet Radio 0.3.2 (21/10/2026, held):
  following the radio's own VFO. `shack_rig.datasetup` rule 6: a setting
  the operator changed on the radio during a session is left as they set
  it ("kept") instead of being put back; what was written is journalled so
  crash recovery agrees; `Setting.app_moves` for a satellite pass's VFOs.
  `FakeIcom.turn_dial` (CI-V transceive 00/01). `shack_packet` RigLink
  keys only on the confirmed channel in FM (`off-channel`, `off-mode`,
  also inside the rig's lease, so a dial turned mid-over unkeys; a TM-V71
  band on a memory channel counts as off the channel), with `tuned()`,
  `confirm_channel()`, `tune_channel()` and `vfo_owner` (a satellite pass).

- shack-rig and shack-packet, for Packet Radio 0.3.2 (21/10/2026, held):
  the Kenwood TM-V71. `shack_rig.kenwood` (KenwoodRig on the PC port; its
  data set-up for the band, VFO, FM simplex, EXT. DATA BAND and SPEED, put
  back on close; FakeTmV71), `shack_rig.cm108` (PTT on a CM108 GPIO pin),
  Find my radio's Kenwood probe (TY before ID: ID beacons on a TNC-2), and
  `shack_packet.radios.KenwoodLink` (AFSK 1200 or G3RUH 9600 through a
  sound-card interface on the DATA jack, keyed by RTS/DTR, CM108 or VOX,
  never CAT). Written from LA3QMA's command list, hamlib and CHIRP; not
  yet checked against a radio.

- shack-packet, for Packet Radio 0.3.0 (20/10/2026): `terminal` reads
  what UK nodes and BBSs print (LinBPQ/TheNet node lines, NODES and ROUTES
  listings a line at a time, BPQMail and FBB message lists, BBS and paging
  prompts and what to answer, conference arrivals, departures and /who);
  `netrom.NodeTable` merges NODES broadcasts and listings into a node
  browser's table (best route, best neighbour, heard directly first);
  `simnode` is a
  pretend BPQ node with its BBS (paged listings, R/K/SP) and chat
  conference, and sends NODES broadcasts, for the simulator and tests.

- shack-packet (0.3.0 workstream D, 20/10/2026): files over connected
  mode (FILES.md). The OHS file protocol between OpenHamShack stations
  (`ohssession.py`, `filexfer.py`): frames in the text stream so it passes
  text-only nodes, HELLO before anything is sent, OFFER/ACCEPT with the
  offset held, DEFLATE when it helps, resume by SHA-256 across reconnects
  and restarts, the whole file hashed at the end, safe received names,
  evidently encrypted files refused (licence clause 11(2)); TEXTZ for long
  text once both ends say so. YAPP and YAPP-C (`yapp.py`, from F6FBB's
  yapp.c) for other software, untried against a real program yet. 7PLUS
  encode/decode (`sevenplus.py`), matched against 7PLUS 2.25 built from
  its source.
- shack-packet (0.3.0 workstream E, 20/10/2026): Winlink B2F (`b2f.py`)
  with LZHUF ported from wl2k-go (`lzhuf.py`, MIT; byte-exact against its
  vectors) and the challenge-response login; a station's store-and-forward
  mail client (`stationmail.py`) and FBB forwarding into the personal
  mailbox; APRS extras: NMEA/gpsd (`nmea.py`), an outbound APRS-IS client
  and iGate rules (`aprsis.py`), weather-station readers (`aprswx.py`),
  object/item/weather/telemetry formats in `aprs.py`, and their schedule
  (`aprs_services.py`). Simulator and fakes only so far.
- shack-packet (new): the packet radio modem, step 1 of the Packet Radio
  app (Brian, 17/10/2026), shared with the Icom app's APRS window and the
  SDR monitor. A new receiver (two front ends, per-tone AGC, seven twist
  slicers, a second-order bit clock): 61 frames on direwolf's
  `gen_packets -n 30` ramps at five amplitudes where direwolf 1.7 gets 54
  and the Icom demodulator it started from 36; 559 of 600 on our own
  noise-and-twist set (Icom: 13); on the WA8LMF TNC Test CD 1008/988
  (tracks 1/2) against direwolf's 1005/983 (1015/1002 with `-P E+`).
  Bell 202 transmit at any rate, the AX.25 frame codec for every frame
  type at modulo 8 and 128, FX.25 both ways (all eleven codes, falling
  back to plain AX.25), APRS payloads from the Icom app with keys instead
  of English. Interop with direwolf tested both ways. About 20 ms CPU per
  second of audio per channel.
- shack-packet: the packet engine, step 2 of the Packet Radio app - no GUI
  and no radio, so the desktop app runs it now and the controller can run
  it later for a BBS or node. p-persistent CSMA on our new DCD or the
  radio's busy; AX.25 2.2 connected mode after the SDL (modulo 8 and 128,
  XID, REJ, RNR flow control, the 2.2 segmenter, digipeater paths); a
  digipeater (off by default); an APRS station (beacons with floors and
  SmartBeaconing, messaging with retry back-off, the heard table); a
  personal mailbox (off by default); IP over AX.25 with ARP and an
  encryption guard for the UK licence's clause 11(2). Nothing transmits
  without a callsign and the transmit switch, read at keying. A simulated
  channel (real audio or frames) for N stations; 100 kB at 938 bit/s
  clean on 1200 baud, 655 with 5 % loss and a beaconing third station.
  Live connected-mode sessions against direwolf 1.7, both ways, both
  moduli.
- shack-packet: the radio links, step 3 of the Packet Radio app
  (`shack_packet.radios`): `IcomLink` (IC-9700 over LAN or USB, IC-705 over
  USB or Wi-Fi, through IcomRig), `YaesuLink` (FTX-1 over USB, through the
  new YaesuRig) and `SoundcardLink` (any radio on a sound card, PTT by
  RTS/DTR, VOX with a warning, or receive-only). One safety core for all
  three: the station checked at keying and every 10 ms while keyed, a hard
  15 s key-down watchdog, unkey on any error, disconnect or close (owed and
  retried when it cannot be sent), audio paced into the radio and the unkey
  held until it has played, and a claim seam for the broker. Rig links key
  under a lease that is the same test, so the rig unkeys on its own if the
  link were stuck. Opening sets the radio up for packet (FM with DATA,
  simplex, tones off, the data source on USB/LAN, the squelch as busy) and
  closing puts it back. `radios.turnaround.measure` times a radio's key-up
  and unkey by its own output meter or a monitor receiver and gives the
  TXDELAY to use, only with the operator's confirmation. Tested against
  fakes only; nothing has been keyed on a real radio.
- shack-rig: for the packet links. Icom: the squelch as carrier sense
  (15 01, polled only when asked), simplex and tones off, DATA MOD read,
  written from the model table only where Icom's guide sources it (the
  IC-9700's) and put back; USB transmit audio (TRANSPORTS.md stage three:
  12 kHz in, the codec's own rate out by an exact integer factor, via aplay
  on the appliance or PortAudio on a desktop; off unless asked for, so the
  Icom app's USB radios are unchanged); PortAudio receive and a thread
  reader for a desktop's Icom on a cable. A Yaesu backend, FTX-1 first,
  from its CAT manual (FT-991A from hamlib, unverified), with a fake.
  `shack_rig.sound`: sound devices by name on any OS at their own rate.
  Serial ports: PTT lines raised only by `set_line`, two stop bits, and
  pyserial on Windows opened with RTS/DTR already low.
- shack-pad: the AKP03E gets a keep-alive (`CRT\0\0CONNECT` every 10 s,
  as mirajazz/OpenDeck send). Without it the pad dropped its connection
  about every 39 s (Ray's, Windows, NB 0.1.20), and each drop blanked and
  re-woke every key: the whole pad flickered. A face that fails to draw no
  longer ends the device's loop, and a desktop pad whose loop ends is
  reopened (and so re-woken) at most once per OPEN_RETRY.
- shack-cw: the skimmer runs both CW engines - the network with the
  language model in its search on every channel, the classic decoder on
  the strongest 64 (CPU: 256 channels at 72% on ohs-box) - and spots a call only
  when it passes a callsign check and has been seen twice (Brian,
  14/10/2026). `Skimmer(engine="both")` is the default: the neural Bank
  and the classic decoder share one gather and print their own words,
  judged apart. A call is confirmed by two decodes (either engine) if the
  callsign list holds it - the RBN week shipped in `nn/cwlm.npz`, plus
  MASTER.SCP if the operator has one (`shack_cw.skim.scp`; never bundled)
  - and by three if it only has a call's shape and DXCC entity. On 57
  RBN-labelled recordings (6.6 h, never the test or dev sets): 70 RBN
  calls spotted against 52 for 0.1.21-beta.1's classic rule, with the
  same precision (77 % of valid spots are calls the RBN heard that day,
  73 % before). A listed call no longer confirms on one decode.
- shack-cw: a neural CW engine, optional (`Decoder(engine="nn")`,
  `Skimmer(engine="nn")`; the classic engine stays the default). Our own
  72 k-parameter causal TCN with a CTC head behind the classic front end
  and squelch, NumPy only at run time, weights in the package
  (`shack_cw/nn/cwnn.npz`), trained on synthetic bands with
  `tools/cw_neural`. On the decoder bench's real clips it copies 23 of 78
  RBN calls as words (classic 16), NCDXF beacons at 25 % CER (classic
  32 %), and prints nothing on noise; a skimmer channel costs 0.06-0.09 %
  of a core in a bank of 64, about half the classic back end or less.
- shack-cw neural engine, round 2 (shipped weights unchanged): weights
  choose their front end (`v2`: an 80 ms window and bins beside the
  tone) and may look ahead; `Ensemble` (weights joined by `+`); an
  optional space model (`Bank.space_lm`, off). Training keeps the front
  end's sums, can fine-tune round 1's lineage into v2, and has a
  co-channel synthetic profile after a weaker station on the same
  frequency was found to wreck the network where the classic engine read
  every letter. `NnDecoder.wpm` / `.locked` no longer raise before the
  first 150 ms. None of it reached the M1 gate on the bench (README).
- shack-cw neural engine: a CW language model in the search
  (`nn.lm`; `Decoder(engine="nn", lm=0.5)`, `Bank.lm`; off by default).
  A streaming CTC beam search with a character n-gram built from real
  agreed CW text and a week of RBN calls (`tools/cw_lm`). On the CW
  survey's held-out lines it copies 127 of 341 RBN calls as words where
  the greedy reading copies 106 (134 against 119 after re-spacing),
  on the dev set 55 of 128 against 49; 0.02 % of a core more a channel,
  characters up to 0.5 s later.
- shack-cw: no Morse from noise. The squelch judges the last 2.5 s of the
  tone over the floor (`snr_db`, default 6 dB) instead of a peak hold that
  static and key clicks cleared; the floor comes from the quieter side and
  its 30th percentile, so a keyed neighbour or a weak signal's own marks
  cannot lift it; Hann-weighted frames and a click test keep a strong
  station a few hundred Hz away out. One or two marks are never locked on,
  and polarity needs more evidence. A latent infinite recursion in
  re-learning after a polarity turn is fixed. `shack_cw.corpus` scores the
  decoder on recordings of the real bands (tools/cw_corpus.py records
  them from KiwiSDRs and annotates them from the RBN); the bench gained
  static, keyed-neighbour, weighting, flutter and two-station cases.
- shack-cw: the CW skimmer (`shack_cw.skim`). It runs one STFT over a
  band's CW segment, tracks every keyed signal (carriers and birdies kept
  out), gives a `Decoder` to each through `feed_frames` (64 channels by
  default, 256 at most), and makes CW Skimmer-style spots: validated calls,
  CQ/DX/BEACON, SNR in 500 Hz, busted calls held back. It runs in a child
  process that never blocks receive. `shack_cw.amplitudes` is the decoder's
  frame step as one function, shared by `Decoder.feed` and the skimmer.
  `python3 -m shack_cw.skim.bench` measures CPU and scores the skimmer on
  synthetic bands, the Kiwi corpus and ohs-box's 20 m recording against
  the RBN.
- shack-pad: finds the AKP03E on Windows/macOS by its vendor collection
  (usage page 0xFFA0, usage 1, as mirajazz/opendeck-akp03), not just
  interface 0, so a keyboard or media-key collection is never opened as
  the pad; `diagnose` (survey, errors in words, a copyable report, the
  makers' software running, report sizes from the descriptor, the
  first-revision AKP03E 0300:1002 named and refused); `InputHost` test
  mode and event listeners; `DesktopPad.status()`, and a missing or
  refused pad is looked for every 2 s rather than every 250 ms.
- shack-rec (new): recordings on the controller. Receive audio as FLAC
  from our own numpy encoder (fixed and LPC predictors, 36-50 % of WAV on
  SSB-band noise), a JSON sidecar with the station, frequency, mode and
  their changes; the recordings folder with a disk-use limit (oldest
  pruned, never a running one) and a free-space floor (507 low_disk); range
  requests; SigMF IQ recordings listed beside audio. One API for every app
  with recordings (python/shack_rec/README.md).
- shack-dsp (new): NR2, the spectral noise reduction (minimum statistics,
  decision-directed + TSNR a-priori SNR, LSA gain, gain floor) the Icom and
  Flex apps run on the receive audio people hear; `Nr2Stream` switches it
  without a click or a lost sample; bench for SNR, CW decode, edges, CPU.
- Band-plan dataset 2026.10.1, converted from openhamshack-nb's ohscore/licences
  and ohsradio/plans and the box's QO-100 guard; UK `checked`, 15 other
  regimes `researched`; 3 cm and LF/MF missing from the plans, marked as
  gaps.
- Band-plan dataset 2026.10.2: `rsgb-2026` and `iaru-r1` are complete
  from 2200 m to 3 cm (`partial: false`, no gaps). The 2200 m, 630 m and
  3 cm rows, and RSGB's missing 13/9/6 cm rows, were transcribed from the
  PDFs into data/bandplan-overrides, which the export applies; 2321-2322
  MHz is 20 kHz, not 25 kHz. New beacon and satellite rows refuse where
  the OpenHamShack guard allowed (listed in test_conformance.py INTENDED).
  Plans gain `unplanned` (the source prints nothing there: a
  `plan_unplanned` warning, not partial) and `advisories` (RSGB 630 m
  Note-1: a `plan_advice` warning, never a refusal).
- Band-plan dataset 2026.10.3: re-exported from openhamshack-nb 3f683ab and
  openhamshack 01d6e59 (both main on 09/10), the overrides applied as before.
  The only change is wording: 28 note and refusal sentences in 15 regimes
  say "OpenHamShack" (and "OpenHamShack Radio") where they said "OpenHamShack"
  ("OpenHamShack HAM"), after the NB session's rename. No limit, class, row or
  status changed; the line-by-line country checks (docs/ham/countries/
  *-check.md) were already in the 2026.10.2 sources. Four new vectors pin
  "unconfirmed = refuse" (BE 2 m, CA 5850 MHz, IT 4 m channel) and that it
  refuses only its own row (CA 5760 MHz still allowed).
- shack-bandplan: `Signal.signal_bw_hz` (vectors: `signalBwHz`), one
  signal's own width when it is known but not where inside its edges it
  sits. The plans' bandwidth limits judge it; positions and the licence
  still judge the edges. openhamshack-nb's guards had this and the port did
  not, so an app switching to the library would have refused WSJT-X's
  FT8 (55 Hz) in every 500 Hz segment because its 3 kHz dial window is
  wider. Additive: None keeps the old answer.
- shack-bandplan: the transmit check, proved equal to the OpenHamShack guards
  on a grid of every row edge, class and QO-100 symbol rate.
- shack-client: local store, sync (vectors T1-T21), discovery, pairing,
  reference controller, controller-side sync service.
- shack-i18n: the check for Python, Kotlin, Swift, JS, HTML and Android
  XML; catalog checks; allow-list; frequency and number formatting.
- shack-cw: the CW decoder, Morse tables and bench, moved from openhamshack-nb
  main 47be138 (ohscore/modes/cw.py decoder, ohscore/dsp/morse.py,
  tools/cw_bench.py) with its tests. `feed(samples, rate)` takes any sample
  rate and returns the events; at 12 kHz the bench prints NB's table
  exactly. The first package with a dependency (numpy).
