"""The Boardroom — server.py
Stats source: Yahoo Fantasy Sports API (replaces nba_api)
"""
import os, time, json, requests, traceback
from datetime import datetime
from flask import Flask, jsonify, request, redirect, send_from_directory
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

# Yahoo NBA game keys by season
YAHOO_GAME_KEYS = {
    "2025-26": "nba",
    "2024-25": "428",
    "2023-24": "418",
    "2022-23": "410",
}

# Yahoo stat IDs -> field names
YAHOO_STAT_MAP = {
    "5": "fgm", "8": "ftm", "10": "threepm",
    "12": "pts", "13": "reb", "15": "ast",
    "16": "stl", "17": "blk", "18": "to",
}

_cache = {}
CACHE_TTL = 3600
YAHOO_TTL = 300
_TOKEN_PATH = "/tmp/br_tokens.json"

def _load_tokens():
    try:
        with open(_TOKEN_PATH) as f: return json.load(f)
    except Exception:
        return {"access_token": None, "refresh_token": None, "expires_at": 0}

def _save_tokens(d):
    try:
        with open(_TOKEN_PATH, "w") as f: json.dump(d, f)
    except Exception: pass

class _Tokens:
    def __getitem__(self, k): return _load_tokens()[k]
    def __setitem__(self, k, v):
        d = _load_tokens(); d[k] = v; _save_tokens(d)
    def get(self, k, default=None): return _load_tokens().get(k, default)
    def update(self, u): d = _load_tokens(); d.update(u); _save_tokens(d)

_tokens = _Tokens()

def _cached(key, fn, ttl=CACHE_TTL):
    now = time.time()
    if key in _cache and now - _cache[key][1] < ttl:
        return _cache[key][0]
    data = fn(); _cache[key] = (data, now); return data

def _merge(lst):
    out = {}
    if isinstance(lst, dict): return lst
    for item in lst:
        if isinstance(item, dict): out.update(item)
    return out

def _find_in_list(lst, key):
    for item in lst:
        if isinstance(item, dict) and key in item: return item[key]
    return None

def _current_season():
    now = datetime.now(); y = now.year
    return f"{y}-{str(y+1)[2:]}" if now.month >= 10 else f"{y-1}-{str(y)[2:]}"

def _est_dd(pts, reb, ast, stl, blk):
    cats = [pts, reb, ast, stl*5, blk*6]; d = sum(1 for v in cats if v >= 10)
    if d >= 2: return round(min(0.95, 0.70+d*0.08), 3)
    n = sum(1 for v in cats if v >= 7)
    return round(min(0.55, 0.25+n*0.10), 3) if n >= 2 else round(min(0.25, pts/60+reb/40+ast/35), 3)

def _est_td(pts, reb, ast, stl, blk):
    cats = [pts, reb, ast, stl*5, blk*6]; d = sum(1 for v in cats if v >= 10)
    if d >= 3: return round(min(0.55, 0.30+d*0.05), 3)
    return 0.12 if d >= 2 and sum(1 for v in cats if v >= 7) >= 3 else round(min(0.08, pts/200+reb/120+ast/100), 3)

def _parse_player_roster(p):
    p_info = _merge(p[0]) if isinstance(p[0], list) else p[0]
    pos_section = p[1] if len(p) > 1 else {}
    selected_pos = ""
    pos_data = pos_section.get("selected_position", [])
    if isinstance(pos_data, list): selected_pos = _merge(pos_data).get("position", "")
    elif isinstance(pos_data, dict): selected_pos = pos_data.get("position", "")
    elig_raw = p_info.get("eligible_positions", {})
    if isinstance(elig_raw, dict):
        elig_list = elig_raw.get("position", [])
        if isinstance(elig_list, list):
            eligible = [e.get("position","") if isinstance(e,dict) else e for e in elig_list]
        else: eligible = [elig_list] if elig_list else []
    else: eligible = []
    name = p_info.get("full_name","")
    if not name:
        n = p_info.get("name", {}); name = n.get("full","") if isinstance(n,dict) else str(n)
    return {"player_key":p_info.get("player_key",""),"name":name,
            "team":p_info.get("editorial_team_abbr",""),"positions":eligible,
            "slot":selected_pos,"status":p_info.get("status",""),
            "injury_note":p_info.get("status_full","")}

