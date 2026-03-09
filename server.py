"""
The Boardroom — server.py
Flask backend with:
  - NBA Stats API (live per-game stats)
  - Yahoo Fantasy OAuth 2.0 (roster sync, standings, league data)
  - Static file serving for fantasy-hoops.html
"""

import os, time, json, secrets, requests
from datetime import datetime
from urllib.parse import urlencode
from flask import Flask, jsonify, request, redirect, session, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=".")
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))
CORS(app, supports_credentials=True)

# ─── YAHOO CONFIG ───────────────────────────────────────────────────────────
YAHOO_CLIENT_ID     = os.environ.get("YAHOO_CLIENT_ID",     "dj0yJmk9TmV3N2cwSzVBZmtlJmQ9WVdrOWRHVkJUV3hTVEhvbWNHbzlNQT09JnM9Y29uc3VtZXJzZWNyZXQmc3Y9MCZ4PWZj")
YAHOO_CLIENT_SECRET = os.environ.get("YAHOO_CLIENT_SECRET", "26b682df3839b70b040110b696a248d3c48fa442")
YAHOO_REDIRECT_URI  = os.environ.get("YAHOO_REDIRECT_URI",  "https://keyzen.art/auth/callback")
YAHOO_AUTH_URL  = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
YAHOO_API_BASE  = "https://fantasysports.yahooapis.com/fantasy/v2"

# ─── TOKEN STORE (in-process dict, safe with 1 worker + threads) ────────────
_cache = {}
CACHE_TTL       = 3600
YAHOO_CACHE_TTL = 300

# Single global dict — persists for the lifetime of the process
_token_store = {
    "access_token":  None,
    "refresh_token": None,
    "expires_at":    0,
}

def _cached(key, fn, ttl=CACHE_TTL):
    now = time.time()
    if key in _cache and now - _cache[key][1] < ttl:
        return _cache[key][0]
    data = fn()
    _cache[key] = (data, now)
    return data

# ─── STATIC ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "fantasy-hoops.html")

# ─── HEALTH ─────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    now = datetime.now()
    y = now.year
    season = f"{y}-{str(y+1)[2:]}" if now.month >= 10 else f"{y-1}-{str(y)[2:]}"
    nba_ok = True
    try:
        from nba_api.stats.endpoints import leaguedashplayerstats  # noqa
    except ImportError:
        nba_ok = False
    return jsonify({
        "status": "ok",
        "season": season,
        "yahoo_authenticated": bool(_token_store.get("access_token")),
        "nba_api_available": nba_ok
    })

# ─── NBA STATS ───────────────────────────────────────────────────────────────
def _current_season():
    now = datetime.now()
    y = now.year
    return f"{y}-{str(y+1)[2:]}" if now.month >= 10 else f"{y-1}-{str(y)[2:]}"

def _est_dd(pts, reb, ast, stl, blk):
    cats = [pts, reb, ast, stl*5, blk*6]
    doubles = sum(1 for v in cats if v >= 10)
    if doubles >= 2: return round(min(0.95, 0.70 + doubles*0.08), 3)
    near = sum(1 for v in cats if v >= 7)
    if near >= 2: return round(min(0.55, 0.25 + near*0.10), 3)
    return round(min(0.25, pts/60 + reb/40 + ast/35), 3)

def _est_td(pts, reb, ast, stl, blk):
    cats = [pts, reb, ast, stl*5, blk*6]
    doubles = sum(1 for v in cats if v >= 10)
    if doubles >= 3: return round(min(0.55, 0.30 + doubles*0.05), 3)
    if doubles >= 2 and sum(1 for v in cats if v >= 7) >= 3: return 0.12
    return round(min(0.08, pts/200 + reb/120 + ast/100), 3)

def _fetch_nba_stats(season):
    try:
        from nba_api.stats.endpoints import leaguedashplayerstats
        time.sleep(0.6)
        result = leaguedashplayerstats.LeagueDashPlayerStats(
            season=season, per_mode_simple="PerGame", season_type_all_star="Regular Season")
        df = result.get_data_frames()[0]
        players = []
        for _, row in df.iterrows():
            if row.get("GP", 0) < 3: continue
            pts=float(row.get("PTS",0)); reb=float(row.get("REB",0)); ast=float(row.get("AST",0))
            stl=float(row.get("STL",0)); blk=float(row.get("BLK",0)); to=float(row.get("TOV",0))
            fgm=float(row.get("FGM",0)); ftm=float(row.get("FTM",0)); tpm=float(row.get("FG3M",0))
            players.append({
                "name": row.get("PLAYER_NAME",""), "team": row.get("TEAM_ABBREVIATION",""),
                "gp": int(row.get("GP",0)), "min": round(float(row.get("MIN",0)),1),
                "pts":round(pts,1),"reb":round(reb,1),"ast":round(ast,1),
                "stl":round(stl,1),"blk":round(blk,1),"to":round(to,1),
                "fgm":round(fgm,1),"ftm":round(ftm,1),"threepm":round(tpm,1),
                "dd_est":_est_dd(pts,reb,ast,stl,blk),"td_est":_est_td(pts,reb,ast,stl,blk),
            })
        return {"season":season,"players":players,"count":len(players),"source":"nba_api"}
    except Exception as e:
        return {"error":str(e),"season":season,"players":[],"source":"nba_api_error"}

