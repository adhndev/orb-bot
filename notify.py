"""
notify.py - phone alerts through a Telegram bot (free).

One-time setup (about 5 minutes):
  1. In Telegram, open a chat with @BotFather, send /newbot, follow the prompts. It gives you a TOKEN.
  2. Open a chat with YOUR new bot and send it any message (a bot cannot message you first).
  3. Set the environment variable TELEGRAM_BOT_TOKEN to the token.
     Not working?  python notify.py check   -> says what is wrong with the token, without printing it.
  4. Run:  python notify.py setup     -> prints your chat id
  5. Set the environment variable TELEGRAM_CHAT_ID to that number.
  6. Run:  python notify.py test      -> a test message should arrive on your phone.

Treat the token like a password: anyone who has it controls the bot. It only ever goes in the
environment, never in config.json, a log or this chat. Alerts never contain your API keys.
Sending happens in a background thread, so a slow or failed send can never delay a trade.
"""
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("notify")
NY, IST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Kolkata")


TOKEN_SHAPE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


def clean_token(raw):
    """Environment variables often arrive with stray spaces or quote marks; remove them."""
    return (raw or "").strip().strip("\"'").strip()


class _Noop:
    configured = False

    def send(self, text, level="info"):
        return False

    def flush(self, timeout=10):
        return True


NOOP = _Noop()


class Notifier:
    def __init__(self, token, chat_id, max_per_hour=30, dedupe_seconds=120, sync=False, transport=None, clock=time.time):
        self.token, self.chat_id = token, str(chat_id)
        self.max_per_hour, self.dedupe, self.sync = max_per_hour, dedupe_seconds, sync
        self.transport, self.clock = transport or self._telegram, clock
        self.configured = bool(token and chat_id)
        self._sent, self._recent, self._dropped = [], {}, 0
        self._q = queue.Queue()
        if not sync:
            threading.Thread(target=self._worker, daemon=True).start()

    @classmethod
    def from_env(cls, cfg=None, **kw):
        n = (cfg or {}).get("notify", {})
        if not n.get("enabled", True):
            return NOOP
        token, chat = clean_token(os.environ.get("TELEGRAM_BOT_TOKEN")), clean_token(os.environ.get("TELEGRAM_CHAT_ID"))
        if not (token and chat):
            log.info("Alerts are OFF: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see notify.py).")
            return NOOP
        return cls(token, chat, max_per_hour=n.get("max_per_hour", 30), **kw)

    def _telegram(self, text):
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % self.token, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20, context=tls()) as r:
                r.read()
        except urllib.error.HTTPError:
            raise                                                  # a real answer from Telegram: retrying will not change it
        except OSError:                                            # timeout or dropped connection: one more try
            with urllib.request.urlopen(req, timeout=30, context=tls()) as r:
                r.read()

    def _worker(self):
        while True:
            msg = self._q.get()
            try:
                self._deliver(msg)
            finally:
                self._q.task_done()

    def flush(self, timeout=10):
        """Wait until queued alerts are sent. Call it before the program exits: the sender is a background
        thread, and exiting kills it, so a last alert ("bot stopped") would otherwise never leave."""
        deadline = time.time() + timeout
        while self._q.unfinished_tasks and time.time() < deadline:
            time.sleep(0.05)
        return not self._q.unfinished_tasks

    def _deliver(self, text):
        try:
            self.transport(text)
        except Exception as e:  # never log the URL: it contains the token
            log.warning("alert could not be sent (%s)", why(e, getattr(self, "token", None)))

    def send(self, text, level="info"):
        """level: info | warn | critical. Critical alerts are never dropped by the hourly cap."""
        if not self.configured:
            return False
        now = self.clock()
        if self._recent.get(text, 0) > now - self.dedupe:
            return False                                           # the same message a moment ago
        self._recent[text] = now
        self._sent = [t for t in self._sent if t > now - 3600]
        if level != "critical" and len(self._sent) >= self.max_per_hour:
            self._dropped += 1
            return False
        self._sent.append(now)
        stamp = datetime.fromtimestamp(now, IST).strftime("%H:%M IST") + " / " + datetime.fromtimestamp(now, NY).strftime("%H:%M ET")
        tag = {"critical": "CRITICAL", "warn": "WARNING"}.get(level, "")
        msg = "[ORB paper %s]%s\n%s" % (stamp, (" " + tag) if tag else "", text[:3500])
        if self._dropped and level != "critical":
            msg += "\n(%d earlier alerts were held back by the hourly limit)" % self._dropped
            self._dropped = 0
        if self.sync:
            self._deliver(msg)
        else:
            self._q.put(msg)
        return True


_TLS = []


