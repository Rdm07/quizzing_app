"""End-to-end test: starts the server on a spare port and plays a whole quiz over HTTP.

    python tests/simulate.py
"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"


def post(path, body):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def event(role, token=""):
    """First SSE message = current view."""
    with urllib.request.urlopen(f"{BASE}/events?role={role}&token={token}") as r:
        for line in r:
            if line.startswith(b"data: "):
                return json.loads(line[6:])


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


proc = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", str(PORT), "--pin", "4242", "--fresh",
                         "--data", tempfile.mkdtemp()],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + "/").read()
            break
        except OSError:
            time.sleep(0.1)

    # pages and assets
    for p in ("/", "/admin", "/screen", "/static/common.js", "/qr.svg"):
        with urllib.request.urlopen(BASE + p) as r:
            check(r.status == 200, f"GET {p}")
    try:
        urllib.request.urlopen(BASE + "/static/../server.py")
        check(False, "path traversal blocked")
    except urllib.error.HTTPError as e:
        check(e.code == 404, "path traversal blocked")

    # admin auth
    check(post("/api/admin/login", {"pin": "0000"})[0] == 403, "wrong PIN rejected")
    check(post("/api/admin/start", {"token": "nope"})[0] == 401, "admin action needs token")
    admin = post("/api/admin/login", {"pin": "4242"})[1]["token"]
    A = lambda action, **kw: post(f"/api/admin/{action}", {"token": admin, **kw})

    # teams join (A,B,C,D); rejoin by name returns same token
    tok = {n: post("/api/join", {"name": n})[1]["token"] for n in "ABCD"}
    check(post("/api/join", {"name": "a"})[1]["token"] == tok["A"], "rejoin by name keeps team")
    check(post("/api/join", {"name": ""})[0] == 400, "empty name rejected")
    v = event("admin", admin)
    ids = {t["name"]: t["id"] for t in v["teams"]}
    check(v["phase"] == "lobby" and len(v["teams"]) == 4, "lobby has 4 teams")
    check(post("/api/pounce", {"token": tok["A"], "text": "x"})[0] == 400, "no pounce in lobby")

    check(A("start")[0] == 200, "start quiz")
    v = event("admin", admin)
    check(v["phase"] == "intro", "Q1 starts with intro slides (title + round)")
    check(v["slide_count"] == 2, "intro has 2 slides")
    A("slide", delta=1)
    check(event("screen")["slide_n"] == 2, "projector slide advanced")
    A("begin_question")
    v = event("admin", admin)
    check(v["phase"] == "pounce" and v["for_team"] == ids["A"], "pounce open, default for-team = first team")

    # B pounces with text, D wants to talk; A leaves blank, C leaves blank
    check(post("/api/pounce", {"token": tok["B"], "text": "Gandhara"})[0] == 200, "B pounces")
    check(post("/api/pounce", {"token": tok["B"], "text": "again"})[0] == 400, "pounce is locked")
    check(post("/api/pounce", {"token": tok["D"], "text": "", "talk": True})[0] == 200, "D pounces to talk")
    check(post("/api/pounce", {"token": tok["C"], "text": ""})[0] == 400, "empty pounce rejected")
    tv = event("team", tok["C"])
    check("Gandhara" not in json.dumps(tv), "teams can't see others' answers")
    check(event("team", tok["B"])["me"]["pounce"]["text"] == "Gandhara", "team sees its own answer")
    sv = event("screen")
    check("Gandhara" not in json.dumps(sv), "projector can't see answers")
    check(event("admin", admin)["pounces"][ids["B"]]["text"] == "Gandhara", "admin sees answers")

    # clockwise bounce starting at A -> A, C (B, D skipped)
    A("set_for", team=ids["A"])
    A("close_pounce")
    v = event("admin", admin)
    check(v["phase"] == "bounce" and v["bounce_queue"] == [ids["A"], ids["C"]], "clockwise queue skips pouncers: A, C")
    check(post("/api/pounce", {"token": tok["A"], "text": "late"})[0] == 400, "pounce closed during bounce")
    check(v["bounce_current"] == ids["A"], "bounce at A")
    A("bounce_next")
    check(event("admin", admin)["bounce_current"] == ids["C"], "bounce moves to C")
    A("bounce_next")
    v = event("admin", admin)
    check(v["phase"] == "scoring", "end of bounce -> scoring")
    check(v["answer_text"].startswith("GANDHARA"), "answer text available to admin")
    check(event("screen")["show_answer"] is True, "answer revealed on projector by default")
    A("toggle_answer")
    check(event("screen")["show_answer"] is False, "admin can flip projector back to the question")
    r = A("submit_scores", points={ids["A"]: "abc"})
    check(r[0] == 400, "non-numeric points rejected")
    check(A("submit_scores", points={ids["B"]: "5", ids["C"]: "2,5"})[0] == 200, "submit points (decimal comma ok)")
    v = event("admin", admin)
    sc = {t["name"]: t["score"] for t in v["teams"]}
    check(sc == {"A": 0, "B": 5, "C": 2.5, "D": 0}, f"scores applied {sc}")
    check(v["qi"] == 1 and v["phase"] == "pounce", "moved on to Q2 (no intro)")
    check(v["for_team"] == ids["B"], "next question suggests next team (B)")

    # anti-clockwise: flip, nobody pounces, for-team = B -> B, A, D, C
    A("toggle_direction")
    v = event("admin", admin)
    check(v["direction"] == -1, "direction flipped to anti-clockwise")
    A("set_for", team=ids["B"])
    A("close_pounce")
    check(event("admin", admin)["bounce_queue"] == [ids["B"], ids["A"], ids["D"], ids["C"]], "anti-clockwise queue B,A,D,C")
    A("stop_bounce")
    check(event("admin", admin)["phase"] == "scoring", "stop bounce -> scoring")
    A("submit_scores", points={})
    v = event("admin", admin)
    check(v["direction"] == -1, "direction stays anti-clockwise across questions (sticky)")
    check(v["for_team"] == ids["A"], "next suggestion steps anti-clockwise from B -> A")

    # scoreboard toggle
    A("toggle_screen")
    check(event("screen")["screen_mode"] == "scoreboard", "projector -> scorecard")
    A("toggle_screen")
    check(event("screen")["screen_mode"] == "slide", "projector -> question")

    # video question (Movies Q5): clip is controlled from the admin phone
    while event("admin", admin)["qi"] != 9:
        A("skip")
    v = event("admin", admin)
    check(v["label"] == "Q5" and v["round"] == "Movies" and v["video"]["state"] == "stopped", "Movies Q5 has a video, ready")
    sv = event("screen")["video"]
    check(sv["url"].startswith("/media/") and 20 < sv["x"] < 21 and sv["state"] == "stopped", "projector gets clip url + position")
    check(A("video_pause")[0] == 200 and event("admin", admin)["video"]["state"] == "stopped", "pause before play is a no-op")
    A("video_play")
    check(event("screen")["video"]["state"] == "playing", "play")
    A("video_pause")
    check(event("screen")["video"]["state"] == "paused", "pause")
    A("video_play"); A("video_full")
    check(event("screen")["video"]["full"] is True, "expand to full screen")
    post("/api/video_ended", {"key": sv["key"]})
    check(event("admin", admin)["video"]["state"] == "ended", "projector reports clip ended")
    A("video_play")
    check(event("admin", admin)["video"]["state"] == "playing", "replay after ended")
    A("video_stop")
    v = event("admin", admin)
    check(v["video"]["state"] == "stopped" and not v["video"]["full"], "stop resets clip and full-screen")
    A("video_play")
    A("close_pounce")
    check(event("admin", admin)["video"]["state"] == "playing", "clip keeps playing while the same slide stays up")
    A("stop_bounce")
    v = event("admin", admin)
    check(v["phase"] == "scoring" and v["show_answer"], "scoring opens on the answer slide")
    check(v["video"] is not None and v["video"]["state"] == "stopped", "answer slide has its own clip, starts stopped")
    sa = event("screen")["video"]
    check(sa["url"] != sv["url"] and sa["key"] != sv["key"], "answer clip is a different video")
    A("video_play")
    check(event("screen")["video"]["state"] == "playing", "answer clip plays")
    with urllib.request.urlopen(urllib.request.Request(BASE + sa["url"], headers={"Range": "bytes=0-99"})) as r:
        check(r.status == 206, "answer clip is served")
    A("toggle_answer")
    v = event("admin", admin)
    check(not v["show_answer"] and v["video"]["key"] == sv["key"] and v["video"]["state"] == "stopped",
          "admin can flip back to the question slide and its clip")
    A("submit_scores", points={})
    check(event("admin", admin)["video"] is None, "no video controls on slides without video")
    check(A("video_play")[0] == 400, "video action rejected on a slide without video")
    # media streaming with Range
    req = urllib.request.Request(BASE + sv["url"], headers={"Range": "bytes=0-99"})
    with urllib.request.urlopen(req) as r:
        body = r.read()
        check(r.status == 206 and len(body) == 100 and r.headers["Content-Range"].startswith("bytes 0-99/"), "media served with Range (206)")
    req = urllib.request.Request(BASE + sv["url"], headers={"Range": "bytes=4-7"})
    with urllib.request.urlopen(req) as r:
        check(r.read() == b"ftyp", "range slice is the mp4 'ftyp' header")
    try:
        urllib.request.urlopen(urllib.request.Request(BASE + sv["url"], headers={"Range": "bytes=999999999999-"}))
        check(False, "bad range -> 416")
    except urllib.error.HTTPError as e:
        check(e.code == 416, "bad range -> 416")
    try:
        urllib.request.urlopen(BASE + "/media/../quiz.json")
        check(False, "media path traversal blocked")
    except urllib.error.HTTPError as e:
        check(e.code == 404, "media path traversal blocked")

    # skip to the written round (Q11)
    while event("admin", admin)["round"] != "Logos":
        A("skip")
    v = event("admin", admin)
    check(v["written"] and v["phase"] == "intro", "written round starts with round slide")
    A("begin_question")
    v = event("admin", admin)
    check(v["slide_count"] == 4, "written round shows 4 logo slides")
    A("goto_slide", index=3)
    check(event("screen")["slide_n"] == 4, "numbered button jumps to slide 4")
    A("goto_slide", index=0)
    check(event("screen")["slide_n"] == 1, "numbered button jumps back to slide 1")
    check(A("goto_slide", index=99)[0] == 200 and event("screen")["slide_n"] == 4, "out-of-range index is clamped")
    post("/api/pounce", {"token": tok["A"], "text": "they are all blue"})
    A("close_pounce")
    v = event("admin", admin)
    check(v["phase"] == "scoring", "written round skips bounce")
    check(v["show_answer"] and v["slide_count"] == 5, "scoring shows the 5 answer slides first")
    A("toggle_answer")
    check(event("admin", admin)["slide_count"] == 4, "flip back shows the 4 question slides")
    A("submit_scores", points={ids["A"]: "10"})

    while event("admin", admin)["phase"] != "finished":
        A("skip")
    v = event("admin", admin)
    check(v["phase"] == "finished", "quiz finishes")
    check(event("screen")["phase"] == "finished", "projector shows final")
    with urllib.request.urlopen(f"{BASE}/results.csv?token={admin}") as r:
        csv_text = r.read().decode("utf-8-sig")
    check("they are all blue" in csv_text and "Gandhara" in csv_text, "results CSV has answers")
    print("\nALL TESTS PASSED")
finally:
    proc.terminate()
    err = proc.stderr.read().decode()
    if err.strip():
        print("SERVER STDERR:\n" + err)
