use arhanpassant::{bench, datagen, nnue::Network, perft, uci::Uci, Position};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

fn flag(args: &[String], name: &str) -> Option<String> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned()
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("bench") => {
            let depth = args.get(1).and_then(|d| d.parse().ok()).unwrap_or(bench::DEFAULT_DEPTH);
            bench::run(depth, Network::embedded().map(Arc::new));
        }
        Some("perft") => {
            let depth: u32 = args.get(1).and_then(|d| d.parse().ok()).unwrap_or(5);
            let fen = if args.len() > 2 { args[2..].join(" ") } else { arhanpassant::START_FEN.to_string() };
            let pos = Position::from_fen(&fen).unwrap_or_else(|e| {
                eprintln!("{e}");
                std::process::exit(2)
            });
            let t = Instant::now();
            let n = perft(&pos, depth);
            let s = t.elapsed().as_secs_f64();
            println!("nodes {n} time {s:.3}s nps {:.0}", n as f64 / s);
        }
        Some("datagen") => {
            let num = |name: &str, default: u64| flag(&args, name).and_then(|v| v.parse().ok()).unwrap_or(default);
            let network = match flag(&args, "--net") {
                Some(path) => Some(Arc::new(Network::load(&path).unwrap_or_else(|e| {
                    eprintln!("{e}");
                    std::process::exit(2)
                }))),
                None => Network::embedded().map(Arc::new),
            };
            let hours: f64 = flag(&args, "--hours").and_then(|v| v.parse().ok()).unwrap_or(0.0);
            let cfg = datagen::Config {
                threads: num("--threads", 1) as usize,
                games: num("--games", 0),
                soft_nodes: num("--nodes", 5000),
                hard_nodes: num("--hard-nodes", 100_000),
                random_plies: num("--random-plies", 8) as usize,
                out_dir: PathBuf::from(flag(&args, "--out").unwrap_or_else(|| "data".into())),
                seed: num("--seed", 1),
                hash_mb: num("--hash", 16) as usize,
                network,
                duration: (hours > 0.0).then(|| Duration::from_secs_f64(hours * 3600.0)),
            };
            if let Err(e) = datagen::run(cfg) {
                eprintln!("datagen failed: {e}");
                std::process::exit(1);
            }
        }
        Some("state") => {
            // `state <fen|startpos> [uci moves...]`: the position as JSON (legal moves with SAN, status).
            let fen = args.get(1).map_or("startpos", String::as_str);
            println!("{}", arhanpassant::game::state_json(fen, &args[2.min(args.len())..].join(" ")));
        }
        Some("dump") => {
            // Print records of a datagen file: placement, side to move, score, result.
            let path = args.get(1).expect("dump FILE [N]");
            let n: usize = args.get(2).and_then(|v| v.parse().ok()).unwrap_or(10);
            let bytes = std::fs::read(path).expect("read data file");
            for chunk in bytes.as_chunks::<{ datagen::RECORD_SIZE }>().0.iter().take(n) {
                let (placement, score, result, stm) = datagen::decode(chunk);
                let side = if stm == arhanpassant::Color::White { "w" } else { "b" };
                println!("{placement} {side} {score} {result}");
            }
        }
        _ => Uci::new().run(),
    }
}
