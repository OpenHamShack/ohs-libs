//! The DAB receiver's parts on the transmitter model's IQ, below the
//! Decoder: sync, the FIC's ensemble, and each service's sub-channel.

use shack_sdr::dab::audio::{Mp2Stream, Superframes};
use shack_sdr::dab::fic::{Ensemble, FicDecoder};
use shack_sdr::dab::msc::SubchannelDecoder;
use shack_sdr::dab::ofdm::{Demod, Event};
use shack_sdr::dab::tables::{CIF_SYMBOLS, FIC_CODED_BITS};
use shack_sdr::dab::tx::{dab_iq, Channel, TxEnsemble};

#[test]
fn a_clean_ensemble_is_heard_whole() {
    let ens = TxEnsemble::standard();
    let iq = dab_iq(&ens, 30, &Channel::default());
    let mut demod = Demod::new();
    let mut fic = FicDecoder::new();
    let mut db = Ensemble::default();
    // Demodulate every MSC symbol.
    let mask = [true; CIF_SYMBOLS];
    let mut frames = 0;
    let mut subs: Vec<Option<SubchannelDecoder>> = vec![None, None];
    let mut mp2 = Mp2Stream::new();
    let mut sf = Superframes::new(64);
    let mut aus = 0;
    let mut pcm = 0;
    for chunk in iq.chunks(50_000) {
        demod.push(chunk);
        while let Some(ev) = demod.next(&mask) {
            let Event::Frame(f) = ev else { continue };
            frames += 1;
            for c in 0..4 {
                for fib in fic.decode(&f.fic[c * FIC_CODED_BITS..(c + 1) * FIC_CODED_BITS]) {
                    db.parse_fib(&fib);
                }
            }
            for (i, s) in ens.services.iter().enumerate() {
                if subs[i].is_none() {
                    if let Some(sc) = db.subchannels.get(&s.subch) {
                        subs[i] = SubchannelDecoder::new(sc.clone());
                    }
                }
            }
            for cif in &f.cifs {
                if let Some(d) = subs[0].as_mut() {
                    if let Some(lf) = d.push_cif(cif) {
                        pcm += mp2.push(&lf).len();
                    }
                }
                if let Some(d) = subs[1].as_mut() {
                    if let Some(lf) = d.push_cif(cif) {
                        if let Some(o) = sf.push(&lf) {
                            aus += o.aus.len();
                            assert_eq!(o.au_errors, 0);
                        }
                    }
                }
            }
        }
    }
    eprintln!("frames {frames} fibs ok {} bad {} offset {:.1} Hz mp2 frames {} errors {} aus {aus}", fic.fibs_ok, fic.fibs_bad, demod.offset_hz, mp2.frames, mp2.errors);
    assert!(frames >= 28, "{frames}");
    assert_eq!(fic.fibs_bad, 0);
    assert_eq!(db.eid, Some(0xc1ce));
    assert_eq!(db.label.as_deref(), Some("Shack Test Mux"));
    assert_eq!(db.services[&0xc221].label.as_deref(), Some("Shack MP2 Tone"));
    assert_eq!(db.services[&0xc3a7].audio().unwrap().ascty, 63);
    assert!(pcm > 80, "{pcm}");
    assert_eq!(mp2.errors, 0);
    assert!(aus > 40, "{aus}");
}

