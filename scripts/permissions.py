"""
permissions.py — how much say you get before Toby does something.

Every action Toby can take is sorted into one of three levels, the same
whether you asked at the computer or from your phone:

  safe        Looks, doesn't touch: checking status, listing and reading
              your files, searching, opening an app or a page. Done at once.

  confirm     Changes something: writing, moving or deleting files (deleting
              always means moving to the trash), running a command, stopping
              a program, downloading, sending a message. Toby asks first, on
              the computer and on your phone, and waits for a yes.

  restricted  Could do serious damage or can't be undone: commands that
              delete recursively, use sudo, format disks or pipe the internet
              into a shell; anything outside your home folder; your SSH keys,
              passwords and keyrings. Refused, unless you've turned on
              "Allow restricted actions" on the computer itself — and even
              then it asks, with the risk spelled out.

The classifier errs toward asking. It reads commands the way a careful
person would skim them, not as a sandbox: it's here so that nothing
consequential happens without you seeing it first, not to contain a
determined attacker (who would need your paired phone anyway).
"""

import os
import re
import shlex
from pathlib import Path

SAFE, CONFIRM, RESTRICTED = "safe", "confirm", "restricted"
LEVELS = (SAFE, CONFIRM, RESTRICTED)

HOME = Path.home()

# Places inside your home folder that hold secrets. Reading them is asking
# Toby to handle credentials; writing them is restricted.
SENSITIVE_PARTS = (".ssh", ".gnupg", ".password-store", ".local/share/keyrings",
                   ".mozilla", ".config/google-chrome", ".config/chromium",
                   ".aws", ".kube", ".docker/config.json", ".netrc", ".git-credentials",
                   "linux-agent/.env", "linux-agent/remote.json", ".config/gh/hosts.yml")

TOOL_LEVELS = {
    # looking
    "system_status": SAFE, "list_windows": SAFE, "focus_window": SAFE,
    "list_programs": SAFE, "list_files": SAFE, "search_files": SAFE,
    "read_file": SAFE, "open_path": SAFE, "job_status": SAFE, "watch_job": SAFE,
    "read_screen": SAFE, "look_at_screen": SAFE, "read_emails": SAFE,
    "open_url": SAFE, "open_app": SAFE, "close_tab": SAFE, "close_active_window": SAFE,
    "show_knowledge_tree": SAFE, "show_knowledge_bubbles": SAFE,
    "add_study_note": SAFE, "enable_study_mode": SAFE, "disable_study_mode": SAFE,
    "set_school_schedule": SAFE, "disable_control": SAFE,
    # your notes: searching them, and Toby's own pages in them (Toby/Memory.md,
    # Toby/Notes/), which it only ever adds to
    "search_notes": SAFE, "remember": SAFE, "add_note": SAFE,
    # changing
    "write_file": CONFIRM, "move_file": CONFIRM, "trash_files": CONFIRM,
    "run_command": CONFIRM, "open_terminal": CONFIRM, "stop_program": CONFIRM,
    "download_file": CONFIRM, "send_discord_message": CONFIRM, "install_package": CONFIRM,
    # mouse, keyboard and pen: covered by the once-per-session control grant
    "move_mouse": CONFIRM, "click_mouse": CONFIRM, "type_text": CONFIRM, "key_press": CONFIRM,
    "click_on": CONFIRM, "drag": CONFIRM, "scroll": CONFIRM, "draw": CONFIRM, "write_by_hand": CONFIRM,
}

# Tools whose approval is the once-per-session "use your mouse and keyboard"
# grant rather than a question each time.
CONTROL_TOOLS = {"move_mouse", "click_mouse", "type_text", "key_press",
                 "click_on", "drag", "scroll", "draw", "write_by_hand"}


class Decision:
    """What the classifier concluded about one action."""

    def __init__(self, level, title, details=(), reason=""):
        self.level = level
        self.title = title
        self.details = list(details)
        self.reason = reason

    def as_dict(self):
        return {"level": self.level, "title": self.title, "details": self.details,
                "reason": self.reason}

    def __repr__(self):
        return f"Decision({self.level!r}, {self.title!r}, reason={self.reason!r})"


def expand(path, cwd=None):
    """A user-supplied path, resolved: ~ expanded, relative to cwd or home."""
    text = str(path or "").strip()
    if not text:
        return Path(cwd or HOME)
    p = Path(os.path.expanduser(text))
    if not p.is_absolute():
        p = Path(cwd or HOME) / p
    return Path(os.path.normpath(p))


def pretty(path):
    """A path as you'd write it: ~/Downloads rather than /home/you/Downloads."""
    p = Path(path)
    try:
        return "~/" + str(p.relative_to(HOME)) if p != HOME else "~"
    except ValueError:
        return str(p)


