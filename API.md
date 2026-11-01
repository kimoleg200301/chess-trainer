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
`POST /api/training/start` JSON `{theme:null|string,min_rating:800,max_rating:1800,mode:"practice"|"review",collection:"lichess"|"public"|"personal"|"all",puzzle_id?:string,exclude_attempt_id?:number}` -> Attempt plus `{starting_fen:string,resumed:boolean}`. Collection defaults to `lichess`; an explicit `puzzle_id` launches a visible puzzle directly, bypassing rating/theme/mode selection. Return 404 if empty.

Attempt:
```
{id:number,puzzle_id:string,fen:string,rating:number,themes:string[],
 status:"active"|"completed"|"revealed",turn:"white"|"black",
 solved_plies:number,total_plies:number,mistakes:number,hints_used:number,
 last_move:string|null,elapsed_ms:number|null,
 collection:"lichess"|"public"|"personal",rating_estimated:boolean}
```
`turn` is the learner color throughout an attempt. FEN is authoritative. Personal practice prioritizes unseen puzzles from the newest actual game (`source_game_at` descending), then source ply ascending within a game. Undated puzzles follow dated ones. A personal puzzle with any `completed` or `revealed` attempt is excluded from automatic practice, including `collection:"all"`. This also excludes a regenerated personal puzzle with the same nonempty source-game URL and source ply. Review mode and explicit `puzzle_id` links intentionally allow replay.

Selecting an unfinished personal puzzle resumes its latest active attempt, preserving position, mistakes, hints and elapsed time. `starting_fen` remains the original learner position. `exclude_attempt_id` skips only the caller's current active personal puzzle when requesting the next position. Exhausted personal practice returns 404 with `code:"personal_collection_complete"`; if only the excluded unfinished position remains, `code:"personal_current_remaining"`. Other empty selections retain their ordinary 404 response. In mixed practice, other available collections remain selectable.

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
- `POST /api/imports/{id}/step` `{}` -> ImportJob. Processes at most one selected ply with a bounded Stockfish budget and deeper verification for candidate moments; persists its cursor and results. Chess.com checks only the requested player's moves; public generation checks both colors and can extract a tactic even if the right move was played. Completed jobs return unchanged. A concurrent step returns 409; busy/unavailable engine returns 503 and the user can retry. No worker continues once the client stops requesting steps.
- `POST /api/imports/{id}/reanalyze` `{}` -> ImportJob. Resets the owned batch for the current analysis version. Reuses saved PGNs rather than downloading different games. Old candidates remain visible as pending until their ply is checked; recalculation updates/removes them. Existing puzzle definitions and training history are retained. Concurrent processing returns 409.
- `POST /api/candidates/{id}/puzzle` `{}` -> `{puzzle_id,collection:"public"|"personal",created:boolean}`. A candidate without a validated tactical line returns 422. Deterministic identities prevent repeated saves of the same puzzle. Both public and personal jobs save eligible puzzles automatically. This endpoint remains an idempotent fallback for older unsaved candidates. Application bootstrap also saves previously eligible unsaved candidates, without recalculating or replacing their definitions.

ImportJobSummary:
```
{id,source:"public"|"chesscom",username:string|null,month:string|null,
 status:"ready"|"running"|"completed"|"failed",total_games,completed_games,
 candidates_count,available_puzzles_count,puzzles_count,
 analysis_version:number,analysis_outdated:boolean,error:string|null,created_at}
```
ImportJob adds `games`:
```
[{id,white,black,result,date,event,game_url,
  status:"pending"|"analysing"|"completed"|"skipped",cursor,total_plies,error,
  source_game_at:string|null,analysis_version:number,
  candidates:[{id,ply,move_number,side:"white"|"black",before_fen,
    played_uci,played_san,best_uci,best_san,loss_cp,score_before,score_after,
    mate_before,mate_after,best_line_san,phase,explanation,
    puzzle_available:boolean,puzzle_id:string|null,analysis_pending:boolean,
    puzzle_reason_code:string|null,puzzle_reason:string|null,analysis_depth:number|null}]}]
```
`available_puzzles_count` counts confirmed candidate moments with a puzzle definition; pending old candidates are excluded. `puzzles_count` counts distinct saved puzzle IDs linked to the batch or matching its nonempty source-game URLs, scoped to the proper collection and owner. This includes previously saved tasks whose error cards disappeared after recalculation and bundled public tasks from the same corpus. These are separate metrics. `puzzle_reason_code`/`puzzle_reason` explain the current rejection or calculation limit; they do not claim that a position can never form a useful exercise.

