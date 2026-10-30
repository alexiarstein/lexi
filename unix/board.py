#!/usr/bin/env python3
"""Unix Airways flight board.

Fetches the company's live flights from Pocket Pilots, merges them into
history.json (so landed flights stick around) and writes index.html.

    ./board.py          # update files
    ./board.py --push   # update, then git commit + push
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CO_ID = "85wsv0i6"
API = f"https://pilots.nimblebit.com/api/air/co{CO_ID}"
HERE = Path(__file__).resolve().parent
HISTORY = HERE / "history.json"
TEMPLATE = HERE / "template.html"
OUT = HERE / "index.html"
KEEP_MS = 36 * 3600 * 1000  # how long landed flights stay on the board
MAX_LANDED = 30


def fetch():
    req = urllib.request.Request(API, headers={"User-Agent": "lexiworks-unix-board/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def airport(a):
    return {"code": a.get("code", "????"), "name": a.get("name", "")}


def merge(history, data):
    now = data.get("now") or int(time.time() * 1000)
    flights = {f["id"]: f for f in history.get("flights", [])}
    live = set()

    for f in data.get("flights", []):
        fid = f"{f['pid']}-{f['t0']}"
        live.add(fid)
        flights[fid] = {
            "id": fid,
            "cs": f.get("cs", "?"),
            "type": f.get("type", ""),
            "reg": f.get("reg", ""),
            "from": airport(f.get("from", {})),
            "to": airport(f.get("to", {})),
            "dep": f["t0"],
            "arr": f["t0"] + int(f.get("eta", 0) * 1000),
            "seen": now,
            "status": "air",
        }

    for fid, f in flights.items():
        if f["status"] == "air" and fid not in live:
            # Gone from the API: it landed somewhere between the last
            # sighting and now. Scheduled arrival is the best guess.
            f["status"] = "landed"
            f["landed"] = min(max(f["arr"], f["seen"]), now)

    # A pilot can only fly one plane: an older "air" leg is stale.
    latest = {}
    for f in flights.values():
        pid = f["id"].split("-")[0]
        if pid not in latest or f["dep"] > latest[pid]["dep"]:
            latest[pid] = f

    kept = [f for f in flights.values() if f["status"] == "air" and latest[f["id"].split("-")[0]] is f]
    landed = sorted(
        (f for f in flights.values() if f["status"] == "landed" and now - f["landed"] < KEEP_MS),
        key=lambda f: f["landed"], reverse=True,
    )[:MAX_LANDED]

    return {"updated": now, "flights": kept + landed}


def render(history):
    payload = json.dumps(history, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", payload)
    OUT.write_text(html)


def push():
    def git(*a):
        subprocess.run(["git", "-C", str(HERE), *a], check=True)
    git("add", "index.html", "history.json")
    if subprocess.run(["git", "-C", str(HERE), "diff", "--cached", "--quiet"]).returncode == 0:
        print("nothing changed")
        return
    git("commit", "-m", "unix: update flight board")
    git("push")


def main():
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    history = merge(history, fetch())
    HISTORY.write_text(json.dumps(history, indent=1) + "\n")
    render(history)
    air = sum(f["status"] == "air" for f in history["flights"])
    print(f"{air} in the air, {len(history['flights']) - air} landed")
    if "--push" in sys.argv:
        push()


if __name__ == "__main__":
    main()