@app.route("/")
def index(): return send_from_directory(".", "fantasy-hoops.html")

@app.route("/api/health")
def health():
    return jsonify({"status":"ok","season":_current_season(),
                    "yahoo_authenticated":bool(_load_tokens().get("access_token")),
                    "redirect_uri":YAHOO_REDIRECT_URI,"stats_source":"yahoo"})

@app.route("/auth/login")
def yahoo_login():
    from urllib.parse import urlencode
    state = f"br_{int(time.time())}"
    params = {"client_id":YAHOO_CLIENT_ID,"redirect_uri":YAHOO_REDIRECT_URI,
              "response_type":"code","scope":"fspt-r","state":state}
    return redirect(f"{YAHOO_AUTH_URL}?{urlencode(params)}")

@app.route("/auth/callback")
def yahoo_callback():
    code = request.args.get("code","")
    if not code: return "Missing code", 400
    r = requests.post(YAHOO_TOKEN_URL,
        data={"grant_type":"authorization_code","code":code,"redirect_uri":YAHOO_REDIRECT_URI},
        auth=(YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET))
    if not r.ok: return f"Token exchange failed: {r.text}", 400
    tok = r.json()
    _save_tokens({"access_token":tok["access_token"],"refresh_token":tok.get("refresh_token",""),
                  "expires_at":time.time()+tok.get("expires_in",3600)})
    _cache.clear()
    return redirect("/?auth=success")

@app.route("/auth/logout")
def yahoo_logout():
    _save_tokens({"access_token":None,"refresh_token":None,"expires_at":0})
    return jsonify({"ok":True})

@app.route("/auth/status")
def auth_status():
    return jsonify({"authenticated":bool(_load_tokens().get("access_token")),
                    "expires_at":_load_tokens().get("expires_at",0)})

def _get_token():
    t = _load_tokens()
    if not t.get("access_token"): return None
    if time.time() > t.get("expires_at",0) - 60:
        r = requests.post(YAHOO_TOKEN_URL,
            data={"grant_type":"refresh_token","refresh_token":t.get("refresh_token","")},
            auth=(YAHOO_CLIENT_ID,YAHOO_CLIENT_SECRET))
        if r.ok:
            tok = r.json(); t["access_token"] = tok["access_token"]
            t["expires_at"] = time.time()+tok.get("expires_in",3600)
            if tok.get("refresh_token"): t["refresh_token"] = tok["refresh_token"]
            _save_tokens(t)
        else: return None
    return t["access_token"]

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

