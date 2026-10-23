//! The code tables the FIC's service information points into (ETSI TS
//! 101 756 and EN 300 401), as the snake_case codes and ISO identifiers
//! the feed carries. No words here: the app turns a code into its own
//! catalog's sentence (the crate's rule), so a programme type is
//! `pop_music` and Radio says "Pop Music".

/// Programme types, ETSI TS 101 756 table 12 (the RDS/DAB European table,
/// International Table Id 1, which every European ensemble signals). Codes
/// 30 and 31 are not used by DAB. North America's RBDS table (Id 2) uses
/// other meanings for the same numbers; we give no code for it rather than
/// a wrong one.
pub const PTY_EUROPE: [&str; 32] = [
    "none", "news", "current_affairs", "information", "sport", "education", "drama", "culture",
    "science", "varied", "pop_music", "rock_music", "easy_listening", "light_classical", "serious_classical",
    "other_music", "weather", "finance", "childrens", "social_affairs", "religion", "phone_in", "travel",
    "leisure", "jazz_music", "country_music", "national_music", "oldies_music", "folk_music", "documentary",
    "", "",
];

/// The programme type's code for an international table, or None.
pub fn pty_code(table: Option<u8>, code: u8) -> Option<&'static str> {
    // An ensemble that never sent FIG 0/9 is taken to use table 1: the
    // only one EN 300 401 ensembles outside North America use.
    match table.unwrap_or(1) {
        1 => PTY_EUROPE.get(code as usize).copied().filter(|s| !s.is_empty()),
        _ => None,
    }
}

/// Languages, ETSI TS 101 756 tables 9 (European, 0x00-0x2B) and 10
/// (others, 0x45-0x7F), as ISO 639-1 codes where one exists (ISO 639-2
/// where not). 0 is "unknown".
pub fn language(code: u8) -> Option<&'static str> {
    const EUROPE: [&str; 0x2c] = [
        "", "sq", "br", "ca", "hr", "cy", "cs", "da", "de", "en", "es", "eo", "et", "eu", "fo", "fr", "fy", "ga",
        "gd", "gl", "is", "it", "se", "la", "lv", "lb", "lt", "hu", "mt", "nl", "no", "oc", "pl", "pt", "ro",
        "rm", "sr", "sk", "sl", "fi", "sv", "tr", "nl-BE", "wa",
    ];
    const OTHERS: [(u8, &str); 59] = [
        (0x45, "zu"), (0x46, "vi"), (0x47, "uz"), (0x48, "ur"), (0x49, "uk"), (0x4a, "th"), (0x4b, "te"),
        (0x4c, "tt"), (0x4d, "ta"), (0x4e, "tg"), (0x4f, "sw"), (0x50, "srn"), (0x51, "so"), (0x52, "si"),
        (0x53, "sn"), (0x54, "sh"), (0x55, "rue"), (0x56, "ru"), (0x57, "qu"), (0x58, "ps"), (0x59, "pa"),
        (0x5a, "fa"), (0x5b, "pap"), (0x5c, "or"), (0x5d, "ne"), (0x5e, "nd"), (0x5f, "mr"), (0x60, "mo"),
        (0x61, "ms"), (0x62, "mg"), (0x63, "mk"), (0x64, "lo"), (0x65, "ko"), (0x66, "km"), (0x67, "kk"),
        (0x68, "kn"), (0x69, "ja"), (0x6a, "id"), (0x6b, "hi"), (0x6c, "he"), (0x6d, "ha"), (0x6e, "gn"),
        (0x6f, "gu"), (0x70, "el"), (0x71, "ka"), (0x72, "ff"), (0x73, "prs"), (0x74, "cv"), (0x75, "zh"),
        (0x76, "my"), (0x77, "bg"), (0x78, "bn"), (0x79, "be"), (0x7a, "bm"), (0x7b, "az"), (0x7c, "as"),
        (0x7d, "hy"), (0x7e, "ar"), (0x7f, "am"),
    ];
    match code {
        0x01..=0x2b => Some(EUROPE[code as usize]),
        _ => OTHERS.iter().find(|o| o.0 == code).map(|o| o.1),
    }
}

/// User application types (ETSI TS 101 756 table 16) we name; others are
/// given as their number.
pub fn user_app(ty: u16) -> Option<&'static str> {
    Some(match ty {
        0x002 => "slideshow",
        0x003 => "bws",
        0x004 => "tpeg",
        0x005 => "dgps",
        0x006 => "tmc",
        0x007 => "spi",
        0x008 => "dab_java",
        0x009 => "dmb",
        0x00a => "ipdc",
        0x00b => "voice",
        0x00c => "middleware",
        0x00d => "filecasting",
        0x44a => "journaline",
        _ => return None,
    })
}

pub const UA_SLIDESHOW: u16 = 0x002;
pub const UA_SPI: u16 = 0x007;

/// Announcement types, ETSI TS 101 756 table 14, by flag bit.
pub const ANNOUNCEMENTS: [&str; 11] = [
    "alarm", "traffic", "transport", "warning", "news", "weather", "event", "special_event", "programme_info",
    "sport", "finance",
];

/// The announcement types a 16-bit ASu or ASw flag field names.
pub fn announcement_types(flags: u16) -> Vec<&'static str> {
    ANNOUNCEMENTS.iter().enumerate().filter(|(i, _)| flags >> i & 1 == 1).map(|(_, s)| *s).collect()
}

