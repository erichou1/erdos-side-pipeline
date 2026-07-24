# Side Pipeline — automated ChatGPT research runs

Drives a **headed Chromium** against ChatGPT to attack a list of hard math /
theoretical-CS problems, one ChatGPT conversation per problem. For each problem
it runs a three-stage flow and saves per-iteration progress to disk:

1. **Adapt** — in a fresh chat, it turns a fixed meta-prompt into a
   problem-specific research prompt.
2. **Research** — in a second fresh chat (one per problem, so you can keep
   chatting there afterwards), it submits the adapted prompt and captures the
   first attempt.
3. **Continue** — it nudges the same chat forward up to a cap, then asks the
   model to self-assess whether the problem is *actually solved*, and finally
   spins up an independent chat to referee the proof before marking it solved.

It runs N problems concurrently as N browser tabs, cooperatively scheduled on a
single thread, and writes each problem's state to
`erdos_problems/side_pipeline_runs/<id>.json`.

---

## Important: this needs a real (non-headless) desktop session

ChatGPT blocks headless browsers (Cloudflare), so the pipeline runs a **visible
Chromium window**. On a Mac Mini that means you must run it inside a
**logged-in graphical session**, not a bare SSH shell:

- Use it at the physically connected display, **or**
- Connect with **Screen Sharing / VNC** (macOS "Remote Management"). Keep that
  session logged in — if the console logs out, the browser can't launch.
