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
            for pair in args.windows(2).filter(|p| p[0] == "--set") {
                let valid = pair[1].split_once('=')
                    .and_then(|(k, v)| v.parse().ok().map(|v| arhanpassant::params::set(k, v)))
                    .unwrap_or(false);
                if !valid {
                    eprintln!("invalid datagen search setting: {}", pair[1]);
                    std::process::exit(2);
                }
            }
            let num = |name: &str, default: u64| flag(&args, name).and_then(|v| v.parse().ok()).unwrap_or(default);
            let network = match flag(&args, "--net") {
                Some(path) => Some(Arc::new(Network::load(&path).unwrap_or_else(|e| {
                    eprintln!("{e}");
                    std::process::exit(2)
                }))),
                None => Network::embedded().map(Arc::new),
            };
            let hours: f64 = flag(&args, "--hours").and_then(|v| v.parse().ok()).unwrap_or(0.0);
            // `--book FILE`: one FEN (or EPD: first four fields) per line to start games from.
            let book = flag(&args, "--book").map(|path| {
                let text = std::fs::read_to_string(&path).unwrap_or_else(|e| {
                    eprintln!("{path}: {e}");
                    std::process::exit(2)
                });
                let fens: Vec<String> = text
                    .lines()
                    .filter_map(|l| {
                        let f: Vec<&str> = l.split_whitespace().take(6).collect();
                        let fen = if f.len() >= 6 && f[4].parse::<u32>().is_ok() { f.join(" ") } else if f.len() >= 4 { format!("{} 0 1", f[..4].join(" ")) } else { return None };
                        Position::from_fen(&fen).ok().map(|_| fen)
                    })
                    .collect();
                if fens.is_empty() {
                    eprintln!("{path}: no usable positions");
                    std::process::exit(2);
                }
                eprintln!("datagen book: {} positions", fens.len());
                Arc::new(fens)
            });
            let cfg = datagen::Config {
                threads: num("--threads", 1).clamp(1, 512) as usize,
                games: num("--games", 0),
                soft_nodes: num("--nodes", 5000),
                hard_nodes: num("--hard-nodes", 100_000),
                random_plies: num("--random-plies", 8) as usize,
                book,
                out_dir: PathBuf::from(flag(&args, "--out").unwrap_or_else(|| "data".into())),
                seed: num("--seed", 1),
                hash_mb: num("--hash", 16) as usize,
                positions_per_file: num("--positions-per-file", 1_000_000),
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
        Some("rescore") => {
            // `rescore --in GAMES.txt --out FILE.bin [--nodes N] [--threads T] [--skip PLIES] [--net PATH]`
            let num = |name: &str, default: u64| flag(&args, name).and_then(|v| v.parse().ok()).unwrap_or(default);
            let input = flag(&args, "--in").expect("--in GAMES.txt");
            let output = flag(&args, "--out").expect("--out FILE.bin");
            let network = match flag(&args, "--net") {
                Some(path) => Some(Arc::new(Network::load(&path).unwrap_or_else(|e| {
                    eprintln!("{e}");
                    std::process::exit(2)
                }))),
                None => Network::embedded().map(Arc::new),
            };
            let lines: Vec<String> = std::fs::read_to_string(&input).expect("read --in").lines().map(str::to_string).collect();
            let (records, used, bad) = datagen::rescore(&lines, num("--nodes", 5000), num("--threads", 1) as usize, 16, network, num("--skip", 8) as usize);
            let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&output).expect("open --out");
            for r in &records {
                std::io::Write::write_all(&mut f, r).expect("write --out");
            }
            println!("games {used} unreadable {bad} positions {}", records.len());
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
