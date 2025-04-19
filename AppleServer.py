from flask import Flask, request, render_template_string
import threading
import time
import re
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
import statsapi
import requests

app = Flask(__name__)

monitored_team_id = 121  # Default team (San Francisco Giants)
current_game_id = None
seen_plays = set()
last_seen_status = ""
server_start_time = datetime.now(timezone.utc)
last_triggered = None
triggered_wins = set()

MLB_TEAMS = {
    "Arizona Diamondbacks": 109,
    "Atlanta Braves": 144,
    "Baltimore Orioles": 110,
    "Boston Red Sox": 111,
    "Chicago Cubs": 112,
    "Chicago White Sox": 145,
    "Cincinnati Reds": 113,
    "Cleveland Guardians": 114,
    "Colorado Rockies": 115,
    "Detroit Tigers": 116,
    "Houston Astros": 117,
    "Kansas City Royals": 118,
    "Los Angeles Angels": 108,
    "Los Angeles Dodgers": 119,
    "Miami Marlins": 146,
    "Milwaukee Brewers": 158,
    "Minnesota Twins": 142,
    "New York Mets": 121,
    "New York Yankees": 147,
    "Oakland Athletics": 133,
    "Philadelphia Phillies": 143,
    "Pittsburgh Pirates": 134,
    "San Diego Padres": 135,
    "San Francisco Giants": 137,
    "Seattle Mariners": 136,
    "St. Louis Cardinals": 138,
    "Tampa Bay Rays": 139,
    "Texas Rangers": 140,
    "Toronto Blue Jays": 141,
    "Washington Nationals": 120
}

def get_latest_game_id(team_id):
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    yesterday = (datetime.now(ZoneInfo("America/New_York")) - timedelta(days=1)).strftime("%Y-%m-%d")
    schedule = statsapi.schedule(start_date=yesterday, end_date=today, team=team_id)

    doubleheader_game2 = None
    in_progress_game = None
    game_over_game = None
    postponed_game = None
    final_game = None

    for game in schedule:
        game_id = game.get("game_id")
        status = game.get("status")

        try:
            game_data = statsapi.get("game", {"gamePk": game_id})
            doubleheader = game_data['gameData']['game'].get('doubleHeader', 'N')
            gid = game_data['gameData']['game'].get('id', '')
        except Exception as e:
            print(f"[WARN] Could not retrieve game details: {e}")
            continue

        print(f"[DEBUG] Found game ID {game_id} with status '{status}' (DoubleHeader: {doubleheader})")

        if doubleheader == 'S' and gid.endswith('-2'):
            print("[INFO] Prioritizing doubleheader Game 2")
            doubleheader_game2 = (game_id, status)
        elif status == "In Progress" or status.startswith("Manager challenge") or status.startswith("Umpire review"):
            in_progress_game = (game_id, status)

        elif status == "Game Over":
            game_over_game = (game_id, status)
        elif status == "Postponed":
            postponed_game = (game_id, status)
        elif status == "Final":
            final_game = (game_id, status)

    return (
        doubleheader_game2 or
        in_progress_game or
        game_over_game or
        postponed_game or
        final_game or
        (None, None)
    )


def fetch_play_data(game_id):
    return statsapi.get("game_playByPlay", {"gamePk": game_id})

def get_team_info(game_id):
    data = statsapi.get("game", {"gamePk": game_id})
    home_id = data['gameData']['teams']['home']['id']
    away_id = data['gameData']['teams']['away']['id']
    return home_id, away_id

def trigger_actuator():
    global last_triggered
    print("[ACTUATOR] Triggering actuator...")
    last_triggered = datetime.utcnow()
    
def should_skip_event(play):
    """Return True if play is a non-at-bat filler event that should not be marked as seen."""
    event = play.get("result", {}).get("event", "").lower()
    filler_events = {
        "batter timeout", "mound visit", "injury delay", "manager visit",
        "challenge", "review", "umpire review", "pitching substitution",
        "warmup", "defensive switch", "offensive substitution", "throwing error",
        "passed ball", "wild pitch"
    }
    return event in filler_events

