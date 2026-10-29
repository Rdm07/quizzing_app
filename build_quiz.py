"""Convert a .pptx quiz deck into a quiz folder the server can host.

    python build_quiz.py "decks/Diwali Party Quiz.pptx"

Needs Microsoft PowerPoint installed (used to render each slide exactly as designed)
and Pillow. Output: quizzes/<name>/{quiz.json, slides/, thumbs/}.

Deck conventions (matching the reference deck):
  * "Q1" title slide starts a question; "Q1 Answer" starts its answer.
  * Picture-only slides after a question/answer belong to it.
  * Embedded videos are extracted and played over the slide on the projector.
  * Completely blank slides are "safety" slides and are dropped.
  * A text slide followed by a Q1 slide (or mentioning "Round") is a round heading.
  * A round heading containing "Written Round" makes that round pounce-only: slides up to
    the first blank slide are the question, everything after is the answer.
Edit quiz.json afterwards if any detection is wrong.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
Q_RE = re.compile(r"^Q\s*(\d+)$", re.I)
ANS_RE = re.compile(r"^Q\s*(\d+)\s+Answer$", re.I)
HERE = Path(__file__).parent


def read_slides(deck):
    """Slides in presentation order: [{'n', 'paras', 'has_pic', 'hidden'}]."""
    z = zipfile.ZipFile(deck)
    pres = ET.fromstring(z.read("ppt/presentation.xml"))
    rels = ET.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels.findall("rel:Relationship", NS)}
    size = pres.find("p:sldSz", NS)
    sz = (int(size.get("cx")), int(size.get("cy")))
    slides = []
    for i, sld in enumerate(pres.find("p:sldIdLst", NS), 1):
        path = "ppt/" + target[sld.get(f"{{{NS['r']}}}id")].lstrip("/").removeprefix("ppt/")
        root = ET.fromstring(z.read(path))
        paras = []
        for p in root.iter(f"{{{NS['a']}}}p"):
            text = ""
            for node in p:
                if node.tag == f"{{{NS['a']}}}br":
                    text += "\n"
                elif node.tag in (f"{{{NS['a']}}}r", f"{{{NS['a']}}}fld"):
                    text += "".join(x.text or "" for x in node.iter(f"{{{NS['a']}}}t"))
            paras += [t.strip() for t in text.split("\n") if t.strip()]
        has_pic = any(True for _ in root.iter(f"{{{NS['a']}}}blip"))
        slides.append({"n": i, "paras": paras, "has_pic": has_pic, "hidden": root.get("show") == "0",
                       "videos": find_videos(z, path, root, sz)})
    return slides


def find_videos(z, slide_path, root, sz):
    """Embedded videos on a slide: [{'media': zip path, 'x','y','w','h': percent of the slide}]."""
    rel_path = f"{Path(slide_path).parent}/_rels/{Path(slide_path).name}.rels".replace("\\", "/")
    if rel_path not in z.namelist():
        return []
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read(rel_path)).findall("rel:Relationship", NS)}
    found = []
    for pic in root.iter(f"{{{NS['p']}}}pic"):
        vf = next(pic.iter(f"{{{NS['a']}}}videoFile"), None)
        xf = next(pic.iter(f"{{{NS['a']}}}xfrm"), None)
        if vf is None or xf is None:
            continue
        target = rels.get(vf.get(f"{{{NS['r']}}}link"), "")
        media = "ppt/" + target.removeprefix("../") if target.startswith("../") else target.lstrip("/")
        off, ext = xf.find("a:off", NS), xf.find("a:ext", NS)
        if media in z.namelist() and off is not None and ext is not None:
            found.append({"media": media,
                          "x": round(int(off.get("x")) / sz[0] * 100, 3), "y": round(int(off.get("y")) / sz[1] * 100, 3),
                          "w": round(int(ext.get("cx")) / sz[0] * 100, 3), "h": round(int(ext.get("cy")) / sz[1] * 100, 3)})
    return found


def blank(s):
    return not s["paras"] and not s["has_pic"]


def structure(slides):
    """Walk the slides and return (title, questions)."""
    live = [s for s in slides if not s["hidden"]]
    questions, intro = [], []
    rnd, written, cur, mode = "", False, None, None
    for k, s in enumerate(live):
        first = s["paras"][0] if s["paras"] else ""
        nxt = next((x for x in live[k + 1:] if not blank(x)), None)
        nxt_first = nxt["paras"][0] if nxt and nxt["paras"] else ""
        is_q, is_ans = Q_RE.match(first), ANS_RE.match(first)
        is_section = (not is_q and not is_ans and s["paras"] and
                      (k == 0 or Q_RE.match(nxt_first) or "round" in " ".join(s["paras"]).lower()))
        if blank(s):
            if cur and mode == "q":
                mode = "gap"
            continue
        if is_section:
            intro.append(s["n"])
            if k > 0:
                rnd = first
                written = "written round" in " ".join(s["paras"]).lower()
                cur, mode = None, None
                if written:
                    cur = {"round": rnd, "label": rnd, "written": True, "intro": intro, "q": [], "a": []}
                    questions.append(cur)
                    intro, mode = [], "q"
        elif is_q:
            cur = {"round": rnd, "label": first, "written": False, "intro": intro, "q": [s["n"]], "a": []}
            questions.append(cur)
            intro, mode = [], "q"
        elif is_ans:
            if cur is None:
                continue
            cur["a"].append(s["n"])
            cur["answer_text"] = [p for p in s["paras"][1:]]
            mode = "a"
        elif cur is not None:
            (cur["q"] if mode == "q" else cur["a"]).append(s["n"])
            if mode != "q":
                cur.setdefault("answer_text", [])
                cur["answer_text"] += s["paras"]
                mode = "a"
    return questions


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("deck")
    ap.add_argument("--name", help="quiz folder name (default: deck file name)")
    ap.add_argument("--out", default=str(HERE / "quizzes"))
    ap.add_argument("--width", type=int, default=1920, help="slide image width in px")
    ap.add_argument("--keep-png", action="store_true")
    args = ap.parse_args()

    deck = Path(args.deck).resolve()
    name = args.name or deck.stem
    out = Path(args.out) / name
    slides = read_slides(deck)
    questions = structure(slides)
    if not questions:
        sys.exit("No questions found - does the deck use 'Q1' / 'Q1 Answer' titles?")

    needed = sorted({n for q in questions for key in ("intro", "q", "a") for n in q[key]})
    raw = out / "_png"
    for _ in range(5):   # OneDrive/antivirus can hold files briefly
        shutil.rmtree(out, ignore_errors=True)
        if not out.exists():
            break
        time.sleep(1)
    if out.exists():
        sys.exit(f"Could not clear {out}. Close anything using it (a running server, File Explorer) and retry.")
    raw.mkdir(parents=True)
    print(f"Exporting {len(slides)} slides with PowerPoint ...")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(HERE / "tools" / "export_slides.ps1"), "-Deck", str(deck),
                    "-OutDir", str(raw), "-Width", str(args.width)], check=True)

    (out / "slides").mkdir()
    (out / "thumbs").mkdir()
    for n in needed:
        src = raw / f"Slide{n}.PNG"
        im = Image.open(src).convert("RGB")
        im.save(out / "slides" / f"{n:03d}.jpg", quality=88, optimize=True)
        th = im.copy()
        th.thumbnail((720, 720))
        th.save(out / "thumbs" / f"{n:03d}.jpg", quality=80)
    if not args.keep_png:
        shutil.rmtree(raw, ignore_errors=True)

    # Embedded videos: the slide image shows the poster frame; the projector plays the clip over it.
    videos, copied = {}, set()
    zf = zipfile.ZipFile(deck)
    for s in slides:
        if s["n"] in needed and s["videos"]:
            if len(s["videos"]) > 1:
                print(f"  note: slide {s['n']} has {len(s['videos'])} videos; only the first is playable")
            v = s["videos"][0]
            name = Path(v["media"]).name
            if name not in copied:
                (out / "media").mkdir(exist_ok=True)
                with zf.open(v["media"]) as src, open(out / "media" / name, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                copied.add(name)
            videos[f"{s['n']:03d}.jpg"] = {"file": name, **{k: v[k] for k in ("x", "y", "w", "h")}}

    title = (slides[0]["paras"] or [name])[0]
    for i, q in enumerate(questions, 1):
        q["id"] = i
        for key in ("intro", "q", "a"):
            q[key] = [f"{n:03d}.jpg" for n in q[key]]
        q["answer_text"] = " / ".join(q.get("answer_text", []))
        q.setdefault("answer_text", "")
    (out / "quiz.json").write_text(json.dumps({"title": title, "videos": videos, "questions": questions}, indent=2,
                                              ensure_ascii=False), encoding="utf-8")
    print(f"\n{title}: {len(questions)} questions, {len(videos)} video slide(s) -> {out}")
    for q in questions:
        tag = " [WRITTEN]" if q["written"] else ""
        print(f"  {q['id']:>2}. {q['round']:<12} {q['label']:<8} q={len(q['q'])} a={len(q['a'])} "
              f"intro={len(q['intro'])}{tag}  {q['answer_text'][:50]}")


if __name__ == "__main__":
    main()