# ── Yahoo player stats ────────────────────────────────────────────────────────
def _fetch_yahoo_players(game_key, target=400):
    """Fetch per-game player stats from Yahoo Fantasy API, paginating in batches of 25."""
    tok = _get_token()
    if not tok: return {"error":"not_authenticated","players":[],"count":0}
    headers = {"Authorization":f"Bearer {tok}"}
    all_players = []; start = 0; batch = 25
    for _ in range((target // batch) + 2):
        url = (f"{YAHOO_API_BASE}/game/{game_key}/players"
               f";start={start};count={batch};out=stats?format=json")
        try: r = requests.get(url, headers=headers, timeout=20)
        except Exception: break
        if r.status_code == 401: return {"error":"token_expired","players":all_players,"count":len(all_players)}
        if not r.ok: break
        try:
            game_data = r.json()["fantasy_content"]["game"]
            ps = None
            for item in game_data:
                if isinstance(item, dict) and "players" in item: ps = item["players"]; break
            if not ps or ps.get("count",0) == 0: break
            cnt = ps["count"]
            for i in range(cnt):
                p_entry = ps.get(str(i),{}).get("player")
                if p_entry:
                    parsed = _parse_yahoo_player_stats(p_entry)
                    if parsed: all_players.append(parsed)
            start += cnt
            if cnt < batch or len(all_players) >= target: break
        except Exception: break
    return {"players":all_players,"count":len(all_players),"source":"yahoo","game_key":game_key}

def _parse_yahoo_player_stats(p_entry):
    if not isinstance(p_entry, list) or len(p_entry) < 2: return None
    info = _merge(p_entry[0]) if isinstance(p_entry[0], list) else p_entry[0]
    name = info.get("full_name","")
    if not name:
        n = info.get("name",{}); name = n.get("full","") if isinstance(n,dict) else str(n)
    if not name: return None
    elig_raw = info.get("eligible_positions",{})
    if isinstance(elig_raw, dict):
        elig_list = elig_raw.get("position",[])
        positions = [e.get("position","") if isinstance(e,dict) else str(e)
                     for e in (elig_list if isinstance(elig_list,list) else [elig_list])]
    else: positions = []
    pos_pref = ["PG","SG","SF","PF","C"]
    pos = next((p for p in pos_pref if p in positions), positions[0] if positions else "F")
    stats_block = p_entry[1] if len(p_entry) > 1 else {}
    ps = (stats_block.get("player_stats",{}) if isinstance(stats_block,dict) else {})
    raw_stats = ps.get("stats",{}).get("stat",[]) if isinstance(ps.get("stats"),dict) else []
    if isinstance(raw_stats, dict): raw_stats = [raw_stats]
    stat_vals = {}
    for s in raw_stats:
        if isinstance(s, dict):
            try: stat_vals[str(s.get("stat_id",""))] = float(s.get("value","0") or 0)
            except (ValueError, TypeError): stat_vals[str(s.get("stat_id",""))] = 0.0
    def g(sid): return stat_vals.get(str(sid), 0.0)
    pts=round(g(12),1); reb=round(g(13),1); ast=round(g(15),1)
    stl=round(g(16),1); blk=round(g(17),1); to=round(g(18),1)
    fgm=round(g(5),1);  ftm=round(g(8),1);  threepm=round(g(10),1)
    if pts == 0 and reb == 0 and ast == 0: return None
    return {"name":name,"team":info.get("editorial_team_abbr",""),"pos":pos,
            "positions":positions,"status":info.get("status",""),
            "injury_note":info.get("status_full",""),
            "pts":pts,"reb":reb,"ast":ast,"stl":stl,"blk":blk,"to":to,
            "fgm":fgm,"ftm":ftm,"threepm":threepm,
            "dd_est":_est_dd(pts,reb,ast,stl,blk),"td_est":_est_td(pts,reb,ast,stl,blk)}

@app.route("/api/stats/current")
def stats_current():
    s = _current_season(); gk = YAHOO_GAME_KEYS.get(s,"nba")
    def fetch():
        result = _fetch_yahoo_players(gk, target=400); result["season"] = s; return result
    d = _cached(f"yahoo_stats_{s}", fetch, CACHE_TTL)
    if d.get("error") == "not_authenticated":
        return jsonify({"error":"not_authenticated","players":[],"season":s}), 200
    if d.get("error") or not d.get("players"):
        return jsonify({"error":d.get("error","no_data"),"players":[],"season":s}), 200
    return jsonify(d)

@app.route("/api/stats/season/<season>")
def stats_season(season):
    gk = YAHOO_GAME_KEYS.get(season)
    if not gk: return jsonify({"error":f"Unknown season: {season}","players":[]}), 400
    def fetch():
        result = _fetch_yahoo_players(gk, target=400); result["season"] = season; return result
    return jsonify(_cached(f"yahoo_stats_{season}", fetch, CACHE_TTL))

@app.route("/api/cache/clear")
def cache_clear():
    _cache.clear(); return jsonify({"cleared":True})

@app.route("/api/yahoo/leagues")
def yahoo_leagues():
    """Fetch all NBA fantasy leagues for this user across all seasons."""
    # Use games;game_codes=nba to get all seasons the user has participated in
    data = _yahoo("/users;use_login=1/games;game_codes=nba/leagues", cache=False)
    if "error" in data: return jsonify(data), 401
    try:
        games = data["fantasy_content"]["users"]["0"]["user"][1]["games"]
        leagues = []
        for i in range(games.get("count", 0)):
            game = games[str(i)]["game"]
            game_info = _merge(game[0]) if isinstance(game[0], list) else game[0]
            season = game_info.get("season", "")
            if not isinstance(game[1], dict) or "leagues" not in game[1]:
                continue
            ld = game[1]["leagues"]
            for j in range(ld.get("count", 0)):
                lg_data = ld[str(j)]["league"]
                lg = _merge(lg_data) if isinstance(lg_data, list) else lg_data
                leagues.append({
                    "league_key":   lg.get("league_key"),
                    "league_id":    lg.get("league_id"),
                    "name":         lg.get("name"),
                    "season":       lg.get("season") or season,
                    "num_teams":    lg.get("num_teams"),
                    "scoring_type": lg.get("scoring_type"),
                    "draft_status": lg.get("draft_status"),
                    "current_week": lg.get("current_week"),
                    "is_finished":  lg.get("is_finished", 0),
                })
        # Sort newest season first
        leagues.sort(key=lambda x: str(x.get("season") or ""), reverse=True)
        return jsonify({"leagues": leagues})
    except Exception as e:
        return jsonify({"error": "parse_error", "detail": str(e), "raw": str(data)[:500]}), 500

@app.route("/api/yahoo/league/<lk>/debug")
def yahoo_debug(lk):
    data = _yahoo(f"/league/{lk}/teams/roster/players", cache=False)
    return jsonify({"raw":str(data)[:3000]})

@app.route("/api/yahoo/league/<lk>/standings")
def yahoo_standings(lk):
    data = _yahoo(f"/league/{lk}/standings", cache=False)
    if "error" in data: return jsonify(data), 401
    try:
        league_list = data["fantasy_content"]["league"]
        league_info = {}
        if isinstance(league_list,list) and league_list:
            first = league_list[0]
            if isinstance(first,dict): league_info = first
        scoring_type = league_info.get("scoring_type","")
        teams_raw = None
        for item in league_list:
            if not isinstance(item,dict): continue
            s = item.get("standings")
            if s is None: continue
            candidates = s if isinstance(s,list) else list(s.values())
            for x in candidates:
                if isinstance(x,dict) and "teams" in x: teams_raw = x["teams"]; break
            if teams_raw: break
        if not teams_raw:
            return jsonify({"error":"no_standings_found","raw":str(league_list)[:1000]}), 500
        teams = []
        for i in range(teams_raw.get("count",0)):
            t = teams_raw[str(i)]["team"]
            info = _merge(t[0]) if isinstance(t[0],list) else t[0]
            stats = t[1] if len(t)>1 else {}
            ts = stats.get("team_standings",{}); ts = _merge(ts) if isinstance(ts,list) else ts
            ot = ts.get("outcome_totals",{}) if isinstance(ts,dict) else {}
            ot = _merge(ot) if isinstance(ot,list) else ot
            mgr = info.get("managers",{}); mgr_name = ""
            if isinstance(mgr,dict):
                m = mgr.get("manager",{}); m = _merge(m) if isinstance(m,list) else m
                mgr_name = m.get("nickname","") if isinstance(m,dict) else ""
            elif isinstance(mgr,list):
                for mg in mgr:
                    if isinstance(mg,dict):
                        m = mg.get("manager",{}); mgr_name = m.get("nickname","") if isinstance(m,dict) else ""; break
            streak = ""
            if isinstance(ts,dict):
                sk = ts.get("streak",{})
                if isinstance(sk,dict): streak = f"{sk.get('type','')[0].upper()}{sk.get('value','')}" if sk.get("type") else ""
            teams.append({"team_key":info.get("team_key"),"name":info.get("name",""),
                "manager":mgr_name,"rank":int(ts["rank"]) if isinstance(ts,dict) and ts.get("rank") else None,
                "wins":ot.get("wins"),"losses":ot.get("losses"),"ties":ot.get("ties"),
                "pct":ot.get("percentage"),"points_for":ts.get("points_for") if isinstance(ts,dict) else None,
                "points_against":ts.get("points_against") if isinstance(ts,dict) else None,
                "streak":streak,"scoring_type":scoring_type})
        teams.sort(key=lambda x: x.get("rank") or 99)
        return jsonify({"teams":teams,"scoring_type":scoring_type,
                        "season":league_info.get("season"),"name":league_info.get("name",""),
                        "is_finished":league_info.get("is_finished",0)})
    except Exception as e:
        return jsonify({"error":"parse_error","detail":str(e),
                        "trace":traceback.format_exc()[-800:],"raw":str(data)[:800]}), 500

@app.route("/api/yahoo/league/<lk>/rosters")
def yahoo_rosters(lk):
    data = _yahoo(f"/league/{lk}/teams/roster/players", cache=False)
    if "error" in data: return jsonify(data), 401
    try:
        league_list = data["fantasy_content"]["league"]
        teams_raw = _find_in_list(league_list,"teams")
        if not teams_raw: return jsonify({"error":"no_teams","raw":str(league_list)[:500]}), 500
        rosters = []
        for i in range(teams_raw.get("count",0)):
            t = teams_raw[str(i)]["team"]
            info = _merge(t[0]) if isinstance(t[0],list) else t[0]
            mgr = info.get("managers",{}); mgr_name = ""
            if isinstance(mgr,dict):
                m = mgr.get("manager",{}); mgr_name = m.get("nickname","") if isinstance(m,dict) else ""
            team_data = t[1] if len(t)>1 else {}
            roster_section = team_data.get("roster",{})
            players_raw = roster_section.get("0",{}).get("players",{})
            if not players_raw:
                for v in roster_section.values():
                    if isinstance(v,dict) and "players" in v: players_raw = v["players"]; break
            players = []
            for j in range(players_raw.get("count",0)):
                p = players_raw[str(j)]["player"]; players.append(_parse_player_roster(p))
            rosters.append({"team_key":info.get("team_key",""),"name":info.get("name",""),
                            "manager":mgr_name,"players":players})
        return jsonify({"rosters":rosters})
    except Exception as e:
        return jsonify({"error":"parse_error","detail":str(e),"trace":traceback.format_exc()[-800:]}), 500

@app.route("/api/yahoo/league/<lk>/my_team")
def yahoo_my_team(lk):
    data = _yahoo(f"/league/{lk}/teams", cache=False)
    if "error" in data: return jsonify(data), 401
    try:
        league_list = data["fantasy_content"]["league"]
        teams_raw = _find_in_list(league_list,"teams")
        if not teams_raw: return jsonify({"error":"no_teams"}), 500
        my_key = None
        for i in range(teams_raw.get("count",0)):
            t = teams_raw[str(i)]["team"]
            info = _merge(t[0]) if isinstance(t[0],list) else t[0]
            mgr = info.get("managers",{})
            if isinstance(mgr,dict):
                m = mgr.get("manager",{})
                if isinstance(m,dict) and m.get("is_current_login")=="1": my_key = info.get("team_key"); break
        if not my_key: return jsonify({"error":"team_not_found"}), 404
        rd = _yahoo(f"/team/{my_key}/roster/players", cache=False)
        team_list = rd["fantasy_content"]["team"]
        roster_section = None
        for item in team_list:
            if isinstance(item,dict) and "roster" in item: roster_section = item["roster"]; break
        players_raw = roster_section.get("0",{}).get("players",{}) if roster_section else {}
        players = [_parse_player_roster(players_raw[str(j)]["player"]) for j in range(players_raw.get("count",0))]
        return jsonify({"team_key":my_key,"players":players,"count":len(players)})
    except Exception as e:
        return jsonify({"error":"parse_error","detail":str(e),"trace":traceback.format_exc()[-600:]}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    print(f"🏀 Boardroom on :{port}  redirect={YAHOO_REDIRECT_URI}")
    app.run(host="0.0.0.0", port=port, debug=True)
