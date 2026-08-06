"""Your Mastodon timeline and account, for a badge to show on a notifications page.

Everything comes from one instance's REST API under `https://<domain>/api/v1`, with a user
access token: Preferences, Development, a new application, and the token it shows you. Four
requests a refresh, against a limit of 300 in five minutes, so the default of two minutes is
nowhere near it.

    verify_credentials      the counters, and who this is
    timelines/home          the newest post in the feed
    notifications           the newest of any kind, and the newest mention
    accounts/:id/statuses   your own newest post, and how it has done

The messages travel as the shape a `notify` page draws - who it is from, what it says, how
long ago, and a word about why - which is the same shape a headline or an RSS entry has, so
none of this is Mastodon-specific by the time it reaches the badge.
"""

import datetime
import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from statsbadge.sources.base import Source

# How often the instance is asked, unless the setting says otherwise. A timeline is not a
# sensor: two minutes is well inside what anyone would notice and a fortieth of the limit.
DEFAULT_EVERY = 120.0
MIN_EVERY = 30.0
MAX_EVERY = 3600.0
RETRY_AFTER = 60.0
FETCH_POLL = 1.0

# How many notifications to read to find the newest of each sort. One request rather than one
# per type, and twenty is far enough back to hold a mention on a quiet account.
NOTIFICATION_SCAN = 20

# A post as long as the protocol allows is five hundred characters, and the page draws two or
# three lines of it. Cut here rather than on the badge: the rest is neither drawn nor worth
# sending every time the set changes.
TEXT_MAX = 160

# The counters, kept once an hour so a graph of them says something. Mastodon reports no
# history of its own, so this is the only place one can come from - which means a ring starts
# empty and fills as the host runs.
HISTORY_EVERY = 3600.0
HISTORY_POINTS = 48
HISTORY_MS = int(HISTORY_EVERY * 1000)
COUNTS = "counts"
WHO = "who"

# What each sort of notification is, in words, for one with no post attached to say instead.
NOTIFICATIONS = {
    "follow": "followed you",
    "follow_request": "asked to follow you",
    "favourite": "favourited your post",
    "reblog": "boosted your post",
    "mention": "mentioned you",
    "poll": "a poll ended",
    "status": "posted",
    "update": "edited a post",
    "admin.sign_up": "signed up",
}

FIELDS = {
    "home": {"label": "Latest in your feed", "item": True},
    "mention": {"label": "Latest mention", "item": True},
    "notification": {"label": "Latest notification", "item": True},
    "mine": {"label": "Your latest post", "item": True},
    "followers": {"label": "Followers", "history": True},
    "following": {"label": "Following", "history": True},
    "posts": {"label": "Posts"},
    "unread": {"label": "Unread notifications"},
    "likes": {"label": "Likes on your latest"},
    "boosts": {"label": "Boosts on your latest"},
}
GROUP = "mastodon"


