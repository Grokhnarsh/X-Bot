"""Gemeinsame Testbausteine."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from xbot.config import Config, Credentials, DiscordRule
from xbot.discord.client import DiscordClientError
from xbot.discord.models import DiscordAuthor, DiscordMessage
from xbot.models import ActionResult, Author, Tweet
from xbot.state import Store, utcnow

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def raw_config() -> dict:
    return yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def config(raw_config, tmp_path) -> Config:
    """Beispielkonfiguration, aber mit Pfaden im Testverzeichnis."""
    cfg = Config.parse(raw_config, credentials=Credentials())
    return replace(
        cfg,
        storage=replace(cfg.storage, database=str(tmp_path / "test.db")),
        content=replace(cfg.content, templates_file=str(ROOT / "content" / "templates.yaml")),
        logging=replace(cfg.logging, file=""),
    )


@pytest.fixture
def fast_config(config) -> Config:
    """Wie ``config``, aber ohne Mindestabstand - fuer Ablauftests."""
    return replace(
        config,
        engagement=replace(
            config.engagement,
            limits=replace(config.engagement.limits, min_seconds_between_actions=0),
            max_actions_per_cycle=10,
        ),
    )


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "state.db")
    store.connect()
    yield store
    store.close()


def make_tweet(tweet_id: str = "1", **overrides) -> Tweet:
    """Ein unauffaelliger Tweet, der alle Standardfilter besteht."""
    base = dict(
        id=tweet_id,
        text="Ein fachlich brauchbarer Beitrag ueber Teststrategien in Python-Projekten.",
        author=Author(id=f"a{tweet_id}", username=f"user{tweet_id}", followers=900),
        lang="de",
        hashtags=("#python",),
        like_count=5,
    )
    base.update(overrides)
    return Tweet(**base)


class FakeXClient:
    """Ersetzt den echten X-Client im Test und protokolliert alle Aufrufe."""

    def __init__(self, tweets=(), *, dry_run: bool = False, username: str = "meinbot", fail: str = "") -> None:
        self.tweets = list(tweets)
        self.dry_run = dry_run
        self.username = username
        self.fail = fail
        self.calls: list[tuple] = []
        self.searches: list[str] = []

    def verify(self) -> Author:
        return Author(id="self", username=self.username, followers=100)

    def search(self, query, **kwargs):
        self.searches.append(query)
        return list(self.tweets)

    def _result(self, action, target_id=None, text=None):
        if self.fail == action:
            return ActionResult(action, ok=False, target_id=target_id, error="simulierter Fehler")
        return ActionResult(
            action, ok=True, dry_run=self.dry_run, target_id=target_id, text=text, result_id="new-id"
        )

    def like(self, tweet_id):
        self.calls.append(("like", tweet_id))
        return self._result("like", tweet_id)

    def repost(self, tweet_id):
        self.calls.append(("repost", tweet_id))
        return self._result("repost", tweet_id)

    def post(self, text, *, in_reply_to=None, quote_of=None):
        action = "reply" if in_reply_to else "post"
        self.calls.append((action, in_reply_to, text))
        return self._result(action, in_reply_to, text)


@pytest.fixture
def fake_client() -> FakeXClient:
    return FakeXClient()


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def make_message(message_id: str = "100", **overrides) -> DiscordMessage:
    """Eine unauffaellige Discord-Nachricht, die alle Standardfilter besteht."""
    base = dict(
        id=message_id,
        channel_id="555000000000000000",
        content="Wie testet ihr eigentlich python Code, der auf Zeitzonen angewiesen ist?",
        author=DiscordAuthor(
            id=f"u{message_id}", username=f"nutzer{message_id}", display_name=f"Nutzer {message_id}"
        ),
        created_at=utcnow(),
    )
    base.update(overrides)
    return DiscordMessage(**base)


class FakeDiscordClient:
    """Ersetzt den echten Discord-Client im Test und protokolliert alle Aufrufe."""

    def __init__(
        self,
        messages=(),
        *,
        dry_run: bool = False,
        user_id: str = "BOT",
        fail: str = "",
        read_error: str = "",
    ) -> None:
        self.messages = list(messages)
        self.dry_run = dry_run
        self.user_id = user_id
        self.fail = fail
        self.read_error = read_error
        self.calls: list[tuple] = []
        self.fetches: list[tuple] = []

    def verify(self) -> DiscordAuthor:
        return DiscordAuthor(id=self.user_id, username="testbot", display_name="Testbot", is_bot=True)

    def fetch_messages(self, channel_id, *, limit=50, after=None):
        self.fetches.append((channel_id, limit, after))
        if self.read_error:
            raise DiscordClientError(self.read_error)
        # Wie der echte Client: aufsteigend nach Schneeflocke.
        passend = [m for m in self.messages if not after or int(m.id) > int(after)]
        return sorted(passend, key=lambda m: int(m.id))

    def _result(self, action, target_id=None, text=None):
        if self.fail == action:
            return ActionResult(action, ok=False, target_id=target_id, error="simulierter Fehler")
        return ActionResult(
            action, ok=True, dry_run=self.dry_run, target_id=target_id, text=text, result_id="new-id"
        )

    def post(self, text, channel_id, *, reply_to=None):
        action = "reply" if reply_to else "post"
        self.calls.append((action, reply_to or channel_id, text))
        return self._result(action, reply_to or channel_id, text)

    def react(self, channel_id, message_id, emoji):
        self.calls.append(("react", message_id, emoji))
        return self._result("react", message_id, emoji)

    def close(self) -> None:
        pass


@pytest.fixture
def discord_config(config) -> Config:
    """Beispielkonfiguration mit eingeschaltetem, testbarem Discord-Teil."""
    return replace(
        config,
        discord=replace(
            config.discord,
            enabled=True,
            posting=replace(
                config.discord.posting,
                channels=("111000000000000000", "222000000000000000"),
                active_hours=(0, 23),
                active_weekdays=(0, 1, 2, 3, 4, 5, 6),
            ),
            engagement=replace(
                config.discord.engagement,
                watch_channels=("555000000000000000",),
                max_actions_per_cycle=10,
                limits=replace(config.discord.engagement.limits, min_seconds_between_actions=0),
            ),
            rules=(
                DiscordRule(
                    name="Python",
                    keywords=("python",),
                    actions=("react", "reply"),
                    emoji="\U0001F440",
                    weight=2.0,
                ),
                DiscordRule(
                    name="Werkzeuge",
                    keywords=("docker", "ci"),
                    actions=("react",),
                    emoji="✅",
                ),
            ),
        ),
    )


@pytest.fixture
def fake_discord_client() -> FakeDiscordClient:
    return FakeDiscordClient()
