"""Gemeinsame Testbausteine."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from xbot.config import Config, Credentials
from xbot.models import ActionResult, Author, Tweet
from xbot.state import Store

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
