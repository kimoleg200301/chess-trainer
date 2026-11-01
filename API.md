# Tactics Lab API contract

Same-origin `/api`; authenticated session cookie. Personal app, no public signup.
All failures use `{ "detail": "human-readable Russian error" }`.

## Authentication
- `POST /api/auth/login` JSON `{username,password}` -> `{username}` and HttpOnly session cookie.
- `GET /api/auth/me` -> `{username}`; 401 otherwise.
- `POST /api/auth/logout` -> 204.

## Catalog
`GET /api/meta?collection=lichess|public|personal|all` -> `{total_puzzles, themes:[{id,name,count}], min_rating,max_rating,collections:[{id,name,count}]}`. Default `lichess`. Counts and themes honor the selected collection; personal puzzles are scoped to the authenticated user. `collections` lists available source choices and visible counts.

## Training
`POST /api/training/start` JSON `{theme:null|string,min_rating:800,max_rating:1800,mode:"practice"|"review",collection:"lichess"|"public"|"personal"|"all",puzzle_id?:string}` -> Attempt. Collection defaults to `lichess`; an explicit `puzzle_id` launches a visible puzzle directly, bypassing rating/theme/mode selection. Return 404 if empty.

Attempt:
```
{id:number,puzzle_id:string,fen:string,rating:number,themes:string[],
 status:"active"|"completed"|"revealed",turn:"white"|"black",
 solved_plies:number,total_plies:number,mistakes:number,hints_used:number,
 last_move:string|null,elapsed_ms:number|null,
 collection:"lichess"|"public"|"personal",rating_estimated:boolean}
```
`turn` is the learner color throughout an attempt. FEN is authoritative.

- `POST /api/training/{id}/move` `{move:"e2e4",elapsed_ms?:number}` -> `{accepted:boolean,attempt:Attempt,reply_move:string|null,feedback:string}`. Correct learner move plus automatic opponent reply. An incorrect legal move does NOT change the board, but increases mistakes. Invalid moves return 422.
- `POST /api/training/{id}/hint` optional `{elapsed_ms:number}` -> `{message:string,from_square:string|null,attempt:Attempt}`. Repeated hints count once; not a clean solve.
- `POST /api/training/{id}/reveal` optional `{elapsed_ms:number}` -> `{attempt:Attempt,solution:[{uci:string,san:string}],game_url:string}`. Solution starts at initial learner position.
- `POST /api/training/{id}/time` `{elapsed_ms:number}` -> Attempt. Saves active solving time without making a move; ownership and Origin rules match other training actions.
- `GET /api/training/{id}/analysis` -> `{evaluation:string,line:string[],engine:string}`. Only completed/revealed; Stockfish analysis may return 503 if unavailable. SAN line starting from current position.