/// ISO 3166 country of an Extended Country Code and the country id (the
/// first nibble of the EId or SId), ETSI TS 101 756 tables 3-7: the
/// European ECCs (E0-E4), which is where DAB is broadcast. A pair not
/// listed gives None and the feed keeps the raw ECC.
pub fn country(ecc: u8, cid: u8) -> Option<&'static str> {
    const TABLE: &[(u8, u8, &str)] = &[
        (0xe0, 0x1, "DE"), (0xe0, 0x2, "DZ"), (0xe0, 0x3, "AD"), (0xe0, 0x4, "IL"), (0xe0, 0x5, "IT"),
        (0xe0, 0x6, "BE"), (0xe0, 0x7, "RU"), (0xe0, 0x8, "PS"), (0xe0, 0x9, "AL"), (0xe0, 0xa, "AT"),
        (0xe0, 0xb, "HU"), (0xe0, 0xc, "MT"), (0xe0, 0xd, "DE"), (0xe0, 0xf, "EG"),
        (0xe1, 0x1, "GR"), (0xe1, 0x2, "CY"), (0xe1, 0x3, "SM"), (0xe1, 0x4, "CH"), (0xe1, 0x5, "JO"),
        (0xe1, 0x6, "FI"), (0xe1, 0x7, "LU"), (0xe1, 0x8, "BG"), (0xe1, 0x9, "DK"), (0xe1, 0xa, "GI"),
        (0xe1, 0xb, "IQ"), (0xe1, 0xc, "GB"), (0xe1, 0xd, "LY"), (0xe1, 0xe, "RO"), (0xe1, 0xf, "FR"),
        (0xe2, 0x1, "MA"), (0xe2, 0x2, "CZ"), (0xe2, 0x3, "PL"), (0xe2, 0x4, "VA"), (0xe2, 0x5, "SK"),
        (0xe2, 0x6, "SY"), (0xe2, 0x7, "TN"), (0xe2, 0x9, "LI"), (0xe2, 0xa, "IS"), (0xe2, 0xb, "MC"),
        (0xe2, 0xc, "LT"), (0xe2, 0xe, "ES"), (0xe2, 0xf, "NO"),
        (0xe3, 0x2, "IE"), (0xe3, 0x3, "TR"), (0xe3, 0x8, "NL"), (0xe3, 0x9, "LV"), (0xe3, 0xa, "LB"),
        (0xe3, 0xc, "HR"), (0xe3, 0xe, "SE"),
        (0xe4, 0x2, "EE"), (0xe4, 0x8, "PT"), (0xe4, 0x9, "SI"),
    ];
    TABLE.iter().find(|t| t.0 == ecc && t.1 == cid).map(|t| t.2)
}

/// DL Plus content types (ETSI TS 102 980 Annex A, the RDS RT+ table) that
/// the feed names; the rest are kept by number only.
pub fn dl_plus_type(ct: u8) -> Option<&'static str> {
    Some(match ct {
        1 => "item_title",
        2 => "item_album",
        3 => "item_tracknumber",
        4 => "item_artist",
        5 => "item_composition",
        6 => "item_movement",
        7 => "item_conductor",
        8 => "item_composer",
        9 => "item_band",
        10 => "item_comment",
        11 => "item_genre",
        31 => "stationname_short",
        32 => "stationname_long",
        33 => "programme_now",
        34 => "programme_next",
        35 => "programme_part",
        36 => "programme_host",
        37 => "programme_editorial_staff",
        39 => "programme_homepage",
        _ => return None,
    })
}

/// "C221" (or eight digits for a 32-bit data service SId).
pub fn sid_hex(sid: u32) -> String {
    if sid > 0xffff {
        format!("{sid:08X}")
    } else {
        format!("{sid:04X}")
    }
}

/// The Gregorian date of a Modified Julian Date (EN 300 401 Annex: MJD 0
/// is 1858-11-17).
pub fn mjd_date(mjd: u32) -> (i32, u32, u32) {
    // Days since 1970-01-01, then the civil-from-days algorithm.
    let z = mjd as i64 - 40_587 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    ((y + (m <= 2) as i64) as i32, m, d)
}

/// UTC seconds since 1970 of an MJD and a time of day.
pub fn mjd_utc(mjd: u32, h: u32, m: u32, s: u32) -> f64 {
    (mjd as f64 - 40_587.0) * 86_400.0 + (h * 3600 + m * 60 + s) as f64
}

/// "2026-10-23T12:34:56Z" from UTC seconds.
pub fn iso_utc(t: f64) -> String {
    let secs = t.floor() as i64;
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (y, mo, d) = mjd_date((days + 40_587) as u32);
    format!("{y:04}-{mo:02}-{d:02}T{:02}:{:02}:{:02}Z", rem / 3600, rem / 60 % 60, rem % 60)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mjd_58000_is_the_fourth_of_september_2017() {
        // EN 300 401 §8.1.3.1's own example, and TS 102 371's.
        assert_eq!(mjd_date(58_000), (2017, 9, 4));
        assert_eq!(mjd_date(50_000), (1995, 10, 10));
        assert_eq!(iso_utc(mjd_utc(58_000, 13, 5, 9)), "2017-09-04T13:05:09Z");
    }

    #[test]
    fn codes_follow_the_tables() {
        assert_eq!(pty_code(None, 10), Some("pop_music"));
        assert_eq!(pty_code(Some(2), 10), None);
        assert_eq!(pty_code(Some(1), 30), None);
        assert_eq!(language(0x09), Some("en"));
        assert_eq!(language(0x08), Some("de"));
        assert_eq!(language(0x7e), Some("ar"));
        assert_eq!(language(0), None);
        assert_eq!(country(0xe1, 0xc), Some("GB"));
        assert_eq!(announcement_types(0b10011), vec!["alarm", "traffic", "news"]);
    }
}