`ply` is zero-based. Scores are centipawns from the mover's perspective; signed mate fields distinguish mate for/against the mover. Phase is a heuristic (`opening`, `middlegame`, `endgame`). Inspectable analysis intentionally exposes improved moves on this screen; ordinary training still hides its solution. A useful improvement need not qualify as a tactical puzzle. Generated ratings are approximate (`rating_estimated:true`). Engine limits favor a compact set of unambiguous tactics; they do not guarantee exhaustive or perfect game analysis.

Identical imports resume the stored job. Overlapping batches can reuse completed analyses only for the same source, requested player and current analysis version. Old analyses do not silently become current through reuse. Import state is stored in PostgreSQL. The UI pauses requests when hidden or navigating away and requires manual resume; an in-flight step may finish and be saved.

## Independent whole-game analysis

The **Анализ** screen uses the existing Chess.com import endpoint to load games, then these separate routes to assess every ply for both players. It does not request tactic-generation steps or create puzzles. All routes are authenticated and user-scoped; POST requests use the existing same-origin checks.

- `GET /api/game-analysis/games` -> `{games:GameAnalysisSource[]}`. Up to 200 unique owned Chess.com games, newest actual game date first, with import ID as a fallback. Overlapping imports share one list entry and one report per user and game fingerprint.
- `POST /api/game-analysis` `{game_id:number}` -> GameAnalysisReport. Starts a report or returns the existing one, including a completed report. `game_id` must belong to the caller's Chess.com imports.
- `GET /api/game-analysis/{id}` -> GameAnalysisReport.
- `POST /api/game-analysis/{id}/step` `{}` -> GameAnalysisReport. Checks one ply, stores its result and advances the cursor atomically. Stockfish shares the existing engine semaphore, with one thread, 32 MiB hash and an eight-second position budget. Completed reports return unchanged. Concurrent processing returns 409; busy or unavailable engine returns 503. Previous checkpoints survive an engine failure and can be resumed. An outdated analysis version returns 409 and requires a restart.
- `POST /api/game-analysis/{id}/restart` `{}` -> GameAnalysisReport. Clears only this report and resets it to the current version. Imported games, tactical puzzles and training history remain intact.

GameAnalysisSource:
```
{id,white,black,result,date,event,game_url,source_game_at:string|null,
 username:string|null,total_plies,analysis_id:number|null,
 analysis_status:"ready"|"running"|"completed"|"failed"|null,analysis_outdated:boolean}
```
GameAnalysisReport:
```
{id,game_id,game:GameAnalysisSource & {initial_fen:string},
 status:"ready"|"running"|"completed"|"failed",cursor,total_plies,
 analysis_version,analysis_outdated,error:string|null,created_at,updated_at,
 summary:{white:GameSideSummary,black:GameSideSummary,complete:boolean},
 moves:GameMoveAnalysis[],category_names:{[category]:string},method:object}
```
GameSideSummary:
```
{accuracy:number|null,provisional_accuracy:number|null,counts:{[category]:number},
 total_moves,analysed_moves,assessed_moves,accuracy_moves,coverage:number,
 complete:boolean,accuracy_complete:boolean}
```
GameMoveAnalysis:
```
{ply,move_number,side:"white"|"black",before_fen,after_fen,
 played_uci,played_san,best_uci:string|null,best_san:string|null,category,
 accuracy:number|null,expected_loss:number|null,
 best_expected:number|null,played_expected:number|null,
 score_before:number|null,score_after:number|null,mate_before:number|null,mate_after:number|null,
 best_line_uci:string[],best_line_san:string[],depth,assessed:boolean,
 reason:string,brilliant_evidence:object|null}
```

