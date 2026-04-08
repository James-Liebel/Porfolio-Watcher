from src.alpha.trader_follow import resolve_token_in_universe
from src.arb.models import ArbEvent, OutcomeMarket


def test_resolve_token_matches_yes_and_no():
    m = OutcomeMarket(
        event_id="e1",
        market_id="m1",
        question="q",
        outcome_name="Yes",
        yes_token_id="111",
        no_token_id="222",
    )
    ev = ArbEvent(event_id="e1", title="Test", markets=[m])
    r_yes = resolve_token_in_universe("111", [ev])
    assert r_yes is not None
    assert r_yes[0].event_id == "e1"
    assert r_yes[1].market_id == "m1"
    assert r_yes[2] == "YES"
    r_no = resolve_token_in_universe("222", [ev])
    assert r_no is not None
    assert r_no[2] == "NO"


def test_resolve_token_unknown_returns_none():
    ev = ArbEvent(
        event_id="e1",
        title="T",
        markets=[
            OutcomeMarket(
                event_id="e1",
                market_id="m1",
                question="q",
                outcome_name="A",
                yes_token_id="a",
                no_token_id="b",
            )
        ],
    )
    assert resolve_token_in_universe("nope", [ev]) is None