def inside_home(path):
    try:
        Path(path).resolve().relative_to(HOME.resolve())
        return True
    except (ValueError, OSError):
        return False


def is_sensitive(path):
    rel = pretty(Path(path).resolve()) if inside_home(path) else str(path)
    rel = rel[2:] if rel.startswith("~/") else rel
    return any(rel == part or rel.startswith(part + "/") for part in SENSITIVE_PARTS) or \
        Path(path).name in (".env", "id_rsa", "id_ed25519")


# -- commands ---------------------------------------------------------------------

_RESTRICTED_COMMANDS = [
    (r"(^|[;&|]\s*|\s)sudo\s", "runs as administrator (sudo)"),
    (r"(^|[;&|]\s*)\s*su(\s|$)", "switches user (su)"),
    (r"(^|[;&|]\s*)\s*doas\s", "runs as administrator (doas)"),
    (r"\bpkexec\b", "runs as administrator (pkexec)"),
    (r"\brm\s+(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b", "deletes folders recursively"),
    (r"\brm\s+(.*\s)?(/|~|\$HOME)/?(\s|$)", "deletes your home or the whole system"),
    (r"\bmkfs(\.\w+)?\b", "formats a disk"),
    (r"\bdd\s+.*\bof=", "writes raw data to a device or file"),
    (r"\b(wipefs|shred|fdisk|parted|sgdisk|cryptsetup)\b", "changes disks or partitions"),
    (r">\s*/dev/(sd|nvme|mmcblk|hd)", "writes directly to a disk"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|fi|da|k)?sh\b", "runs a script straight from the internet"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "a fork bomb"),
    (r"\b(shutdown|poweroff|reboot|halt)\b", "shuts down or restarts the computer"),
    (r"\bsystemctl\s+(poweroff|reboot|halt|suspend|hibernate)\b", "shuts down or suspends the computer"),
    (r"\bchmod\s+(-[a-zA-Z]*R|--recursive)", "changes permissions recursively"),
    (r"\bchown\b", "changes file ownership"),
    (r"\bchmod\s+[0-7]*777\b", "makes files writable by everyone"),
    (r"\bgit\s+push\b.*(--force|-f\b)", "force-pushes, which can destroy history"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f)", "throws away uncommitted work"),
    (r"\b(passwd|chpasswd|usermod|useradd|userdel|visudo)\b", "changes accounts or passwords"),
    (r"\bcrontab\s+-r\b", "deletes scheduled jobs"),
    (r"\b(pacman|yay|paru|apt|apt-get|dnf|zypper)\s+(-R|remove|purge|-S|install)", "installs or removes system packages"),
    (r"\bsystemctl\s+(disable|mask|stop)\b", "stops or disables system services"),
    (r"\b(iptables|nft|ufw)\b", "changes the firewall"),
    (r"\bssh-keygen\b|\bgpg\s+--(delete|gen)", "changes your keys"),
    (r"\btailscale\s+(down|logout|up)\b", "changes how Toby is reached"),
    (r">\s*(/etc|/usr|/boot|/var|/opt)/", "writes to system files"),
]

_CHANGES_COMMANDS = re.compile(
    r"\b(rm|mv|cp|mkdir|rmdir|touch|ln|chmod|tee|sed\s+-i|truncate|git\s+(commit|push|checkout|merge|rebase|stash|pull)|"
    r"npm\s+(install|i|ci|publish)|pip\s+install|cargo\s+(install|publish)|kill|pkill|killall)\b|(?<![0-9&])>(?!&)")


def classify_command(command, cwd=None):
    """How risky a shell command looks. Never SAFE: running anything asks."""
    text = str(command or "").strip()
    if not text:
        return Decision(RESTRICTED, "Run an empty command", reason="there's nothing to run")
    for pattern, why in _RESTRICTED_COMMANDS:
        if re.search(pattern, text):
            return Decision(RESTRICTED, f"Run: {text[:80]}", [f"In {pretty(expand(cwd))}"] if cwd else [],
                            reason=f"it {why}")
    changes = bool(_CHANGES_COMMANDS.search(text))
    outside = []
    for token in _path_tokens(text):
        target = expand(token, cwd)
        if is_sensitive(target):
            return Decision(RESTRICTED, f"Run: {text[:80]}", reason=f"it touches {pretty(target)}, which holds secrets")
        if token.startswith(("/", "~")) and not inside_home(target) and not token.startswith(("/tmp", "/dev/null", "/proc")):
            if changes:
                return Decision(RESTRICTED, f"Run: {text[:80]}",
                                reason=f"it changes {token}, outside your home folder")
            outside.append(token)
    details = [f"In {pretty(expand(cwd))}"] if cwd else []
    if changes:
        details.append("This can change files or programs.")
    if outside:
        details.append("It reads " + ", ".join(outside[:3]) + ", outside your home folder.")
    return Decision(CONFIRM, f"Run: {text[:80]}", details, reason="running a command")


