"""Authorized-site resolution: the cases the real Alsense site operation must handle, at every layer.

Resolution happens in the backend against `SiteDirectory.permitted_sites(user)`; replacing the synthetic adapter
with the real operation must keep every test here passing.
"""

from dataclasses import replace

import pytest

from alsense_chat.adapters import synthetic
from alsense_chat.adapters.alsense import AlsenseSiteDirectory
from alsense_chat.adapters.contracts import Site
from alsense_chat.adapters.errors import IntegrationError
from alsense_chat.adapters.synthetic.services import SyntheticSiteDirectory
from alsense_chat.domains.sites import resolve
from alsense_chat.store import Store
from alsense_chat.tools import Context


def sites(*names: str) -> list[Site]:
    return [Site(id=f"s{index}", name=name, tenant_id="t") for index, name in enumerate(names, 1)]


@pytest.mark.parametrize(
    ("name", "names", "match", "found", "exact"),
    [
        ("Store North", ["Store North", "Store South"], "one", ["Store North"], True),
        ("store north", ["Store North", "Store South"], "one", ["Store North"], True),
        ("the Demo East Hall site", ["Demo East Hall", "Demo East Depot"], "one", ["Demo East Hall"], True),
        ("Demo East Hall Annex", ["Demo East Hall", "Demo East Hall Annex"], "one", ["Demo East Hall Annex"], True),
        ("Demo North Annex", ["Demo North"], "one", ["Demo North"], False),
        ("Copenhagen Airport", ["Copenhagen"], "one", ["Copenhagen"], False),
        ("Demo North and Demo South", ["Demo North"], "one", ["Demo North"], False),
        ("North", ["Store North", "Store South"], "one", ["Store North"], False),
        ("demo nrth", ["Demo North"], "one", ["Demo North"], False),
        ("Store North", ["Demo North"], "one", ["Demo North"], False),
        ("Demo East", ["Demo East Hall", "Demo East Depot"], "several", ["Demo East Hall", "Demo East Depot"], False),
        ("Demo South", ["Demo North"], "none", [], False),
        ("Harbour View", ["Demo North", "Demo East Hall"], "none", [], False),
        (None, ["Demo North"], "all", ["Demo North"], True),
        ("all sites", ["Demo North", "Demo South"], "all", ["Demo North", "Demo South"], False),
        ("my site", ["Demo North"], "all", ["Demo North"], True),
    ],
)
def test_resolve_matches_only_against_the_given_sites(name, names, match, found, exact):
    resolution = resolve(name, sites(*names))
    assert (resolution.match, [site.name for site in resolution.sites], resolution.exact) == (match, found, exact)
    assert [site.name for site in resolution.alternatives] == (names if match == "none" else [])


def context(tmp_path, persona="alice", *faults, services=None) -> Context:
    source = services or synthetic.build()
    if faults:
        source = synthetic.with_faults(source, frozenset(faults))
    user = source.identity.users_me(persona)
    store = Store(str(tmp_path / "sites.sqlite3"))
    _, session_id = store.create_session(user, 3600)
    return Context(source, store, session_id, user)


def test_one_matching_site_is_verified_for_this_turn(tmp_path):
    ctx = context(tmp_path)
    output = ctx.execute("get_permitted_sites", {"name": "Demo North"})
    assert output["data"]["match"] == "one" and output["data"]["exact"] is True
    assert [site["id"] for site in output["data"]["sites"]] == ["site-a"]
    assert ctx.verified_sites == {"site-a"}


def test_several_similar_sites_are_offered_and_none_is_verified(tmp_path):
    ctx = context(tmp_path, "carol")
    output = ctx.execute("get_permitted_sites", {"name": "Demo East"})
    assert output["data"]["match"] == "several"
    assert [site["name"] for site in output["data"]["sites"]] == ["Demo East Hall", "Demo East Depot"]
    assert ctx.verified_sites == set()


def test_no_matching_site_offers_only_permitted_alternatives(tmp_path):
    ctx = context(tmp_path)
    output = ctx.execute("get_permitted_sites", {"name": "Harbour View"})
    assert output["data"] == {"match": "none", "sites": [], "alternatives": [{"id": "site-a", "name": "Demo North"}]}
    assert ctx.verified_sites == set()


def test_a_site_without_access_looks_exactly_like_a_missing_site(tmp_path):
    missing = context(tmp_path).execute("get_permitted_sites", {"name": "Harbour View"})
    elsewhere = context(tmp_path).execute("get_permitted_sites", {"name": "Demo South"})
    assert missing["data"] == elsewhere["data"]
    assert "site-b" not in str(elsewhere) and "Demo South" not in str(elsewhere)


def test_site_service_unavailable_is_an_explicit_failure(tmp_path):
    ctx = context(tmp_path, "alice", "sites_unavailable")
    output = ctx.execute("get_permitted_sites", {"name": "Demo North"})
    assert (output["category"], output["service"]) == ("unavailable", "sites")
    assert output["error"] == "The site directory is temporarily unavailable. Try again in a moment."
    assert ctx.verified_sites == set() and ctx.evidence == []


def test_sites_the_directory_returns_beyond_the_session_permissions_are_dropped(tmp_path):
    class LeakyDirectory(SyntheticSiteDirectory):
        def permitted_sites(self, user):
            return [*super().permitted_sites(user), Site(id="site-b", name="Demo South", tenant_id="customer-b")]

    ctx = context(tmp_path, services=replace(synthetic.build(), sites=LeakyDirectory()))
    output = ctx.execute("get_permitted_sites", {})
    assert [site["id"] for site in output["data"]["sites"]] == ["site-a"]
    assert ctx.execute("get_permitted_sites", {"name": "Demo South"})["data"]["match"] == "none"


def test_the_model_cannot_use_a_site_id_it_did_not_resolve_this_turn(tmp_path):
    ctx = context(tmp_path)
    draft = {"site_id": "site-a", "report_type": "temperature-log", "format": "pdf"}
    draft |= {"start": "2026-01-01", "end": "2026-01-07"}
    assert ctx.execute("prepare_report", draft, caller="model")["category"] == "unverified_site"
    ctx.execute("get_permitted_sites", {"name": "Store North"}, caller="model")
    assert ctx.verified_sites == set(), "a near match must be confirmed by the user first"
    assert ctx.execute("prepare_report", draft, caller="model")["category"] == "unverified_site"
    ctx.execute("get_permitted_sites", {"name": "Demo North"}, caller="model")
    assert "error" not in ctx.execute("prepare_report", draft, caller="model")
    assert (
        ctx.execute("prepare_report", {**draft, "site_id": "site-b"}, caller="model")["category"] == "unverified_site"
    )


def test_a_permitted_name_inside_a_longer_name_is_never_verified(tmp_path):
    ctx = context(tmp_path)
    output = ctx.execute("get_permitted_sites", {"name": "Demo North Annex"}, caller="model")
    assert (output["data"]["match"], output["data"]["exact"]) == ("one", False)
    assert ctx.verified_sites == set()


def test_the_only_site_counts_as_resolved_when_listing_all(tmp_path):
    ctx = context(tmp_path)
    ctx.execute("get_permitted_sites", {}, caller="model")
    assert ctx.verified_sites == {"site-a"}
    carol = context(tmp_path, "carol")
    carol.execute("get_permitted_sites", {}, caller="model")
    assert carol.verified_sites == set()


def test_the_real_site_operation_is_an_explicit_placeholder(tmp_path):
    user = synthetic.build().identity.users_me("alice")
    with pytest.raises(IntegrationError) as error:
        AlsenseSiteDirectory().permitted_sites(user)
    assert error.value.service == "sites"
