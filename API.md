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
 last_move:string|null}
```
`turn` is the learner color throughout an attempt. FEN is authoritative.

- `POST /api/training/{id}/move` `{move:"e2e4"}` -> `{accepted:boolean,attempt:Attempt,reply_move:string|null,feedback:string}`. Correct learner move plus automatic opponent reply. An incorrect legal move does NOT change the board, but increases mistakes. Invalid moves return 422.
- `POST /api/training/{id}/hint` -> `{message:string,from_square:string|null,attempt:Attempt}`. Repeated hints count once; not a clean solve.
- `POST /api/training/{id}/reveal` -> `{attempt:Attempt,solution:[{uci:string,san:string}],game_url:string}`. Solution starts at initial learner position.
- `GET /api/training/{id}/analysis` -> `{evaluation:string,line:string[],engine:string}`. Only completed/revealed; Stockfish analysis may return 503 if unavailable. SAN line starting from current position.

## Statistics
`GET /api/stats` -> `{solved:number,total_attempts:number,accuracy:number,streak:number,due_count:number,by_theme:[{id:string,name:string,attempts:number,solved:number}],recent:[{puzzle_id:string,rating:number,status:string,created_at:string}]}`.
Accuracy is percent of finalized attempts solved cleanly (no mistakes, hints, or reveal). Streak is consecutive clean solves, not daily streak.

## Health
`GET /api/health` -> `{status:"ok",database:"ok",engine_available:boolean}`. No authentication required.

## Seed data
Root will create `data/puzzles.csv` with standard Lichess CSV columns including PuzzleId,FEN,Moves,Rating,Themes,GameUrl,OpeningTags. IMPORTANT: FEN is BEFORE first opponent move. Apply first move for initial learner position; solution begins at second move. Dataset is CC0; provenance in `data/README.md`.
