# Pyrrhic-rs

`pyrrhic-rs` is a library for use in chess engines to probe the Syzygy endgame tablesbases during a search.

## Usage
Pyrrhic's original API is unsafe, with potential for memory corruption if used improperly. Therefore `pyrrhic-rs` wraps this unsafe API in the `TableBases` struct, which guards against memory- and thread-unsafe usage of the Pyrrhic API.


As `pyrrhic-rs` is designed to be used within an existing engine, the user must implement the `EngineAdapter` trait on a type for the probing code to be able to use the engine's own move generation code. Afterwards, `Tablebases::new()` can be called using this type as a parameter.

### Example using `cozy_chess`:

```rust
use cozy_chess::*;

struct CozyChessAdapter;

impl EngineAdapter for CozyChessAdapter {
    fn pawn_attacks(color: pyrrhic_rs::Color, sq: u64) -> u64 {
        let attacks = get_pawn_attacks(
            Square::index(sq as usize),
            if color == pyrrhic_rs::Color::Black {
                Color::Black
            } else {
                Color::White
            },
        );
        attacks.0
    }
    fn knight_attacks(sq: u64) -> u64 {
        get_knight_moves(Square::index(sq as usize)).0
    }
    fn bishop_attacks(sq: u64, occ: u64) -> u64 {
        get_bishop_moves(Square::index(sq as usize), BitBoard(occ)).0
    }
    fn rook_attacks(sq: u64, occ: u64) -> u64 {
        get_rook_moves(Square::index(sq as usize), BitBoard(occ)).0
    }
    fn king_attacks(sq: u64) -> u64 {
        get_king_moves(Square::index(sq as usize)).0
    }
    fn queen_attacks(sq: u64, occ: u64) -> u64 {
        (get_bishop_moves(Square::index(sq as usize), BitBoard(occ))
            | get_rook_moves(Square::index(sq as usize), BitBoard(occ)))
        .0
    }
}

fn main() {
    let tb = pyrrhic_rs::TableBases::<CozyChessAdapter>::new("./syzygy/tb345:./syzygy/tb6:./syzygy/tb7").unwrap();
}
```

## Copyright
`pyrrhic-rs` was initially transliterated from the original [Pyrrhic](https://github.com/AndyGrant/Pyrrhic) library in C, and is therefore subject to the following copyrights:

- [Fathom](https://github.com/basil00/Fathom) © 2015 basil, all rights reserved
- Modifications Copyright © 2016-2019 by Jon Dart
- Modifications Copyright © 2020-2020 by Andrew Grant

## Acknowledgments
- Ronald "Syzygy" de Man, creator of the Syzygy tablebases
- [C2Rust](https://github.com/immunant/c2rust), used to initally translate the C code into Rust code