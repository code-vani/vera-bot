# Deploying this bot to get a public URL

The challenge requires a **public bot URL** the judge harness can call. I
can't expose a public URL from this environment, so you'll need to deploy
these files somewhere. Fastest free options, easiest first:

## Option A — Render (free tier, ~5 min, no card needed for basic tier)
1. Push this folder to a new GitHub repo (just these files: `bot.py`,
   `composer.py`, `conversation.py`, `requirements.txt`, `Dockerfile`).
2. Go to https://render.com → New → Web Service → connect the repo.
3. Render auto-detects the `Dockerfile`. Leave build/start commands blank
   (Dockerfile handles it). Instance type: Free.
4. Deploy. Render gives you a URL like `https://your-bot.onrender.com`.
5. That's your submission URL. Note: free tier sleeps after inactivity —
   the judge's `/v1/healthz` poll will wake it, but the very first call
   after a sleep can be slow. If the harness is strict about that, upgrade
   to a paid instance for the test window.

## Option B — Railway (free trial credits, similarly simple)
1. https://railway.app → New Project → Deploy from GitHub repo.
2. It also picks up the `Dockerfile` automatically.
3. Under Settings → Networking, generate a public domain.

## Option C — Fly.io
```bash
fly launch     # follow prompts, it'll detect the Dockerfile
fly deploy
```

## Option D — quick & temporary: ngrok (for local testing only, not for
final submission — tunnels die when your laptop sleeps)
```bash
uvicorn bot:app --host 0.0.0.0 --port 8080 &
ngrok http 8080
```

## Before you submit
- Hit `https://<your-url>/v1/healthz` from a browser or `curl` — confirm it
  returns `200` with real counts once you've pushed context.
- Run `local_test.py` against the deployed URL (change `BASE` at the top of
  the file) to be sure nothing broke in deployment.
- Keep the instance awake/live for the full evaluation window per the brief.
