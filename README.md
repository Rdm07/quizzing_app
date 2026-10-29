# Bounce/Pounce quiz server

Host a bounce/pounce quiz on a local network. Slides are shown exactly as designed in your deck.
One laptop runs the server and the projector; the quiz master and each team use a phone on the same Wi-Fi.

## Setup (once)
```
conda activate env_quiz
pip install -r requirements.txt
```

## 1. Build a quiz from a deck
Needs Microsoft PowerPoint installed (it renders the slides).
```
python build_quiz.py "decks/Diwali Party Quiz.pptx"
```
This writes `quizzes/<deck name>/` and prints how it read the deck. Check the list. Deck conventions:

| Slide | Meaning |
|---|---|
| Title starting `Q1`, `Q2`... | Starts a question |
| Picture-only slides after it | More of the same question |
| Title `Q1 Answer` (and any slides after) | The answer |
| Embedded video | Extracted to `quizzes/<name>/media/`; plays on the projector over its place on the slide (see Videos) |
| Completely blank slide | Safety slide, dropped |
| Text slide followed by a `Q1` slide | Round heading, shown before the round's first question |
| Round heading containing `Written Round` | Pounce-only round: slides up to the first blank slide are the question, the rest are the answer |

If anything is misdetected, edit `quizzes/<name>/quiz.json` (each question lists its slide files).

## 2. Run
```
python server.py                      # or: --quiz "<name>" --port 8000 --pin 1234 --fresh
```
It prints three URLs:
- **Projector** `http://localhost:8000/screen` - on the laptop driving the projector, open it with
  `powershell -ExecutionPolicy Bypass -File tools\open_screen.ps1` (add `-Port N` if not 8000). That opens it full screen with
  video sound allowed. Lobby shows a join QR code.
- **Teams** `http://<laptop-ip>:8000/` - one phone per team, enter a team name.
- **Quiz master** `http://<laptop-ip>:8000/admin` - enter the PIN (`/admin?pin=1234` logs straight in).

If phones can't connect: allow Python through Windows Firewall (private networks) when prompted, and check the Wi-Fi
doesn't isolate devices (many guest/corporate networks do). A phone hotspot or a small router works.

## How a question runs
1. **Intro** (first question of a round): round slide(s) on the projector. Admin taps *Show question*.
2. **Pounce open**: teams type an answer, tap *Pounce, but talk to admin*, or leave it blank. Pounces lock on submit.
   Admin picks who the question is *for*, then taps *Close pounce → start bounce*.
3. **Bounce**: starts at the "for" team and moves in the chosen direction, skipping teams that pounced.
   Admin taps *Next team* until done, or *Stop bounce*. The projector shows who it is for and who is bouncing.
4. **Scoring**: admin sees every team with a points field (decimals fine) and each pounce answer. The projector shows the
   answer slides straight away; the admin button flips back to the question slides (and again to the answer). *Submit points* moves to the next question.
5. **Written round**: no bounce; teams type answers, admin scores them all.

Admin extras: **Screen** button flips the projector between question and scorecard; **Bounce** button flips clockwise /
anti-clockwise and stays that way until flipped back (it applies from the next bounce). Clockwise follows the team list
top to bottom - reorder teams under *Seating / bounce order* to match who sits where.

## Videos
Videos embedded in slides play on the projector, in the same spot they sit on the slide. Until you press play the slide shows
its normal still frame. On the admin phone, a **Video** panel appears whenever the slide on screen has one:
**Play / Pause / Resume / Replay**, **Stop** (back to the still frame) and **Expand to full screen**.
Moving to a different slide resets the clip; staying on the same slide (e.g. closing pounce, starting bounce) lets it keep playing.
Scoring opens on the answer slide, so a question clip still running when the bounce ends stops there. An answer slide with its
own clip (e.g. Movies Q5) gets the same Video panel.

- **Sound is always on** when the projector is opened with `tools\open_screen.ps1` (it starts Edge/Chrome with autoplay allowed,
  in its own profile so the setting applies even if the browser is already open). If `/screen` is opened by hand instead, the
  browser may block sound: clips then play silently until anyone clicks or presses a key on the projector page.
- Videos are played only on the projector browser (the laptop), so phones never download them.
- Use H.264/MP4 clips (PowerPoint's default). They are copied as-is, so a long clip makes the quiz folder big.
- If a slide has more than one video, only the first is playable.

## Files
- `data/<quiz>.state.json` - autosaved game (restart the server to resume; `--fresh` starts over)
- `data/<quiz>.results.csv` - every pounce answer and points awarded, rewritten after each question
- `tests/simulate.py` - end-to-end test (`python tests/simulate.py`)

Before using with a real audience, check the deck's facts and answers: the app displays your slides as-is.
