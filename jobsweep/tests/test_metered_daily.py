import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest

from jobsweep import metered_daily as daily
from jobsweep.config import Settings
from jobsweep.http import HTTPError
from jobsweep.models import SearchQuery
from jobsweep.sources.base import SourceContext
from jobsweep.sources.hasdata_indeed import HasDataIndeedSource
from jobsweep.sources.theirstack import TheirStackSource


class Taxonomy:
    def search_plan(self, *args, **kwargs):
        for i in range(40):
            yield SearchQuery(
                keywords=f"analyst {i}",
                extra={"pages": 5, "limit": 1000, "actor_input": {"maxResults": 99999}},
            )


def ledger():
    return {"version": 1, "reservations": [], "cursors": {}}


def test_month_of_daily_searches_stays_below_5000_and_rotates(monkeypatch):
    monkeypatch.setattr(daily, "available_units", lambda *args: (10000, daily.DAILY_QUERIES[args[-1]]))
    state = ledger()
    keywords = set()
    for n in range(31):
        plan = daily.reserve(
            state,
            Taxonomy(),
            Settings(),
            None,
            day=(date(2026, 10, 1) + timedelta(days=n)).isoformat(),
            selected=list(daily.SOURCES),
        )
        for query in plan["sources"]["hasdata_indeed"]["queries"]:
            keywords.add(query["keywords"])
            assert query["extra"] == {"pages": 1}
        assert len(plan["sources"]["theirstack"]["queries"]) == 1
        assert plan["sources"]["apify"]["queries"][0]["extra"]["max_charge_usd"] == 0.08
    totals = {s: sum(r["units"] for r in state["reservations"] if r["source"] == s) for s in daily.SOURCES}
    assert totals == {"theirstack": 186, "hasdata_indeed": 3100, "apify": 248}
    assert len(keywords) == 40


def test_rerun_never_spends_twice_even_if_first_run_failed(monkeypatch):
    monkeypatch.setattr(daily, "available_units", lambda *args: (10000, 10))
    state = ledger()
    first = daily.reserve(state, Taxonomy(), Settings(), None, day="2026-10-25", selected=["hasdata_indeed"])
    assert first["sources"]
    monkeypatch.setattr(daily, "available_units", lambda *args: pytest.fail("rerun must not call provider"))
    rerun = daily.reserve(state, Taxonomy(), Settings(), None, day="2026-10-25", selected=["hasdata_indeed"])
    assert not rerun["sources"]
    assert "already reserved" in rerun["skipped"]["hasdata_indeed"]


def test_monthly_cap_live_balance_and_rotation_across_reset(monkeypatch):
    monkeypatch.setattr(daily, "available_units", lambda *args: (15, 2))
    state = ledger()
    state["reservations"] = [{"day": "2026-10-24", "source": "theirstack", "units": 195}]
    plan = daily.reserve(state, Taxonomy(), Settings(), None, day="2026-10-25", selected=["theirstack"])
    assert not plan["sources"]
    plan = daily.reserve(state, Taxonomy(), Settings(), None, day="2026-11-01", selected=["theirstack"])
    assert len(plan["sources"]["theirstack"]["queries"]) == 1
    assert plan["sources"]["theirstack"]["units"] == 6