Categories are `brilliant` (бриллиантовый), `great` (сильный), `best` (лучший), `excellent` (отличный), `good` (хороший), `inaccuracy` (неточность), `mistake` (ошибка), `blunder` (зевок), `forced` (вынужденный), and `unassessed` (не оценён). Each move has exactly one category. Scores and mate distances are from the mover's perspective; `ply` is zero-based. Best lines and suggested moves are intentionally visible in this analysis screen.

The method is Tactics Lab's own Stockfish heuristic, not Chess.com's formula. A centipawn score maps to `E(cp) = 1 / (1 + exp(-cp / 300))`, with `cp` clamped to ±3000; mate for/against the mover maps to 1/0 and a terminal draw to 0.5. `loss = max(0, E(best) - E(played))`; move accuracy is `100 * exp(-6 * loss)`. Ordinary loss thresholds are ≤0.01 excellent, ≤0.03 good, ≤0.07 inaccuracy, ≤0.15 mistake, otherwise blunder. A checked engine choice within 0.01 is best. Conservative extra checks can mark a critical unique choice as great or a sound material sacrifice as brilliant; these labels may differ from Chess.com.

Side accuracy is the arithmetic mean of assessed non-forced move accuracies. `coverage` is the assessed percentage of that side's total moves, from 0 to 100. Partial reports expose `provisional_accuracy`; final `accuracy` stays null until the report finishes and every move for that side is assessed. A completed report can still contain `unassessed` moves and therefore lack a final percentage. Forced moves count toward coverage but not the mean; a side with only forced moves has no measured accuracy. `complete` describes processed moves; `accuracy_complete` describes full assessment, not the existence of a non-forced sample.

Each checkpoint and completed report is stored in PostgreSQL independently of tactic-generation state. The client stops new steps on pause, navigation or a hidden tab and resumes only on request; an in-flight step may still save. No background worker continues analysis while the screen is closed.

## Portable progress
`GET /api/progress/export` -> authenticated JSON attachment `{format_version:1|2,exported_at,username,attempts:[...],reviews:[...],generated_puzzles?:[...]}`. Contains only the current user's progress, no passwords/sessions. Pure Lichess exports retain version 1. Version 2 includes all owned personal puzzle definitions (including unplayed ones), plus generated public puzzles referenced by progress. These definitions contain solution moves so training remains portable. Optional `source_game_at`, `source_ply` and `created_at` preserve chronological practice order; earlier version-2 exports without those fields remain supported, with unknown game dates left null. Import jobs, their intermediate analysis and independent whole-game reports (including completed ones) are not included; use a full PostgreSQL backup for those.

Restore on a new server with `python -m app.restore_progress FILE --username USER`. The target account must exist and have no attempts, reviews or personal puzzles. The CLI validates references, legal puzzle lines and states before importing atomically; it never replaces existing progress. Personal definitions and references are remapped to the target owner. Attempt records include nullable `elapsed_ms`. Older format-version-1 backups without the field remain importable and retain unknown durations. Existing export size and record limits remain enforced.

## Health
`GET /api/health` -> `{status:"ok",database:"ok",engine_available:boolean}`. No authentication required.

Optional `FRONTEND_DIST` serves a built frontend at `/` in the Render Free image. API routes remain distinct and unknown `/api/*` paths return JSON 404.

## Seed data
`data/puzzles.csv` uses standard Lichess CSV columns including PuzzleId,FEN,Moves,Rating,Themes,GameUrl,OpeningTags. IMPORTANT: FEN is BEFORE first opponent move. Apply first move for initial learner position; solution begins at second move. Dataset is CC0; provenance in `data/README.md`.

`data/generated-puzzles.json` supplies the independently generated public bank; `data/public-games.pgn` supplies its complete real-game corpus. Bootstrap validates generated identities and legal moves, then inserts idempotently. Both use the same before-opponent-move convention. Source and reproducible generator commands are recorded in `data/README.md`.
