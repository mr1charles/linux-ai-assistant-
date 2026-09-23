"""
approvals.py — one place for "Toby wants to do this. Allow?"

When an action needs your yes (see permissions.py), the task runner asks
here and waits. The question shows up on the computer, in Toby's panel, and
on every paired phone at the same moment; whichever answers first decides,
and the other one's prompt disappears. If nobody answers within ten minutes
it counts as no, so a task left waiting while you're away never goes ahead
on its own.

Some questions may only be answered at the computer itself — pairing a new
phone is one — and those are marked local_only and never sent to phones.
"""

import secrets
import threading
import time

PENDING, ALLOWED, DENIED, EXPIRED, CANCELLED = "pending", "allowed", "denied", "expired", "cancelled"
DEFAULT_TIMEOUT_S = 600


class ApprovalCenter:
    def __init__(self, on_change=None, clock=time.time, timeout=DEFAULT_TIMEOUT_S):
        self.on_change = on_change
        self.clock = clock
        self.timeout = timeout
        self._cond = threading.Condition()
        self._items = {}
        self._order = []

    def request(self, title, level="confirm", details=(), reason="", tool="", origin="",
                local_only=False, kind="action"):
        """Put a question up. Returns its id; wait() for the answer."""
        now = self.clock()
        item = {"id": secrets.token_hex(6), "kind": kind, "level": level, "title": str(title)[:160],
                "details": [str(d)[:200] for d in details][:12], "reason": str(reason)[:160],
                "tool": tool, "origin": origin, "local_only": bool(local_only),
                "created": now, "expires": now + self.timeout, "status": PENDING, "answered_by": None}
        with self._cond:
            self._items[item["id"]] = item
            self._order.append(item["id"])
            self._trim()
            self._cond.notify_all()
        self._changed()
        return item["id"]

    def wait(self, approval_id, cancel_check=None, poll=0.25):
        """Block until answered, expired or cancelled. True only for a yes."""
        with self._cond:
            item = self._items.get(approval_id)
            if item is None:
                return False
            while item["status"] == PENDING:
                if self.clock() >= item["expires"]:
                    item["status"] = EXPIRED
                    break
                if cancel_check is not None and cancel_check():
                    item["status"] = CANCELLED
                    break
                self._cond.wait(timeout=poll)
            allowed = item["status"] == ALLOWED
        self._changed()
        return allowed

    def ask(self, *args, cancel_check=None, **kwargs):
        """request() then wait(): True if you said yes."""
        return self.wait(self.request(*args, **kwargs), cancel_check=cancel_check)

    def answer(self, approval_id, allow, by="computer", from_phone=False):
        with self._cond:
            item = self._items.get(approval_id)
            if item is None or item["status"] != PENDING:
                return False
            if from_phone and item["local_only"]:
                return False
            item["status"] = ALLOWED if allow else DENIED
            item["answered_by"] = by
            self._cond.notify_all()
        self._changed()
        return True

    def cancel_all(self):
        with self._cond:
            for item in self._items.values():
                if item["status"] == PENDING:
                    item["status"] = CANCELLED
            self._cond.notify_all()
        self._changed()

    def pending(self, for_phone=False):
        """Open questions, oldest first, as plain dicts."""
        with self._cond:
            now = self.clock()
            items = [dict(self._items[i]) for i in self._order
                     if i in self._items and self._items[i]["status"] == PENDING and now < self._items[i]["expires"]]
        if for_phone:
            items = [i for i in items if not i["local_only"]]
        return items

    def get(self, approval_id):
        with self._cond:
            item = self._items.get(approval_id)
            return dict(item) if item else None

    def _trim(self):
        # keep the last few answered ones for a moment, so a phone that
        # asks "what happened to that?" can find out
        done = [i for i in self._order if self._items[i]["status"] != PENDING]
        for i in done[:-20]:
            self._order.remove(i)
            del self._items[i]

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception as e:
                print("APPROVAL CHANGE ERROR:", e, flush=True)