def tls():
    """Certificate checking for Telegram with WINDOWS' own trusted list (the one your browser uses), via the
    'truststore' package. Antivirus web scanning re-signs Telegram's certificate with its own, which Windows trusts but
    Python's built-in list does not. Every certificate is still verified. Without truststore: Python's default."""
    if not _TLS:
        try:
            import ssl, truststore
            _TLS.append(truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
        except Exception:
            _TLS.append(None)
    return _TLS[0]


def why(e, secret=None):
    """A short, token-free description of a failed request, e.g. 'URLError: [SSL: CERTIFICATE_VERIFY_FAILED] ...'."""
    reason = getattr(e, "reason", None)
    text = "%s: %s" % (type(e).__name__, reason if reason is not None else e)
    secret = secret or clean_token(os.environ.get("TELEGRAM_BOT_TOKEN"))
    if secret:
        text = text.replace(secret, "<token>")
    text = re.sub(r"(telegram\.org/)bot[^/\s]*", r"\1bot<token>", text)
    return re.sub(r"\d{5,}:[A-Za-z0-9_-]{20,}", "<token>", text)[:200]


CERT_HELP = ("\nThe certificate was refused: something (usually antivirus web scanning) re-signs Telegram's certificate. "
             "Fix: run  pip install truststore  so the bot checks certificates with Windows' own list, then reopen "
             "the dashboard.")


NETWORK_HELP = ("Could not get an answer from Telegram (%s). Your token and chat id are probably fine: this looks like the "
                "network between this PC and api.telegram.org.\nTry: run the command again (it can be a one-off); open "
                "https://api.telegram.org in a browser on this PC; turn a VPN on or off; try your phone's hotspot; and "
                "check that a firewall or antivirus is not blocking Python.\nThe bot itself does not depend on Telegram: "
                "if alerts cannot be sent it logs a warning and keeps trading.")


def _api(token, method):
    with urllib.request.urlopen("https://api.telegram.org/bot%s/%s" % (token, method), timeout=15, context=tls()) as r:
        return json.loads(r.read().decode())


def diagnose(raw):
    """Plain-English findings about a token, without ever printing it."""
    out = []
    if not raw:
        return ["TELEGRAM_BOT_TOKEN is empty or not set in this window."]
    if raw != raw.strip():
        out.append("It has a space or line break at the start or end.")
    if raw.strip()[:1] in "\"'" or raw.strip()[-1:] in "\"'":
        out.append("It has quote marks stuck to it (they get removed automatically, but check how you set it).")
    t = clean_token(raw)
    if ":" not in t:
        out.append("It has no colon. A real token looks like 123456789:ABC... (digits, a colon, then letters and digits).")
    elif not TOKEN_SHAPE.match(t):
        out.append("It does not look like a bot token (length %d). Copy it again from the BotFather chat, "
                   "all of it, with no spaces." % len(t))
    if re.search(r"bot$", t, re.I) and ":" not in t:
        out.append("That looks like the bot's USERNAME, not the token.")
    return out or ["The shape looks right."]


def _call(token, method):
    try:
        return _api(token, method)
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            sys.exit("Telegram rejected the token (HTTP %d). It is wrong, incomplete, or was revoked.\n"
                     "Fix: in the BotFather chat send /token, pick your bot, copy the token again, and set it again.\n"
                     "Findings: %s" % (e.code, " ".join(diagnose(os.environ.get("TELEGRAM_BOT_TOKEN")))))
        sys.exit("Telegram returned HTTP %d." % e.code)
    except OSError as e:
        sys.exit(NETWORK_HELP % why(e) + (CERT_HELP if "CERTIFICATE_VERIFY_FAILED" in str(e) and tls() is None else ""))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    token = clean_token(os.environ.get("TELEGRAM_BOT_TOKEN"))
    if cmd not in ("check", "setup", "test"):
        sys.exit(__doc__)
    if cmd == "check":
        print("Token findings:", " ".join(diagnose(os.environ.get("TELEGRAM_BOT_TOKEN"))))
        print("Chat id set:", bool(clean_token(os.environ.get("TELEGRAM_CHAT_ID"))))
        if token:
            me = _call(token, "getMe").get("result", {})
            print("Telegram accepts the token. Your bot is @%s" % me.get("username", "?"))
        return
    if not token:
        sys.exit("Set the TELEGRAM_BOT_TOKEN environment variable first (then run: python notify.py check).")
    if cmd == "setup":
        res = _call(token, "getUpdates").get("result", [])
        chats = {}
        for u in res:
            m = u.get("message") or u.get("channel_post") or {}
            c = m.get("chat")
            if c:
                chats[c["id"]] = c.get("username") or c.get("first_name") or c.get("title") or ""
        if not chats:
            sys.exit("No messages found. Open a chat with your bot in Telegram, send it any message, then run this again.")
        for cid, name in chats.items():
            print("chat id:", cid, "(" + name + ")")
        print("\nSet TELEGRAM_CHAT_ID to the number for your own chat, then run: python notify.py test")
    else:
        n = Notifier(token, clean_token(os.environ.get("TELEGRAM_CHAT_ID")), sync=True)
        if not n.configured:
            sys.exit("Set TELEGRAM_CHAT_ID first (run: python notify.py setup).")
        try:
            n._telegram("Test message. If you can read this on your phone, alerts work.")
        except urllib.error.HTTPError as e:
            sys.exit("Telegram refused the message (HTTP %d). For 400, the chat id is wrong; for 403, open your bot "
                     "in Telegram and press Start." % e.code)
        except OSError as e:
            sys.exit(NETWORK_HELP % type(getattr(e, "reason", e)).__name__)
        print("Sent. Check Telegram.")


if __name__ == "__main__":
    main()