@app.route("/api/stats/current")
def stats_current():
    season = _current_season()
    data = _cached(f"nba_{season}", lambda: _fetch_nba_stats(season))
    if not data.get("players"):
        prev = f"{int(season[:4])-1}-{season[:4][2:]}"
        data = _cached(f"nba_{prev}", lambda: _fetch_nba_stats(prev))
        data["note"] = "Current season unavailable, showing previous"
    return jsonify(data)

@app.route("/api/stats/season/<season>")
def stats_season(season):
    return jsonify(_cached(f"nba_{season}", lambda: _fetch_nba_stats(season)))

@app.route("/api/cache/clear")
def cache_clear():
    _cache.clear()
    return jsonify({"cleared": True})

# ─── YAHOO OAUTH ─────────────────────────────────────────────────────────────
_pending_states = {}  # avoids session cookie issues on Railway
@app.route("/auth/login")
def yahoo_login():
    state = secrets.token_urlsafe(16)
    _pending_states[state] = time.time()
    # Clean up old states older than 10 min
    cutoff = time.time() - 600
    for k in list(_pending_states.keys()):
        if _pending_states[k] < cutoff:
            del _pending_states[k]
    params = {"client_id":YAHOO_CLIENT_ID,"redirect_uri":YAHOO_REDIRECT_URI,
              "response_type":"code","scope":"fspt-r","state":state}
    return redirect(f"{YAHOO_AUTH_URL}?{urlencode(params)}")

@app.route("/auth/callback")
def yahoo_callback():
    error = request.args.get("error")
    if error:
        return f"<h2>Auth error: {error}</h2><a href='/'>Back</a>", 400
    code  = request.args.get("code")
    state = request.args.get("state")
    if state not in _pending_states:
        return "<h2>State invalid — please try connecting again</h2><a href=\'/?retry=1\'>Back</a>", 403
    del _pending_states[state]
    resp = requests.post(YAHOO_TOKEN_URL, data={
        "grant_type":"authorization_code","code":code,"redirect_uri":YAHOO_REDIRECT_URI,
    }, auth=(YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET))
    if not resp.ok:
        return f"<h2>Token exchange failed</h2><pre>{resp.text}</pre>", 400
    tok = resp.json()
    _token_store["access_token"]  = tok["access_token"]
    _token_store["refresh_token"] = tok.get("refresh_token","")
    _token_store["expires_at"]    = time.time() + tok.get("expires_in", 3600)
    return redirect("/?yahoo=connected")

@app.route("/auth/logout")
def yahoo_logout():
    _token_store.clear()
    return redirect("/")

@app.route("/auth/status")
def auth_status():
    authenticated = bool(_token_store.get("access_token"))
    expired = authenticated and time.time() > _token_store.get("expires_at", 0)
    return jsonify({"authenticated": authenticated and not expired, "expired": expired})

def _get_access_token():
    if not _token_store.get("access_token"): return None
    if time.time() > _token_store.get("expires_at", 0) - 60:
        resp = requests.post(YAHOO_TOKEN_URL, data={
            "grant_type":"refresh_token","refresh_token":_token_store.get("refresh_token",""),
        }, auth=(YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET))
        if resp.ok:
            tok = resp.json()
            _token_store["access_token"] = tok["access_token"]
            _token_store["expires_at"]   = time.time() + tok.get("expires_in", 3600)
            if tok.get("refresh_token"): _token_store["refresh_token"] = tok["refresh_token"]
        else: return None
    return _token_store["access_token"]