class Mastodon(Source):
    name = "mastodon"
    label = "Mastodon"

    settings = (
        {"key": "domain", "label": "Instance", "type": "text",
         "hint": "The host your account is on, like fosstodon.org - no https://"},
        {"key": "access_token", "label": "Access token", "type": "text", "secret": True,
         "hint": "Preferences, Development, New application. `read` is all it needs, and "
                 "the token is shown once the application is made"},
        {"key": "every", "label": "Ask every", "type": "number",
         "default": int(DEFAULT_EVERY),
         "hint": "Seconds. A timeline is not a sensor, and the default is a fortieth of "
                 "what the API allows"},
    )

    @classmethod
    def available(cls):
        return True

    def __init__(self, config):
        super().__init__(config)
        # What the fetcher last brought back, and the hourly counter rings. Both are replaced
        # on the fetcher's thread and read while sampling, so both go through the lock.
        self._readings = {}
        self._counts = {}
        self._counts_at = None
        self._lock = threading.Lock()
        self._next = 0.0
        self._next_history = 0.0
        self._fetcher = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._read_settings()

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        """Take up the rings the last run kept, then fetch on a thread of its own.

        Nothing in `sample` may wait on a network: every source shares the collector's
        thread and the first sample is taken while the server is still starting up.
        """
        kept = self.store.get(COUNTS) or {}
        with self._lock:
            self._counts = {name: list(points) for name, points in kept.items()
                            if isinstance(points, list)}
        if self._fetcher is None:
            self._stop.clear()
            self._fetcher = threading.Thread(target=self._fetch_loop, daemon=True,
                                             name="statsbadge-mastodon")
            self._fetcher.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._fetcher is not None:
            self._fetcher.join(timeout=2.0)
            self._fetcher = None

    def configure(self, settings):
        """Take settings while running, and ask again rather than waiting out the interval."""
        super().configure(settings)
        self._read_settings()
        self._next = 0.0
        self._wake.set()

    def _read_settings(self):
        self.domain = str(self.config.get("domain") or "").strip()
        self.domain = self.domain.replace("https://", "").replace("http://", "").strip("/")
        self.token = str(self.config.get("access_token") or "").strip()
        try:
            every = float(self.config.get("every") or DEFAULT_EVERY)
        except (TypeError, ValueError):
            every = DEFAULT_EVERY
        self.every = max(MIN_EVERY, min(MAX_EVERY, every))
        # One group, and slow: a timeline fetched every two minutes has no business in a
        # frame the badge collects every second.
        self.groups = {GROUP: {"label": "Mastodon", "slow": True, "fields": dict(FIELDS)}}
        self.provides = (GROUP,)

    # -- sampling -----------------------------------------------------------

    def sample(self, frame, dt):
        """Whatever the fetcher last brought back. Nothing here touches the network."""
        with self._lock:
            readings = dict(self._readings)
        if readings:
            frame[GROUP] = readings

    def series(self):
        """The counter rings, on the hour they are kept at.

        The collector would sample these at its own rate, and ninety seconds of a follower
        count is a flat line. An hour apart is the shape of a week.
        """
        with self._lock:
            counts = {name: list(points) for name, points in self._counts.items()}
            at = self._counts_at
        if not counts or at is None:
            return {}
        age_ms = max(0, int((time.monotonic() - at) * 1000))
        return {f"{GROUP}.{name}": {"points": points, "every_ms": HISTORY_MS,
                                    "age_ms": age_ms}
                for name, points in counts.items() if points}

    def note_fault(self, exc):
        """What the instance said, without a type name in front of it."""
        if isinstance(exc, MastodonError):
            self.faults += 1
            self.last_fault = str(exc)
            return
        super().note_fault(exc)

    # -- fetching -----------------------------------------------------------

    def _fetch_loop(self):
        while not self._stop.is_set():
            try:
                self._refresh()
            except Exception as exc:
                # The fetcher must not die, or the timeline would stand at whatever it last
                # was with nothing ever replacing it.
                self.note_fault(exc)
            self._wake.wait(FETCH_POLL)
            self._wake.clear()

    def _refresh(self):
        if not self.domain or not self.token:
            # Not a fault: an extension nobody has given an account to is unconfigured, and
            # counting that would report a broken source on every host that installed it.
            self.last_fault = "no instance and token set" if not (self.domain or self.token) \
                else ("no instance set" if not self.domain else "no access token set")
            return
        if time.monotonic() < self._next:
            return
        try:
            readings = self._fetch()
        except Exception as exc:
            self._next = time.monotonic() + RETRY_AFTER
            self.note_fault(exc)
            return
        with self._lock:
            self._readings = readings
        self._keep_counts(readings)
        self._next = time.monotonic() + self.every
        self.note_ok()

    def _fetch(self):
        me = self._get("/accounts/verify_credentials")
        readings = {
            "followers": me.get("followers_count"),
            "following": me.get("following_count"),
            "posts": me.get("statuses_count"),
        }
        # Kept so a name can be put to your own post without asking again for it.
        self.store.set(WHO, {"id": me.get("id"),
                             "name": me.get("display_name") or me.get("username")})

        home = self._get("/timelines/home?limit=1")
        readings["home"] = _post_item(home[0]) if home else None

        notes = self._get(f"/notifications?limit={NOTIFICATION_SCAN}")
        readings["notification"] = _notification_item(notes[0]) if notes else None
        mention = next((n for n in notes if n.get("type") == "mention"), None)
        readings["mention"] = _notification_item(mention) if mention else None

        unread = self._get("/notifications/unread_count")
        readings["unread"] = (unread or {}).get("count")

        mine = self._get(f"/accounts/{me['id']}/statuses"
                         "?limit=1&exclude_replies=true&exclude_reblogs=true")
        if mine:
            readings["mine"] = _post_item(mine[0])
            readings["likes"] = mine[0].get("favourites_count")
            readings["boosts"] = mine[0].get("reblogs_count")
        return readings

    def _keep_counts(self, readings):
        """Append this hour's counters to their rings, if an hour has gone by.

        On the wall clock rather than on how often the fetcher runs, so the spacing the
        badge is told about is the spacing the points are really on.
        """
        now = time.monotonic()
        if now < self._next_history:
            return
        self._next_history = now + HISTORY_EVERY
        with self._lock:
            for name in ("followers", "following"):
                value = readings.get(name)
                if value is None:
                    continue
                ring = self._counts.setdefault(name, [])
                ring.append(int(value))
                del ring[0:max(0, len(ring) - HISTORY_POINTS)]
            self._counts_at = now
            kept = {name: list(points) for name, points in self._counts.items()}
        self.store.set(COUNTS, kept)

    # -- talking to it ------------------------------------------------------

    def _get(self, path):
        request = urllib.request.Request(
            f"https://{self.domain}/api/v1{path}",
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # The status alone says nothing useful: a token missing a scope and a token that
            # has been revoked are both 401, and the body says which.
            detail = ""
            try:
                detail = (json.loads(exc.read().decode("utf-8")) or {}).get("error") or ""
            except Exception:
                detail = ""
            raise MastodonError(f"HTTP {exc.code}"
                                + (f": {detail}" if detail else "")) from exc


class MastodonError(Exception):
    """What the instance said was wrong, as one line for the config UI to show."""


# -- turning a post into a message ------------------------------------------

_TAGS = re.compile(r"<[^>]+>")
_BREAKS = re.compile(r"<br\s*/?>|</p\s*>", re.I)


def text_of(content):
    """A status body as one line of plain text.

    Mastodon sends HTML. Paragraph and line breaks become spaces rather than being dropped,
    or two sentences run together into one word; everything else goes, entities included,
    since `&amp;` on a badge is not what anybody wrote.
    """
    if not content:
        return ""
    flat = _TAGS.sub("", _BREAKS.sub(" ", content))
    return re.sub(r"\s+", " ", html.unescape(flat)).strip()[:TEXT_MAX]


def _who(account):
    return (account or {}).get("display_name") or (account or {}).get("acct") or "someone"


def _age(stamp):
    """Seconds since an ISO 8601 timestamp, or None if it cannot be read."""
    if not stamp:
        return None
    try:
        when = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((datetime.datetime.now(datetime.timezone.utc) - when).total_seconds()))


