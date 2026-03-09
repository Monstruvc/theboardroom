# The Boardroom — Deployment Guide

## Files in this folder
- `fantasy-hoops.html` — The frontend app
- `server.py` — Flask backend (NBA stats + Yahoo OAuth)
- `requirements.txt` — Python dependencies
- `Procfile` — Process config for Railway/Render
- `railway.toml` — Railway-specific config

---

## Deploy to Railway (recommended, free tier)

1. **Create a GitHub repo** and push all 5 files into it

2. **Go to railway.app** → New Project → Deploy from GitHub → select your repo

3. **Add environment variables** in Railway dashboard → Variables:
   ```
   YAHOO_CLIENT_ID      = dj0yJmk9TmV3N2cwSzVBZmtlJmQ9WVdrOWRHVkJUV3hTVEhvbWNHbzlNQT09JnM9Y29uc3VtZXJzZWNyZXQmc3Y9MCZ4PWZj
   YAHOO_CLIENT_SECRET  = 26b682df3839b70b040110b696a248d3c48fa442
   YAHOO_REDIRECT_URI   = https://keyzen.art/auth/callback
   FLASK_SECRET         = (generate any random string)
   ```

4. **Railway will assign a URL** like `something.railway.app`

5. **Point keyzen.art to Railway:**
   - In your domain registrar/DNS: add a CNAME record
     - Name: `@` (or `keyzen.art`)
     - Value: your Railway domain (e.g. `boardroom.up.railway.app`)
   - In Railway: Settings → Domains → add `keyzen.art`

6. **Done.** Visit https://keyzen.art → click "Connect Yahoo" → authorize → rosters sync automatically.

---

## Environment Variables Reference
| Variable | Value |
|---|---|
| YAHOO_CLIENT_ID | Your Yahoo app consumer key |
| YAHOO_CLIENT_SECRET | Your Yahoo app consumer secret |
| YAHOO_REDIRECT_URI | https://keyzen.art/auth/callback |
| FLASK_SECRET | Any random string (for session security) |
| PORT | Set automatically by Railway |

---

## OAuth Flow
1. User clicks "Connect Yahoo" → `/auth/login` → Yahoo login page
2. User authorizes → Yahoo redirects to `https://keyzen.art/auth/callback?code=...`
3. Server exchanges code for access + refresh tokens (stored in memory)
4. Frontend detects `?yahoo=connected` and loads league data automatically
5. Tokens auto-refresh when they expire (1-hour lifetime)

**Note:** Tokens are stored in memory — if the server restarts, users need to re-authenticate.
For persistence, swap `_token_store` dict for a SQLite/Redis store.
