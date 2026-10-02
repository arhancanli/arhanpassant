//! Game state as JSON, shared by the WebAssembly build (the website) and the
//! `state` command of the native binary (the community game on GitHub).

use crate::Position;

/// A JSON string literal.
pub fn json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            c if (c as u32) < 0x20 => out.push(' '),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// `{"error": msg}`
pub fn json_error(msg: &str) -> String {
    format!("{{\"error\":{}}}", json_str(msg))
}

/// A JSON array of already-encoded items.
pub fn json_list(items: impl Iterator<Item = String>) -> String {
    format!("[{}]", items.collect::<Vec<_>>().join(","))
}

/// Replay `moves` from `fen`; returns the final position, game hashes and SAN history.
pub fn replay(fen: &str, moves: &str) -> Result<(Position, Vec<u64>, Vec<String>), String> {
    let mut pos = if fen == "startpos" || fen.is_empty() {
        Position::startpos()
    } else {
        Position::from_fen(fen).map_err(|e| e.to_string())?
    };
    let mut hashes = vec![pos.hash()];
    let mut sans = Vec::new();
    for u in moves.split_whitespace() {
        let m = pos.parse_uci_move(u).ok_or_else(|| format!("illegal move {u}"))?;
        sans.push(pos.san(m));
        pos.play(m);
        hashes.push(pos.hash());
    }
    Ok((pos, hashes, sans))
}

fn threefold(hashes: &[u64], halfmove: usize) -> bool {
    let n = hashes.len();
    let mut count = 1;
    let mut i = 4;
    while i <= halfmove.min(n - 1) {
        if hashes[n - 1 - i] == hashes[n - 1] {
            count += 1;
        }
        i += 2;
    }
    count >= 3
}

/// Position after `moves` (UCI) from `fen` (or `startpos`) as JSON: FEN, side to
/// move, check, mate, stalemate, draw reason, legal moves with SAN, SAN history.
pub fn state_json(fen: &str, moves: &str) -> String {
    let (pos, hashes, sans) = match replay(fen, moves) {
        Ok(r) => r,
        Err(e) => return json_error(&e),
    };
    let legal = pos.legal_moves();
    let draw = if legal.is_empty() {
        None
    } else if pos.halfmove_clock() >= 100 {
        Some("fifty-move rule")
    } else if pos.is_insufficient_material() {
        Some("insufficient material")
    } else if threefold(&hashes, pos.halfmove_clock() as usize) {
        Some("threefold repetition")
    } else {
        None
    };
    format!(
        "{{\"fen\":{},\"turn\":\"{}\",\"check\":{},\"checkmate\":{},\"stalemate\":{},\"draw\":{},\"legal\":{},\"history\":{}}}",
        json_str(&pos.fen()),
        if pos.side_to_move() == crate::Color::White { "w" } else { "b" },
        pos.in_check(),
        legal.is_empty() && pos.in_check(),
        legal.is_empty() && !pos.in_check(),
        draw.map_or("null".to_string(), json_str),
        json_list(legal.iter().map(|m| format!("[{},{}]", json_str(&m.to_uci()), json_str(&pos.san(m))))),
        json_list(sans.iter().map(|s| json_str(s)))
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reports_mate_repetition_and_errors() {
        let s = state_json("startpos", "f2f3 e7e5 g2g4 d8h4");
        assert!(s.contains("\"checkmate\":true") && s.contains("\"legal\":[]"), "{s}");
        let s = state_json("startpos", "g1f3 g8f6 f3g1 f6g8 g1f3 g8f6 f3g1 f6g8");
        assert!(s.contains("\"draw\":\"threefold repetition\""), "{s}");
        let s = state_json("startpos", "e2e4 e7e5 g1f3");
        assert!(s.contains("[\"b8c6\",\"Nc6\"]") && s.contains("\"history\":[\"e4\",\"e5\",\"Nf3\"]"), "{s}");
        assert!(state_json("startpos", "e2e5").contains("\"error\""));
    }
}