`elapsed_ms` is measured active solving time, sent by the client as a nonnegative integer in milliseconds (at most JavaScript's maximum safe integer). The client pauses timing outside the visible solving screen and while awaiting actions. Updates are cumulative and monotonic; delayed lower values do not decrease time, and finalized attempts ignore late timing updates. Incorrect legal moves and hints also save the supplied time. No-body hint/reveal requests and old move requests remain supported. New attempts start with `elapsed_ms:null` until a measurement is received; `null` means unknown, not zero. Historical records are not estimated from wall-clock timestamps.

## Statistics
`GET /api/stats` -> `{solved:number,total_attempts:number,accuracy:number,streak:number,due_count:number,average_solution_ms:number|null,timed_solutions:number,by_theme:[{id:string,name:string,attempts:number,solved:number}],recent:[{puzzle_id:string,rating:number,collection:string,rating_estimated:boolean,status:string,created_at:string,elapsed_ms:number|null}]}`.
Accuracy is percent of finalized attempts solved cleanly (no mistakes, hints, or reveal). Streak is consecutive clean solves, not daily streak. `average_solution_ms` is the arithmetic mean of measured completed solutions, rounded to whole milliseconds; active, revealed and unknown-time attempts are excluded. `timed_solutions` is the number contributing to this mean; a measured zero is valid. Recent attempts include their saved duration, which is frozen on completion or reveal.

## Real-game imports and generation
All routes require authentication and the existing same-origin checks. IDs are scoped to the current user.

- `POST /api/imports/chesscom` `{username:string,month?:"YYYY-MM"|null,limit?:number}` -> ImportJob. Only public completed standard games; limit 1–50, default 10. Missing month selects the latest available monthly archive. Sequential, size/time-bounded requests to `api.chess.com`; no credentials required. Unavailable profile/archive, rate limiting and malformed upstream data return readable errors.
- `POST /api/imports/public` `{}` -> ImportJob, using the packaged `public-games.pgn` (up to 50 games).
- `GET /api/imports` -> `{jobs:ImportJobSummary[]}`, latest 100 owned batches.
- `GET /api/imports/{id}` -> ImportJob.
- `POST /api/imports/{id}/step` `{}` -> ImportJob. Processes at most two selected plies with a bounded Stockfish budget; persists its cursor and results. Chess.com checks only the requested player's moves; public generation checks both colors and can extract a tactic even if the right move was played. Completed jobs return unchanged. A concurrent step returns 409; busy/unavailable engine returns 503 and the user can retry. No worker continues once the client stops requesting steps.
- `POST /api/candidates/{id}/puzzle` `{}` -> `{puzzle_id,collection:"public"|"personal",created:boolean}`. A candidate without a validated tactical line returns 422. Deterministic identities prevent repeated saves of the same puzzle. Public jobs save eligible puzzles automatically; personal candidates require this action.

ImportJobSummary:
```
{id,source:"public"|"chesscom",username:string|null,month:string|null,
 status:"ready"|"running"|"completed"|"failed",total_games,completed_games,
 candidates_count,puzzles_count,error:string|null,created_at}
```
ImportJob adds `games`:
```
[{id,white,black,result,date,event,game_url,
  status:"pending"|"analysing"|"completed"|"skipped",cursor,total_plies,error,
  candidates:[{id,ply,move_number,side:"white"|"black",before_fen,
    played_uci,played_san,best_uci,best_san,loss_cp,score_before,score_after,
    mate_before,mate_after,best_line_san,phase,explanation,
    puzzle_available:boolean,puzzle_id:string|null}]}]
```
`ply` is zero-based. Scores are centipawns from the mover's perspective; signed mate fields distinguish mate for/against the mover. Phase is a heuristic (`opening`, `middlegame`, `endgame`). Inspectable analysis intentionally exposes improved moves on this screen; ordinary training still hides its solution. A useful improvement need not qualify as a tactical puzzle. Generated ratings are approximate (`rating_estimated:true`). Engine limits favor a compact set of unambiguous tactics; they do not guarantee exhaustive or perfect game analysis.

Identical imports resume the stored job. Overlapping batches can reuse completed analyses for the same source and requested player. Import state is stored in PostgreSQL. The UI pauses requests when hidden or navigating away and requires manual resume; an in-flight step may finish and be saved.

## Portable progress
`GET /api/progress/export` -> authenticated JSON attachment `{format_version:1|2,exported_at,username,attempts:[...],reviews:[...],generated_puzzles?:[...]}`. Contains only the current user's progress, no passwords/sessions. Pure Lichess exports retain version 1. Version 2 includes all owned personal puzzle definitions (including unplayed ones), plus generated public puzzles referenced by progress. These definitions contain solution moves so training remains portable. Import jobs and their intermediate analysis are not included; use a database backup for those.

Restore on a new server with `python -m app.restore_progress FILE --username USER`. The target account must exist and have no attempts, reviews or personal puzzles. The CLI validates references, legal puzzle lines and states before importing atomically; it never replaces existing progress. Personal definitions and references are remapped to the target owner. Attempt records include nullable `elapsed_ms`. Older format-version-1 backups without the field remain importable and retain unknown durations. Existing export size and record limits remain enforced.

## Health
`GET /api/health` -> `{status:"ok",database:"ok",engine_available:boolean}`. No authentication required.

Optional `FRONTEND_DIST` serves a built frontend at `/` in the Render Free image. API routes remain distinct and unknown `/api/*` paths return JSON 404.

## Seed data
`data/puzzles.csv` uses standard Lichess CSV columns including PuzzleId,FEN,Moves,Rating,Themes,GameUrl,OpeningTags. IMPORTANT: FEN is BEFORE first opponent move. Apply first move for initial learner position; solution begins at second move. Dataset is CC0; provenance in `data/README.md`.

`data/generated-puzzles.json` supplies the independently generated public bank; `data/public-games.pgn` supplies its complete real-game corpus. Bootstrap validates generated identities and legal moves, then inserts idempotently. Both use the same before-opponent-move convention. Source and reproducible generator commands are recorded in `data/README.md`.