def _yahoo_get(path, use_cache=True, ttl=YAHOO_CACHE_TTL):
    def _fetch():
        token = _get_access_token()
        if not token: return {"error":"not_authenticated"}
        sep = "&" if "?" in path else "?"
        r = requests.get(f"{YAHOO_API_BASE}{path}{sep}format=json",
                         headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401: return {"error":"token_expired"}
        if not r.ok: return {"error":f"yahoo_{r.status_code}","detail":r.text[:300]}
        return r.json()
    return _cached(f"yahoo_{path}", _fetch, ttl) if use_cache else _fetch()

def _parse_info_list(info_list):
    """Yahoo returns team/player info as a list of dicts — merge them."""
    d = {}
    for item in info_list:
        if isinstance(item, dict):
            d.update(item)
    return d

# ─── YAHOO FANTASY ENDPOINTS ─────────────────────────────────────────────────

@app.route("/api/yahoo/leagues")
def yahoo_leagues():
    data = _yahoo_get("/users;use_login=1/games;game_codes=nba/leagues", use_cache=False)
    if "error" in data: return jsonify(data), 401
    try:
        games = data["fantasy_content"]["users"]["0"]["user"][1]["games"]
        leagues = []
        for i in range(games["count"]):
            game = games[str(i)]["game"]
            game_info = game[0]
            if "leagues" not in game[1]: continue
            league_data = game[1]["leagues"]
            for j in range(league_data["count"]):
                lg = league_data[str(j)]["league"][0]
                leagues.append({
                    "league_key": lg.get("league_key"), "league_id": lg.get("league_id"),
                    "name": lg.get("name"), "season": lg.get("season"),
                    "num_teams": lg.get("num_teams"), "scoring_type": lg.get("scoring_type"),
                    "draft_status": lg.get("draft_status"), "current_week": lg.get("current_week"),
                })
        return jsonify({"leagues": leagues})
    except Exception as e:
        return jsonify({"error":"parse_error","detail":str(e),"raw":str(data)[:500]}), 500

@app.route("/api/yahoo/league/<league_key>/standings")
def yahoo_standings(league_key):
    data = _yahoo_get(f"/league/{league_key}/standings")
    if "error" in data: return jsonify(data), 401
    try:
        league_raw = data["fantasy_content"]["league"]
        # league_raw is a list — find the dict that has "standings"
        teams_raw = None
        for item in league_raw:
            if not isinstance(item, dict): continue
            if "standings" not in item: continue
            standings = item["standings"]
            # standings can be list or dict
            if isinstance(standings, list):
                for s in standings:
                    if isinstance(s, dict) and "teams" in s:
                        teams_raw = s["teams"]; break
            elif isinstance(standings, dict):
                for v in standings.values():
                    if isinstance(v, dict) and "teams" in v:
                        teams_raw = v["teams"]; break
            if teams_raw: break
        if teams_raw is None:
            return jsonify({"error":"parse_error","detail":"could not find teams in standings","raw":str(data)[:800]}), 500
        teams = []
        for i in range(teams_raw.get("count", 0)):
            t = teams_raw[str(i)]["team"]
            # t is a list: [info_list_or_dict, stats_dict]
            info = _parse_info_list(t[0]) if isinstance(t[0], list) else t[0]
            stats = t[1] if len(t) > 1 else {}
            ts = stats.get("team_standings", {})
            if isinstance(ts, list): ts = ts[0] if ts else {}
            ot = ts.get("outcome_totals", {}) if isinstance(ts, dict) else {}
            # Manager
            mgr = info.get("managers", {})
            if isinstance(mgr, dict):
                mgr_obj = mgr.get("manager", {})
                mgr_name = mgr_obj.get("nickname","") if isinstance(mgr_obj, dict) else ""
            else:
                mgr_name = ""
            teams.append({
                "team_key": info.get("team_key"), "team_id": info.get("team_id"),
                "name": info.get("name", ""),
                "manager": mgr_name,
                "rank": ts.get("rank") if isinstance(ts, dict) else None,
                "wins": ot.get("wins"), "losses": ot.get("losses"), "ties": ot.get("ties"),
                "pct": ot.get("percentage"),
                "points_for": ts.get("points_for") if isinstance(ts, dict) else None,
                "points_against": ts.get("points_against") if isinstance(ts, dict) else None,
            })
        return jsonify({"teams": teams})
    except Exception as e:
        import traceback
        return jsonify({"error":"parse_error","detail":str(e),"trace":traceback.format_exc()[-800:],"raw":str(data)[:800]}), 500

@app.route("/api/yahoo/league/<league_key>/rosters")
def yahoo_rosters(league_key):
    data = _yahoo_get(f"/league/{league_key}/teams/roster/players")
    if "error" in data: return jsonify(data), 401
    try:
        teams_raw = data["fantasy_content"]["league"][1]["teams"]
        rosters = []
        for i in range(teams_raw["count"]):
            t = teams_raw[str(i)]["team"]
            info = _parse_info_list(t[0])
            roster_raw = t[1]["roster"]["0"]["players"]
            players = []
            for j in range(roster_raw["count"]):
                p = roster_raw[str(j)]["player"]
                p_info = _parse_info_list(p[0])
                pos_data = p[1].get("selected_position", [{}])
                selected_pos = pos_data[1].get("position","") if len(pos_data) > 1 else ""
                eligible = [ep.get("position") for ep in p_info.get("eligible_positions",{}).get("position",[]) if isinstance(ep,dict)]
                players.append({
                    "player_key": p_info.get("player_key"),
                    "name": p_info.get("full_name", p_info.get("name",{}).get("full","")),
                    "team": p_info.get("editorial_team_abbr",""),
                    "positions": eligible, "slot": selected_pos,
                    "status": p_info.get("status",""),
                    "injury_note": p_info.get("status_full",""),
                })
            rosters.append({
                "team_key": info.get("team_key"), "team_id": info.get("team_id"),
                "name": info.get("name"),
                "manager": info.get("managers",{}).get("manager",{}).get("nickname",""),
                "players": players,
            })
        return jsonify({"rosters": rosters})
    except Exception as e:
        return jsonify({"error":"parse_error","detail":str(e),"raw":str(data)[:800]}), 500

@app.route("/api/yahoo/league/<league_key>/my_team")
def yahoo_my_team(league_key):
    teams_data = _yahoo_get(f"/league/{league_key}/teams", use_cache=False)
    if "error" in teams_data: return jsonify(teams_data), 401
    try:
        teams_raw = teams_data["fantasy_content"]["league"][1]["teams"]
        my_team_key = None
        for i in range(teams_raw["count"]):
            info = _parse_info_list(teams_raw[str(i)]["team"][0])
            mgr = info.get("managers",{}).get("manager",{})
            if mgr.get("is_current_login") == "1":
                my_team_key = info.get("team_key")
                break
        if not my_team_key:
            return jsonify({"error":"could_not_find_user_team"}), 404
        roster_data = _yahoo_get(f"/team/{my_team_key}/roster/players", use_cache=False)
        players_raw = roster_data["fantasy_content"]["team"][1]["roster"]["0"]["players"]
        players = []
        for j in range(players_raw["count"]):
            p = players_raw[str(j)]["player"]
            p_info = _parse_info_list(p[0])
            pos_data = p[1].get("selected_position",[{}])
            selected_pos = pos_data[1].get("position","") if len(pos_data) > 1 else ""
            eligible = [ep.get("position") for ep in p_info.get("eligible_positions",{}).get("position",[]) if isinstance(ep,dict)]
            players.append({
                "player_key": p_info.get("player_key"),
                "name": p_info.get("full_name",""),
                "team": p_info.get("editorial_team_abbr",""),
                "positions": eligible, "slot": selected_pos,
                "status": p_info.get("status",""),
                "injury_note": p_info.get("status_full",""),
            })
        return jsonify({"team_key":my_team_key,"players":players,"count":len(players)})
    except Exception as e:
        return jsonify({"error":"parse_error","detail":str(e)}), 500

@app.route("/api/yahoo/league/<league_key>/scoreboard")
def yahoo_scoreboard(league_key):
    data = _yahoo_get(f"/league/{league_key}/scoreboard")
    if "error" in data: return jsonify(data), 401
    try:
        matchups_raw = data["fantasy_content"]["league"][1]["scoreboard"]["0"]["matchups"]
        matchups = []
        for i in range(matchups_raw["count"]):
            m = matchups_raw[str(i)]["matchup"]
            teams_in_match = []
            tc = m.get("0",{}).get("teams",{})
            for j in range(tc.get("count",0)):
                t = tc[str(j)]["team"]
                info = _parse_info_list(t[0])
                teams_in_match.append({
                    "name": info.get("name"), "team_key": info.get("team_key"),
                    "points": t[1].get("team_points",{}).get("total",""),
                    "projected": t[1].get("team_projected_points",{}).get("total",""),
                })
            matchups.append({"week": m.get("week"), "teams": teams_in_match})
        return jsonify({"matchups": matchups})
    except Exception as e:
        return jsonify({"error":"parse_error","detail":str(e)}), 500

# ─── ENTRYPOINT ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🏀 The Boardroom on port {port}")
    print(f"   Yahoo redirect: {YAHOO_REDIRECT_URI}")
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_ENV")=="development")