def background_loop():
    global current_game_id, seen_plays, last_seen_status

    while True:
        game_id, status = get_latest_game_id(monitored_team_id)

        if not game_id:
            print("[INFO] No active or final games found.")
            time.sleep(15)
            continue

        if current_game_id != game_id:
            print(f"[INFO] Switched to new game ID: {game_id}")
            current_game_id = game_id
            seen_plays.clear()

        if status != last_seen_status:
            print(f"[DEBUG] Game status changed: {status}")
            last_seen_status = status

        if status in ["Final", "Game Over"] and game_id not in triggered_wins:
            try:
                data = statsapi.get("game", {"gamePk": game_id})
                home_team_id = data['gameData']['teams']['home']['id']
                away_team_id = data['gameData']['teams']['away']['id']
                linescore = data.get("liveData", {}).get("linescore", {})
                home_score = linescore.get("teams", {}).get("home", {}).get("runs", 0)
                away_score = linescore.get("teams", {}).get("away", {}).get("runs", 0)

                print(f"[FINAL] Final score — Home: {home_score}, Away: {away_score}")

                if ((home_team_id == monitored_team_id and home_score > away_score) or
                    (away_team_id == monitored_team_id and away_score > home_score)):
                    print(f"[VICTORY] Monitored team {monitored_team_id} won! Raising the apple!")
                    trigger_actuator()
                    triggered_wins.add(game_id)
            except Exception as e:
                print(f"[ERROR] Failed to check final score: {e}")

        try:
            data = fetch_play_data(game_id)
            all_plays = data.get("allPlays", [])
            print(f"[DEBUG] Retrieved {len(all_plays)} plays.")

            for play in all_plays[-2:]:
                idx = play["about"]["atBatIndex"]
                desc = play.get("result", {}).get("description", "")
                events = play.get("playEvents", [])
                start_str = events[0].get("startTime") if events else None


                print(f"[PLAY {idx}] ===============================")
                print(f"Description: {desc}")
                print(f"Start Time (raw): {start_str}")

                if not desc or not start_str:
                    print("[WAIT] Description not yet available, will check again later.")
                    continue  # Don't mark as seen

                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if start_dt < server_start_time - timedelta(minutes=1):
                    print(f"[SKIP] Play happened before server started at {server_start_time}")
                    continue

                if idx in seen_plays:
                    print(f"[SKIP] Already seen complete play.")
                    continue

                half_inning = play["about"].get("halfInning")
                is_home_batting = (half_inning == "bottom")
                home_id, away_id = get_team_info(game_id)
                batting_team_id = home_id if is_home_batting else away_id

                desc_lower = desc.lower()
                is_hit = False

                if "double play" in desc_lower or "triple play" in desc_lower:
                    print("[SKIP] This was a double or triple play — not a hit.")
                elif re.search(r'\b(singles?|doubles?|triples?|homers?)\b', desc_lower):
                    is_hit = True

                if batting_team_id == monitored_team_id and is_hit:
                    print("[HIT] Valid hit by monitored team!")
                    trigger_actuator()
                    time.sleep(5)
                else:
                    print("[SKIP] Not a valid hit by monitored team")

                if not should_skip_event(play):
                    seen_plays.add(idx)
                else:
                    print("[SKIP] Filler event (timeout, mound visit, etc) — not marking as seen.")


        except Exception as e:
            print(f"[ERROR] Fetching or processing play data failed: {e}")
        
        time.sleep(15)



@app.route("/")
def index():
    team_options = "".join(
        f'<option value="{id}" {"selected" if id == monitored_team_id else ""}>{name}</option>'
        for name, id in MLB_TEAMS.items()
    )
    html = f"""
    <html><body>
    <h1>Apple Server</h1>
    <form method="POST" action="/set_team">
        <label>Select Team:</label>
        <select name="team_id">{team_options}</select>
        <button type="submit">Set Team</button>
    </form>
    </body></html>
    """
    return render_template_string(html)

@app.route("/set_team", methods=["POST"])
def set_team():
    global monitored_team_id
    try:
        monitored_team_id = int(request.form["team_id"])
        print(f"[INFO] Updated monitored team to: {monitored_team_id}")
    except:
        return "Invalid team ID", 400
    return "Team updated", 200

@app.route("/trigger")
def trigger_route():
    global last_triggered
    if last_triggered:
        if (datetime.utcnow() - last_triggered).total_seconds() < 5:
            return "TRIGGER"
    return "NONE"

if __name__ == "__main__":
    threading.Thread(target=background_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)

