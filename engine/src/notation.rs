//! Standard Algebraic Notation (SAN): `Nf3`, `exd5`, `O-O`, `e8=Q+`, `Rad1#`.

use crate::position::Position;
use crate::types::*;

impl Position {
    /// SAN for a legal move in this position.
    pub fn san(&self, m: Move) -> String {
        let mut s = String::with_capacity(8);
        if m.is_castle() {
            s.push_str(if m.flags() == flag::KING_CASTLE { "O-O" } else { "O-O-O" });
        } else {
            let pt = self.moved_piece(m).piece_type();
            let to = square_name(m.to());
            if pt == PieceType::Pawn {
                if m.is_capture() {
                    s.push((b'a' + file_of(m.from())) as char);
                    s.push('x');
                }
                s.push_str(&to);
                if let Some(p) = m.promotion() {
                    s.push('=');
                    s.push(p.to_char().to_ascii_uppercase());
                }
            } else {
                s.push(pt.to_char().to_ascii_uppercase());
                // Disambiguate against other pieces of the same type that can reach `to`.
                let rivals: Vec<Move> = self
                    .legal_moves()
                    .iter()
                    .filter(|o| *o != m && o.to() == m.to() && self.moved_piece(*o).piece_type() == pt)
                    .collect();
                if !rivals.is_empty() {
                    let same_file = rivals.iter().any(|o| file_of(o.from()) == file_of(m.from()));
                    let same_rank = rivals.iter().any(|o| rank_of(o.from()) == rank_of(m.from()));
                    if !same_file {
                        s.push((b'a' + file_of(m.from())) as char);
                    } else if !same_rank {
                        s.push((b'1' + rank_of(m.from())) as char);
                    } else {
                        s.push_str(&square_name(m.from()));
                    }
                }
                if m.is_capture() {
                    s.push('x');
                }
                s.push_str(&to);
            }
        }
        let after = self.after(m);
        if after.in_check() {
            s.push(if after.legal_moves().is_empty() { '#' } else { '+' });
        }
        s
    }

    /// Parse SAN (tolerates missing or extra `+`/`#`, annotations like `!?`, and `0-0`).
    pub fn parse_san(&self, text: &str) -> Option<Move> {
        let clean = |t: &str| -> String {
            t.trim()
                .trim_end_matches(['+', '#', '!', '?'])
                .replace("0-0-0", "O-O-O")
                .replace("0-0", "O-O")
                .replace("e.p.", "")
                .trim()
                .to_string()
        };
        let want = clean(text);
        self.legal_moves().iter().find(|&m| clean(&self.san(m)) == want)
    }
}

#[cfg(test)]
mod tests {
    use crate::Position;

    fn san_of(fen: &str, uci: &str) -> String {
        let p = Position::from_fen(fen).unwrap();
        p.san(p.parse_uci_move(uci).unwrap())
    }

    #[test]
    fn basics() {
        let start = crate::START_FEN;
        assert_eq!(san_of(start, "g1f3"), "Nf3");
        assert_eq!(san_of(start, "e2e4"), "e4");
        // Castling, captures, promotion with check.
        assert_eq!(san_of("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"), "O-O");
        assert_eq!(san_of("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1c1"), "O-O-O");
        assert_eq!(san_of("4k3/8/8/3p4/4P3/8/8/4K3 w - - 0 1", "e4d5"), "exd5");
        assert_eq!(san_of("8/4P3/8/8/8/8/8/k3K3 w - - 0 1", "e7e8q"), "e8=Q");
        assert_eq!(san_of("k7/4P3/8/8/8/8/8/4K3 w - - 0 1", "e7e8q"), "e8=Q+");
        // Mate.
        assert_eq!(san_of("6k1/5ppp/8/8/8/8/5PPP/3R2K1 w - - 0 1", "d1d8"), "Rd8#");
    }

    #[test]
    fn disambiguation() {
        // Two rooks on the same rank: file letter.
        assert_eq!(san_of("4k3/8/8/8/8/8/8/R4RK1 w - - 0 1", "a1d1"), "Rad1");
        // A blocked rival needs no disambiguation (the king on e1 stops Rh1-d1).
        assert_eq!(san_of("4k3/8/8/8/8/8/8/R3K2R w - - 0 1", "a1d1"), "Rd1");
        // Two knights on the same file: rank digit.
        assert_eq!(san_of("4k3/8/8/1N6/8/1N6/8/4K3 w - - 0 1", "b3d4"), "N3d4");
        // Three queens: full square when file and rank both clash.
        assert_eq!(san_of("5k2/8/8/8/Q6Q/8/8/Q3K3 w - - 0 1", "a4d4"), "Qa4d4");
    }

    #[test]
    fn parse_roundtrip() {
        let p = Position::from_fen("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1").unwrap();
        for m in p.legal_moves().iter() {
            assert_eq!(p.parse_san(&p.san(m)), Some(m), "{}", p.san(m));
        }
        assert_eq!(p.parse_san("0-0").map(|m| m.to_uci()), Some("e1g1".into()));
    }
}
