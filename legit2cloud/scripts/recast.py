#!/usr/bin/env python3
"""Recast: a real moment from a real video, performed by a cartoon character.

    python recast.py --video dinner.mp4 --character turkey -o out.mp4

What it does, in order (each stage's result is cached in the work dir, so a rerun resumes):

  1. listen    the video's speech, with times
  2. propose   candidate moments: runs of whole sentences that fit a render
  3. rank      a language model scores each candidate for how funny / quotable it is
  4. look      a vision model looks at frames of the best candidates: is there ONE person,
               where are they in the frame, how are they framed; a cut inside the moment
               disqualifies it
  5. choose    the moment with the best JOINT score (funny x re-renderable), its length
               snapped to the renderer's segment grid so no part-segment is paid for
  6. crop      the moment is cut out and cropped to that one person (9:16)
  7. cast      a character picture is made in the SAME framing as the person
  8. render    motion video: the character performs the person's movement, real audio kept

Everything goes through the public API with an ordinary project key (L2C_API_KEY).
The pure helpers at the top are what the method consists of; `--selftest` checks them.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                                # a deployed copy carries the client beside it
sys.path.insert(0, os.path.join(HERE, "..", "web", "skill", "legit2cloud", "scripts"))
sys.path.insert(0, os.path.join(HERE, "..", "..", ".claude", "skills", "legit2cloud", "scripts"))

FPS = 24
SEG_FIRST = 81          # frames in the renderer's first segment
SEG_NEXT = 80           # frames every later segment adds
MIN_SECONDS = 3.0
MAX_SECONDS = 10.5      # three segments
TEXT_MODEL = os.getenv("RECAST_TEXT_MODEL", "qwen3.8:27b")
VISION_MODEL = os.getenv("RECAST_VISION_MODEL", "minicpm-v:latest")
STT_MODEL = os.getenv("RECAST_STT_MODEL", "whisper-large-v3-turbo")
OUT_W, OUT_H = 480, 832

CHARACTERS = {
    "turkey": "a friendly cartoon turkey with big expressive eyes, brown and orange feathers, a red wattle, "
              "wearing a small pilgrim hat, with wing-arms and feathered hands",
    "elf": "a cheerful cartoon Christmas elf with pointed ears, rosy cheeks, a green tunic and a red hat with a bell",
    "pilgrim": "a round cartoon pilgrim with a tall black buckled hat, a white collar and a bushy moustache",
    "snowman": "a cartoon snowman with stick arms, a carrot nose, a striped scarf and a top hat",
    "gingerbread": "a cartoon gingerbread person with white icing trim, gumdrop buttons and a wide icing smile",
}
SCENES = {
    "thanksgiving": "a warm family dining room, a table set for Thanksgiving dinner behind, soft evening light",
    "christmas": "a cosy living room with a decorated Christmas tree and warm string lights behind",
    "plain": "a clean softly lit studio with a plain warm backdrop",
}
FRAMING_WORDS = {
    "head": "close-up portrait, head and shoulders",
    "waist": "waist-up, both arms and hands visible",
    "full": "full body from head to toe, standing",
}


# ---------------------------------------------------------------------------
# The method (pure; no I/O)
# ---------------------------------------------------------------------------

def grid_lengths(max_seconds: float = MAX_SECONDS) -> list[float]:
    """Render lengths that are whole segments: 81, 161, 241 ... frames."""
    out, frames = [], SEG_FIRST
    while frames / FPS <= max_seconds + 1e-6:
        out.append(frames / FPS)
        frames += SEG_NEXT
    return out


def snap_window(start: float, end: float, source_seconds: float, max_seconds: float = MAX_SECONDS):
    """Fit [start, end) to the segment grid.

    The renderer pays a whole segment for any part of one, so the window is widened to the next
    grid length when the source has room (the extra goes mostly after the last word: a reaction
    beat), or narrowed to the grid length below when it does not. Returns (start, seconds)."""
    want = max(0.0, end - start)
    grid = grid_lengths(max_seconds)
    up = next((g for g in grid if g >= want - 0.15), None)
    if up is not None and up <= source_seconds + 1e-6:
        pad = up - want
        lead = min(start, pad * 0.25)
        s = start - lead
        if s + up > source_seconds:
            s = max(0.0, source_seconds - up)
        return round(s, 3), round(up, 3)
    down = [g for g in grid if g <= min(want, source_seconds) + 1e-6]
    if not down:
        return round(max(0.0, min(start, source_seconds - grid[0])), 3), round(min(grid[0], source_seconds), 3)
    return round(start, 3), round(down[-1], 3)


_SENTENCE_END = re.compile(r"""(?<=[.!?…]["'”’)])\s+|(?<=[.!?…])\s+""")


def split_sentences(segments):
    """Speech arrives in long timed chunks (up to ~30 s) with no word times. Cut each chunk into
    its sentences and give every sentence a share of the chunk's time in proportion to its
    length. The times are estimates; snap_to_pauses() then moves them onto real pauses."""
    out = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text or s["end"] <= s["start"]:
            continue
        parts = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]
        total = sum(len(p) for p in parts) or 1
        t = float(s["start"])
        for p in parts:
            d = (s["end"] - s["start"]) * len(p) / total
            out.append({"start": t, "end": t + d, "text": p})
            t += d
    return out


