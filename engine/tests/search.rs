use arhanpassant::search::{search_threads, Limits, Searcher, Shared};
use arhanpassant::Position;

#[test]
fn smp_counts_all_workers_and_limits_their_combined_work() {
    for threads in [1, 4, 16] {
        let shared = Shared::new(4, None);
        let mut searchers: Vec<_> = (0..threads).map(|_| Searcher::new(shared.clone())).collect();
        let root = Position::startpos();
        let limit = 40_000;
        let result = search_threads(
            &mut searchers, &root, &[root.hash()],
            &Limits { nodes: Some(limit), ..Default::default() }, 0, &mut |_| {},
        );
        assert!(root.legal_moves().iter().any(|m| m == result.best_move));
        assert_eq!(result.nodes, searchers.iter().map(|s| s.nodes).sum::<u64>());
        // Threads publish work every 1024 nodes; at most one partial batch per
        // worker remains when another worker reaches the shared limit.
        assert!(result.nodes >= limit, "{threads} threads: {} nodes", result.nodes);
        assert!(result.nodes <= limit + 1024 * threads as u64, "{threads} threads: {} nodes", result.nodes);
    }
}