- Keep the Mac awake while it runs (see [Keeping it running](#keeping-it-running)).

You interact with ChatGPT's UI **once** (to log in and pick a model); after that
it runs unattended.

---

## Prerequisites

- macOS on the Mac Mini (Apple Silicon or Intel).
- **Python 3.11+** (`python3 --version`).
- A **ChatGPT account** with access to the model you want the runs to use
  (e.g. GPT Pro / GPT-5 Thinking).

---

## Setup (exact steps)

```bash
# 1. Clone
git clone https://github.com/erichou1/erdos-side-pipeline.git
cd erdos-side-pipeline

# 2. Create a virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Install the Chromium that Playwright drives
playwright install chromium

# 4. Log in to the SAME ChatGPT account and choose your model
#    (opens a Chromium window)
python login.py
#    - Log in with the SAME ChatGPT account you use now.
#    - Select the same model (e.g. GPT Pro) in the UI.
#    - New chats the pipeline opens inherit that model.
#    - Press Enter in the terminal to save the session and close.
#    (Or skip this and copy your existing .chatgpt_profile over — see below.)

# 5. Run it — same project, same everything
python side_pipeline.py --workers 20
```

That's it. It already targets the **same `erdos3` Project** by default, so no
project flag is needed. Progress is printed to the terminal and written under
`erdos_problems/side_pipeline_runs/`.

> The login session is stored in `./.chatgpt_profile/` (git-ignored). You only
> redo the login if you log out or the session expires.

---

## Choosing the model

The pipeline **does not** switch models in code — it uses whatever model is
currently selected in the ChatGPT UI for that profile. Set it in `login.py`
(step 5) by picking the model from ChatGPT's model dropdown. New chats inherit
the last-selected model.

---

## Same account & project (already configured)

The goal is to run **exactly what runs today** — same ChatGPT account, same
`erdos3` Project — just on the Mac Mini.

- **Same project:** already the built-in default, so plain
  `python side_pipeline.py --workers 20` targets the `erdos3` Project. Nothing
  to set.
- **Same account — two options:**
  1. **Log in again** (simplest): `python login.py`, sign in with the same
     ChatGPT account, pick the same model, press Enter.
  2. **Copy the exact session** (no re-login): from the machine running it now,
     copy the whole `.chatgpt_profile/` directory into the clone on the Mac
     Mini. This transfers the logged-in session as-is. Do this **while nothing
     is using either profile**, and copy it directly (AirDrop / `scp` / an
     external drive) — never commit it to git. Example:
     ```bash
     # on the Mac Mini, from the repo root:
     scp -r you@current-mac:/Users/eric/workspace/erdos/.chatgpt_profile ./.chatgpt_profile
     ```
     > Only ONE Chromium may use a profile at a time, so don't run the pipeline
     > on both machines against the same copied profile simultaneously.

To override the Project on this machine only, set `CHATGPT_PROJECT_URL` in `.env`
(copy from `.env.example`) or pass `--project-url "..."` (the flag wins).

---

## The problem list

`side_pipeline_problems.json` is a JSON array of objects:

```json
[
  {
    "id": "AINT-23",
    "title": "Short human-readable title",
    "statement": "Full problem statement the model must solve.",
    "references": "URLs / notes the adapt stage should consult."
  }
]
```

- Ships with ~350 ranked math + TCS problems.
- `side_pipeline_problems.example.json` is a tiny sample to copy from.
- Point the pipeline at any file with `--problems my_problems.json`.
- `build_side_pipeline_problems.py` is the helper used to generate the shipped
  list from a source Markdown file (reference only; edit paths inside to reuse).

---

## Common options

```bash
python side_pipeline.py \
  --problems side_pipeline_problems.json \
  --workers 20 \          # concurrent problems / browser tabs
  --max-rounds 20         # continuation nudges after the first attempt
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--workers` | 20 | How many problems run at once (one tab each). |
| `--max-rounds` | 20 | Continuation rounds before giving up on a problem. |
| `--problems` | `side_pipeline_problems.json` | Problem list to run. |
| `--project-url` | the `erdos3` project | Project to create chats in. |
| `--profile-dir` | `./.chatgpt_profile` | Chromium profile (login state). |
| `--dry-run` | off | No browser; simulates the flow to sanity-check state files. |

Workers start **staggered** (a new chat opens every ~20s) to be gentle on
ChatGPT's rate limits, so the active count ramps up over the first few minutes.

---

## Where results go

- Per-problem state: `erdos_problems/side_pipeline_runs/<id>.json` — includes the
  adapted prompt, each research attempt, self-assessment, verification, and the
  conversation URLs so you can open any chat yourself.
- The chats themselves live in your ChatGPT account (in the Project if you set one).

## Resuming

Just run the same command again. On startup the pipeline reads the state files
and **resumes** in-progress problems, skips finished ones, and starts the rest.
To start completely fresh, delete `erdos_problems/side_pipeline_runs/` first.

---

## Keeping the website updated (from the Mac Mini)

`side_pipeline.py` only writes local files — it does **not** touch the status
website. The site's `data.json` is normally rebuilt by a separate publisher on
the main machine that needs a private database. So to keep the site's
side-pipeline section updating **even while the main Mac is closed**, run the
bundled lightweight publisher on the Mac Mini alongside the pipeline:

```bash
# one-time: let git push to the private status repo from the Mini
gh auth login          # authenticate as the same GitHub account (erichou1)

# smoke-test one cycle
python side_status_publish.py --once

# run it continuously (updates the site every ~60s)
caffeinate -dimsu python side_status_publish.py
```

Every ~60s it fetches the current `data.json`, replaces **only** the
`side_pipeline` section with a fresh build from your local
`erdos_problems/side_pipeline_runs/`, refreshes the liveness timestamp, and
force-updates the `status-live` branch. It needs **no** database or secrets —
just git push access to the status repo.

> **Only one publisher may write `status-live` at a time.** When the Mac Mini is
> the authoritative runner, **stop the main machine's**
> `status_site/live_refresh.py` — otherwise the two overwrite each other's
> side-pipeline section every cycle. While the Mini publishes, the site's other
> (EGMRA) sections stay frozen at their last snapshot; the side-pipeline section
> updates live.

---

## Keeping it running

Runs can take a long time (especially on slow "thinking" models). Prevent sleep
and keep it alive across an SSH disconnect:

```bash
# Prevent the Mac from sleeping while attached to the run
caffeinate -dimsu python side_pipeline.py --workers 20

# ...or detach with tmux (recommended over nohup for a headed GUI app)
brew install tmux            # if needed
tmux new -s pipeline
caffeinate -dimsu python side_pipeline.py --workers 20
#   detach: Ctrl-b then d   |   reattach later: tmux attach -t pipeline
```

Also set **System Settings → Displays / Battery → "Prevent automatic sleeping"**
(and disable screen lock) so the graphical session the browser needs stays up.

---

## Troubleshooting

- **"Not logged into ChatGPT in profile ..."** — run `python login.py`, log in,
  press Enter, then rerun.
- **Chromium won't launch / crashes immediately** — you're likely in a headless
  SSH shell. Run inside a real desktop session (Screen Sharing / console).
- **"SingletonLock" / profile busy** — only one Chromium may use a profile at a
  time. Make sure no other run (or `login.py`) is open on the same profile. If a
  stale lock remains: `rm -f .chatgpt_profile/SingletonLock`.
- **Rate limited / slow** — the pipeline backs off automatically. Reduce
  `--workers` if a slow, usage-capped model keeps throttling.
- **Nothing seems to happen for a while** — deep-reasoning models can take many
  minutes per reply; watch the timestamped log lines.

---

## Files

| File | Purpose |
|------|---------|
| `side_pipeline.py` | The pipeline (state machine + scheduler + CLI). |
| `erdos_common.py` | Shared ChatGPT page helpers (compose, extract, rename, rate-limit). |
| `login.py` | One-time login / model-selection helper. |
| `side_pipeline_meta_prompt.txt` | The meta-prompt the adapt stage specializes. |
| `side_pipeline_problems.json` | The problem list to run. |
| `side_pipeline_problems.example.json` | Minimal sample problem list. |
| `build_side_pipeline_problems.py` | Helper that generated the shipped list. |
| `side_status_publish.py` | Lightweight, DB-free publisher that keeps the website's side-pipeline section live from this machine. |

Not committed (git-ignored): `.chatgpt_profile/` (your session), `.env`,
`erdos_problems/side_pipeline_runs/` (run output), logs.
