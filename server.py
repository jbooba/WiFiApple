from flask import Flask, request, render_template_string, redirect
import threading
import time
import re
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
from collections import deque
import statsapi

app = Flask(__name__)

# =====================
# ---- Server State ----
# =====================
monitored_team_id = 121  # Default team (New York Mets)
current_game_id = None
seen_plays = set()
last_seen_status = ""
server_start_time = datetime.now(timezone.utc)
triggered_wins = set()

# Trigger queue + stats
_trigger_q = deque()                 # FIFO of pending triggers; one dequeue == one Arduino action
_state_lock = threading.Lock()       # Guards queue + simple state
last_enqueued_at = None              # When we last queued a trigger
last_dequeued_at = None              # When Arduino last successfully pulled a trigger (HTTP 200)

# =====================
# ---- MLB Teams UI ----
# =====================
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

# =====================
# ---- Helpers ----
# =====================

def get_latest_game_id(team_id):
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    yesterday = (datetime.now(ZoneInfo("America/New_York")) - timedelta(days=1)).strftime("%Y-%m-%d")
    schedule = statsapi.schedule(start_date=yesterday, end_date=today, team=team_id)

    in_progress_game = None
    doubleheader_game2 = None
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

        if status == "In Progress" or status.startswith("Manager challenge") or status.startswith("Umpire review"):
            in_progress_game = (game_id, status)
        elif doubleheader == 'S' and gid.endswith('-2'):
            print("[INFO] Found doubleheader Game 2")
            doubleheader_game2 = (game_id, status)
        elif status == "Game Over":
            game_over_game = (game_id, status)
        elif status == "Postponed":
            postponed_game = (game_id, status)
        elif status == "Final":
            final_game = (game_id, status)

    return (
        in_progress_game or
        doubleheader_game2 or
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


def should_skip_event(play):
    """Return True if play is a non-at-bat filler event that should not be marked as seen."""
    event = play.get("result", {}).get("event", "").lower()
    filler_events = {
        "batter timeout", "mound visit", "injury delay", "manager visit",
        "challenge", "review", "umpire review", "pitching substitution",
        "warmup", "defensive switch", "offensive substitution", "throwing error",
        "passed ball", "wild pitch", "steals"
    }
    return event in filler_events


def queue_trigger(reason: str):
    global last_enqueued_at
    with _state_lock:
        _trigger_q.append({
            "reason": reason,
            "enqueued_at": datetime.utcnow().isoformat()
        })
        last_enqueued_at = datetime.utcnow()
        print(f"[QUEUE] Trigger queued ({reason}). Pending count = {len(_trigger_q)}")


# =====================
# ---- Background Loop ----
# =====================

def background_loop():
    global current_game_id, seen_plays, last_seen_status, triggered_wins

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

        # Victory trigger once per game
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
                    print(f"[VICTORY] Monitored team won — queueing win trigger")
                    queue_trigger("TEAM_WIN")
                    triggered_wins.add(game_id)
            except Exception as e:
                print(f"[ERROR] Failed to check final score: {e}")

        try:
            data = fetch_play_data(game_id)
            all_plays = data.get("allPlays", [])
            print(f"[DEBUG] Retrieved {len(all_plays)} plays.")

            # Look at the last couple of fully-formed plays for freshness
            for play in all_plays[-3:]:
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
                    print(f"[SKIP] Already processed play {idx}.")
                    continue

                half_inning = play["about"].get("halfInning")
                is_home_batting = (half_inning == "bottom")
                home_id, away_id = get_team_info(game_id)
                batting_team_id = home_id if is_home_batting else away_id

                desc_lower = desc.lower()
                is_dinger = False

                if "double play" in desc_lower or "triple play" in desc_lower:
                    print("[SKIP] Double/triple play — not a hit.")

                if "steals" in desc_lower:
                    print("[SKIP] Stolen base. At-Bat is ongoing")
                elif re.search(r'\b(homers?)\b', desc_lower) or re.search(r'\b(grand slam?)\b', desc_lower):
                    is_dinger = True

                if batting_team_id == monitored_team_id and is_dinger:
                    print("[HIT] Dinger detected — queueing trigger for Arduino pull.")
                    queue_trigger("DINGER")
                else:
                    print("[SKIP] Not a monitored-team dinger.")

                if not should_skip_event(play):
                    seen_plays.add(idx)
                else:
                    print("[SKIP] Filler event — not marking as seen.")

        except Exception as e:
            print(f"[ERROR] Fetching or processing play data failed: {e}")

        time.sleep(15)


# =====================
# ---- HTTP Routes ----
# =====================

@app.route("/")
def index():
    team_options = "".join(
        f'<option value="{id}" {"selected" if id == monitored_team_id else ""}>{name}</option>'
        for name, id in MLB_TEAMS.items()
    )
    pending = len(_trigger_q)
    html = f"""
    <html><body>
    <h1>Apple Server</h1>
    <form method=\"POST\" action=\"/set_team\">
        <label>Select Team:</label>
        <select name=\"team_id\">{team_options}</select>
        <button type=\"submit\">Set Team</button>
    </form>
    <hr/>
    <p><b>Pending triggers:</b> {pending}</p>
    <p><b>Last enqueued:</b> {last_enqueued_at}</p>
    <p><b>Last Arduino pull (ACK):</b> {last_dequeued_at}</p>

    <form method=\"POST\" action=\"/manual_trigger\" style=\"margin-top:10px;\">
        <button type=\"submit\">Trigger Apple Now 🍎</button>
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
    except Exception:
        return "Invalid team ID", 400
    return "Team updated", 200


@app.route("/manual_trigger", methods=["POST"])  # Button on the homepage
def manual_trigger():
    queue_trigger("MANUAL_BUTTON")
    return redirect("/", code=303)


@app.route("/trigger")
def trigger_route():
    """
    Arduino polls this endpoint.
      - If there is at least one pending trigger in the queue, pop one and return "TRIGGER" (200 OK).
      - Otherwise return "NONE" (200 OK).

    This guarantees the trigger persists until the Arduino has *successfully* received
    an HTTP 200 response from this route.
    """
    global last_dequeued_at
    with _state_lock:
        if _trigger_q:
            _trigger_q.popleft()
            last_dequeued_at = datetime.utcnow()
            print(f"[DEQUEUE] Arduino pulled a trigger. Remaining = {len(_trigger_q)}")
            return "TRIGGER", 200
        else:
            return "NONE", 200


@app.route("/status")
def status():
    with _state_lock:
        return {
            "monitored_team_id": monitored_team_id,
            "current_game_id": current_game_id,
            "pending_triggers": len(_trigger_q),
            "last_enqueued_at": last_enqueued_at.isoformat() if last_enqueued_at else None,
            "last_dequeued_at": last_dequeued_at.isoformat() if last_dequeued_at else None,
        }, 200


# Optional: manual enqueue for testing via curl
@app.route("/test/queue", methods=["POST"])  # curl -X POST http://host:5000/test/queue
def test_queue():
    queue_trigger("MANUAL_TEST")
    return {"ok": True, "pending": len(_trigger_q)}, 200


if __name__ == "__main__":
    threading.Thread(target=background_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
