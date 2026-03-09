"""The Boardroom — server.py"""
import os, time, json, secrets, requests, traceback
from datetime import datetime
from urllib.parse import urlencode
from flask import Flask, jsonify, request, redirect, session, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=".")
app.secret_key = os.environ.get("FLASK_SECRET", "boardroom2026secretkey")
CORS(app, supports_credentials=True)

YAHOO_CLIENT_ID     = os.environ.get("YAHOO_CLIENT_ID",     "dj0yJmk9TmV3N2cwSzVBZmtlJmQ9WVdrOWRHVkJUV3hTVEhvbWNHbzlNQT09JnM9Y29uc3VtZXJzZWNyZXQmc3Y9MCZ4PWZj")
YAHOO_CLIENT_SECRET = os.environ.get("YAHOO_CLIENT_SECRET", "26b682df3839b70b040110b696a248d3c48fa442")
YAHOO_REDIRECT_URI  = os.environ.get("YAHOO_REDIRECT_URI",  "https://app.keyzen.art/auth/callback")
YAHOO_AUTH_URL  = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
YAHOO_API_BASE  = "https://fantasysports.yahooapis.com/fantasy/v2"

# Single global token dict — safe with 1 gunicorn worker
_tokens = {"access_token": None, "refresh_token": None, "expires_at": 0}
_pending_states = {}
_cache = {}
CACHE_TTL = 3600
YAHOO_TTL = 300

def _cached(key, fn, ttl=CACHE_TTL):
    now = time.time()
    if key in _cache and now - _cache[key][1] < ttl:
        return _cache[key][0]
    data = fn()
    _cache[key] = (data, now)
    return data

# ── helpers ──────────────────────────────────────────────────────────────────
def _merge(lst):
    """Merge a Yahoo list-of-dicts into one flat dict."""
    out = {}
    if isinstance(lst, dict): return lst
    for item in lst:
        if isinstance(item, dict):
            out.update(item)
    return out

def _find_in_list(lst, key):
    """Find first dict in list that contains key."""
    for item in lst:
        if isinstance(item, dict) and key in item:
            return item[key]
    return None

def _parse_player(p):
    """Parse a Yahoo player entry (list of [info_list, pos_dict])."""
    p_info = _merge(p[0]) if isinstance(p[0], list) else p[0]
    pos_section = p[1] if len(p) > 1 else {}

    # selected position
    pos_data = pos_section.get("selected_position", [])
    if isinstance(pos_data, list):
        selected_pos = _merge(pos_data).get("position", "")
    else:
        selected_pos = pos_data.get("position", "") if isinstance(pos_data, dict) else ""

    # eligible positions
    elig_raw = p_info.get("eligible_positions", {})
    if isinstance(elig_raw, dict):
        elig_list = elig_raw.get("position", [])
        if isinstance(elig_list, list):
            eligible = [e.get("position","") if isinstance(e,dict) else e for e in elig_list]
        else:
            eligible = [elig_list] if elig_list else []
    else:
        eligible = []

    # name
    name = p_info.get("full_name","")
    if not name:
        n = p_info.get("name", {})
        name = n.get("full","") if isinstance(n, dict) else str(n)

    return {
        "player_key": p_info.get("player_key",""),
        "name": name,
        "team": p_info.get("editorial_team_abbr",""),
        "positions": eligible,
        "slot": selected_pos,
        "status": p_info.get("status",""),
        "injury_note": p_info.get("status_full",""),
    }

# ── static ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "fantasy-hoops.html")

@app.route("/api/health")
def health():
    now = datetime.now()
    y = now.year
    season = f"{y}-{str(y+1)[2:]}" if now.month >= 10 else f"{y-1}-{str(y)[2:]}"
    return jsonify({"status":"ok","season":season,
                    "yahoo_authenticated": bool(_tokens["access_token"]),
                    "redirect_uri": YAHOO_REDIRECT_URI})

# ── NBA stats ─────────────────────────────────────────────────────────────────
def _current_season():
    now = datetime.now(); y = now.year
    return f"{y}-{str(y+1)[2:]}" if now.month >= 10 else f"{y-1}-{str(y)[2:]}"

def _est_dd(pts,reb,ast,stl,blk):
    cats=[pts,reb,ast,stl*5,blk*6]; d=sum(1 for v in cats if v>=10)
    if d>=2: return round(min(0.95,0.70+d*0.08),3)
    n=sum(1 for v in cats if v>=7)
    return round(min(0.55,0.25+n*0.10),3) if n>=2 else round(min(0.25,pts/60+reb/40+ast/35),3)