def _path_tokens(text):
    try:
        parts = shlex.split(text, posix=True)
    except ValueError:
        parts = text.split()
    return [p for p in parts if p.startswith(("/", "~")) or "/" in p and not p.startswith(("-", "http"))]


# -- actions ------------------------------------------------------------------------

def classify(action, settings=None):
    """A Decision for one action as the model wrote it."""
    tool = action.get("tool", "")
    level = TOOL_LEVELS.get(tool, CONFIRM)

    if tool == "run_command":
        return classify_command(action.get("command"), action.get("cwd"))

    if tool == "open_terminal":
        cmd = str(action.get("command", "")).strip()
        where = pretty(expand(action.get("cwd")))
        if cmd:
            inner = classify_command(cmd, action.get("cwd"))
            if inner.level == RESTRICTED:
                return inner
            return Decision(CONFIRM, f"Open a terminal and run: {cmd[:70]}", [f"In {where}"], "running a command")
        return Decision(SAFE, f"Open a terminal in {where}")

    paths = []
    if tool in ("read_file", "list_files", "search_files", "open_path", "write_file"):
        paths = [expand(action.get("path"))]
    elif tool == "move_file":
        paths = [expand(action.get("from")), expand(action.get("to"))]
    elif tool == "trash_files":
        raw = action.get("paths") or ([action["path"]] if action.get("path") else [])
        paths = [expand(p) for p in raw]
    elif tool == "download_file":
        paths = [expand(action.get("to") or "~/Downloads")]

    for p in paths:
        if not inside_home(p):
            if tool in ("read_file", "list_files", "search_files", "open_path") and not is_sensitive(p):
                # looking at system files (a log, /etc/os-release) is fine
                continue
            return Decision(RESTRICTED, _title(tool, action, paths),
                            reason=f"{pretty(p)} is outside your home folder")
        if is_sensitive(p):
            if tool in ("read_file", "list_files", "search_files", "open_path"):
                return Decision(CONFIRM, _title(tool, action, paths),
                                [f"{pretty(p)} holds passwords or keys."], "reading secrets")
            return Decision(RESTRICTED, _title(tool, action, paths),
                            reason=f"{pretty(p)} holds passwords or keys")

    if tool == "trash_files":
        if not paths:
            return Decision(CONFIRM, "Delete nothing", reason="no files given")
        folders = sorted({pretty(p.parent) for p in paths})
        title = (f"Delete {len(paths)} file{'s' if len(paths) != 1 else ''} from "
                 f"{folders[0] if len(folders) == 1 else 'several folders'}")
        details = [pretty(p) for p in paths[:8]] + ([f"…and {len(paths) - 8} more"] if len(paths) > 8 else [])
        details.append("They go to the trash, so you can restore them.")
        if any(p == HOME or p.parent == HOME and p.name in ("Documents", "Desktop", "Pictures", "Projects")
               for p in paths):
            return Decision(RESTRICTED, title, details, reason="it would delete a whole top-level folder")
        return Decision(CONFIRM, title, details, "deleting files")

    return Decision(level, _title(tool, action, paths), _details(tool, action),
                    reason="" if level == SAFE else "it changes something")


def _title(tool, action, paths):
    first = pretty(paths[0]) if paths else ""
    titles = {
        "read_file": f"Read {first}", "list_files": f"Look in {first}",
        "search_files": f"Search {first} for \"{str(action.get('query', ''))[:40]}\"",
        "open_path": f"Open {first}", "write_file": f"{'Add to' if action.get('append') else 'Write'} {first}",
        "move_file": f"Move {first} to {pretty(paths[1]) if len(paths) > 1 else ''}",
        "download_file": f"Download {str(action.get('url', ''))[:60]} to {first}",
        "stop_program": f"Stop {str(action.get('match', 'a program'))[:40]}",
        "send_discord_message": "Send a Discord message",
        "install_package": f"Install {str(action.get('package', ''))[:40]}",
    }
    return titles.get(tool, tool.replace("_", " ").capitalize())


def _details(tool, action):
    if tool == "write_file":
        content = str(action.get("content", ""))
        lines = content.count("\n") + (1 if content else 0)
        return [f"{lines} line{'s' if lines != 1 else ''}, {len(content)} characters."]
    if tool == "send_discord_message":
        return [str(action.get("content", ""))[:200]]
    if tool == "stop_program":
        return ["Unsaved work in it may be lost."]
    return []


def needs_approval(decision):
    return decision.level != SAFE


def allowed_at_all(decision, settings):
    """Restricted actions are refused unless you've allowed them on the computer."""
    if decision.level != RESTRICTED:
        return True
    return bool((settings or {}).get("allow_restricted_actions", False))
