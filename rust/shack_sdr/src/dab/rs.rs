//! DAB+'s outer code (TS 102 563 §6.1): Reed-Solomon RS(120, 110, t = 5),
//! shortened from RS(255, 245) over GF(2^8) with P(x) = x^8 + x^4 + x^3 +
//! x^2 + 1, alpha = 2, generator roots alpha^0..alpha^9. Encoding for the
//! transmitter model; decoding by syndromes, Berlekamp-Massey, a Chien
//! search and Forney's formula. Written from the specification and the
//! textbook algorithm; nothing here is ported.

const NROOTS: usize = 10;

struct Gf {
    exp: [u8; 512],
    log: [u8; 256],
}

impl Gf {
    fn new() -> Gf {
        let mut exp = [0u8; 512];
        let mut log = [0u8; 256];
        let mut x: u16 = 1;
        for i in 0..255 {
            exp[i] = x as u8;
            log[x as usize] = i as u8;
            x <<= 1;
            if x & 0x100 != 0 {
                x ^= 0x11d;
            }
        }
        for i in 255..512 {
            exp[i] = exp[i - 255];
        }
        Gf { exp, log }
    }
    fn mul(&self, a: u8, b: u8) -> u8 {
        if a == 0 || b == 0 {
            0
        } else {
            self.exp[self.log[a as usize] as usize + self.log[b as usize] as usize]
        }
    }
    fn div(&self, a: u8, b: u8) -> u8 {
        if a == 0 {
            0
        } else {
            self.exp[(self.log[a as usize] as usize + 255 - self.log[b as usize] as usize) % 255]
        }
    }
    fn pow(&self, i: usize) -> u8 {
        self.exp[i % 255]
    }
}

fn gf() -> &'static Gf {
    static G: std::sync::OnceLock<Gf> = std::sync::OnceLock::new();
    G.get_or_init(Gf::new)
}

/// g(x) = prod (x + alpha^i), i = 0..9, highest degree first.
fn generator() -> Vec<u8> {
    let g = gf();
    let mut p = vec![1u8];
    for i in 0..NROOTS {
        let a = g.pow(i);
        let mut q = vec![0u8; p.len() + 1];
        for (j, &c) in p.iter().enumerate() {
            q[j] ^= c;
            q[j + 1] ^= g.mul(c, a);
        }
        p = q;
    }
    p
}

/// The 10 parity bytes for `data` (up to 245 bytes; DAB+ uses 110).
pub fn parity(data: &[u8]) -> [u8; NROOTS] {
    let g = gf();
    let gen = generator();
    let mut r = [0u8; NROOTS];
    for &d in data {
        let fb = d ^ r[0];
        r.copy_within(1.., 0);
        r[NROOTS - 1] = 0;
        if fb != 0 {
            for j in 0..NROOTS {
                r[j] ^= g.mul(fb, gen[j + 1]);
            }
        }
    }
    r
}

