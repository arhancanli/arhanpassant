//! arena: play two UCI engines against each other in colour-swapped game pairs
//! and decide, with a sequential probability ratio test, whether the first is
//! stronger than the second by the requested margin.
//!
//! arena --engine name=new cmd=./a opt.EvalFile=net.nnue --engine name=base cmd=./b \
//!       --tc 8+0.08 --book books/UHO_4060_v4.epd --concurrency 4 --games 40000 \
//!       --sprt 0,5 --out result.json

mod engine;
mod game;
mod sprt;

use engine::{Engine, EngineSpec};
use game::{play, Adjudication, GameRecord, Outcome, TimeControl};
use std::collections::BTreeMap;
use std::io::Write;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

struct Args {
    engines: Vec<EngineSpec>,
    tc: TimeControl,
    book: Option<String>,
    concurrency: usize,
    games: usize,
    seed: u64,
    sprt: Option<(f64, f64)>,
    alpha: f64,
    beta: f64,
    out: Option<String>,
    games_out: Option<String>,
    nice: Option<i32>,
    adj: Adjudication,
    quiet: bool,
}

fn parse_args() -> Result<Args, String> {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mut a = Args {
        engines: Vec::new(),
        tc: TimeControl::Fischer { base: 8000, inc: 80 },
        book: None,
        concurrency: 1,
        games: 1000,
        seed: 1,
        sprt: None,
        alpha: 0.05,
        beta: 0.05,
        out: None,
        games_out: None,
        nice: None,
        adj: Adjudication::default(),
        quiet: false,
    };
    let mut i = 0;
    while i < argv.len() {
        let next = |i: usize| argv.get(i + 1).cloned().ok_or_else(|| format!("{} needs a value", argv[i]));
        match argv[i].as_str() {
            "--engine" => {
                let mut words = Vec::new();
                while i + 1 < argv.len() && !argv[i + 1].starts_with("--") {
                    i += 1;
                    words.push(argv[i].clone());
                }
                a.engines.push(EngineSpec::parse(&words)?);
            }
            "--tc" => {
                a.tc = TimeControl::parse(&next(i)?)?;
                i += 1;
            }
            "--book" => {
                a.book = Some(next(i)?);
                i += 1;
            }
            "--concurrency" => {
                a.concurrency = next(i)?.parse().map_err(|_| "bad concurrency")?;
                i += 1;
            }
            "--games" => {
                a.games = next(i)?.parse().map_err(|_| "bad games")?;
                i += 1;
            }
            "--seed" => {
                a.seed = next(i)?.parse().map_err(|_| "bad seed")?;
                i += 1;
            }
            "--sprt" => {
                let v = next(i)?;
                let (x, y) = v.split_once(',').ok_or("--sprt elo0,elo1")?;
                a.sprt = Some((x.parse().map_err(|_| "bad elo0")?, y.parse().map_err(|_| "bad elo1")?));
                i += 1;
            }
            "--alpha" => {
                a.alpha = next(i)?.parse().map_err(|_| "bad alpha")?;
                i += 1;
            }
            "--beta" => {
                a.beta = next(i)?.parse().map_err(|_| "bad beta")?;
                i += 1;
            }
            "--out" => {
                a.out = Some(next(i)?);
                i += 1;
            }
            "--games-out" => {
                a.games_out = Some(next(i)?);
                i += 1;
            }
            "--nice" => {
                a.nice = Some(next(i)?.parse().map_err(|_| "bad nice")?);
                i += 1;
            }
            "--time-margin" => {
                a.adj.time_margin = next(i)?.parse().map_err(|_| "bad margin")?;
                i += 1;
            }
            "--no-adjudication" => {
                a.adj.resign_moves = u32::MAX;
                a.adj.draw_moves = u32::MAX / 4;
            }
            "--quiet" => a.quiet = true,
            other => return Err(format!("unknown argument {other}")),
        }
        i += 1;
    }
    if a.engines.len() != 2 {
        return Err("exactly two --engine specs are required".into());
    }
    Ok(a)
}

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
}

fn load_openings(args: &Args) -> Result<Vec<String>, String> {
    let mut rng = Rng(args.seed);
    let mut list = Vec::new();
    match &args.book {
        Some(path) => {
            let text = std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?;
            for line in text.lines() {
                let f: Vec<&str> = line.split_whitespace().take(4).collect();
                if f.len() == 4 {
                    let fen = format!("{} 0 1", f.join(" "));
                    if arhanpassant::Position::from_fen(&fen).is_ok() {
                        list.push(fen);
                    }
                }
            }
        }
        None => {
            // Random 8-ply openings.
            while list.len() < 50_000 {
                let mut pos = arhanpassant::Position::startpos();
                let mut ok = true;
                for _ in 0..8 {
                    let moves = pos.legal_moves();
                    if moves.is_empty() {
                        ok = false;
                        break;
                    }
                    pos.play(moves[(rng.next() % moves.len() as u64) as usize]);
                }
                if ok && !pos.legal_moves().is_empty() {
                    list.push(pos.fen());
                }
            }
        }
    }
    if list.is_empty() {
        return Err("no usable openings".into());
    }
    for i in (1..list.len()).rev() {
        let j = (rng.next() % (i as u64 + 1)) as usize;
        list.swap(i, j);
    }
    Ok(list)
}

