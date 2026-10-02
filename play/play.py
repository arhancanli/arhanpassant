"""The community game on GitHub: everyone plays White, together, against ArhanPassant.

.github/workflows/play.yml runs this when an issue titled "play: <move>" is
opened. Every open play issue is handled, oldest first: the move is checked
against the current position, played and answered by the engine; the board
(board.svg), the play page (README.md) and the record (game.json, games/*.pgn)
on the `play` branch are redrawn and pushed; then the issue gets the reply and
is closed.

    python play/play.py --engine target/release/arhanpassant --game GAME_DIR --repo owner/name

--dry-run reads issues from a JSON file and neither pushes nor comments.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
TITLE = re.compile(r"^\s*play:\s*([a-h][1-8][a-h][1-8][qrbn]?)\s*$", re.I)
TOKEN = re.compile(r"game (\d+), position (\d+)", re.I)
THINK_MS = 2000
SQ = 56
LIGHT, DARK, LAST = "#dde4ea", "#7d93a7", "rgba(242,184,75,0.55)"
PIECE_NAMES = {"K": "King", "Q": "Queen", "R": "Rook", "B": "Bishop", "N": "Knight", "P": "Pawn"}


# ---------------------------------------------------------------- engine

class Engine:
    def __init__(self, path):
        self.path = path
        out = subprocess.run([path], input="uci\nquit\n", capture_output=True, text=True, check=True).stdout
        self.name = next((l[len("id name "):].strip() for l in out.splitlines() if l.startswith("id name ")), "ArhanPassant")

    def state(self, moves):
        out = subprocess.run([self.path, "state", "startpos", *moves], capture_output=True, text=True, check=True).stdout
        s = json.loads(out)
        if "error" in s:
            raise ValueError(s["error"])
        return s

    def reply(self, moves):
        """Best move for the side to move, and its score (kind, value) from that side's view."""
        p = subprocess.Popen([self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        position = "position startpos" + (" moves " + " ".join(moves) if moves else "")
        threads = max(1, min(4, os.cpu_count() or 1))
        p.stdin.write(f"setoption name Threads value {threads}\nsetoption name Hash value 128\n{position}\ngo movetime {THINK_MS}\n")
        p.stdin.flush()
        best, score = None, None
        for line in p.stdout:
            t = line.split()
            if t[:1] == ["info"] and "score" in t:
                i = t.index("score")
                score = (t[i + 1], int(t[i + 2]))
            elif t[:1] == ["bestmove"]:
                best = t[1]
                break
        p.stdin.write("quit\n")
        p.stdin.flush()
        p.wait(timeout=30)
        if not best or best == "0000":
            raise RuntimeError("the engine returned no move")
        return best, score


def outcome(s):
    """(result, reason) when the game is over, else None. White is the community."""
    if s["checkmate"]:
        return ("1-0" if s["turn"] == "b" else "0-1"), "checkmate"
    if s["stalemate"]:
        return "1/2-1/2", "stalemate"
    if s["draw"]:
        return "1/2-1/2", s["draw"]
    return None


def white_view(score):
    """The engine's score (it plays Black) as words from White's side."""
    if score is None:
        return None
    kind, v = score
    if kind == "mate":
        return f"the engine sees mate in {v}" if v > 0 else f"White has mate in {-v}"
    return f"it rates the position {-v / 100:+.2f} for White"


# ---------------------------------------------------------------- record

def new_record():
    return {"game": 1, "moves": [], "players": [], "record": {"engine": 0, "community": 0, "draw": 0},
            "movers": {}, "games": [], "last": None}


def load(game_dir):
    path = os.path.join(game_dir, "game.json")
    return json.load(open(path)) if os.path.exists(path) else new_record()


def numbered(sans, start=0):
    out = []
    for i, san in enumerate(sans, start):
        if i % 2 == 0:
            out.append(f"{i // 2 + 1}. {san}")
        elif not out:
            out.append(f"{i // 2 + 1}... {san}")
        else:
            out.append(san)
    return " ".join(out)


def pgn(g, sans, result, reason, engine_name, date):
    tags = [("Event", "GitHub community game"), ("Site", "https://github.com/arhancanli/arhanpassant"),
            ("Date", date.replace("-", ".")), ("Round", str(g["game"])), ("White", "GitHub community"),
            ("Black", engine_name), ("Result", result), ("Termination", reason)]
    head = "".join(f'[{k} "{v}"]\n' for k, v in tags)
    return f"{head}\n{numbered(sans)} {result}\n"


def finish(g, sans, result, reason, engine_name, game_dir):
    date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(os.path.join(game_dir, "games"), exist_ok=True)
    with open(os.path.join(game_dir, "games", f"{g['game']}.pgn"), "w") as f:
        f.write(pgn(g, sans, result, reason, engine_name, date))
    winner = {"1-0": "community", "0-1": "engine"}.get(result, "draw")
    g["record"][winner] += 1
    g["games"].append({"game": g["game"], "result": result, "reason": reason, "plies": len(sans),
                       "players": g["players"], "date": date})
    g["game"] += 1
    g["moves"], g["players"] = [], []


# ---------------------------------------------------------------- drawing

def piece_symbols():
    defs = []
    for name in sorted(os.listdir(os.path.join(HERE, "pieces"))):
        svg = open(os.path.join(HERE, "pieces", name)).read()
        inner = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1)
        defs.append(f'<symbol id="{name[:2]}" viewBox="0 0 45 45">{inner.strip()}</symbol>')
    return "\n".join(defs)


def board_svg(fen, last=None, check=False, turn="w"):
    rows = fen.split()[0].split("/")
    board = {}
    for r, row in enumerate(rows):
        f = 0
        for ch in row:
            if ch.isdigit():
                f += int(ch)
            else:
                board[(7 - r) * 8 + f] = ch
                f += 1
    marks = set()
    if last:
        marks = {(int(last[1]) - 1) * 8 + "abcdefgh".index(last[0]), (int(last[3]) - 1) * 8 + "abcdefgh".index(last[2])}
    parts = []
    for sq in range(64):
        f, r = sq % 8, sq // 8
        x, y = f * SQ, (7 - r) * SQ
        dark = (f + r) % 2 == 0
        parts.append(f'<rect x="{x}" y="{y}" width="{SQ}" height="{SQ}" fill="{DARK if dark else LIGHT}"/>')
        if sq in marks:
            parts.append(f'<rect x="{x}" y="{y}" width="{SQ}" height="{SQ}" fill="{LAST}"/>')
        p = board.get(sq)
        if check and p == ("K" if turn == "w" else "k"):
            parts.append(f'<circle cx="{x + SQ / 2}" cy="{y + SQ / 2}" r="{SQ * 0.48}" fill="url(#check)"/>')
        if f == 0:
            parts.append(f'<text x="{x + 3}" y="{y + 13}" class="c" fill="{LIGHT if dark else DARK}">{r + 1}</text>')
        if r == 0:
            parts.append(f'<text x="{x + SQ - 10}" y="{y + SQ - 4}" class="c" fill="{LIGHT if dark else DARK}">{"abcdefgh"[f]}</text>')
    for sq, p in sorted(board.items()):
        f, r = sq % 8, sq // 8
        ref = ("w" if p.isupper() else "b") + p.lower()
        parts.append(f'<use href="#{ref}" xlink:href="#{ref}" x="{f * SQ}" y="{(7 - r) * SQ}" width="{SQ}" height="{SQ}"/>')
    size = 8 * SQ
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{size}" height="{size}" viewBox="0 0 {size} {size}">\n'
            f'<defs>\n<radialGradient id="check"><stop offset="0" stop-color="#e2462c" stop-opacity="0.95"/>'
            f'<stop offset="0.5" stop-color="#e2462c" stop-opacity="0.5"/><stop offset="1" stop-color="#e2462c" stop-opacity="0"/>'
            f'</radialGradient>\n{piece_symbols()}\n</defs>\n'
            f'<style>.c{{font:600 11px ui-monospace,SFMono-Regular,Menlo,monospace}}</style>\n'
            + "\n".join(parts) + "\n</svg>\n")