def _est_td(pts,reb,ast,stl,blk):
    cats=[pts,reb,ast,stl*5,blk*6]; d=sum(1 for v in cats if v>=10)
    if d>=3: return round(min(0.55,0.30+d*0.05),3)
    return 0.12 if d>=2 and sum(1 for v in cats if v>=7)>=3 else round(min(0.08,pts/200+reb/120+ast/100),3)

def _fetch_nba(season):
    try:
        from nba_api.stats.endpoints import leaguedashplayerstats
        time.sleep(0.6)
        df = leaguedashplayerstats.LeagueDashPlayerStats(
            season=season, per_mode_simple="PerGame", season_type_all_star="Regular Season"
        ).get_data_frames()[0]
        players = []
        for _, row in df.iterrows():
            if row.get("GP",0) < 3: continue
            pts=float(row.get("PTS",0)); reb=float(row.get("REB",0)); ast=float(row.get("AST",0))
            stl=float(row.get("STL",0)); blk=float(row.get("BLK",0))
            players.append({"name":row.get("PLAYER_NAME",""),"team":row.get("TEAM_ABBREVIATION",""),
                "gp":int(row.get("GP",0)),"pts":round(pts,1),"reb":round(reb,1),"ast":round(ast,1),
                "stl":round(stl,1),"blk":round(blk,1),"to":round(float(row.get("TOV",0)),1),
                "fgm":round(float(row.get("FGM",0)),1),"ftm":round(float(row.get("FTM",0)),1),
                "threepm":round(float(row.get("FG3M",0)),1),
                "dd_est":_est_dd(pts,reb,ast,stl,blk),"td_est":_est_td(pts,reb,ast,stl,blk)})
        return {"season":season,"players":players,"count":len(players),"source":"nba_api"}
    except Exception as e:
        return {"error":str(e),"players":[],"source":"error"}

@app.route("/api/stats/current")
def stats_current():
    s = _current_season()
    d = _cached(f"nba_{s}", lambda: _fetch_nba(s))
    if not d.get("players"):
        prev = f"{int(s[:4])-1}-{s[:4][2:]}"; d = _cached(f"nba_{prev}", lambda: _fetch_nba(prev))
    return jsonify(d)

@app.route("/api/stats/season/<season>")
def stats_season(season):
    return jsonify(_cached(f"nba_{season}", lambda: _fetch_nba(season)))

@app.route("/api/cache/clear")
def cache_clear():
    _cache.clear(); return jsonify({"cleared":True})

# ── Yahoo OAuth ───────────────────────────────────────────────────────────────
@app.route("/auth/login")
def yahoo_login():
    # Use a timestamp-based state — survives process restarts unlike in-memory dict
    state = f"br_{int(time.time())}"
    params = {"client_id":YAHOO_CLIENT_ID,"redirect_uri":YAHOO_REDIRECT_URI,
              "response_type":"code","scope":"fspt-r","state":state}
    return redirect(f"{YAHOO_AUTH_URL}?{urlencode(params)}")

@app.route("/auth/callback")
def yahoo_callback():
    err = request.args.get("error")
    if err: return f"<h2>Yahoo error: {err}</h2><a href='/'>Back</a>", 400
    code = request.args.get("code")
    state = request.args.get("state", "")
    # Validate state is ours and not older than 10 minutes
    if not state.startswith("br_"):
        return "<h2>Invalid state — <a href='/auth/login'>try again</a></h2>", 403
    try:
        ts = int(state.split("_")[1])
        if time.time() - ts > 600:
            return "<h2>Login expired — <a href='/auth/login'>try again</a></h2>", 403
    except Exception:
        return "<h2>Invalid state — <a href='/auth/login'>try again</a></h2>", 403
    resp = requests.post(YAHOO_TOKEN_URL, data={
        "grant_type":"authorization_code","code":code,"redirect_uri":YAHOO_REDIRECT_URI,
    }, auth=(YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET))
    if not resp.ok:
        return f"<h2>Token exchange failed</h2><pre>{resp.text}</pre>", 400
    tok = resp.json()
    _tokens["access_token"]  = tok["access_token"]
    _tokens["refresh_token"] = tok.get("refresh_token","")
    _tokens["expires_at"]    = time.time() + tok.get("expires_in", 3600)
    return redirect("/?yahoo=connected")