@pytest.mark.parametrize("total,cap", [(5000, 10), (1000, 3), (100, 0)])
def test_hasdata_checks_actual_plan_without_assuming_paid_provider(total, cap):
    def respond(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"data": {"totalCredits": total, "availableCredits": 50}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert daily.available_units(client, Settings(env={"HASDATA_API_KEY": "test"}), "hasdata_indeed") == (
            50,
            cap,
        )


def test_apify_requires_included_credit_and_existing_hard_limit():
    def respond(request):
        assert request.method == "GET"
        if request.url.path.endswith("/limits"):
            return httpx.Response(
                200, json={"data": {"limits": {"maxMonthlyUsageUsd": 5}, "current": {"monthlyUsageUsd": 4.8}}}
            )
        return httpx.Response(200, json={"data": {"plan": {"monthlyUsageCreditsUsd": 5}}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        available, cap = daily.available_units(client, Settings(env={"APIFY_TOKEN": "test"}), "apify")
        assert 9 <= available <= 10 and cap == 1


def test_apify_does_not_approve_an_overage_limit():
    def respond(request):
        if request.url.path.endswith("/limits"):
            return httpx.Response(
                200, json={"data": {"limits": {"maxMonthlyUsageUsd": 100}, "current": {"monthlyUsageUsd": 0}}}
            )
        return httpx.Response(200, json={"data": {"plan": {"monthlyUsageCreditsUsd": 5}}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        plan = daily.reserve(
            ledger(),
            Taxonomy(),
            Settings(env={"APIFY_TOKEN": "test"}),
            client,
            day="2026-10-25",
            selected=["apify"],
        )
    assert not plan["sources"]
    assert "overage" in plan["skipped"]["apify"]


def test_unknown_balance_response_fails_closed_without_leaking_body():
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"secret": "never print"}))
    ) as client:
        plan = daily.reserve(
            ledger(),
            Taxonomy(),
            Settings(env={"THEIRSTACK_API_KEY": "test"}),
            client,
            day="2026-10-25",
            selected=["theirstack"],
        )
    assert not plan["sources"]
    assert "KeyError" in plan["skipped"]["theirstack"]
    assert "never print" not in json.dumps(plan)


def test_corrupt_ledger_cannot_silently_reset_budget(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text('{"version": 0}')
    with pytest.raises(ValueError, match="refusing"):
        daily.read_ledger(path)


def test_plan_cannot_be_expanded_after_its_budget_was_reserved(monkeypatch, tmp_path):
    monkeypatch.setattr(daily, "available_units", lambda *args: (1000, 1))
    state = ledger()
    plan = daily.reserve(state, Taxonomy(), Settings(), None, day=daily.today(), selected=["theirstack"])
    plan["sources"]["theirstack"]["queries"][0]["extra"]["limit"] = 1000
    daily.write_json(tmp_path / "plan.json", plan)
    daily.write_json(tmp_path / "ledger.json", state)
    with pytest.raises(ValueError, match="changed after reservation"):
        daily.run_plan(Settings(), tmp_path / "plan.json", tmp_path / "ledger.json")


def test_consumed_local_plan_cannot_be_replayed(tmp_path):
    daily.write_json(tmp_path / "plan.json", {"day": daily.today(), "sources": {}, "skipped": {}})
    daily.write_json(tmp_path / "ledger.json", ledger())
    assert daily.run_plan(Settings(), tmp_path / "plan.json", tmp_path / "ledger.json")["total_jobs"] == 0
    with pytest.raises(ValueError, match="already consumed"):
        daily.run_plan(Settings(), tmp_path / "plan.json", tmp_path / "ledger.json")


def test_theirstack_does_not_buy_more_rows_to_replace_unusable_results():
    requests = []

    def respond(request):
        requests.append(request)
        assert json.loads(request.content)["limit"] == 10
        return httpx.Response(200, json={"data": [{}] * 10})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            source = TheirStackSource(
                SourceContext(client=client, settings=Settings(env={"THEIRSTACK_API_KEY": "test"}))
            )
            assert await source.search(SearchQuery(keywords="analyst", limit=10)) == []

    asyncio.run(run())
    assert len(requests) == 1


def test_hasdata_timeout_does_not_repeat_paid_request():
    requests = []

    def respond(request):
        requests.append(request)
        raise httpx.ReadTimeout("simulated timeout")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            source = HasDataIndeedSource(
                SourceContext(client=client, settings=Settings(env={"HASDATA_API_KEY": "test"}))
            )
            with pytest.raises(HTTPError):
                await source.search(SearchQuery(keywords="analyst"))

    asyncio.run(run())
    assert len(requests) == 1