/// Correct `word` (data then 10 parity bytes, up to 255 bytes) in place.
/// Ok(bytes corrected), or Err when there are more errors than it can
/// correct (it then leaves the word as it was).
pub fn correct(word: &mut [u8]) -> Result<usize, ()> {
    let g = gf();
    let n = word.len();
    // Syndromes: S_j = r(alpha^j), the first byte the highest power.
    let mut s = [0u8; NROOTS];
    for (j, sj) in s.iter_mut().enumerate() {
        let a = g.pow(j);
        let mut acc = 0u8;
        for &b in word.iter() {
            acc = g.mul(acc, a) ^ b;
        }
        *sj = acc;
    }
    if s.iter().all(|&x| x == 0) {
        return Ok(0);
    }
    // Berlekamp-Massey: the error locator, lowest degree first.
    let mut lambda = vec![0u8; NROOTS + 1];
    lambda[0] = 1;
    let mut b = lambda.clone();
    let mut l = 0usize;
    let mut m = 1usize;
    let mut bb = 1u8;
    for r in 0..NROOTS {
        let mut d = s[r];
        for i in 1..=l {
            d ^= g.mul(lambda[i], s[r - i]);
        }
        if d == 0 {
            m += 1;
            continue;
        }
        let coef = g.div(d, bb);
        let t = lambda.clone();
        for i in 0..=NROOTS {
            if i >= m && b[i - m] != 0 {
                lambda[i] ^= g.mul(coef, b[i - m]);
            }
        }
        if 2 * l <= r {
            l = r + 1 - l;
            b = t;
            bb = d;
            m = 1;
        } else {
            m += 1;
        }
    }
    if l > NROOTS / 2 {
        return Err(());
    }
    // Chien search over the word's positions: byte k (from the start) is
    // the coefficient of x^(n-1-k); an error there has locator X = alpha^(n-1-k).
    let mut pos = Vec::new();
    for k in 0..n {
        let e = n - 1 - k;
        let xinv = g.pow(255 - e % 255);
        let mut v = 0u8;
        let mut p = 1u8;
        for &c in lambda.iter().take(l + 1) {
            v ^= g.mul(c, p);
            p = g.mul(p, xinv);
        }
        if v == 0 {
            pos.push(k);
        }
    }
    if pos.len() != l {
        return Err(());
    }
    // Forney: Omega = S(x) Lambda(x) mod x^NROOTS; e = X Omega(1/X) / Lambda'(1/X)
    // with the first root alpha^0.
    let mut omega = [0u8; NROOTS];
    for i in 0..NROOTS {
        let mut acc = 0u8;
        for j in 0..=i.min(l) {
            acc ^= g.mul(lambda[j], s[i - j]);
        }
        omega[i] = acc;
    }
    let mut fixes = Vec::with_capacity(pos.len());
    for &k in &pos {
        let e = n - 1 - k;
        let x = g.pow(e);
        let xinv = g.pow(255 - e % 255);
        let mut num = 0u8;
        let mut p = 1u8;
        for &c in &omega {
            num ^= g.mul(c, p);
            p = g.mul(p, xinv);
        }
        // Lambda'(x): odd terms only, in characteristic 2.
        let mut den = 0u8;
        let mut p = 1u8;
        for i in (1..=l).step_by(2) {
            den ^= g.mul(lambda[i], p);
            p = g.mul(p, g.mul(xinv, xinv));
        }
        if den == 0 {
            return Err(());
        }
        fixes.push((k, g.mul(x, g.div(num, den))));
    }
    for (k, v) in &fixes {
        word[*k] ^= v;
    }
    Ok(fixes.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn word(seed: u64) -> Vec<u8> {
        let mut rng = crate::testsig::Rng::new(seed);
        let data: Vec<u8> = (0..110).map(|_| rng.next_u64() as u8).collect();
        let mut w = data.clone();
        w.extend_from_slice(&parity(&data));
        w
    }

    #[test]
    fn a_clean_word_has_no_syndrome() {
        let mut w = word(1);
        assert_eq!(correct(&mut w), Ok(0));
    }

    #[test]
    fn up_to_five_bytes_are_corrected_and_six_refused() {
        let mut rng = crate::testsig::Rng::new(9);
        for trial in 0..200 {
            let good = word(trial);
            let nerr = 1 + trial as usize % 6;
            let mut w = good.clone();
            let mut at: Vec<usize> = Vec::new();
            while at.len() < nerr {
                let k = (rng.next_u64() % 120) as usize;
                if !at.contains(&k) {
                    at.push(k);
                    w[k] ^= 1 + (rng.next_u64() % 255) as u8;
                }
            }
            let r = correct(&mut w);
            if nerr <= 5 {
                assert_eq!(r, Ok(nerr), "trial {trial}");
                assert_eq!(w, good);
            } else {
                // Six errors are past t = 5: refused (or, rarely, miscorrected
                // to another codeword; never claimed as the original).
                assert!(r.is_err() || w != good, "trial {trial}");
            }
        }
    }
}
