//! Static exchange evaluation: does a capture sequence on one square gain at
//! least `threshold`?

use crate::bitboard::*;
use crate::position::Position;
use crate::types::*;

pub const SEE_VALUE: [i32; 6] = [100, 320, 330, 500, 900, 0];

#[inline(always)]
pub fn see_value(pt: PieceType) -> i32 {
    SEE_VALUE[pt.idx()]
}

/// True when the exchange started by `m` nets at least `threshold` centipawns.
pub fn see_ge(pos: &Position, m: Move, threshold: i32) -> bool {
    // Castling, promotions and en passant: approximate as the immediate gain.
    if m.is_castle() || m.is_promotion() || m.is_ep() {
        let gain = if m.is_ep() { SEE_VALUE[0] } else { 0 };
        return gain >= threshold;
    }
    let from = m.from();
    let to = m.to();
    let mut swap = pos.captured(m).map_or(0, see_value) - threshold;
    if swap < 0 {
        return false;
    }
    swap = see_value(pos.moved_piece(m).piece_type()) - swap;
    if swap <= 0 {
        return true;
    }

    let bishops = pos.type_bb(PieceType::Bishop) | pos.type_bb(PieceType::Queen);
    let rooks = pos.type_bb(PieceType::Rook) | pos.type_bb(PieceType::Queen);
    let mut occ = pos.occupied() ^ bb(from) ^ bb(to);
    let mut attackers = pos.attackers_to(to, occ) & occ;
    let mut stm = pos.side_to_move();
    let mut res = true;

    loop {
        stm = stm.flip();
        attackers &= occ;
        let stm_attackers = attackers & pos.color_bb(stm);
        if stm_attackers == 0 {
            break;
        }
        res = !res;
        // Least valuable attacker first.
        let mut next = None;
        for pt in PieceType::ALL {
            let b = stm_attackers & pos.type_bb(pt);
            if b != 0 {
                next = Some((pt, b));
                break;
            }
        }
        let (pt, b) = next.expect("attacker exists");
        if pt == PieceType::King {
            // The king may only recapture if the other side has nothing left.
            return if attackers & pos.color_bb(stm.flip()) != 0 { !res } else { res };
        }
        swap = see_value(pt) - swap;
        if swap < res as i32 {
            break;
        }
        occ ^= bb(lsb(b));
        match pt {
            PieceType::Pawn | PieceType::Bishop => attackers |= bishop_attacks(to, occ) & bishops,
            PieceType::Rook => attackers |= rook_attacks(to, occ) & rooks,
            PieceType::Queen => attackers |= (bishop_attacks(to, occ) & bishops) | (rook_attacks(to, occ) & rooks),
            _ => {}
        }
    }
    res
}

#[cfg(test)]
mod tests {
    use super::*;

    fn see(fen: &str, uci: &str, t: i32) -> bool {
        let pos = Position::from_fen(fen).unwrap();
        let m = pos.parse_uci_move(uci).unwrap();
        see_ge(&pos, m, t)
    }

    #[test]
    fn exchanges() {
        // Pawn takes undefended knight: +320.
        assert!(see("4k3/8/8/3n4/4P3/8/8/4K3 w - - 0 1", "e4d5", 320));
        assert!(!see("4k3/8/8/3n4/4P3/8/8/4K3 w - - 0 1", "e4d5", 321));
        // Queen takes pawn defended by pawn: loses the queen for a pawn.
        assert!(!see("4k3/8/2p5/3p4/8/8/3Q4/4K3 w - - 0 1", "d2d5", 0));
        // Rook takes rook defended by rook, with our second rook behind (x-ray): even trade.
        assert!(see("3rk3/3r4/8/8/8/8/3R4/3RK3 w - - 0 1", "d2d7", 0));
        assert!(!see("3rk3/3r4/8/8/8/8/3R4/3RK3 w - - 0 1", "d2d7", 1));
    }
}
