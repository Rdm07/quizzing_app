"""Bounce/Pounce quiz server for a local network.

    python server.py                    # hosts the only (or first) quiz in quizzes/
    python server.py --quiz "Diwali Party Quiz" --port 8000 --pin 1234 --fresh

Pages:  /  (teams)   /admin  (quiz master phone)   /screen  (projector)
Standard library only, except the optional `qrcode` package for the join QR code.
"""
import argparse
import csv
import io
import json
import mimetypes
import random
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).parent
STATIC = HERE / "static"


class GameError(Exception):
    pass


class Game:
    def __init__(self, quiz_dir, data_dir, pin, fresh=False):
        self.quiz_dir = Path(quiz_dir)
        self.quiz = json.loads((self.quiz_dir / "quiz.json").read_text(encoding="utf-8"))
        self.questions = self.quiz["questions"]
        self.videos = self.quiz.get("videos", {})   # slide file -> {file, x, y, w, h} (percent of slide)
        self.pin = pin
        self.admin_tokens = set()
        self.cv = threading.Condition()
        self.version = 0
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = data_dir / f"{self.quiz_dir.name}.state.json"
        self.results_path = data_dir / f"{self.quiz_dir.name}.results.csv"
        self.join_url = ""
        self.other_urls = []   # other addresses this machine has (Wi-Fi vs VPN etc.)
        self.s = self.fresh_state()
        if self.state_path.exists() and not fresh:
            self.s.update(json.loads(self.state_path.read_text(encoding="utf-8")))

    @staticmethod
    def fresh_state():
        return {
            "teams": [],            # seating order = join order; defines bounce order
            "phase": "lobby",       # lobby | intro | pounce | bounce | scoring | finished
            "qi": 0,
            "slide": 0,             # index into the slide set currently on the projector
            "show_answer": False,   # scoring phase: projector shows the answer slides (on by default; admin can flip back)
            "screen_mode": "slide", # slide | scoreboard (projector)
            "direction": 1,         # +1 clockwise (join order), -1 anti-clockwise; sticky
            "for_team": None,
            "last_for": None,
            "queue": [],
            "pos": 0,
            "pounces": {},          # team id -> {"text": str, "talk": bool}
            "history": [],
            "video": {"key": None, "state": "stopped", "full": False},   # video on the current slide
        }

    # ---------- helpers ----------
    def q(self):
        return self.questions[min(self.s["qi"], len(self.questions) - 1)]

    def team(self, tid):
        return next((t for t in self.s["teams"] if t["id"] == tid), None)

    def by_token(self, token):
        return next((t for t in self.s["teams"] if t["token"] == token), None)

    def slide_set(self):
        s, q = self.s, self.q()
        if s["phase"] == "intro":
            return q["intro"]
        if s["phase"] == "scoring" and s["show_answer"]:
            return q["a"]
        return q["q"]

    def cur_slide(self):
        sl = self.slide_set()
        return sl[min(self.s["slide"], len(sl) - 1)] if sl else None

    def suggest_for(self):
        """Default 'question is for' team: one step on from the previous question's team."""
        ts = self.s["teams"]
        if not ts:
            return None
        ids = [t["id"] for t in ts]
        last = self.s["last_for"]
        if last not in ids:
            return ids[0]
        return ids[(ids.index(last) + self.s["direction"]) % len(ids)]

    def build_queue(self):
        ts, s = self.s["teams"], self.s
        ids = [t["id"] for t in ts]
        start = ids.index(s["for_team"]) if s["for_team"] in ids else 0
        order = [ids[(start + s["direction"] * k) % len(ids)] for k in range(len(ids))]
        return [i for i in order if i not in s["pounces"]]

    def enter_question(self, qi):
        s = self.s
        s.update(qi=qi, slide=0, show_answer=False, pounces={}, queue=[], pos=0, for_team=None)
        if qi >= len(self.questions):
            s.update(phase="finished", qi=len(self.questions) - 1, screen_mode="scoreboard")
            return
        if self.q()["intro"]:
            s["phase"] = "intro"
        else:
            self.begin_pounce()

    def begin_pounce(self):
        s = self.s
        s.update(phase="pounce", slide=0, for_team=self.suggest_for())

    def to_scoring(self):
        self.s.update(phase="scoring", slide=0, show_answer=True)   # answer slides go up by default

    # ---------- team actions ----------
    def join(self, name, token):
        name = " ".join((name or "").split())[:30]
        with self.cv:
            t = self.by_token(token) if token else None
            if not t:
                if not name:
                    raise GameError("Enter a team name")
                t = next((x for x in self.s["teams"] if x["name"].lower() == name.lower()), None)
            if not t:
                t = {"id": "t" + secrets.token_hex(3), "name": name, "token": secrets.token_hex(8), "score": 0.0}
                self.s["teams"].append(t)
            self.commit()
            return {"token": t["token"]}

    def pounce(self, token, text, talk):
        with self.cv:
            t = self.by_token(token)
            if not t:
                raise GameError("Unknown team")
            if self.s["phase"] != "pounce":
                raise GameError("Pounce is closed")
            if t["id"] in self.s["pounces"]:
                raise GameError("You have already pounced")
            text = (text or "").strip()[:500]
            if not text and not talk:
                raise GameError("Type an answer or tap 'Pounce, talk to admin'")
            self.s["pounces"][t["id"]] = {"text": text, "talk": bool(talk)}
            self.commit()

    # ---------- admin actions ----------
    def admin(self, action, p):
        s = self.s
        with self.cv:
            ph = s["phase"]
            if action == "start":
                if ph != "lobby" or not s["teams"]:
                    raise GameError("Need at least one team in the lobby")
                self.enter_question(0)
            elif action == "begin_question" and ph == "intro":
                self.begin_pounce()
            elif action == "set_for" and ph == "pounce":
                if not self.team(p.get("team")):
                    raise GameError("Unknown team")
                s["for_team"] = p["team"]
            elif action == "close_pounce" and ph == "pounce":
                if self.q()["written"]:
                    self.to_scoring()
                else:
                    s["last_for"] = s["for_team"]
                    s["queue"], s["pos"] = self.build_queue(), 0
                    if s["queue"]:
                        s.update(phase="bounce", slide=0)
                    else:
                        self.to_scoring()
            elif action == "bounce_next" and ph == "bounce":
                s["pos"] += 1
                if s["pos"] >= len(s["queue"]):
                    self.to_scoring()
            elif action == "bounce_prev" and ph == "bounce":
                s["pos"] = max(0, s["pos"] - 1)
            elif action == "stop_bounce" and ph == "bounce":
                self.to_scoring()
            elif action == "toggle_direction":
                s["direction"] *= -1   # applies to the next bounce / suggestion
            elif action == "set_mode":
                if p.get("mode") not in ("slide", "scoreboard"):
                    raise GameError("Bad mode")
                s["screen_mode"] = p["mode"]
            elif action == "toggle_screen":
                s["screen_mode"] = "scoreboard" if s["screen_mode"] == "slide" else "slide"
            elif action == "slide":
                n = len(self.slide_set())
                s["slide"] = max(0, min(n - 1, s["slide"] + int(p.get("delta", 0))))
            elif action == "goto_slide":
                n = len(self.slide_set())
                s["slide"] = max(0, min(n - 1, int(p.get("index", 0))))
            elif action in ("video_play", "video_pause", "video_stop", "video_full"):
                if self.cur_slide() not in self.videos:
                    raise GameError("There is no video on this slide")
                v = s["video"]
                if action == "video_play":
                    v["state"] = "playing"
                elif action == "video_pause":
                    v["state"] = "paused" if v["state"] == "playing" else v["state"]
                elif action == "video_stop":
                    v.update(state="stopped", full=False)
                else:
                    v["full"] = not v["full"]
            elif action == "toggle_answer" and ph == "scoring":
                s.update(show_answer=not s["show_answer"], slide=0, screen_mode="slide")
            elif action == "submit_scores" and ph == "scoring":
                self.submit_scores(p.get("points") or {})
            elif action == "skip" and ph in ("intro", "pounce", "bounce", "scoring"):
                self.enter_question(s["qi"] + 1)
            elif action == "move_team":
                ts = s["teams"]
                i = next((k for k, t in enumerate(ts) if t["id"] == p.get("team")), None)
                if i is None:
                    raise GameError("Unknown team")
                j = i + (-1 if int(p.get("delta", 0)) < 0 else 1)
                if 0 <= j < len(ts):
                    ts[i], ts[j] = ts[j], ts[i]
            elif action == "remove_team" and ph == "lobby":
                s["teams"] = [t for t in s["teams"] if t["id"] != p.get("team")]
            elif action == "restart":
                s.update(self.fresh_state())
            else:
                raise GameError(f"Can't '{action}' right now")
            self.commit()

    def submit_scores(self, points):
        s, q = self.s, self.q()
        awarded = {}
        for t in s["teams"]:
            raw = str(points.get(t["id"], "") or "0").strip().replace(",", ".")
            try:
                v = float(raw)
            except ValueError:
                raise GameError(f"'{raw}' is not a number (team {t['name']})")
            if abs(v) > 1000:
                raise GameError("Points look wrong")
            awarded[t["id"]] = v
        for t in s["teams"]:
            t["score"] = round(t["score"] + awarded[t["id"]], 2)
        s["history"].append({"qid": q["id"], "round": q["round"], "label": q["label"],
                             "points": awarded, "pounces": dict(s["pounces"]),
                             "names": {t["id"]: t["name"] for t in s["teams"]}})
        self.write_results()
        self.enter_question(s["qi"] + 1)

    def write_results(self):
        try:
            with open(self.results_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["question", "round", "label", "team", "pounce_answer", "talk_to_admin", "points"])
                for h in self.s["history"]:
                    for tid, pts in h["points"].items():
                        pz = h["pounces"].get(tid, {})
                        w.writerow([h["qid"], h["round"], h["label"], h["names"].get(tid, tid),
                                    pz.get("text", ""), "yes" if pz.get("talk") else "", pts])
        except OSError as e:   # e.g. the CSV is open in Excel
            print(f"  warning: could not write {self.results_path.name} ({e}); it is rewritten after every question.")

    def commit(self):
        cur = self.cur_slide()   # moving to another slide always resets the video
        if self.s["video"].get("key") != cur:
            self.s["video"] = {"key": cur, "state": "stopped", "full": False}
        self.version += 1
        self.cv.notify_all()
        self.save()

    def video_ended(self, key):
        with self.cv:
            v = self.s["video"]
            if v.get("key") == key and v["state"] == "playing":
                v["state"] = "ended"
                self.commit()

    def save(self):
        """Best-effort autosave: Windows (OneDrive/antivirus) can briefly lock files, and that must never break a live quiz."""
        data = json.dumps(self.s, ensure_ascii=False)
        tmp = self.state_path.with_suffix(".tmp")
        for attempt in range(5):
            try:
                tmp.write_text(data, encoding="utf-8")
                tmp.replace(self.state_path)
                return
            except OSError:
                time.sleep(0.05 * (attempt + 1))
        try:
            self.state_path.write_text(data, encoding="utf-8")
        except OSError as e:
            print(f"  warning: could not save game state ({e}); the quiz continues in memory.")

    # ---------- views ----------
    def view(self, role, token=None):
        s, q = self.s, self.q()
        ids = [t["id"] for t in s["teams"]]
        cur = s["queue"][s["pos"]] if s["phase"] == "bounce" and s["pos"] < len(s["queue"]) else None
        v = {
            "title": self.quiz["title"], "phase": s["phase"], "qi": s["qi"], "total": len(self.questions),
            "round": q["round"], "label": q["label"], "written": q["written"],
            "direction": s["direction"], "screen_mode": s["screen_mode"],
            "teams": [{"id": t["id"], "name": t["name"], "score": t["score"], "pounced": t["id"] in s["pounces"]}
                      for t in s["teams"]],
            "for_team": s["for_team"], "bounce_current": cur, "bounce_queue": s["queue"], "bounce_pos": s["pos"],
        }
        if role == "screen":
            sl = self.slide_set()
            v["slide_url"] = f"/slides/{sl[min(s['slide'], len(sl) - 1)]}" if sl else None
            v["slide_n"], v["slide_count"] = min(s["slide"], len(sl) - 1) + 1, len(sl)
            v["show_answer"] = s["show_answer"]
            v["join_url"] = self.join_url
            v["other_urls"] = self.other_urls
            v["video"] = self.video_view(True)
        elif role == "team":
            t = self.by_token(token)
            v["me"] = None if not t else {"id": t["id"], "name": t["name"], "score": t["score"],
                                          "pounce": s["pounces"].get(t["id"])}
        elif role == "admin":
            sl = self.slide_set()
            v.update(
                pounces={tid: pz for tid, pz in s["pounces"].items()},
                slide_n=min(s["slide"], len(sl) - 1) + 1, slide_count=len(sl), show_answer=s["show_answer"],
                q_thumbs=[f"/thumbs/{x}" for x in q["q"]], a_thumbs=[f"/thumbs/{x}" for x in q["a"]],
                intro_thumbs=[f"/thumbs/{x}" for x in q["intro"]],
                cur_thumb=f"/thumbs/{sl[min(s['slide'], len(sl) - 1)]}" if sl else None,
                answer_text=q.get("answer_text", ""), join_url=self.join_url, other_urls=self.other_urls, video=self.video_view(False),
            )
        return v


    def video_view(self, with_url):
        key = self.cur_slide()
        info = self.videos.get(key)
        if not info:
            return None
        v = {"key": key, "state": self.s["video"]["state"], "full": self.s["video"]["full"]}
        if with_url:
            v.update(url=f"/media/{info['file']}", x=info["x"], y=info["y"], w=info["w"], h=info["h"])
        return v


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    game: Game = None
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *a):
        pass

    def send_bytes(self, code, body, ctype, cache=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send_bytes(code, json.dumps(obj).encode(), "application/json")

    def send_ranged(self, path):
        """Stream a big media file, honouring Range requests (browsers need them to play/seek video)."""
        path = Path(path)
        if not path.is_file():
            return self.send_bytes(404, b"Not found", "text/plain")
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        m = (self.headers.get("Range") or "").strip()
        if m.startswith("bytes="):
            a, _, b = m[6:].split(",")[0].partition("-")
            try:
                if a == "":
                    start = max(0, size - int(b))
                else:
                    start = int(a)
                    end = min(int(b), size - 1) if b else size - 1
            except ValueError:
                start, end = 0, size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionError, OSError):
            pass   # browser closed the connection (normal when seeking)

    def send_file(self, path, cache=None):
        path = Path(path)
        if not path.is_file():
            return self.send_bytes(404, b"Not found", "text/plain")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send_bytes(200, path.read_bytes(), ctype, cache)

    def safe_join(self, base, rel):
        p = (base / unquote(rel)).resolve()
        return p if base.resolve() in p.parents else Path("/nonexistent")

    def do_GET(self):
        u = urlparse(self.path)
        g = self.game
        if u.path == "/":
            return self.send_file(STATIC / "team.html")
        if u.path in ("/admin", "/screen"):
            return self.send_file(STATIC / (u.path[1:] + ".html"))
        if u.path.startswith("/static/"):
            return self.send_file(self.safe_join(STATIC, u.path[8:]))
        if u.path.startswith("/slides/") or u.path.startswith("/thumbs/"):
            kind = u.path.split("/")[1]
            return self.send_file(self.safe_join(g.quiz_dir / kind, u.path.split("/", 2)[2]), "public, max-age=3600")
        if u.path.startswith("/media/"):
            return self.send_ranged(self.safe_join(g.quiz_dir / "media", u.path[7:]))
        if u.path == "/qr.svg":
            return self.send_qr()
        if u.path == "/results.csv":
            if not self.is_admin(parse_qs(u.query).get("token", [""])[0]):
                return self.send_bytes(401, b"Admin only", "text/plain")
            return self.send_file(g.results_path)
        if u.path == "/events":
            return self.events(parse_qs(u.query))
        self.send_bytes(404, b"Not found", "text/plain")

    def is_admin(self, token):
        return bool(token) and token in self.game.admin_tokens

    def send_qr(self):
        try:
            import qrcode
            import qrcode.image.svg
            buf = io.BytesIO()
            qrcode.make(self.game.join_url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10).save(buf)
            self.send_bytes(200, buf.getvalue(), "image/svg+xml")
        except Exception:
            self.send_bytes(404, b"qrcode package not installed", "text/plain")

    def events(self, qs):
        g = self.game
        role = qs.get("role", ["screen"])[0]
        token = qs.get("token", [""])[0]
        if role not in ("screen", "team", "admin"):
            return self.send_bytes(400, b"bad role", "text/plain")
        if role == "admin" and not self.is_admin(token):
            return self.send_bytes(401, b"Admin only", "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        last = -1
        try:
            while True:
                with g.cv:
                    if g.version == last:
                        g.cv.wait(15)
                    changed = g.version != last
                    last = g.version
                    payload = json.dumps(g.view(role, token)) if changed else None
                self.wfile.write(b"data: " + payload.encode() + b"\n\n" if payload else b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def do_POST(self):
        g = self.game
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
        except ValueError:
            return self.send_json({"error": "Bad request"}, 400)
        path = urlparse(self.path).path
        try:
            if path == "/api/join":
                return self.send_json(g.join(body.get("name"), body.get("token")))
            if path == "/api/pounce":
                g.pounce(body.get("token"), body.get("text"), body.get("talk"))
                return self.send_json({"ok": True})
            if path == "/api/video_ended":   # projector reports the clip finished
                g.video_ended(body.get("key"))
                return self.send_json({"ok": True})
            if path == "/api/admin/login":
                if not secrets.compare_digest(str(body.get("pin", "")), g.pin):
                    return self.send_json({"error": "Wrong PIN"}, 403)
                tok = secrets.token_hex(12)
                g.admin_tokens.add(tok)
                return self.send_json({"token": tok})
            if path.startswith("/api/admin/"):
                if not self.is_admin(body.get("token")):
                    return self.send_json({"error": "Not authorised"}, 401)
                g.admin(path.rsplit("/", 1)[1], body)
                return self.send_json({"ok": True})
            self.send_json({"error": "Not found"}, 404)
        except GameError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:   # never leave a phone hanging
            print(f"  error handling {path}: {e!r}")
            self.send_json({"error": "Server error, try again"}, 500)


def lan_ips():
    """All usable IPv4 addresses of this machine, home/office Wi-Fi style ranges first.

    A laptop often has several (Wi-Fi, VPN, virtual adapters); phones can only reach the one on their own network.
    """
    found = set()
    try:
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))   # no packets sent; reveals the default-route interface
        found.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    def rank(ip):
        a, b = (int(x) for x in ip.split(".")[:2])
        return 0 if (a, b) == (192, 168) else 1 if a == 172 and 16 <= b <= 31 else 2 if a == 10 else 3
    ips = sorted((ip for ip in found if not ip.startswith(("127.", "169.254."))), key=lambda ip: (rank(ip), ip))
    return ips or ["127.0.0.1"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiz", help="folder name inside quizzes/ (default: first found)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--pin", help="admin PIN (default: random 4 digits)")
    ap.add_argument("--data", help="folder for saved game state and results (default: data/)")
    ap.add_argument("--ip", help="address phones should use (default: auto; if wrong, pick one from the list printed at startup)")
    ap.add_argument("--fresh", action="store_true", help="ignore any saved game and start over")
    args = ap.parse_args()

    quizzes = sorted(p for p in (HERE / "quizzes").glob("*") if (p / "quiz.json").exists())
    if not quizzes:
        raise SystemExit("No quizzes found. Run: python build_quiz.py <deck.pptx>")
    qdir = next((p for p in quizzes if p.name == args.quiz), None) if args.quiz else quizzes[0]
    if qdir is None:
        raise SystemExit(f"Quiz '{args.quiz}' not found. Available: {', '.join(p.name for p in quizzes)}")

    pin = args.pin or f"{random.randint(0, 9999):04d}"
    game = Game(qdir, Path(args.data) if args.data else HERE / "data", pin, args.fresh)
    ips = lan_ips()
    ip = args.ip or ips[0]
    game.join_url = f"http://{ip}:{args.port}/"
    game.other_urls = [f"http://{i}:{args.port}/" for i in ips if i != ip]
    Handler.game = game

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 64

    print(f"\n  Quiz: {game.quiz['title']}  ({len(game.questions)} questions)")
    print(f"  Teams join   : {game.join_url}")
    if game.other_urls:
        print("  Other addresses on this machine (use one of these instead if phones can't connect to the above):")
        for u in game.other_urls:
            print(f"                 {u}")
        print("  Phones must be on the same Wi-Fi network as the address they use; VPN/Ethernet addresses won't work.")
    print(f"  Quiz master  : {game.join_url}admin     PIN: {pin}")
    print(f"  Projector    : http://localhost:{args.port}/screen")
    if game.s["teams"] or game.s["history"]:
        print(f"  Resuming saved game ({len(game.s['teams'])} teams). Use --fresh to start over.")
    print("\n  Ctrl+C to stop.\n")
    try:
        Server(("0.0.0.0", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