def _post_item(status):
    """One status as the four things a notifications page draws.

    A boost carries no text of its own - the content is on the post inside it - so what is
    drawn is the original, and who boosted it is the note. Getting that wrong is a page of
    blank messages, which is how the API says "this is a boost".
    """
    inner = status.get("reblog") or status
    note = None
    if status.get("reblog"):
        note = f"boosted by {_who(status.get('account'))}"
    elif status.get("in_reply_to_id"):
        note = "reply"
    return {
        "title": _who(inner.get("account")),
        "text": text_of(inner.get("content")) or (inner.get("spoiler_text") or ""),
        "age_s": _age(status.get("created_at")),
        "note": note,
    }


def _notification_item(note):
    """One notification as a message, whatever sort it is.

    Half of them carry a post and half do not: a follow is somebody and a verb. So the words
    for the type are the text where there is nothing else, and the note where there is - a
    favourite reads better as your own post with "favourited your post" beside it.
    """
    kind = note.get("type") or ""
    said = NOTIFICATIONS.get(kind, kind.replace("_", " ") or "did something")
    status = note.get("status")
    return {
        "title": _who(note.get("account")),
        "text": text_of((status or {}).get("content")) or said,
        "age_s": _age(note.get("created_at")),
        "note": said if status else None,
    }
