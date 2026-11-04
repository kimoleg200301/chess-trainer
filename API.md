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

`elapsed_ms` is measured active solving time, sent by the client as a nonnegative integer in milliseconds (at most JavaScript's maximum safe integer). The client measures wall-clock deltas, including time in a minimized, hidden or temporarily frozen browser tab, and reconciles the display on return. Timing pauses on the settings/other app screens and while awaiting actions; completion/reveal freezes the result. A hidden-tab checkpoint does not pause the clock. If the browser discards the page entirely, only saved time can be recovered. Updates are cumulative and monotonic; delayed lower values do not decrease time, and finalized attempts ignore late timing updates. Incorrect legal moves and hints also save the supplied time. No-body hint/reveal requests and old move requests remain supported. New attempts start with `elapsed_ms:null` until a measurement is received; `null` means unknown, not zero. Historical records are not estimated from wall-clock timestamps.

## Statistics
`GET /api/stats` -> `{solved:number,total_attempts:number,accuracy:number,streak:number,due_count:number,average_solution_ms:number|null,timed_solutions:number,by_theme:[{id:string,name:string,attempts:number,solved:number}],recent:[{puzzle_id:string,rating:number,collection:string,rating_estimated:boolean,status:string,created_at:string,elapsed_ms:number|null}]}`.
Accuracy is percent of finalized attempts solved cleanly (no mistakes, hints, or reveal). Streak is consecutive clean solves, not daily streak. `average_solution_ms` is the arithmetic mean of measured completed solutions, rounded to whole milliseconds; active, revealed and unknown-time attempts are excluded. `timed_solutions` is the number contributing to this mean; a measured zero is valid. Recent attempts include their saved duration, which is frozen on completion or reveal.

## Real-game imports and generation
All routes require authentication and the existing same-origin checks. IDs are scoped to the current user.

- `POST /api/imports/chesscom` `{username:string,month?:"YYYY-MM"|null,limit?:number}` -> ImportJob. Only public completed standard games; limit 1–50, default 10. Missing month selects the latest available monthly archive. Sequential, size/time-bounded requests to `api.chess.com`; no credentials required. Unavailable profile/archive, rate limiting and malformed upstream data return readable errors.
- `POST /api/imports/public` `{}` -> ImportJob, using the packaged `public-games.pgn` (up to 50 games).
- `GET /api/imports` -> `{jobs:ImportJobSummary[]}`, latest 100 owned batches.
- `GET /api/imports/{id}` -> ImportJob.
- `POST /api/imports/{id}/step` `{}` -> ImportJob. Processes at most one selected ply with a bounded Stockfish budget and deeper verification for candidate moments; persists its cursor and results. Chess.com checks only the requested player's moves; public generation checks both colors and can extract a tactic even if the right move was played. Completed jobs return unchanged. A concurrent step returns 409; busy/unavailable engine returns 503 and the user can retry. This remains a single-step compatibility endpoint; background execution is controlled separately by the persisted `background_requested` flag.
- `POST /api/imports/{id}/reanalyze` `{}` -> ImportJob. Resets the owned batch for the current analysis version. Reuses saved PGNs rather than downloading different games. Old candidates remain visible as pending until their ply is checked; recalculation updates/removes them. Existing puzzle definitions and training history are retained. Concurrent processing returns 409.
- `POST /api/candidates/{id}/puzzle` `{}` -> `{puzzle_id,collection:"public"|"personal",created:boolean}`. A candidate without a validated tactical line returns 422. Deterministic identities prevent repeated saves of the same puzzle. Both public and personal jobs save eligible puzzles automatically. This endpoint remains an idempotent fallback for older unsaved candidates. Application bootstrap also saves previously eligible unsaved candidates, without recalculating or replacing their definitions.

ImportJobSummary:
```
{id,source:"public"|"chesscom",username:string|null,month:string|null,
 status:"ready"|"running"|"completed"|"failed",background_requested:boolean,total_games,completed_games,
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

Identical imports reopen the stored job and refresh any newly available Chess.com reference accuracies without recalculating its report. Overlapping batches can reuse completed analyses only for the same source, requested player and current analysis version. Old analyses do not silently become current through reuse. Import state is stored in PostgreSQL. The UI polls saved results while visible and refreshes on return. Hiding or leaving the screen never changes server execution intent. Explicit pause permits the current step to finish and checkpoint, then prevents further steps.

## Independent whole-game analysis

The **Анализ** screen synchronizes recent Chess.com games and retains the existing import endpoint for a selected historical month, then uses separate routes to assess every ply for both players. Sync does not start whole-game analysis or tactical generation. All routes are authenticated and user-scoped; POST requests use the existing same-origin checks.

- `GET /api/game-analysis/games` -> `{games:GameAnalysisSource[],sync:ChessComSyncState}`. Up to 200 unique owned Chess.com games, newest actual game date first, with import ID as a fallback. Overlapping imports share one list entry and one report per user and game fingerprint. This GET only reads stored data.
- `POST /api/game-analysis/sync` `{username:string,automatic?:boolean}` -> ChessComSyncState. Trims and lowercases the nickname, saves it to the caller's account and durably queues synchronization; no Chess.com request is made in this handler. Repeated requests for a pending nickname are idempotent. Minimum interval is 30 seconds for manual requests and five minutes for `automatic:true`; a request within this interval returns the current state. Changing the nickname invalidates an older in-flight result without removing stored games. Username validation, authentication and Origin checks match the import routes.
- `POST /api/game-analysis` `{game_id:number}` -> GameAnalysisReport. Starts a report or returns the existing one, including a completed report. `game_id` must belong to the caller's Chess.com imports.
- `GET /api/game-analysis/{id}` -> GameAnalysisReport.
- `POST /api/game-analysis/{id}/step` `{}` -> GameAnalysisReport. Checks one ply, stores its result and advances the cursor atomically. Stockfish 19 shares the existing engine semaphore, with two threads, 256 MiB hash, up to 2,000,000 nodes / three seconds per search and a 30-second position budget. Unassessed moves receive one separate checkpoint with up to 4,000,000 nodes / six seconds per search and 45 seconds per position. Completed reports with no pending retry return unchanged. Concurrent processing returns 409; busy or unavailable engine returns 503. Previous checkpoints survive an engine failure and can be resumed. An outdated analysis version returns 409 and requires a restart.
- `POST /api/game-analysis/{id}/restart` `{}` -> GameAnalysisReport. Clears only this report and resets it to the current version. Imported games, tactical puzzles and training history remain intact.

ChessComSyncState:
```
{username:string|null,status:"idle"|"ready"|"running"|"completed"|"failed",
 revision:number,background_requested:boolean,requested_at:string|null,
 last_synced_at:string|null,next_sync_at:string|null,
 added_games:number,checked_games:number,error:string|null}
```

The shared server worker downloads up to 200 completed standard games from the two newest available archive months. It deduplicates owned Chess.com games by fingerprint and canonical game URL, appending only missing games to monthly sync batches. Existing reports, cursors, exercises, ratings and coach baselines are retained. The 100-batch cap still applies; an error rolls back all additions from that sync. Source errors retain the nickname and previous games, with a readable `failed` state. Queued work survives browser closure and server restart. The visible analysis list requests automatic sync on entry, every five minutes, and on return/connection recovery when due; no new periodic analysis-list requests are scheduled while the browser is closed. Chess.com archive publication may lag actual play.

Successful `/api/imports/chesscom` also remembers its nickname. Migration `0009_chesscom_sync` initializes existing accounts from their latest valid Chess.com import, falling back to the personal coach nickname. It queues no work and changes no existing report or coach data. Sync settings and imported games are part of a full PostgreSQL backup, not the training-progress JSON export.

GameAnalysisSource:
```
{id,white,black,result,date,event,game_url,source_game_at:string|null,
 username:string|null,chesscom_accuracies:{white?:number,black?:number}|null,
 total_plies,analysis_id:number|null,
 analysis_status:"ready"|"running"|"completed"|"failed"|null,analysis_outdated:boolean,background_requested:boolean}
```

`chesscom_accuracies` contains original, optional game-level percentages from the public archive's `accuracies` field, separately from locally calculated `summary.accuracy`. Only finite numeric values from 0 through 100 are accepted; a missing side is omitted and no valid sides yields `null`. These reference scores do not provide or validate individual move classifications. Sync and repeated historical imports update valid reference sides in every owned copy matched by fingerprint or canonical Chess.com game URL, preserving reports and all other game metadata. Missing or invalid upstream values do not erase a previously known side. Newly imported overlapping batches inherit known reference scores. No additional Chess.com analysis request or Stockfish recalculation is triggered by a reference refresh.

GameAnalysisReport:
```
{id,game_id,game:GameAnalysisSource & {initial_fen:string},
 status:"ready"|"running"|"completed"|"failed",background_requested:boolean,cursor,total_plies,
 analysis_version,analysis_outdated,error:string|null,created_at,updated_at,
 summary:{white:GameSideSummary,black:GameSideSummary,complete:boolean},
 moves:GameMoveAnalysis[],category_names:{[category]:string},method:object}
```
GameSideSummary:
```
{accuracy:number|null,provisional_accuracy:number|null,estimated_chesscom_accuracy:number|null,counts:{[category]:number},
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

`estimated_chesscom_accuracy` is a separate optional game-level approximation from an explicitly installed calibration profile. It never replaces `accuracy`, move classifications, or `game.chesscom_accuracies`. The estimate requires a completed current-version report, fully assessed non-forced evidence for the matching player, and the profile's exact time control. It is `null` outside that scope or when evidence is incomplete. `method.calibration`, when an estimate is present, provides the profile ID, scope, formula and aggregate test metrics. The UI labels this estimate with `≈` and keeps original Chess.com values distinct. Summary calibration is applied when reading a report; it does not rerun Stockfish or mutate saved analysis rows. See [ACCURACY_CALIBRATION.md](ACCURACY_CALIBRATION.md) for the benchmark and its limits.

Each checkpoint and completed report is stored in PostgreSQL independently of tactic-generation state. The lifespan worker continues requested analyses without browser requests, including while the screen is closed. Pausing is explicit; an in-flight step may still save. Saved intent and checkpoints survive application restart.

## Personal coach

All `/api/coach` routes require authentication and use the existing Origin protection. Profiles, games, exercises and reviews are scoped to the authenticated account. One profile per account fixes the player, exact time control and activation timestamp; repeated starts are idempotent and changing those values returns 409 without replacing history.

- `GET /api/coach` -> CoachDashboard; `profile:null` means not started.
- `POST /api/coach/start` `{username:string,time_control?:string}` -> CoachDashboard. Default `300+5`; base 30–10800 seconds, increment 0–180 seconds. `300+0` canonicalizes to `300`. Queues background ingestion, without downloading archives inside the request.
- `POST /api/coach/pause` `{}` -> CoachDashboard. Pauses ingestion and engine work; an in-flight bounded step may finish.
- `POST /api/coach/resume` `{}` -> CoachDashboard. Resumes saved cursors, retries failed games and requests an archive check.
- `POST /api/coach/sync` `{}` -> CoachDashboard. Queues an archive check; a paused coach returns 409. Repeated checks do not duplicate games.
- `POST /api/coach/practice` `{skill_id?:string,game_id?:number,ply?:number,exclude_attempt_id?:number}` -> CoachAttempt. The optional game and zero-based ply must be supplied together and belong to the coach. Without a skill, selects the first priority or calculation practice. Resumes an active task, otherwise selects due repetition, an unseen own-game moment, then a new thematic Lichess puzzle. `exclude_attempt_id` skips the current task for this selection.
- `GET /api/coach/practice/{id}` -> CoachAttempt.
- `POST /api/coach/practice/{id}/move` `{move:string,elapsed_ms?:number}` -> `{accepted,attempt:CoachAttempt,feedback}`. Illegal moves return 422. A wrong legal move leaves the position unchanged and increments mistakes. Own-game decision tasks accept the checked best move or a sufficiently equivalent engine-verified alternative; tactical puzzles follow their checked line, with alternative immediate mates accepted. Temporary engine failure or contention returns 503 without counting an error.
- `POST /api/coach/practice/{id}/hint` optional `{elapsed_ms:number}` -> `{attempt,message,from_square:string|null}`. First hint gives the skill's search direction; later hints identify a starting square. Hints prevent a clean solve.
- `POST /api/coach/practice/{id}/reveal` optional `{elapsed_ms:number}` -> CoachAttempt with solution; schedules repetition.
- `POST /api/coach/practice/{id}/time` `{elapsed_ms:number}` -> CoachAttempt. Cumulative nonnegative milliseconds, at most JavaScript's safe integer maximum; smaller or post-completion updates do not change elapsed time.

CoachDashboard contains `{profile,skills,priorities,summary,methodology,games,active_attempt_id}`. `priorities` are up to three skill IDs. `methodology` is explanatory text. `profile` includes `username,time_control,started_at,active,status,baseline_target:40,baseline_games,baseline_completed,baseline_ready,new_games,new_completed,processed_moves,total_moves,progress,last_sync_at,next_sync_at,error,syncing,review_due`; status is `loading|analysing|tracking|paused|error`. The game list includes source URLs, player result, `cohort:baseline|followup`, analysis status and cursor. Game-level timeout examples have `ply:null`; they link to a game and generic time-management practice rather than an invented position.

A skill contains `{id,title,description,recommendation,practice_tip,themes,priority,status,baseline,recent,delta,comparable,metric_label,evidence,training}`. IDs are `hanging_piece,fork,pin,mate,king_safety,defense,conversion,opening,endgame,time_management,calculation`. Status is `focus|reinforce|monitor|insufficient`. Metric windows contain `{games,opportunities,errors,rate}` plus clock/severity details; `rate` is nullable per 100 relevant assessed non-forced own moves, or per 100 games with a known result reason for timeouts. Phase/starting-position contexts have their own denominators. Motif rates use all eligible own moves. Overlapping skill counts must not be summed. `delta` is the recent rate minus baseline rate only when comparison criteria are met. `training:{completed,clean,new_clean}` reports exercise outcomes separately from game evidence.

CoachAttempt contains `{id,skill_id,skill_title,kind,fen,starting_fen,learner_white,status,mistakes,hints_used,elapsed_ms,last_move,ply_index,total_plies,prompt,source,feedback,solution,target_seconds,is_new}`. `kind` is `decision|puzzle`; status is `active|completed|revealed`. Active attempts never expose their solution. On completion/reveal, `solution:{initial_fen,moves:[{uci,san,fen}],explanation}` is available. `source` includes `game_url,move_number,own_game`. `target_seconds` is an optional practice guide, not an automatic failure deadline. An alternative accepted decision may differ from the stored original line, which is identified in feedback. Clean completions schedule repetition after 1/3/7/14/30/60 days; mistakes, hints or reveal schedule it after 10 minutes. These attempts do not change ordinary puzzle ratings or ordinary training statistics.

The baseline is the newest up to 40 available completed standard Chess.com games of the exact control, including rated and unrated games, ending at or before activation. Monthly archives are paged newest first; fewer than 40 available games remain a smaller baseline. It is immutable once collected. Followup includes games ending after activation, including while initial analysis runs or tracking is paused. The worker checks archives every five minutes while the server runs, catches up missed months after downtime and deduplicates game fingerprints. Public archive publication can lag actual play. Source metadata retains per-ply clocks and elapsed move time adjusted for increment; missing clocks remain null and are not bridged across gaps.

Each own-player move is checked with the shared Stockfish engine limits and durable checkpoints; unassessed moves receive a deeper retry. Temporary source errors preserve the import cursor and retry later. Explicit resume recovers failed engine work. Reports compare baseline with the latest 40 fully processed followup games; partial games do not enter trends. A comparison requires at least 10 games and 50 relevant assessed moves per window (10 known outcomes for timeout rates), the same control, and at least 90% own-move assessment coverage. No psychological cause or calibrated skill rating is inferred. Sparse evidence stays insufficient; exercises alone never establish game mastery. Supplementary puzzles are tactical practice, not comprehensive positional or endgame instruction.

Coach tables are included in full PostgreSQL backups. Existing `/api/progress/export` and its restore format do **not** include coach profiles, games, attempts or reviews.

## Background analysis lifecycle

- Import creation (`/api/imports/chesscom`, `/api/imports/public`), report creation (`/api/game-analysis`), and the reanalyze/restart endpoints accept an optional JSON `background:boolean` (default `false` for compatibility). With `true`, execution is scheduled by the initial request, without a follow-up from the browser. The training library sends `true`; importing games solely for the separate review screen does not start tactical generation.
- `POST /api/imports/{id}/resume` and `POST /api/game-analysis/{id}/resume` `{}` enable background processing. Repeated starts are idempotent, completed records stay completed, failed records become retryable, and outdated versions require explicit recalculation (409).
- `POST /api/imports/{id}/pause` and `POST /api/game-analysis/{id}/pause` `{}` disable background processing. These routes return the updated owned ImportJob/GameAnalysisReport and use the same authentication and Origin protection as the existing routes. Pause can be accepted while the engine is computing; that position may still be saved. `background_requested`, rather than `status:"running"` alone, determines whether more work is scheduled.
- The API lifespan starts a worker, which fairly interleaves queued batches/reports by oldest checkpoint, with one bounded step at a time. PostgreSQL coordinator/advisory locks prevent duplicate background work across processes. Existing per-user and engine locks still apply. Temporary engine/lock contention is retried; engine failures preserve the last checkpoint and require explicit resume. Completion clears background intent.
- Migration `0006_background_analysis` defaults existing records to paused; no historical analysis silently resumes. Pending intent survives process restart and is read from PostgreSQL without a client connection. Graceful shutdown finishes the current bounded position; a crash can only lose the unfinished step.
- The server must be running. The primary WSL server is updated according to `deploy/SERVER.md`; host sleep or shutdown suspends processing until it is running again. The archived Render Free deployment may sleep after 15 minutes without incoming requests. Use an always-on host for uninterrupted long jobs. Screen Wake Lock only keeps the visible training display awake; background execution does not depend on it.

## Stockfish benchmark

Authenticated diagnostics run the fixed `app` profile: Stockfish built-in `bench`, eight bundled positions, NNUE, MultiPV 2, one thread, 32 MiB hash, depth 13, three sequential repetitions. Each process has a 60-second timeout. These routes never modify games, puzzles, attempts or saved analyses.

- `GET /api/diagnostics/stockfish-benchmark` -> profile and limits.
- `POST /api/diagnostics/stockfish-benchmark` `{}` -> HTTP 202 with `{id,status,started_at,finished_at,profile,repetitions,result,error}`. Extra request fields are rejected. The job runs in a background thread; repeat starts by its owner return the same running job.
- `GET /api/diagnostics/stockfish-benchmark/{id}` -> owned job status and, after completion, the report. Status is `running`, `completed` or `failed`. Errors use `{code,detail}` and may retain partial results.

The benchmark holds the shared engine slot for the whole job. A busy engine or another user's running benchmark returns 503; a 30-second cooldown after completion returns 429. Polling does not start additional processes. Results are held in bounded memory for up to 30 minutes from job creation and disappear on application restart. Unknown or unowned jobs return 404. The CLI client saves the returned JSON for comparison; see [BENCHMARK.md](BENCHMARK.md).

## Portable progress
`GET /api/progress/export` -> authenticated JSON attachment `{format_version:1|2,exported_at,username,attempts:[...],reviews:[...],generated_puzzles?:[...]}`. Contains only the current user's progress, no passwords/sessions. Pure Lichess exports retain version 1. Version 2 includes all owned personal puzzle definitions (including unplayed ones), plus generated public puzzles referenced by progress. These definitions contain solution moves so training remains portable. Optional `source_game_at`, `source_ply` and `created_at` preserve chronological practice order; earlier version-2 exports without those fields remain supported, with unknown game dates left null. Import jobs, their intermediate analysis and independent whole-game reports (including completed ones) are not included; use a full PostgreSQL backup for those.

Restore on a new server with `python -m app.restore_progress FILE --username USER`. The target account must exist and have no attempts, reviews or personal puzzles. The CLI validates references, legal puzzle lines and states before importing atomically; it never replaces existing progress. Personal definitions and references are remapped to the target owner. Attempt records include nullable `elapsed_ms`. Older format-version-1 backups without the field remain importable and retain unknown durations. Existing export size and record limits remain enforced.

## Health
`GET /api/health` -> `{status:"ok",database:"ok",engine_available:boolean}`. No authentication required.

Optional `FRONTEND_DIST` serves a built frontend at `/` in the Render Free image. API routes remain distinct and unknown `/api/*` paths return JSON 404.

## Seed data
`data/puzzles.csv` uses standard Lichess CSV columns including PuzzleId,FEN,Moves,Rating,Themes,GameUrl,OpeningTags. IMPORTANT: FEN is BEFORE first opponent move. Apply first move for initial learner position; solution begins at second move. Dataset is CC0; provenance in `data/README.md`.

`data/generated-puzzles.json` supplies the independently generated public bank; `data/public-games.pgn` supplies its complete real-game corpus. Bootstrap validates generated identities and legal moves, then inserts idempotently. Both use the same before-opponent-move convention. Source and reproducible generator commands are recorded in `data/README.md`.