@app.route("/auth/logout")
def yahoo_logout():
    _tokens.update({"access_token":None,"refresh_token":None,"expires_at":0})
    return redirect("/")

@app.route("/auth/status")
def auth_status():
    ok = bool(_tokens["access_token"]) and time.time() < _tokens["expires_at"]
    return jsonify({"authenticated": ok})

def _get_token():
    if not _tokens["access_token"]: return None
    if time.time() > _tokens["expires_at"] - 60:
        r = requests.post(YAHOO_TOKEN_URL, data={
            "grant_type":"refresh_token","refresh_token":_tokens["refresh_token"]},
            auth=(YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET))
        if r.ok:
            t = r.json()
            _tokens["access_token"] = t["access_token"]
            _tokens["expires_at"] = time.time() + t.get("expires_in",3600)
            if t.get("refresh_token"): _tokens["refresh_token"] = t["refresh_token"]
        else: return None
    return _tokens["access_token"]

def _yahoo(path, cache=True, ttl=YAHOO_TTL):
    def fetch():
        tok = _get_token()
        if not tok: return {"error":"not_authenticated"}
        sep = "&" if "?" in path else "?"
        r = requests.get(f"{YAHOO_API_BASE}{path}{sep}format=json",
                         headers={"Authorization":f"Bearer {tok}"})
        if r.status_code == 401: return {"error":"token_expired"}
        if not r.ok: return {"error":f"yahoo_{r.status_code}","detail":r.text[:300]}
        return r.json()
    return _cached(f"y_{path}", fetch, ttl) if cache else fetch()

# ── Yahoo Fantasy endpoints ───────────────────────────────────────────────────
@app.route("/api/yahoo/leagues")
def yahoo_leagues():
    data = _yahoo("/users;use_login=1/games;game_codes=nba/leagues", cache=False)
    if "error" in data: return jsonify(data), 401
    try:
        games = data["fantasy_content"]["users"]["0"]["user"][1]["games"]
        leagues = []
        for i in range(games["count"]):
            game = games[str(i)]["game"]
            if "leagues" not in game[1]: continue
            ld = game[1]["leagues"]
            for j in range(ld["count"]):
                lg = ld[str(j)]["league"][0]
                leagues.append({"league_key":lg.get("league_key"),"league_id":lg.get("league_id"),
                    "name":lg.get("name"),"season":lg.get("season"),
                    "num_teams":lg.get("num_teams"),"scoring_type":lg.get("scoring_type"),
                    "draft_status":lg.get("draft_status"),"current_week":lg.get("current_week")})
        return jsonify({"leagues":leagues})
    except Exception as e:
        return jsonify({"error":"parse_error","detail":str(e),"raw":str(data)[:500]}), 500

@app.route("/api/yahoo/league/<lk>/debug")
def yahoo_debug(lk):
    """Returns raw Yahoo response for debugging."""
    data = _yahoo(f"/league/{lk}/teams/roster/players", cache=False)
    return jsonify({"raw": str(data)[:3000]})

@app.route("/api/yahoo/league/<lk>/standings")
def yahoo_standings(lk):
    data = _yahoo(f"/league/{lk}/standings")
    if "error" in data: return jsonify(data), 401
    try:
        league_list = data["fantasy_content"]["league"]
        teams_raw = None
        for item in league_list:
            if not isinstance(item, dict): continue
            s = item.get("standings")
            if s is None: continue
            if isinstance(s, list):
                for x in s:
                    if isinstance(x, dict) and "teams" in x: teams_raw = x["teams"]; break
            elif isinstance(s, dict):
                for v in s.values():
                    if isinstance(v, dict) and "teams" in v: teams_raw = v["teams"]; break
            if teams_raw: break
        if not teams_raw:
            return jsonify({"error":"no_teams","raw":str(data)[:500]}), 500
        teams = []
        for i in range(teams_raw.get("count",0)):
            t = teams_raw[str(i)]["team"]
            info = _merge(t[0]) if isinstance(t[0], list) else t[0]
            stats = t[1] if len(t)>1 else {}
            ts = stats.get("team_standings",{})
            if isinstance(ts, list): ts = ts[0] if ts else {}
            ot = ts.get("outcome_totals",{}) if isinstance(ts,dict) else {}
            mgr = info.get("managers",{})
            mgr_name = ""
            if isinstance(mgr, dict):
                m = mgr.get("manager",{})
                mgr_name = m.get("nickname","") if isinstance(m,dict) else ""
            teams.append({"team_key":info.get("team_key"),"name":info.get("name",""),
                "manager":mgr_name,"rank":ts.get("rank") if isinstance(ts,dict) else None,
                "wins":ot.get("wins"),"losses":ot.get("losses"),"ties":ot.get("ties"),
                "pct":ot.get("percentage"),
                "points_for":ts.get("points_for") if isinstance(ts,dict) else None,
                "points_against":ts.get("points_against") if isinstance(ts,dict) else None})
        return jsonify({"teams":teams})
    except Exception as e:
        return jsonify({"error":"parse_error","detail":str(e),"trace":traceback.format_exc()[-600:]}), 500

