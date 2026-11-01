"""Employee wiki: Diataxis sections, app and dashboard cards, live role portals, owners and review dates."""

from __future__ import annotations

import pytest

from mcp_server.store import CachedStore
from mcp_server.wiki_store import Wiki, WikiError
from tests.test_wiki_relations import Mem


def page(title, kind="page", body="Enough words to not be an empty page here.", **fm):
    lines = [f'title: "{title}"', f'type: "{kind}"', 'scope: "work"', 'source_ids: ["x"]']
    for k, v in fm.items():
        lines.append(f"{k}: {v}" if k == "audience" else f'{k}: "{v}"')
    return "---\n" + "\n".join(lines) + f"\n---\n\n# {title}\n\n{body}\n"


@pytest.fixture
def wiki():
    w = Wiki(CachedStore(Mem()), today=lambda: "2026-10-28")
    w.write("roles/accounting", page("Accounting", "role", owner="people/cfo", review_by="2027-01-31"), "seed")
    w.write("roles/office-manager", page("Office manager", "role", owner="people/pat-example", review_by="2027-01-31"), "seed")
    w.write("apps/widgets", page("WIDGETS", "app", owner="people/pat-example", review_by="2027-01-31", url="https://widgets.example.test",
                                audience='["all"]', body="Widget orders for every customer."), "seed")
    w.write("dashboards/ar-aging", page("A/R aging", "dashboard", owner="people/cfo", review_by="2027-01-31",
                                        url="https://erp.example.com/app/center/card.nl?sc=-29",
                                        system_role="Accounting", audience='["accounting"]'), "seed")
    w.write("how-to/post-a-vendor-bill", page("Post a vendor bill", "how-to", owner="people/cfo", review_by="2026-09-30",
                                              audience='["accounting"]'), "seed")
    w.write("tutorials/first-week", page("Your first week", "tutorial", audience='["all"]'), "seed")
    return w


def test_new_sections_are_valid_paths(wiki):
    for p in ("tutorials/x", "how-to/x", "explanation/x", "apps/x", "dashboards/y", "roles/z"):
        assert wiki._page(p)[0] == p


def test_portal_lists_pages_for_the_role_live(wiki):
    acc = wiki.read("roles/accounting")
    assert "## Everything for this role (live: 4 pages)" in acc
    assert acc.index("### Apps") < acc.index("### Dashboards") < acc.index("### Tutorials") < acc.index("### How-to")
    assert "[[dashboards/ar-aging|A/R aging]] · [open](https://erp.example.com" in acc
    assert "[[how-to/post-a-vendor-bill|Post a vendor bill]]" in acc
    om = wiki.read("roles/office-manager")
    assert "live: 2 pages" in om and "ar-aging" not in om                      # audience, not everyone
    assert "Everything for this role" not in wiki.store.read("roles/accounting.md")   # computed, never stored


def test_dashboard_cards_hold_a_link_and_a_system_role_never_data(wiki):
    with pytest.raises(WikiError, match="system_role"):
        wiki.write("dashboards/x", page("X", "dashboard", url="https://erp.example.com/x"), "x")
    with pytest.raises(WikiError, match="https"):
        wiki.write("apps/x", page("X", "app", url="http://plain.example.test"), "x")
    with pytest.raises(WikiError, match="review_by"):
        wiki.write("how-to/x", page("X", "how-to", review_by="next quarter"), "x")
    from mcp_server import employee
    assert employee.DATE.fullmatch("2027-01-31") and not employee.DATE.fullmatch("2027-01-31\n")   # no `$` newline gap
    with pytest.raises(WikiError, match="audience"):
        wiki.write("how-to/x", page("X", "how-to", audience='"accounting"'), "x")


def test_lint_owner_review_and_audience(wiki):
    wiki.write("explanation/why-two-silos", page("Why two silos", "explanation", owner="people/pat-example", review_by="2027-01-31",
                                                 audience='["sales"]'), "x")
    kinds = {(i["kind"], i["page"]) for i in wiki.lint()["examples"] if "page" in i}
    assert ("review_overdue", "how-to/post-a-vendor-bill.md") in kinds
    assert ("missing_owner", "tutorials/first-week.md") in kinds and ("missing_review_by", "tutorials/first-week.md") in kinds
    assert ("unknown_audience", "explanation/why-two-silos.md") in kinds
    assert ("orphan", "dashboards/ar-aging.md") not in kinds                   # reached through its portal


def test_start_here_points_staff_at_their_portal(wiki):
    start = wiki.read("reference/start-here")
    assert "## For staff" in start and "[[roles/accounting|Accounting]]" in start
    assert "- Dashboards: 1 pages" in start and "- Tutorials (learn): 1 pages" in start
