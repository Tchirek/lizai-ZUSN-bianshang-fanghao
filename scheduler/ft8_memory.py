"""What the station remembers of the stations it has worked, for the FT8 chat.

Two layers, as agent memory is commonly kept:
  episodes  each completed QSO with a station: when, both grids, the reports, every frame both
            ways, and the free-text chat that followed; written as it happens, never rewritten
  notes     a few lasting facts about the other operator (name, place, rig, what was said or
            promised), distilled by the model from a chat once it is over; each distillation
            gets the old notes with the new chat and returns the new set, so facts are merged,
            updated or dropped rather than piled up
Recall is by callsign, which is an exact key here, so there is no search index: whenever a
station is on the air again its notes and latest episodes go into the prompt.

Kept in ~/influ/ft8/memory.json; rebuilt from auto.jsonl when that file is missing.
"""
import json
import os
import threading
import time

SHOWN = 5                     # latest episodes recalled in full; older ones only counted
CHAT_GAP_S = 600              # chat this long after an episode's end starts a chat-only one


class Memory:
    def __init__(self, path, log_path=None):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path) as f:
                self.stations = json.load(f)["stations"]
        except (OSError, ValueError, KeyError):
            self.stations = {}
            if log_path:
                self.rebuild(log_path)

    def rebuild(self, log_path):
        """Episodes from the transmit/receive log, for QSOs made before memory was kept."""
        try:
            with open(log_path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("event") == "qso":
                        self.record_qso(r, save=False)
                    elif r.get("event") in ("llm_receive", "llm_transmit") and r.get("peer"):
                        self.record_chat(r["peer"], r["event"] == "llm_transmit", r.get("message", ""),
                                         r.get("t", 0), save=False)
        except OSError:
            return
        self.save()

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dict(stations=self.stations), f, ensure_ascii=False)
            os.replace(tmp, self.path)

    def station(self, call):
        return self.stations.setdefault(call, dict(episodes=[], notes=[], noted_upto=0))

    def record_qso(self, qso, save=True):
        with self.lock:
            self.station(qso["call"])["episodes"].append(dict(
                start=qso["start"], end=qso["end"], grid=qso.get("grid", ""),
                rst_sent=qso.get("rst_sent", ""), rst_rcvd=qso.get("rst_rcvd", ""),
                frames=[[f["slot"], f["tx"], f["message"]] for f in qso.get("log", [])], chat=[]))
        if save:
            self.save()

    def record_chat(self, call, tx, text, t, save=True):
        with self.lock:
            episodes = self.station(call)["episodes"]
            if not episodes or t - max([episodes[-1]["end"]] + [c[2] for c in episodes[-1]["chat"]]) > CHAT_GAP_S:
                episodes.append(dict(start=t, end=t, grid="", rst_sent="", rst_rcvd="", frames=[], chat=[]))
            episodes[-1]["chat"].append([tx, text, t])
        if save:
            self.save()

    def worked_before(self, call, before):
        return any(e["end"] < before for e in self.stations.get(call, {}).get("episodes", []))

    def recall(self, call, before):
        """What to remind the model of when `call` is on the air at `before`: the notes, the
        latest episodes before it, and how to use them; "" for a station never worked."""
        st = self.stations.get(call)
        earlier = [e for e in (st or {}).get("episodes", []) if e["end"] < before]
        if not earlier and not (st or {}).get("notes"):
            return ""
        lines = [f"Earlier contacts with {call}: {len(earlier)}."]
        for e in earlier[-SHOWN:]:
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(e["start"]))
            parts = [when]
            if e["grid"]:
                parts.append(f"their grid {e['grid']}")
            if e["rst_sent"] or e["rst_rcvd"]:
                parts.append(f"reports {e['rst_sent'] or '?'} sent, {e['rst_rcvd'] or '?'} received")
            if e["chat"]:
                parts.append("chat: " + " / ".join(f"{'we' if tx else 'they'}: {text}" for tx, text, _ in e["chat"]))
            lines.append("- " + ", ".join(parts))
        if st.get("notes"):
            lines.append("Notes about them: " + "; ".join(st["notes"]))
        lines.append("If they mention an earlier contact, show that you remember it.")
        return "\n".join(lines)

    def unnoted(self, call):
        """The latest episode of `call` with chat not yet distilled into notes, else None."""
        st = self.stations.get(call)
        if not st or not st["episodes"]:
            return None
        e = st["episodes"][-1]
        last = max([c[2] for c in e["chat"]], default=0)
        return e if e["chat"] and last > st["noted_upto"] else None

    def set_notes(self, call, notes, upto):
        with self.lock:
            st = self.station(call)
            st["notes"], st["noted_upto"] = notes, upto
        self.save()