#[derive(Default)]
struct Stats {
    wins: u64,
    losses: u64,
    draws: u64,
    penta: [u64; 5],
    reasons: BTreeMap<String, u64>,
    llr: f64,
    decision: Option<&'static str>,
}

impl Stats {
    fn games(&self) -> u64 {
        self.wins + self.losses + self.draws
    }
}

fn wrap_nice(spec: &EngineSpec, nice: Option<i32>) -> EngineSpec {
    // Run through `nice` so matches yield to interactive work.
    match nice {
        Some(n) => {
            static NEXT_WRAPPER: AtomicUsize = AtomicUsize::new(0);
            let wrapper = std::env::temp_dir().join(format!("arena-nice-{}-{}.sh", std::process::id(), NEXT_WRAPPER.fetch_add(1, Ordering::Relaxed)));
            let quoted = spec.cmd.replace('\'', "'\"'\"'");
            let script = format!("#!/bin/sh\nexec nice -n {n} '{quoted}'\n");
            std::fs::write(&wrapper, script).expect("write nice wrapper");
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&wrapper, std::fs::Permissions::from_mode(0o755)).ok();
            }
            EngineSpec { cmd: wrapper.to_string_lossy().into_owned(), ..spec.clone() }
        }
        None => spec.clone(),
    }
}

fn score_for_a(outcome: Outcome, a_is_white: bool) -> u32 {
    // 0 = loss, 1 = draw, 2 = win (half-points)
    match (outcome, a_is_white) {
        (Outcome::Draw, _) => 1,
        (Outcome::WhiteWin, true) | (Outcome::BlackWin, false) => 2,
        _ => 0,
    }
}