def snap_to_pauses(units, pauses, reach: float = 1.2):
    """Move each boundary between two sentences onto the nearest pause in the audio (a pause is
    (start, end) of near-silence), if one lies within `reach` seconds. A sentence boundary that
    sits inside a pause cuts no word in half; one that found no pause keeps its estimate."""
    units = [dict(u) for u in units]
    mids = sorted((a + b) / 2 for a, b in pauses)
    for i in range(1, len(units)):
        t = units[i]["start"]
        lo = units[i - 1]["start"] + 0.3
        hi = units[i]["end"] - 0.3
        near = [m for m in mids if abs(m - t) <= reach and lo < m < hi]
        if near:
            m = min(near, key=lambda v: abs(v - t))
            units[i - 1]["end"] = units[i]["start"] = round(m, 3)
            units[i]["snapped"] = True
    return units


def candidate_windows(segments, min_s: float = MIN_SECONDS, max_s: float = MAX_SECONDS, limit: int = 40):
    """Every run of consecutive spoken segments whose span fits [min_s, max_s]."""
    segs = [s for s in segments if (s.get("text") or "").strip() and s["end"] > s["start"]]
    out = []
    for i in range(len(segs)):
        for j in range(i, len(segs)):
            span = segs[j]["end"] - segs[i]["start"]
            if span > max_s:
                break
            if span >= min_s:
                out.append({"start": segs[i]["start"], "end": segs[j]["end"],
                            "text": " ".join(s["text"].strip() for s in segs[i:j + 1])})
    if len(out) > limit:  # keep an even spread, never only the beginning
        step = len(out) / limit
        out = [out[int(k * step)] for k in range(limit)]
    for n, c in enumerate(out):
        c["id"] = n
    return out


