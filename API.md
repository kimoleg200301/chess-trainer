# Tactics Lab API contract

Same-origin `/api`; authenticated session cookie. Personal app, no public signup.
All failures use `{ "detail": "human-readable Russian error" }`.

## Authentication
- `POST /api/auth/login` JSON `{username,password}` -> `{username}` and HttpOnly session cookie.
- `GET /api/auth/me` -> `{username}`; 401 otherwise.
- `POST /api/auth/logout` -> 204.

## Catalog
`GET /api/meta` -> `{total_puzzles, themes:[{id,name,count}], min_rating,max_rating}`.

## Training
`POST /api/training/start` JSON `{theme:null|string,min_rating:800,max_rating:1800,mode:"practice"|"review"}` -> Attempt. Return 404 if empty.

Attempt:
```
{id:number,puzzle_id:string,fen:string,rating:number,themes:string[],
 status:"active"|"completed"|"revealed",turn:"white"|"black",
 solved_plies:number,total_plies:number,mistakes:number,hints_used:number,
 last_move:string|null,elapsed_ms:number|null}
```
`turn` is the learner color throughout an attempt. FEN is authoritative.

- `POST /api/training/{id}/move` `{move:"e2e4",elapsed_ms?:number}` -> `{accepted:boolean,attempt:Attempt,reply_move:string|null,feedback:string}`. Correct learner move plus automatic opponent reply. An incorrect legal move does NOT change the board, but increases mistakes. Invalid moves return 422.
- `POST /api/training/{id}/hint` optional `{elapsed_ms:number}` -> `{message:string,from_square:string|null,attempt:Attempt}`. Repeated hints count once; not a clean solve.
- `POST /api/training/{id}/reveal` optional `{elapsed_ms:number}` -> `{attempt:Attempt,solution:[{uci:string,san:string}],game_url:string}`. Solution starts at initial learner position.
- `POST /api/training/{id}/time` `{elapsed_ms:number}` -> Attempt. Saves active solving time without making a move; ownership and Origin rules match other training actions.
- `GET /api/training/{id}/analysis` -> `{evaluation:string,line:string[],engine:string}`. Only completed/revealed; Stockfish analysis may return 503 if unavailable. SAN line starting from current position.

`elapsed_ms` is measured active solving time, sent by the client as a nonnegative integer in milliseconds (at most JavaScript's maximum safe integer). The client pauses timing outside the visible solving screen and while awaiting actions. Updates are cumulative and monotonic; delayed lower values do not decrease time, and finalized attempts ignore late timing updates. Incorrect legal moves and hints also save the supplied time. No-body hint/reveal requests and old move requests remain supported. New attempts start with `elapsed_ms:null` until a measurement is received; `null` means unknown, not zero. Historical records are not estimated from wall-clock timestamps.

## Statistics
`GET /api/stats` -> `{solved:number,total_attempts:number,accuracy:number,streak:number,due_count:number,average_solution_ms:number|null,timed_solutions:number,by_theme:[{id:string,name:string,attempts:number,solved:number}],recent:[{puzzle_id:string,rating:number,status:string,created_at:string,elapsed_ms:number|null}]}`.
Accuracy is percent of finalized attempts solved cleanly (no mistakes, hints, or reveal). Streak is consecutive clean solves, not daily streak. `average_solution_ms` is the arithmetic mean of measured completed solutions, rounded to whole milliseconds; active, revealed and unknown-time attempts are excluded. `timed_solutions` is the number contributing to this mean; a measured zero is valid. Recent attempts include their saved duration, which is frozen on completion or reveal.

## Portable progress
`GET /api/progress/export` -> authenticated JSON attachment `{format_version:1,exported_at,username,attempts:[...],reviews:[...]}`. Contains only the current user's progress, no password/session/solution data. Limited to 5 MiB and 10,000 records in each collection.

Restore on a new server with `python -m app.restore_progress FILE --username USER`. The target account must exist and have no attempts/reviews. The CLI validates all references and states, then imports atomically; it never replaces existing progress. Attempt records include nullable `elapsed_ms`. Older format-version-1 backups without the field remain importable and retain unknown durations.

## Health
`GET /api/health` -> `{status:"ok",database:"ok",engine_available:boolean}`. No authentication required.

Optional `FRONTEND_DIST` serves a built frontend at `/` in the Render Free image. API routes remain distinct and unknown `/api/*` paths return JSON 404.

## Seed data
Root will create `data/puzzles.csv` with standard Lichess CSV columns including PuzzleId,FEN,Moves,Rating,Themes,GameUrl,OpeningTags. IMPORTANT: FEN is BEFORE first opponent move. Apply first move for initial learner position; solution begins at second move. Dataset is CC0; provenance in `data/README.md`.
