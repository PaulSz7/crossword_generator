# Plan: Add Backend to Run Crossword Generator from Web UI

## Context
The Generate tab currently only builds a CLI command string for copy-paste. We want to actually execute the generator from the browser and stream logs + results back in real-time. No web framework exists in the project, so we use Python's stdlib `http.server`.

## Deliverables
1. **`web/server.py`** — new file (~150 lines), stdlib HTTP server
2. **`web/prototype.html`** — modify Generate tab + add gallery auto-load

---

## 1. `web/server.py`

**Routes:**

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Serve `web/prototype.html` |
| GET | `/api/crosswords` | List all JSON docs from `local_db/collections/crosswords/` |
| POST | `/api/generate` | Run generator subprocess, stream NDJSON response |
| DELETE | `/api/generate` | Cancel running generation |

**POST `/api/generate` flow:**
- Receive JSON body with form params (camelCase keys)
- `build_cli_args(params)` converts to `["--height", "15", ...]`
- Spawn: `subprocess.Popen([sys.executable, "-m", "main", *args], stdout=PIPE, stderr=PIPE)` with `PYTHONUNBUFFERED=1`
- Stream stderr lines as `{"type":"log","text":"..."}\n`
- On exit code 0: read stdout, send `{"type":"result","data":{...}}\n` then `{"type":"done","success":true}\n`
- On failure: send `{"type":"error","text":"..."}` then `{"type":"done","success":false}`
- Use `selectors.DefaultSelector` to multiplex stderr reads without blocking
- Module-level `_active_process` — only one generation at a time (409 if busy)

**DELETE `/api/generate`:** `terminate()` then `kill()` after 2s if needed.

**GET `/api/crosswords`:** Glob `*.json` from store dir, parse each, return as JSON array (newest first).

**Streaming:** Skip `Transfer-Encoding: chunked` — just flush after each write. Browsers handle streaming fetch fine with an open connection.

---

## 2. `web/prototype.html` Changes

**CSS additions:**
- `.log-output` — terminal-like area (black bg, monospace, max-height 400px, overflow-y scroll, auto-scroll)
- `.log-line` — individual line, color-coded by level (INFO=dim, WARNING=yellow, ERROR=red)
- `.gen-status` — running/success/error indicator next to buttons

**HTML changes to Generate tab:**
- Add **Run** button and **Cancel** button (hidden until running) next to existing "Generate Command"
- Add status indicator span
- Add log output area below the command block

**New JS functions:**

- `collectFormParams()` — extract form values into a JSON object (mirrors `buildCommand()` logic but as data)
- `runGeneration()` — POST to `/api/generate` with `fetch()`, read `response.body` as `ReadableStream`, parse NDJSON lines, dispatch to `handleStreamMessage()`
- `handleStreamMessage(msg)` — append logs, handle result/done/error
- `appendLog(text, level)` — add line to log area with auto-scroll
- `cancelGeneration()` — `AbortController.abort()` + DELETE request
- `loadGalleryFromServer()` — fetch `/api/crosswords`, merge into `galleryItems`, re-render gallery; called on page load (silently fails if no server)
- `viewLatestCrossword()` — switch to Crosswords tab, open the newest gallery item

**Running state:** Run button disabled + Cancel shown while active. `activeAbort` (AbortController) tracks the in-flight request.

**After success:** Refresh gallery from server, show "Done! View crossword" link that switches to the Crosswords tab and opens the new puzzle.

---

## Key Files
- `web/server.py` — **new**
- `web/prototype.html` — **modify** (Generate tab UI + JS streaming + gallery auto-load)
- `main.py` — read-only (CLI arg reference)
- `crossword/engine/crossword_store.py` — read-only (store dir path: `local_db/collections/crosswords/`)

## Verification
1. `cd crossword_generator && python web/server.py` — starts on http://127.0.0.1:8080
2. Open browser, Crosswords tab should auto-populate gallery from store
3. Generate tab: fill form (e.g. height=15, width=12, theme="Test", difficulty=EASY, words=MARE), click Run
4. Logs stream in real-time, result appears on completion
5. "View crossword" link switches to gallery and opens the new puzzle
6. Test cancel mid-generation
7. Test with no server (open file directly) — gallery falls back to manual file picker, Run button fails gracefully