# ---------------------------------------------------------------- the play page

def issue_link(repo, g, uci, san):
    title = urllib.parse.quote(f"play: {uci}")
    body = urllib.parse.quote(f"Game {g['game']}, position {len(g['moves'])}. "
                              "Press Submit new issue; the engine replies here within a few minutes.")
    return f"[{san}](https://github.com/{repo}/issues/new?title={title}&body={body})"


def move_table(repo, g, s):
    groups = {}
    for uci, san in s["legal"]:
        kind = "K" if san.startswith("O-O") else (san[0] if san[0] in "KQRBN" else "P")
        groups.setdefault(kind, []).append((san, uci))
    rows = ["| Piece | Moves |", "|---|---|"]
    for kind in "KQRBNP":
        if kind in groups:
            links = " · ".join(issue_link(repo, g, uci, san) for san, uci in sorted(groups[kind]))
            rows.append(f"| {PIECE_NAMES[kind]} | {links} |")
    return "\n".join(rows)


def describe(result, reason):
    return {"1-0": f"Community won by {reason}", "0-1": f"Engine won by {reason}"}.get(result, f"Drawn by {reason}")


def page_md(g, s, repo, engine):
    rec = g["record"]
    ply = len(g["moves"])
    lines = [
        "# Play ArhanPassant on GitHub",
        "",
        "Everyone plays White, together, against the engine on this one board. Pick a move below: GitHub opens "
        "an issue with the move filled in, you press **Submit new issue**, and the engine answers within a few "
        "minutes, on your issue and on this page.",
        "",
        '<p align="center"><img src="board.svg" width="448" alt="The current position"></p>',
        "",
    ]
    status = f"**Game {g['game']}, move {ply // 2 + 1}, White to move.**"
    last = g.get("last")
    if ply == 0:
        status += " A new game: the first move is yours."
    elif last and last.get("game") == g["game"] and last.get("reply"):
        status += f" The engine's last move was **{last['reply']}**" + (f"; {last['view']}." if last.get("view") else ".")
    lines += [status, "", "## Your move", "", move_table(repo, g, s), ""]
    if ply:
        lines += ["## This game", "", numbered(s["history"]), ""]
    lines += ["## Record", ""]
    total = rec["engine"] + rec["community"] + rec["draw"]
    if total:
        lines.append(f"Finished games: {total}. The engine won {rec['engine']}, the community {rec['community']}, "
                     f"and {rec['draw']} {'was' if rec['draw'] == 1 else 'were'} drawn.")
    else:
        lines.append("No game has finished yet.")
    movers = sorted(g["movers"].items(), key=lambda kv: (-kv[1], kv[0].lower()))[:20]
    if movers:
        lines += ["", "| Player | Moves |", "|---|---|"] + [f"| {u} | {n} |" for u, n in movers]
    if g["games"]:
        lines += ["", "## Finished games", "", "| Game | Result | Moves | Players | Score sheet |", "|---|---|---|---|---|"]
        for x in reversed(g["games"][-20:]):
            players = ", ".join(x["players"][:6]) + (" and others" if len(x["players"]) > 6 else "")
            lines.append(f"| {x['game']} | {describe(x['result'], x['reason'])} | {(x['plies'] + 1) // 2} | {players} | "
                         f"[PGN](games/{x['game']}.pgn) |")
    lines += [
        "",
        "---",
        "",
        f"The engine is {engine.name}, thinking {THINK_MS // 1000} seconds a move on GitHub's servers. "
        "Its strength, and how it keeps improving, are on the "
        "[engine page](https://github.com/arhancanli/arhanpassant). For a game with a clock, play it "
        "[on Lichess](https://lichess.org/@/arhanpassant) or [on the website](https://arhanpassant.vercel.app/play). "
        "This page, board.svg, game.json and every finished game (games/) live on the `play` branch. "
        "Piece images by Colin M.L. Burnett, CC BY-SA 3.0.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- one move

def handle(issue, g, engine, repo):
    """Apply one issue's move. Returns (reply text, close reason, played)."""
    user = issue["author"]["login"]
    page = f"https://github.com/{repo}/blob/play/README.md"
    m = TITLE.match(issue["title"])
    if not m:
        return (f"I couldn't read a move in that title. Titles look like `play: e2e4` (from-square, to-square). "
                f"The easiest way is to pick a move on the [play page]({page}).", "not planned", False)
    uci = m.group(1).lower()
    tok = TOKEN.search(issue.get("body") or "")
    if tok and (int(tok.group(1)) != g["game"] or int(tok.group(2)) != len(g["moves"])):
        return (f"Another player moved first, so `{uci}` was for a position that has passed. "
                f"Pick again from the current board on the [play page]({page}).", "not planned", False)
    s = engine.state(g["moves"])
    legal = dict(map(tuple, s["legal"]))
    if uci not in legal:
        sample = ", ".join(san for _, san in s["legal"][:12])
        return (f"`{uci}` isn't a legal move in the current position. Some legal moves: {sample}. "
                f"The [play page]({page}) lists them all as links.", "not planned", False)
    num = len(g["moves"]) // 2 + 1
    san = legal[uci]
    g["moves"].append(uci)
    if user not in g["players"]:
        g["players"].append(user)
    g["movers"][user] = g["movers"].get(user, 0) + 1
    after = engine.state(g["moves"])
    end = outcome(after)
    text = f"You played **{num}. {san}**."
    reply_san, view = None, None
    if not end:
        best, score = engine.reply(g["moves"])
        reply_san = dict(map(tuple, after["legal"]))[best]
        g["moves"].append(best)
        view = white_view(score)
        text += f" ArhanPassant answered **{num}... {reply_san}**" + (f"; {view}." if view else ".")
        after = engine.state(g["moves"])
        end = outcome(after)
    g["last"] = {"game": g["game"], "by": user, "move": san, "reply": f"{num}... {reply_san}" if reply_san else None,
                 "view": view, "issue": issue["number"], "at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if end:
        result, reason = end
        finished = g["game"]
        finish(g, after["history"], result, reason, engine.name, GAME_DIR)
        text += f"\n\n**Game {finished} is over: {describe(result, reason).lower()}.** A new game has started; " \
                f"[make the first move]({page})."
    else:
        text += f"\n\nThe next move is anyone's, including yours: [pick it on the play page]({page})."
    return text, "completed", True


GAME_DIR = "game"


def git(*args):
    return subprocess.run(["git", "-C", GAME_DIR, *args], capture_output=True, text=True, check=True).stdout.strip()


def redraw(g, engine, repo):
    s = engine.state(g["moves"])
    last = g["moves"][-1] if g["moves"] else None
    with open(os.path.join(GAME_DIR, "board.svg"), "w") as f:
        f.write(board_svg(s["fen"], last, s["check"], s["turn"]))
    with open(os.path.join(GAME_DIR, "README.md"), "w") as f:
        f.write(page_md(g, s, repo, engine))
    with open(os.path.join(GAME_DIR, "game.json"), "w") as f:
        json.dump(g, f, indent=1)
        f.write("\n")


def main():
    global GAME_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True)
    ap.add_argument("--game", required=True, help="working tree of the play branch")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--dry-run", metavar="ISSUES_JSON")
    args = ap.parse_args()
    GAME_DIR = args.game
    engine = Engine(args.engine)
    g = load(GAME_DIR)
    if args.dry_run:
        issues = json.load(open(args.dry_run))
    else:
        out = subprocess.run(["gh", "issue", "list", "--repo", args.repo, "--state", "open", "--limit", "100",
                              "--json", "number,title,body,author,createdAt"], capture_output=True, text=True, check=True).stdout
        issues = json.loads(out)
    issues = sorted((i for i in issues if i["title"].strip().lower().startswith("play:")), key=lambda i: i["createdAt"])
    if not issues and not os.path.exists(os.path.join(GAME_DIR, "game.json")):
        redraw(g, engine, args.repo)
        if not args.dry_run:
            git("add", "-A")
            git("commit", "-q", "-m", "Community game: first board")
            git("push", "-q", "origin", "HEAD:play")
        print("initialised the board")
    for issue in issues:
        text, reason, played = handle(issue, g, engine, args.repo)
        if played:
            redraw(g, engine, args.repo)
            if not args.dry_run:
                git("add", "-A")
                git("commit", "-q", "-m", f"Community game: {issue['author']['login']} played {g['last']['move']} (#{issue['number']})")
                git("push", "-q", "origin", "HEAD:play")
                sha = git("rev-parse", "HEAD")
                text = text.replace("\n\n", f"\n\n![The board](https://raw.githubusercontent.com/{args.repo}/{sha}/board.svg)\n\n", 1) \
                    if "\n\n" in text else text + f"\n\n![The board](https://raw.githubusercontent.com/{args.repo}/{sha}/board.svg)"
        print(f"#{issue['number']} {issue['title']!r}: {reason}\n{text}\n")
        if not args.dry_run:
            subprocess.run(["gh", "issue", "comment", str(issue["number"]), "--repo", args.repo, "--body-file", "-"],
                           input=text, text=True, check=True)
            subprocess.run(["gh", "issue", "close", str(issue["number"]), "--repo", args.repo, "--reason", reason], check=True)


if __name__ == "__main__":
    main()