@app.route("/api/yahoo/league/<lk>/rosters")
def yahoo_rosters(lk):
    data = _yahoo(f"/league/{lk}/teams/roster/players", cache=False)
    if "error" in data: return jsonify(data), 401
    try:
        league_list = data["fantasy_content"]["league"]
        # Find the item with "teams"
        teams_raw = _find_in_list(league_list, "teams")
        if not teams_raw:
            return jsonify({"error":"no_teams","raw":str(league_list)[:500]}), 500

        rosters = []
        for i in range(teams_raw.get("count",0)):
            t = teams_raw[str(i)]["team"]
            info = _merge(t[0]) if isinstance(t[0], list) else t[0]
            mgr = info.get("managers",{})
            mgr_name = ""
            if isinstance(mgr, dict):
                m = mgr.get("manager",{})
                mgr_name = m.get("nickname","") if isinstance(m,dict) else ""

            # roster section
            team_data = t[1] if len(t)>1 else {}
            roster_section = team_data.get("roster",{})
            players_raw = roster_section.get("0",{}).get("players",{})
            if not players_raw:
                # try alternate structure
                for v in roster_section.values():
                    if isinstance(v,dict) and "players" in v:
                        players_raw = v["players"]; break

            players = []
            for j in range(players_raw.get("count",0)):
                p = players_raw[str(j)]["player"]
                players.append(_parse_player(p))

            rosters.append({"team_key":info.get("team_key",""),"name":info.get("name",""),
                            "manager":mgr_name,"players":players})
        return jsonify({"rosters":rosters})
    except Exception as e:
        return jsonify({"error":"parse_error","detail":str(e),
                        "trace":traceback.format_exc()[-800:]}), 500

@app.route("/api/yahoo/league/<lk>/my_team")
def yahoo_my_team(lk):
    data = _yahoo(f"/league/{lk}/teams", cache=False)
    if "error" in data: return jsonify(data), 401
    try:
        league_list = data["fantasy_content"]["league"]
        teams_raw = _find_in_list(league_list, "teams")
        if not teams_raw: return jsonify({"error":"no_teams"}), 500
        my_key = None
        for i in range(teams_raw.get("count",0)):
            t = teams_raw[str(i)]["team"]
            info = _merge(t[0]) if isinstance(t[0], list) else t[0]
            mgr = info.get("managers",{})
            if isinstance(mgr, dict):
                m = mgr.get("manager",{})
                if isinstance(m,dict) and m.get("is_current_login")=="1":
                    my_key = info.get("team_key"); break
        if not my_key: return jsonify({"error":"team_not_found"}), 404
        rd = _yahoo(f"/team/{my_key}/roster/players", cache=False)
        team_list = rd["fantasy_content"]["team"]
        roster_section = None
        for item in team_list:
            if isinstance(item,dict) and "roster" in item:
                roster_section = item["roster"]; break
        players_raw = roster_section.get("0",{}).get("players",{}) if roster_section else {}
        players = [_parse_player(players_raw[str(j)]["player"]) for j in range(players_raw.get("count",0))]
        return jsonify({"team_key":my_key,"players":players,"count":len(players)})
    except Exception as e:
        return jsonify({"error":"parse_error","detail":str(e),"trace":traceback.format_exc()[-600:]}), 500

# ── entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    print(f"🏀 Boardroom on :{port}  redirect={YAHOO_REDIRECT_URI}")
    app.run(host="0.0.0.0", port=port, debug=True)