fn json_escape(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("arena: {e}");
            std::process::exit(2);
        }
    };
    let openings = match load_openings(&args) {
        Ok(o) => Arc::new(o),
        Err(e) => {
            eprintln!("arena: {e}");
            std::process::exit(2);
        }
    };
    let specs: Vec<EngineSpec> = args.engines.iter().map(|s| wrap_nice(s, args.nice)).collect();
    let (name_a, name_b) = (args.engines[0].name.clone(), args.engines[1].name.clone());
    let pairs = args.games.div_ceil(2);
    let next = Arc::new(AtomicUsize::new(0));
    let stop = Arc::new(AtomicBool::new(false));
    let stats = Arc::new(Mutex::new(Stats::default()));
    let games_log = args.games_out.as_ref().map(|p| Arc::new(Mutex::new(std::fs::File::create(p).expect("games-out file"))));
    let start = Instant::now();
    let bounds = sprt::bounds(args.alpha, args.beta);

    println!(
        "arena: {name_a} vs {name_b}, {} openings, up to {} games, concurrency {}{}",
        openings.len(),
        pairs * 2,
        args.concurrency,
        args.sprt.map_or(String::new(), |(e0, e1)| format!(", SPRT [{e0}, {e1}] bounds ({:.2}, {:.2})", bounds.0, bounds.1))
    );

    let mut workers = Vec::new();
    for _ in 0..args.concurrency {
        let (specs, openings, next, stop, stats) = (specs.clone(), openings.clone(), next.clone(), stop.clone(), stats.clone());
        let games_log = games_log.clone();
        let (tc, adj, sprt_bounds, quiet) = (args.tc, args.adj, args.sprt, args.quiet);
        let (alpha_b, beta_b) = bounds;
        let names = (name_a.clone(), name_b.clone());
        workers.push(std::thread::spawn(move || {
            let mut a = Engine::start(&specs[0]).expect("start engine A");
            let mut b = Engine::start(&specs[1]).expect("start engine B");
            loop {
                if stop.load(Ordering::Relaxed) {
                    break;
                }
                let k = next.fetch_add(1, Ordering::Relaxed);
                if k >= pairs {
                    break;
                }
                let fen = &openings[k % openings.len()];
                let g1 = play([&mut a, &mut b], fen, tc, &adj);
                let g2 = play([&mut b, &mut a], fen, tc, &adj);
                // Restart engines that died.
                for (e, spec) in [(&mut a, &specs[0]), (&mut b, &specs[1])] {
                    if e.sync().is_err() {
                        *e = Engine::start(spec).expect("restart engine");
                    }
                }
                let s1 = score_for_a(g1.outcome, true);
                let s2 = score_for_a(g2.outcome, false);
                if stop.load(Ordering::Relaxed) {
                    break;
                }
                if let Some(log) = &games_log {
                    let mut f = log.lock().unwrap();
                    for (g, white, black) in [(&g1, &names.0, &names.1), (&g2, &names.1, &names.0)] {
                        let _ = writeln!(f, "{}", game_json(g, white, black));
                    }
                }
                let mut st = stats.lock().unwrap();
                for s in [s1, s2] {
                    match s {
                        2 => st.wins += 1,
                        0 => st.losses += 1,
                        _ => st.draws += 1,
                    }
                }
                st.penta[(s1 + s2) as usize] += 1;
                for g in [&g1, &g2] {
                    *st.reasons.entry(g.reason.split(':').next().unwrap_or("").to_string()).or_default() += 1;
                }
                if let Some((e0, e1)) = sprt_bounds {
                    st.llr = sprt::llr(&st.penta, e0, e1);
                    if st.llr >= beta_b && st.decision.is_none() {
                        st.decision = Some("H1");
                        stop.store(true, Ordering::Relaxed);
                    } else if st.llr <= alpha_b && st.decision.is_none() {
                        st.decision = Some("H0");
                        stop.store(true, Ordering::Relaxed);
                    }
                }
                let n = st.games();
                if !quiet && (n % 20 == 0 || st.decision.is_some()) {
                    let e = sprt::elo_estimate(&st.penta);
                    println!(
                        "games {n}: +{} -{} ={} penta {:?} elo {:+.1} [{:+.1}, {:+.1}] llr {:.2}",
                        st.wins, st.losses, st.draws, st.penta, e.elo, e.lo, e.hi, st.llr
                    );
                }
            }
        }));
    }
    for w in workers {
        let _ = w.join();
    }
    if args.nice.is_some() {
        for spec in &specs {
            let _ = std::fs::remove_file(&spec.cmd);
        }
    }

    let st = stats.lock().unwrap();
    let e = sprt::elo_estimate(&st.penta);
    let decision = st.decision.unwrap_or(if args.sprt.is_some() { "inconclusive" } else { "none" });
    println!(
        "final: {name_a} vs {name_b}: games {} +{} -{} ={} penta {:?} elo {:+.1} [{:+.1}, {:+.1}] llr {:.2} decision {decision} ({:.0}s)",
        st.games(),
        st.wins,
        st.losses,
        st.draws,
        st.penta,
        e.elo,
        e.lo,
        e.hi,
        st.llr,
        start.elapsed().as_secs_f64()
    );
    println!("reasons: {:?}", st.reasons);
    if let Some(path) = &args.out {
        let tc = match args.tc {
            TimeControl::Fischer { base, inc } => format!("{}+{}", base as f64 / 1000.0, inc as f64 / 1000.0),
            TimeControl::Nodes(n) => format!("nodes={n}"),
            TimeControl::MoveTime(m) => format!("movetime={m}"),
        };
        let reasons: Vec<String> = st.reasons.iter().map(|(k, v)| format!("\"{}\": {v}", json_escape(k))).collect();
        let sprt = args.sprt.map_or("null".into(), |(e0, e1)| {
            format!("{{\"elo0\": {e0}, \"elo1\": {e1}, \"alpha\": {}, \"beta\": {}, \"llr\": {:.4}, \"lower\": {:.4}, \"upper\": {:.4}}}", args.alpha, args.beta, st.llr, bounds.0, bounds.1)
        });
        let json = format!(
            "{{\n  \"engine\": \"{}\",\n  \"baseline\": \"{}\",\n  \"tc\": \"{tc}\",\n  \"book\": {},\n  \"seed\": {},\n  \"games\": {},\n  \"wins\": {},\n  \"losses\": {},\n  \"draws\": {},\n  \"penta\": {:?},\n  \"elo\": {:.2},\n  \"elo_lo\": {:.2},\n  \"elo_hi\": {:.2},\n  \"sprt\": {sprt},\n  \"decision\": \"{decision}\",\n  \"seconds\": {:.0},\n  \"reasons\": {{{}}}\n}}\n",
            json_escape(&name_a),
            json_escape(&name_b),
            args.book.as_ref().map_or("null".into(), |b| format!("\"{}\"", json_escape(b))),
            args.seed,
            st.games(),
            st.wins,
            st.losses,
            st.draws,
            st.penta,
            e.elo,
            e.lo,
            e.hi,
            start.elapsed().as_secs_f64(),
            reasons.join(", ")
        );
        std::fs::write(path, json).expect("write --out");
    }
}

fn game_json(g: &GameRecord, white: &str, black: &str) -> String {
    let result = match g.outcome {
        Outcome::WhiteWin => "1-0",
        Outcome::BlackWin => "0-1",
        Outcome::Draw => "1/2-1/2",
    };
    format!(
        "{{\"white\": \"{}\", \"black\": \"{}\", \"fen\": \"{}\", \"result\": \"{result}\", \"reason\": \"{}\", \"moves\": \"{}\"}}",
        json_escape(white),
        json_escape(black),
        json_escape(&g.start_fen),
        json_escape(&g.reason),
        g.moves.join(" ")
    )
}
