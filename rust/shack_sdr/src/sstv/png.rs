//! The finished picture as a PNG: 8-bit RGB, no interlace, the Up filter
//! (SSTV rows are much like the row above, so it roughly halves the size
//! over None), deflated by miniz_oxide. The CRC is ours: a table and
//! eight lines, not worth a dependency.

fn crc32(data: &[&[u8]]) -> u32 {
    static TABLE: std::sync::OnceLock<[u32; 256]> = std::sync::OnceLock::new();
    let t = TABLE.get_or_init(|| {
        let mut t = [0u32; 256];
        for (n, e) in t.iter_mut().enumerate() {
            let mut c = n as u32;
            for _ in 0..8 {
                c = if c & 1 == 1 { 0xedb8_8320 ^ (c >> 1) } else { c >> 1 };
            }
            *e = c;
        }
        t
    });
    let mut c = 0xffff_ffffu32;
    for part in data {
        for &b in *part {
            c = t[((c ^ b as u32) & 0xff) as usize] ^ (c >> 8);
        }
    }
    c ^ 0xffff_ffff
}

fn chunk(out: &mut Vec<u8>, tag: &[u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(tag);
    out.extend_from_slice(data);
    out.extend_from_slice(&crc32(&[tag, data]).to_be_bytes());
}

/// `rgb` is `width` x `height` x 3, rows top down.
pub fn encode(rgb: &[u8], width: u32, height: u32) -> Vec<u8> {
    let stride = width as usize * 3;
    assert_eq!(rgb.len(), stride * height as usize);
    let mut raw = Vec::with_capacity((stride + 1) * height as usize);
    for y in 0..height as usize {
        let row = &rgb[y * stride..(y + 1) * stride];
        if y == 0 {
            raw.push(0);
            raw.extend_from_slice(row);
        } else {
            let up = &rgb[(y - 1) * stride..y * stride];
            raw.push(2);
            raw.extend(row.iter().zip(up).map(|(a, b)| a.wrapping_sub(*b)));
        }
    }
    let mut out = b"\x89PNG\r\n\x1a\n".to_vec();
    let mut ihdr = Vec::with_capacity(13);
    ihdr.extend_from_slice(&width.to_be_bytes());
    ihdr.extend_from_slice(&height.to_be_bytes());
    ihdr.extend_from_slice(&[8, 2, 0, 0, 0]);
    chunk(&mut out, b"IHDR", &ihdr);
    chunk(&mut out, b"IDAT", &miniz_oxide::deflate::compress_to_vec_zlib(&raw, 6));
    chunk(&mut out, b"IEND", &[]);
    out
}

/// Read back what `encode` wrote (any filter, one or more IDATs): for the
/// tests and the score, not a general PNG reader.
pub fn decode(png: &[u8]) -> Option<(u32, u32, Vec<u8>)> {
    if png.get(..8)? != b"\x89PNG\r\n\x1a\n" {
        return None;
    }
    let (mut pos, mut w, mut h, mut idat) = (8, 0u32, 0u32, Vec::new());
    while pos + 8 <= png.len() {
        let len = u32::from_be_bytes(png[pos..pos + 4].try_into().ok()?) as usize;
        let tag = &png[pos + 4..pos + 8];
        let data = png.get(pos + 8..pos + 8 + len)?;
        match tag {
            b"IHDR" => {
                w = u32::from_be_bytes(data[0..4].try_into().ok()?);
                h = u32::from_be_bytes(data[4..8].try_into().ok()?);
                if data[8] != 8 || data[9] != 2 {
                    return None;
                }
            }
            b"IDAT" => idat.extend_from_slice(data),
            _ => {}
        }
        pos += 12 + len;
    }
    let raw = miniz_oxide::inflate::decompress_to_vec_zlib(&idat).ok()?;
    let stride = w as usize * 3;
    let mut out = vec![0u8; stride * h as usize];
    for y in 0..h as usize {
        let f = raw[y * (stride + 1)];
        let src = &raw[y * (stride + 1) + 1..(y + 1) * (stride + 1)];
        for x in 0..stride {
            let a = if x >= 3 { out[y * stride + x - 3] } else { 0 };
            let b = if y > 0 { out[(y - 1) * stride + x] } else { 0 };
            let c = if x >= 3 && y > 0 { out[(y - 1) * stride + x - 3] } else { 0 };
            let pred = match f {
                0 => 0,
                1 => a,
                2 => b,
                3 => ((a as u16 + b as u16) / 2) as u8,
                4 => {
                    let p = a as i16 + b as i16 - c as i16;
                    let (pa, pb, pc) = ((p - a as i16).abs(), (p - b as i16).abs(), (p - c as i16).abs());
                    if pa <= pb && pa <= pc {
                        a
                    } else if pb <= pc {
                        b
                    } else {
                        c
                    }
                }
                _ => return None,
            };
            out[y * stride + x] = src[x].wrapping_add(pred);
        }
    }
    Some((w, h, out))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_and_checks_its_crc() {
        let (w, h) = (37u32, 11u32);
        let img: Vec<u8> = (0..w * h * 3).map(|i| (i * 7919 % 251) as u8).collect();
        let png = encode(&img, w, h);
        assert_eq!(decode(&png), Some((w, h, img)));
        // The standard check value of CRC-32.
        assert_eq!(crc32(&[b"123456789"]), 0xcbf4_3926);
    }
}
