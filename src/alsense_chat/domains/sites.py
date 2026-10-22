"""Authorized-site resolution: from a friendly site name to one of the signed-in user's permitted sites.

1. Retrieve the sites the user may access (`SiteDirectory.permitted_sites`; the real operation goes in
   `adapters/alsense`).
2. Match the friendly name against those sites only, so a site without access is never found or revealed.
3. One match: use its stable site id. Several: ask the user to choose. None: say the site wasn't found or
   isn't available, and offer the sites the user can choose instead.

The model never supplies a site id it didn't get from this lookup, for a single confirmed site, in the same turn:
`Context.execute` refuses it. Only a lookup that resolves to exactly one site marks that site as verified.
"""

import difflib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from ..adapters.contracts import Site
from .base import Arguments, Handler

if TYPE_CHECKING:
    from ..tools import Context

# Words that describe a place rather than name it.
GENERIC = {"the", "a", "an", "our", "my", "your", "site", "sites", "store", "stores", "shop", "location", "branch"}
LIST_ALL = {"", "*", "all", "all sites", "every site", "everything"}
Match = Literal["one", "several", "none", "all"]


@dataclass(frozen=True)
class Resolution:
    match: Match
    sites: list[Site]
    exact: bool = False
    alternatives: list[Site] = field(default_factory=list)


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s'’-]", " ", text.casefold()).split())


def tokens(text: str) -> list[str]:
    return normalize(text).split()


def similar(left: str, right: str) -> bool:
    if left == right or left.rstrip("s") == right.rstrip("s"):
        return True
    return len(left) >= 4 and len(right) >= 4 and difflib.SequenceMatcher(None, left, right).ratio() >= 0.8


def contains(text: str, phrase: str) -> bool:
    return bool(phrase) and re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", text) is not None


def other_words(query: str, name: str) -> list[str]:
    """The words of `query` besides the site name, ignoring words that only describe a place."""
    rest = re.sub(rf"(?<![\w-]){re.escape(name)}(?![\w-])", " ", query, count=1)
    return [word for word in rest.split() if word not in GENERIC]


def resolve(name: str | None, sites: list[Site]) -> Resolution:
    """Match a friendly name to one of `sites`. Pure function: no service calls."""
    query = normalize(name or "")
    if query in LIST_ALL:
        return Resolution("all", list(sites), exact=len(sites) == 1)
    same = [site for site in sites if site.id.casefold() == query or normalize(site.name) == query]
    if same:
        return Resolution("one", same, exact=True) if len(same) == 1 else Resolution("several", same)
    named = [site for site in sites if contains(query, normalize(site.name))]
    # "Demo East Hall Annex" also contains "Demo East Hall": keep only the longest names that were mentioned.
    named = [
        site
        for site in named
        if not any(other is not site and contains(normalize(other.name), normalize(site.name)) for other in named)
    ]
    if len(named) == 1:
        # "Demo North Annex" contains "Demo North" but may be another place: confirm it with the user first.
        return Resolution("one", named, exact=not other_words(query, normalize(named[0].name)))
    if named:
        return Resolution("several", named)
    words = [word for word in query.split() if word not in GENERIC]
    if not words:
        return Resolution("all", list(sites), exact=len(sites) == 1)
    scored: list[tuple[float, Site]] = []
    for site in sites:
        name_tokens = tokens(site.name)
        # Every word the user gave must belong to this site's name: "Demo South" never matches "Demo North".
        if not all(any(similar(word, token) for token in name_tokens) for word in words):
            continue
        covered = sum(1 for token in name_tokens if any(similar(token, word) for word in words))
        scored.append((covered / len(name_tokens), site))
    if not scored:
        return Resolution("none", [], alternatives=list(sites))
    best = max(score for score, _ in scored)
    top = [site for score, site in scored if score == best]
    return Resolution("one", top, exact=False) if len(top) == 1 else Resolution("several", top)


def site_view(ctx: "Context", site: Site) -> dict:
    alarms = ctx.services.sites.active_alarms(ctx.user, site.id)
    return {"id": site.id, "name": site.name, "alarms": [alarm.model_dump() for alarm in alarms]}


class PermittedSitesQuery(Arguments):
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Friendly site name the user mentioned. Omit it to list every site.",
    )


def get_permitted_sites(ctx: "Context", args: dict) -> dict:
    allowed = ctx.permitted_site_ids()
    # Defense in depth: never return a site outside the session's own permissions, whatever the service says.
    sites = [site for site in ctx.services.sites.permitted_sites(ctx.user) if site.id in allowed]
    resolution = resolve(args.get("name"), sites)
    single = resolution.match == "one" and resolution.exact
    if single or (resolution.match == "all" and len(resolution.sites) == 1):
        ctx.verified_sites.add(resolution.sites[0].id)
    result: dict = {"match": resolution.match, "sites": [site_view(ctx, site) for site in resolution.sites]}
    if resolution.match == "one":
        result["exact"] = resolution.exact
    if resolution.match == "none":
        result["alternatives"] = [{"id": site.id, "name": site.name} for site in resolution.alternatives]
    return result


HANDLERS = {
    "get_permitted_sites": Handler(
        schema=PermittedSitesQuery,
        run=get_permitted_sites,
        summarize=lambda result: f"match:{result['match']}",
        label="Your sites",
        audit=lambda args: {"name": "given" if args.get("name") else "omitted"},
    ),
}