def even_windows(source_seconds: float, seconds: float = 6.7, limit: int = 12):
    """Fallback when there is no timed speech: evenly spaced windows."""
    if source_seconds <= seconds:
        return [{"id": 0, "start": 0.0, "end": source_seconds, "text": ""}]
    n = min(limit, max(1, int(source_seconds // (seconds / 2)) - 1))
    step = (source_seconds - seconds) / max(1, n - 1) if n > 1 else 0
    return [{"id": k, "start": round(k * step, 2), "end": round(k * step + seconds, 2), "text": ""} for k in range(n)]


def render_score(looks) -> float:
    """0..1: how well a moment can be re-performed, from what the vision model saw in its frames.

    One person, visible in every frame, framed waist-up or wider, staying in one place."""
    looks = [l for l in looks if l]
    if not looks:
        return 0.0
    score = 1.0
    people = [int(l.get("people") or 0) for l in looks]
    if any(p == 0 for p in people):
        return 0.0
    extra = max(people) - 1
    score *= 1.0 if extra == 0 else (0.7 if extra == 1 else 0.45)      # others can be cropped out, at a cost
    framing = [l.get("framing") for l in looks]
    score *= {"full": 1.0, "waist": 1.0, "head": 0.6}.get(max(set(framing), key=framing.count), 0.5)
    faces = sum(1 for l in looks if l.get("face_visible", True))
    if faces * 2 <= len(looks):                                         # hidden in most frames, not one
        score *= 0.6
    boxes = [l.get("box") for l in looks if valid_box(l.get("box"))]
    if len(boxes) >= 2:   # the person wanders: centre drift across the frames
        cx = sorted((b[0] + b[2]) / 2 for b in boxes)
        if len(cx) >= 4:  # one frame's box may be a misreading: drop the worse end
            cx = cx[1:] if (cx[1] - cx[0]) > (cx[-1] - cx[-2]) else cx[:-1]
        drift = cx[-1] - cx[0]
        score *= 1.0 if drift < 0.2 else (0.75 if drift < 0.35 else 0.45)
    return round(score, 3)


def joint_score(funny: float, render: float, cuts: int) -> float:
    """A cut inside the moment breaks the motion: disqualify. Otherwise funny x renderable, with
    renderability weighted up: an unfunny clean render is dull, a funny broken render is unusable."""
    if cuts > 0:
        return 0.0
    return round(max(0.0, min(1.0, funny)) * (render ** 1.5), 4)


def valid_box(b) -> bool:
    try:
        x0, y0, x1, y1 = (float(v) for v in b)
    except (TypeError, ValueError):
        return False
    return 0 <= x0 < x1 <= 1.0001 and 0 <= y0 < y1 <= 1.0001 and (x1 - x0) > 0.08 and (y1 - y0) > 0.08


def union_box(boxes):
    boxes = [b for b in boxes if valid_box(b)]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def crop_rect(box, src_w: int, src_h: int, out_w: int = OUT_W, out_h: int = OUT_H, margin: float = 0.18):
    """The largest-detail out_w:out_h crop that holds the person (box is 0..1) plus a margin for
    their gestures. No box: the centre. Returns even pixel (x, y, w, h)."""
    ar = out_w / out_h
    if not valid_box(box):
        box = [0.0, 0.0, 1.0, 1.0]
    bx0, by0, bx1, by1 = box[0] * src_w, box[1] * src_h, box[2] * src_w, box[3] * src_h
    bw, bh = (bx1 - bx0) * (1 + 2 * margin), (by1 - by0) * (1 + margin)
    w = max(bw, bh * ar)
    h = w / ar
    if h > src_h:
        h, w = float(src_h), src_h * ar
    if w > src_w:
        w, h = float(src_w), src_w / ar
    cx = (bx0 + bx1) / 2
    top = by0 - (by1 - by0) * margin * 0.5          # keep headroom: anchor on the head, not the middle
    x = min(max(0.0, cx - w / 2), src_w - w)
    y = min(max(0.0, top), src_h - h)
    ev = lambda v: int(v) // 2 * 2
    return ev(x), ev(y), max(2, ev(w)), max(2, ev(h))


# How far down a whole-body CHARACTER each framing reaches. Cartoon characters are mostly head and hat:
# with a person's proportions (head = 0.36) a head-and-shoulders zoom cut the face off at the beak.
KEEP_OF_BODY = {"head": 0.56, "waist": 0.68, "full": 1.0}


def match_frame(char_framing: str, subj_framing: str, img_w: int, img_h: int, top: float = 0.06):
    """Zoom the character picture so the same body part is in view as on the person. A picture generator
    often ignores "waist-up" and draws the whole body, and a whole-body character driven by a waist-up
    person barely moves. It leans on one fact only (which framing the picture has) and on the character
    being centred, because a vision model's boxes on a cartoon are not to be trusted.
    Returns even pixel (x, y, w, h) of the same shape as the picture, or None when it already matches."""
    have, want = KEEP_OF_BODY.get(char_framing, 1.0), KEEP_OF_BODY.get(subj_framing, 0.58)
    if want >= have - 0.01:
        return None
    k = min(1.0, want / have + top)                          # the share of the picture's height to keep
    ev = lambda v: int(v) // 2 * 2
    w, h = ev(img_w * k), ev(img_h * k)
    return ev((img_w - w) / 2), ev(img_h * top), w, min(h, ev(img_h * (1 - top)))


def first_json(text: str):
    """The first JSON object or array in a model's answer (they wrap it in prose and fences)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    for opener, closer in (("{", "}"), ("[", "]")):
        i = text.find(opener)
        while i != -1:
            depth, in_str, esc = 0, False, False
            for j in range(i, len(text)):
                ch = text[j]
                if in_str:
                    esc = (ch == "\\") and not esc
                    if ch == '"' and not esc:
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[i:j + 1])
                        except ValueError:
                            break
            i = text.find(opener, i + 1)
    return None


def character_prompt(character: str, scene: str, framing: str) -> str:
    return (f"{CHARACTERS.get(character, character)}, {FRAMING_WORDS.get(framing, FRAMING_WORDS['waist'])}, "
            f"facing the camera, centred, arms relaxed at the sides, mouth closed, "
            f"in {SCENES.get(scene, scene)}. 3D animated family film style, soft lighting, vertical frame, "
            f"one character only, no text.")


def _selftest():
    g = grid_lengths()
    assert [round(x, 3) for x in g] == [3.375, 6.708, 10.042], g
    assert snap_window(10.0, 15.0, 60.0) == (9.573, 6.708)          # widened, most of the pad after
    assert snap_window(0.0, 5.0, 60.0) == (0.0, 6.708)              # cannot lead before zero
    assert snap_window(55.0, 60.0, 60.0) == (53.292, 6.708)         # pushed back from the end
    assert snap_window(0.0, 5.0, 5.0) == (0.0, 3.375)               # no room to widen: narrowed
    assert snap_window(2.0, 30.0, 60.0) == (2.0, 10.042)            # over the cap: narrowed
    s, n = snap_window(1.0, 4.3, 60.0)
    assert n == 3.375 and s <= 1.0
    segs = [{"start": 0, "end": 2, "text": "a"}, {"start": 2, "end": 5, "text": "b"},
            {"start": 5, "end": 9, "text": "c"}, {"start": 9, "end": 30, "text": "long"}]
    c = candidate_windows(segs)
    assert {(w["start"], w["end"]) for w in c} == {(0, 5), (0, 9), (2, 5), (2, 9), (5, 9)}, c
    assert all(w["id"] == i for i, w in enumerate(c))
    assert len(candidate_windows([{"start": i, "end": i + 1.0, "text": "x"} for i in range(200)])) == 40
    assert len(even_windows(46.6)) >= 6 and even_windows(4.0)[0]["end"] == 4.0
    u = split_sentences([{"start": 0, "end": 10, "text": 'Aaaa. Bbbbbbbbb? "Cc!" Ddddd'}, {"start": 10, "end": 10, "text": "x"}])
    assert [x["text"] for x in u] == ["Aaaa.", "Bbbbbbbbb?", '"Cc!"', "Ddddd"], u
    assert u[0]["start"] == 0 and abs(u[-1]["end"] - 10) < 1e-9 and abs(u[0]["end"] - 2.0) < 1e-9
    sn = snap_to_pauses(u, [(2.4, 2.8), (9.0, 9.2), (30, 31)])
    assert sn[1]["start"] == 2.6 and sn[0]["end"] == 2.6 and sn[1].get("snapped")       # moved onto the pause
    assert sn[2]["start"] == u[2]["start"] and not sn[2].get("snapped")                 # none near: kept
    assert all(a["end"] == b["start"] for a, b in zip(sn, sn[1:]))
    assert render_score([one_ := {"people": 1, "framing": "waist", "face_visible": True, "box": [0.3, 0.1, 0.7, 0.9]},
                         {**one_, "face_visible": False}, one_]) == 1.0                  # hidden in one frame only
    one = {"people": 1, "framing": "waist", "face_visible": True, "box": [0.3, 0.1, 0.7, 0.9]}
    assert render_score([one, one, one]) == 1.0
    assert render_score([one, {**one, "people": 0}]) == 0.0
    assert render_score([{**one, "people": 3}]) == 0.45
    assert render_score([one, {**one, "box": [0.6, 0.1, 0.99, 0.9]}]) < 1.0
    assert render_score([{**one, "framing": "head"}]) == 0.6
    assert joint_score(0.9, 1.0, 1) == 0.0 and joint_score(0.9, 1.0, 0) == 0.9
    assert joint_score(0.9, 0.45, 0) < joint_score(0.6, 1.0, 0)     # a clean render beats a funnier mess
    assert not valid_box([0.5, 0.5, 0.4, 0.9]) and not valid_box(None) and valid_box([0, 0, 1, 1])
    assert union_box([[0.2, 0.1, 0.5, 0.8], [0.3, 0.2, 0.7, 0.9], "x"]) == [0.2, 0.1, 0.7, 0.9]
    x, y, w, h = crop_rect([0.4, 0.2, 0.6, 0.9], 1920, 1080)
    assert abs(w / h - OUT_W / OUT_H) < 0.01 and h <= 1080 and x >= 0 and x + w <= 1920 and y + h <= 1080
    assert x < 0.5 * 1920 < x + w                                   # the person is inside
    assert crop_rect(None, 1080, 1920)[2:] == (1080, 1872)          # no box: the whole width
    assert first_json('sure:\n```json\n{"a": [1, {"b": "}"}]}\n``` done') == {"a": [1, {"b": "}"}]}
    assert first_json("<think>{bad}</think> [1,2]") == [1, 2] and first_json("nope") is None
    fx, fy, fw, fh = match_frame("full", "waist", 720, 1248)
    assert (fx, fy, fw, fh) == (94, 74, 532, 922)                           # whole body drawn: zoomed to the waist
    assert abs(fw / fh - 720 / 1248) < 0.01 and fx + fw <= 720 and fy + fh <= 1248
    assert match_frame("full", "head", 720, 1248)[3] < fh
    assert match_frame("waist", "waist", 720, 1248) is None and match_frame("head", "full", 720, 1248) is None
    p = character_prompt("turkey", "thanksgiving", "waist")
    assert "turkey" in p and "waist-up" in p and "Thanksgiving" in p
    print("recast selftest ok")


# ---------------------------------------------------------------------------
# I/O: ffmpeg + the API
# ---------------------------------------------------------------------------

def run(cmd, timeout=900):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if r.returncode != 0:
        raise SystemExit(f"{os.path.basename(cmd[0])} failed: {r.stderr.decode('utf-8', 'replace')[-500:]}")
    return r


def probe(path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "json", path])
    d = json.loads(r.stdout.decode())
    try:
        seconds = float(d["format"].get("duration") or 0)
    except ValueError:
        seconds = 0.0                                   # a still picture
    return seconds, int(d["streams"][0]["width"]), int(d["streams"][0]["height"])


def count_cuts(path, start, seconds, threshold=0.35) -> int:
    r = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", path,
                        "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    return r.stderr.decode("utf-8", "replace").count("pts_time:")


def find_pauses(audio_path, noise_db=-30, min_pause=0.16):
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", audio_path, "-af",
                        f"silencedetect=noise={noise_db}dB:d={min_pause}", "-f", "null", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    err = r.stderr.decode("utf-8", "replace")
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", err)]
    return [[max(0.0, a), b] for a, b in zip(starts, ends)]


def frame_jpeg(path, t, out, width=448):
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
         "-vf", f"scale={width}:-2", "-q:v", "4", out])
    return out


def cached(work, name, fn, fresh=False):
    p = os.path.join(work, name + ".json")
    if not fresh and os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    v = fn()
    with open(p, "w", encoding="utf-8") as f:
        json.dump(v, f, indent=1)
    return v


def transcribe(l2c, audio_path):
    base, key = l2c._cfg()
    boundary = "----recast" + uuid.uuid4().hex
    parts = []
    for k, v in (("model", STT_MODEL), ("response_format", "verbose_json"),
                 ("timestamp_granularities[]", "segment")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    with open(audio_path, "rb") as f:
        audio = f.read()
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.mp3"\r\n'
                 f'Content-Type: audio/mpeg\r\n\r\n'.encode() + audio + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    for attempt in range(20):
        req = urllib.request.Request(base + "/nodes/llm/v1/audio/transcriptions", data=body, method="POST",
                                     headers={"x-api-key": key,
                                              "content-type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 503) and attempt < 19:
                print(f"[recast] speech model busy or warming ({e.code}); retrying", file=sys.stderr)
                time.sleep(15)
                continue
            raise SystemExit(f"transcription failed (HTTP {e.code}): {detail}")
    raise SystemExit("transcription did not start")


def chat_json(l2c, model, content, max_tokens=900):
    data, _ = l2c._chat({"model": model, "messages": [{"role": "user", "content": content}],
                         "temperature": 0.2, "max_tokens": max_tokens})
    try:
        text = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        text = ""
    return first_json(text), text


def rank_funny(l2c, cands):
    listing = "\n".join(f'{c["id"]}: [{c["end"] - c["start"]:.1f}s] "{c["text"][:400]}"' for c in cands)
    prompt = (
        "These are candidate moments from one home or event video, each a short stretch of what was said. "
        "A cartoon character will re-perform ONE of them with the real audio, to be sent to a family group "
        "chat. Score every candidate from 0 to 1 for how funny, quotable or delightful it would be on its own, "
        "with no context: a punchline, a rant, a confident wrong claim, a complaint, an odd one-liner. "
        "A moment that starts or ends mid-thought scores low.\n\n" + listing +
        '\n\nAnswer with JSON only: {"scores": [{"id": <id>, "funny": <0..1>, "why": "<6 words>"}], '
        '"motion": "<one sentence: how this kind of speaker is probably moving, e.g. a person talking and '
        'gesturing with both hands while seated>"}')
    j, raw = chat_json(l2c, TEXT_MODEL, prompt, max_tokens=2500)
    scores = {}
    for row in (j or {}).get("scores", []) if isinstance(j, dict) else []:
        try:
            scores[int(row["id"])] = (float(row["funny"]), str(row.get("why") or ""))
        except (KeyError, TypeError, ValueError):
            pass
    if not scores:
        print("[recast] the ranking answer could not be read; every candidate scored equal", file=sys.stderr)
    return {"scores": {str(k): v for k, v in scores.items()},
            "motion": (j or {}).get("motion") if isinstance(j, dict) else None, "raw": raw[:1500]}


LOOK_PROMPT = (
    "Look at this video frame. Answer with JSON only:\n"
    '{"people": <how many people are clearly visible>, '
    '"box": [x0, y0, x1, y1] of the MAIN person (the largest or the one talking), as fractions of the '
    "image width and height from the top-left corner, covering their head down to the lowest visible part "
    'of their body, "framing": "head" (head and shoulders only) | "waist" (waist-up) | "full" (whole body), '
    '"face_visible": true|false}')


def look(l2c, frame_path, runs_off=True):
    j, _ = chat_json(l2c, VISION_MODEL, [{"type": "text", "text": LOOK_PROMPT}, l2c._image_part(frame_path)],
                     max_tokens=200)
    if not isinstance(j, dict):
        return None
    box = j.get("box")
    if isinstance(box, list) and len(box) == 4:
        try:
            box = [float(v) for v in box]
            if max(box) > 1.5:                       # some models answer in 0..1000 or pixels
                scale = 1000.0 if max(box) <= 1000 else max(box)
                box = [v / scale for v in box]
        except (TypeError, ValueError):
            box = None
    else:
        box = None
    f = str(j.get("framing") or "").lower()
    if runs_off and f == "full" and valid_box(box) and box[3] > 0.93:
        f = "waist"      # the body runs off the bottom of the frame: the feet are not in view
    return {"people": int(j.get("people") or 0), "box": box if valid_box(box) else None,
            "framing": f if f in FRAMING_WORDS else "waist", "face_visible": bool(j.get("face_visible", True))}


EXIT_WAITING = 75        # a job is still running: run the same command again later and it picks up
EXIT_NO_MOMENT = 3       # nothing in this video can be re-performed


def run_job(l2c, pipeline, make_data, title, out, timeout, work=None, name=None):
    """Submit once, wait up to `timeout`, download. The job id is kept in the work dir, so a rerun
    waits on the same job instead of paying for a second one; still running at the deadline exits 75."""
    memo = os.path.join(work, name + ".json") if work and name else None
    job_id = None
    if memo and os.path.exists(memo):
        with open(memo, encoding="utf-8") as f:
            job_id = json.load(f).get("job_id")
    if not job_id:
        body = {"pipeline": pipeline, "inputData": make_data(), "title": title}
        # A site that submits for its customers names whom the job is for (the orchestrator trusts this
        # only from its bridge projects): that is what lets a buyer's render take rented capacity
        # instead of waiting behind the site's free work.
        wallet = os.getenv("RECAST_CUSTOMER_WALLET")
        if wallet:
            body["customerWallet"] = json.loads(wallet)
        data, _ = l2c._request("POST", "/jobs", body)
        job_id = (data.get("job") or data).get("job_id")
        if not job_id:
            raise SystemExit(f"{pipeline}: no job id came back")
        if memo:
            with open(memo, "w", encoding="utf-8") as f:
                json.dump({"job_id": job_id, "pipeline": pipeline}, f)
        print(f"[recast] {pipeline} job {job_id}", file=sys.stderr)
    deadline = time.time() + timeout
    while True:
        job, _ = l2c._request("GET", f"/jobs/{job_id}")
        job = job.get("job") or job
        status = (job.get("status") or "").lower()
        if status in ("completed", "failed", "cancelled", "canceled", "timeout"):
            break
        if time.time() >= deadline:
            print(f"[recast] {pipeline} job {job_id} is still {status or 'running'}", file=sys.stderr)
            raise SystemExit(EXIT_WAITING)
        time.sleep(10)
    if status != "completed":
        if memo:
            os.replace(memo, memo + ".failed")       # a rerun may try once more
        l2c._job_failed(job_id, job)
    outs, _ = l2c._request("GET", f"/jobs/{job_id}/outputs")
    arts = outs.get("outputs") or []
    if not arts:
        raise SystemExit(f"{pipeline} job {job_id} produced no file")
    l2c._download(job_id, key=arts[0].get("key"), out=out)
    return job_id


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--video")
    ap.add_argument("--character", default="turkey", help=f"{', '.join(CHARACTERS)} or your own description")
    ap.add_argument("--scene", default="thanksgiving", help=f"{', '.join(SCENES)} or your own description")
    ap.add_argument("--character-image", help="use this picture instead of making one")
    ap.add_argument("--character-framing", choices=list(FRAMING_WORDS),
                    help="how --character-image is framed; it is then zoomed to match the person")
    ap.add_argument("--max-seconds", type=float, default=6.8)
    ap.add_argument("--look-top", type=int, default=4, help="how many of the funniest candidates to look at")
    ap.add_argument("--work", help="work dir (default: <video>.recast)")
    ap.add_argument("--plan-only", action="store_true", help="stop before anything is rendered")
    ap.add_argument("--rank", type=int, default=1, help="render the Nth best moment instead of the best")
    ap.add_argument("--cast-only", action="store_true", help="make and fit the character, then stop before the video")
    ap.add_argument("--timeout", type=int, default=4 * 3600)
    ap.add_argument("-o", "--out", default="recast.mp4")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.video:
        ap.error("--video is required")
    import l2c  # the skill's client: auth, uploads, jobs, chat

    work = a.work or (os.path.splitext(a.video)[0] + ".recast")
    os.makedirs(work, exist_ok=True)
    dur, sw, sh = probe(a.video)
    print(f"[recast] source {dur:.1f}s {sw}x{sh}", file=sys.stderr)

    # 1. listen
    def _listen():
        mp3 = os.path.join(work, "audio.mp3")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", a.video, "-vn", "-ac", "1", "-ar", "16000",
                            "-b:a", "48k", mp3], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
        if r.returncode != 0 or not os.path.exists(mp3) or os.path.getsize(mp3) < 2000:
            return {"segments": []}                  # a video with no sound: moments are picked by look alone
        return transcribe(l2c, mp3)
    tr = cached(work, "1_transcript", _listen)
    segments = [{"start": float(s["start"]), "end": float(s["end"]), "text": s.get("text", "")}
                for s in (tr.get("segments") or []) if "start" in s and "end" in s]

    # 2. propose
    pauses = cached(work, "2_pauses", lambda: find_pauses(os.path.join(work, "audio.mp3")) if segments else [])
    segments = snap_to_pauses(split_sentences(segments), pauses)
    cands = candidate_windows(segments, max_s=a.max_seconds) if segments else []
    if not cands:
        print("[recast] no timed speech; using evenly spaced windows", file=sys.stderr)
        cands = even_windows(dur, seconds=min(a.max_seconds, 6.7))
    print(f"[recast] {len(segments)} spoken segments -> {len(cands)} candidate moments", file=sys.stderr)

    # 3. rank
    ranking = cached(work, "3_ranking", lambda: rank_funny(l2c, cands))
    for c in cands:
        f, why = ranking["scores"].get(str(c["id"]), (0.5, ""))
        c["funny"], c["why"] = float(f), why
    order = sorted(cands, key=lambda c: -c["funny"])[:max(1, a.look_top)]

    # 4. look
    def _look_all():
        out = {}
        for c in order:
            start, seconds = snap_window(c["start"], c["end"], dur, a.max_seconds)
            looks = []
            for k, frac in enumerate((0.1, 0.3, 0.5, 0.7, 0.9)):
                fp = frame_jpeg(a.video, start + seconds * frac, os.path.join(work, f"look_{c['id']}_{k}.jpg"))
                looks.append(look(l2c, fp))
            out[str(c["id"])] = {"start": start, "seconds": seconds, "looks": looks,
                                 "cuts": count_cuts(a.video, start + 0.2, max(0.5, seconds - 0.4))}
        return out
    seen = cached(work, "4_looks", _look_all)

    # 5. choose
    table = []
    for c in order:
        s = seen[str(c["id"])]
        r = render_score(s["looks"])
        table.append({"id": c["id"], "start": s["start"], "seconds": s["seconds"], "funny": c["funny"],
                      "render": r, "cuts": s["cuts"], "joint": joint_score(c["funny"], r, s["cuts"]),
                      "why": c["why"], "text": c["text"][:160]})
    table.sort(key=lambda t: -t["joint"])
    for t in table:
        print(f"[recast]   joint {t['joint']:.2f} = funny {t['funny']:.2f} x render {t['render']:.2f} "
              f"cuts {t['cuts']}  {t['start']:.1f}s+{t['seconds']:.1f}s  {t['text'][:70]!r}", file=sys.stderr)
    best = table[min(max(0, a.rank - 1), len(table) - 1)]
    if best["joint"] <= 0:
        print("no moment in this video can be re-performed: it needs one person in view, "
              "without a camera cut, for at least three seconds", file=sys.stderr)
        raise SystemExit(EXIT_NO_MOMENT)
    looks = [l for l in seen[str(best["id"])]["looks"] if l]
    framing = max((l["framing"] for l in looks), key=[l["framing"] for l in looks].count)
    box = union_box([l["box"] for l in looks])

    # 6. crop
    x, y, w, h = crop_rect(box, sw, sh)
    drive = os.path.join(work, "drive.mp4")
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{best['start']:.3f}", "-t", f"{best['seconds']:.3f}",
         "-i", a.video, "-vf", f"crop={w}:{h}:{x}:{y},scale={OUT_W}:{OUT_H}:flags=lanczos,fps={FPS}",
         "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", drive])
    plan = {"moment": best, "framing": framing, "box": box, "crop": [x, y, w, h],
            "motion": ranking.get("motion"), "candidates": table,
            "character_prompt": character_prompt(a.character, a.scene, framing)}
    with open(os.path.join(work, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=1)
    print(f"[recast] chose {best['start']:.2f}s + {best['seconds']:.2f}s, framing {framing}, crop {w}x{h}+{x}+{y}",
          file=sys.stderr)
    if a.plan_only:
        print(json.dumps(plan, indent=1))
        return

    # 7. cast
    char_png = a.character_image or os.path.join(work, "character.png")
    if not os.path.exists(char_png):
        run_job(l2c, "image_generate", lambda: {"prompt": plan["character_prompt"], "width": 720, "height": 1248},
                "recast character", char_png, min(a.timeout, 1800), work, "6_character_job")

    # 7b. fit: put the character in its frame the way the person is in theirs
    drawn = a.character_framing            # a picture supplied with its framing needs no look
    if not a.character_image:
        drawn = (cached(work, "5_character_look", lambda: look(
            l2c, frame_jpeg(char_png, 0, os.path.join(work, "character_look.jpg")), runs_off=False) or {})
        ).get("framing", "full")
    if drawn:
        iw, ih = probe(char_png)[1:]
        fit = match_frame(drawn, framing, iw, ih)
        if fit:
            fx, fy, fw, fh = fit
            fitted = os.path.join(work, "character_fit.png")
            run(["ffmpeg", "-v", "error", "-y", "-i", char_png, "-vf",
                 f"crop={fw}:{fh}:{fx}:{fy},scale=720:1248:flags=lanczos", "-frames:v", "1", fitted])
            print(f"[recast] character drawn {drawn}, person is {framing}: recropped {fw}x{fh}+{fx}+{fy}",
                  file=sys.stderr)
            char_png = fitted

    if a.cast_only:
        print(json.dumps({"character": char_png}))
        return

    # 8. render
    data = lambda: {
        "input_image": l2c._upload_source(char_png), "input_video": l2c._upload_source(drive),
        "seconds": best["seconds"], "start": 0, "aspect": "9:16",
        "prompt": ("Character appearance description: " + CHARACTERS.get(a.character, a.character) +
                   ", the same character as the reference picture.\nBackground description: " +
                   SCENES.get(a.scene, a.scene) + ", steady camera."),
        "motion_prompt": plan["motion"] or "A person talking and gesturing with their hands."}
    job_id = run_job(l2c, "video_motion", data, "recast", a.out, a.timeout, work, "7_render_job")
    print(json.dumps({"job_id": job_id, "file": a.out, "moment": best}))


if __name__ == "__main__":
    main()
