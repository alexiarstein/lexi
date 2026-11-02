#!/usr/bin/env python3
"""Unix Airways flight board.

Fetches the company's live flights from Pocket Pilots, merges them into
history.json (so landed flights stick around), keeps running totals and
regional standings, and writes index.html.

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
SITE = "https://pilots.nimblebit.com"
API = f"{SITE}/api/air/co{CO_ID}"
HERE = Path(__file__).resolve().parent
HISTORY = HERE / "history.json"
TEMPLATE = HERE / "template.html"
OUT = HERE / "index.html"
KEEP_MS = 36 * 3600 * 1000  # how long landed flights stay on the board
MAX_LANDED = 30
MAX_WEEKS = 12  # final weekly standings kept in history

# Countries shown in the standings strip, in display order.
REGIONS = ["AR", "UY", "BR", "CL", "PY", "BO", "PE", "MX", "US"]
TABLE_SIZE = 25  # /api/tables only lists the top 25


def fetch(url=API):
    req = urllib.request.Request(url, headers={"User-Agent": "lexiworks-unix-board/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def try_fetch(path):
    try:
        return fetch(SITE + path)
    except Exception as e:  # extras are optional: keep the board going
        print(f"warning: {path}: {e}", file=sys.stderr)
        return None


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

    return {**history, "updated": now, "flights": kept + landed}


def tally(history, before):
    """Fold flights that are new (or just landed) since `before` into totals."""
    t = history.get("totals") or {"since": history["updated"], "flights": 0, "pilots": [],
                                  "minutes": 0, "landed": 0, "types": {}, "airports": {}}
    pilots = set(t["pilots"])
    for f in history["flights"]:
        old = before.get(f["id"])
        if old is None:
            t["flights"] += 1
            t["types"][f["type"]] = t["types"].get(f["type"], 0) + 1
            pilots.add(f["id"].split("-")[0])
        if f["status"] == "landed" and (old is None or old["status"] != "landed"):
            t["landed"] += 1
            t["minutes"] += round((f["landed"] - f["dep"]) / 60000)
            code = f["to"]["code"]
            t["airports"][code] = t["airports"].get(code, 0) + 1
    t["pilots"] = sorted(pilots)
    history["totals"] = t
    return history


def held_stats(held):
    top = sorted(held, key=lambda k: -held[k].get("v", 0))[:5]
    return {
        "n": len(held),
        "v": sum(a.get("v", 0) for a in held.values()),
        "top": [{"code": k, "v": held[k].get("v", 0)} for k in top],
    }


def standing(region, table):
    """Our place in a country's weekly table."""
    rows = table.get("list", [])
    cos = table.get("cos", {})
    name = lambda i: cos.get(i, {}).get("name", i)
    ids = [r["id"] for r in rows]
    s = {"region": region, "n": len(rows)}
    if rows:
        s["leader"] = {"name": name(rows[0]["id"]), "v": rows[0]["value"]}
    if CO_ID in ids:
        i = ids.index(CO_ID)
        s["rank"] = i + 1
        s["v"] = rows[i]["value"]
        if i:
            s["ahead"] = {"name": name(rows[i - 1]["id"]), "v": rows[i - 1]["value"]}
    else:
        # Unlisted: nothing delivered this week, or below the top-25 cutoff.
        s["v"] = 0
        if len(rows) >= TABLE_SIZE:
            s["cutoff"] = rows[-1]["value"]
    return s


def world_rank(world, regions):
    """Our place in the weekly company table (/api/world lists the top 15).

    Outside the top 15 the value is the sum of our tracked country tables:
    a slight undercount, since countries outside REGIONS are missed.
    """
    rows = world.get("table", [])
    cos = world.get("cos", {})
    name = lambda i: cos.get(i, {}).get("name", i)
    ids = [r["id"] for r in rows]
    w = {"n": len(rows), "top": [{"name": name(r["id"]), "v": r["value"], "us": r["id"] == CO_ID} for r in rows[:5]]}
    if CO_ID in ids:
        i = ids.index(CO_ID)
        w["rank"] = i + 1
        w["v"] = rows[i]["value"]
        if i:
            w["ahead"] = {"name": name(rows[i - 1]["id"]), "v": rows[i - 1]["value"]}
    else:
        w["v"] = sum(r["v"] for r in regions if r.get("rank"))
        w["approx"] = True
        if rows:
            w["cutoff"] = rows[-1]["value"]
    return w


def close_week(history, now, prev):
    """The weekly table resets: when our value drops sharply, file last week's result."""
    cur = history["stats"].get("world")
    if prev and cur and prev.get("v", 0) > 0 and cur["v"] < prev["v"] / 2:
        weeks = history.setdefault("weeks", [])
        weeks.append({"end": prev.get("t", now), "rank": prev.get("rank"), "v": prev["v"]})
        del weeks[:-MAX_WEEKS]
    if cur:
        cur["t"] = now


def extras(history, data):
    """Company stats; each part keeps its last good value if a fetch fails."""
    stats = history.get("stats", {})
    stats["live"] = len({f["pid"] for f in data.get("flights", [])})
    if "held" in data:
        stats["held"] = held_stats(data["held"])

    flying = try_fetch("/api/flying")
    if flying and "flying" in flying:
        stats["flying"] = flying["flying"]
    world = try_fetch("/api/world?v=2")
    if world:
        stats["bases"] = [b["code"] for b in world.get("bases", []) if b.get("co") == CO_ID]

    old = {r["region"]: r for r in stats.get("regions", [])}
    regions = []
    for region in REGIONS:
        table = try_fetch(f"/api/tables?region={region}")
        if table is not None:
            regions.append(standing(region, table))
        elif region in old:
            regions.append(old[region])
    stats["regions"] = regions

    prev = stats.get("world")
    if world:
        stats["world"] = world_rank(world, regions)
    history["stats"] = stats
    close_week(history, history["updated"], prev)
    return history


def render(history):
    # The page only needs the pilot count, not the ids.
    totals = history.get("totals")
    if totals:
        history = {**history, "totals": {**totals, "pilots": len(totals["pilots"])}}
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
    before = {f["id"]: dict(f) for f in history.get("flights", [])}  # merge() mutates flights
    data = fetch()
    history = extras(tally(merge(history, data), before), data)
    HISTORY.write_text(json.dumps(history, indent=1) + "\n")
    render(history)
    air = sum(f["status"] == "air" for f in history["flights"])
    print(f"{air} in the air, {len(history['flights']) - air} landed")
    if "--push" in sys.argv:
        push()


if __name__ == "__main__":
    main()
