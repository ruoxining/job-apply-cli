from types import SimpleNamespace

import pytest

from job_apply_agent.ats.cua_harness import UnsafeComputerAction
from job_apply_agent.ats.lcu_harness import LcuBrowserSession, document_observation

TREE = """Accessibility source: at_spi
0 frame Chromium
  1 entry Address bar
  2 document web Job application
    3 heading Apply
    4 entry (settable) Email
    5 push button Continue
    6 check box (unchecked) Confirm
    7 push button Submit application
    8 entry (disabled) Secret
  9 push button Close tab
"""


def test_only_web_document_controls_and_no_final_submit():
    state = document_observation(TREE, url="https://ats.test/job", generation=2)
    assert [r["ref"] for r in state["refs"]] == ["p2:4", "p2:5", "p2:6", "p2:7"]
    assert state["refs"][0]["actions"] == ["click", "fill"]
    assert state["refs"][-1]["actions"] == []
    assert "Address bar" not in state["outline"]
    assert "Close tab" not in state["outline"]


@pytest.mark.parametrize("text", ["", "0 frame Job", "Accessibility source: at_spi\n0 frame"])
def test_missing_accessibility_is_not_ready(text):
    with pytest.raises(UnsafeComputerAction):
        document_observation(text, url="https://ats.test", generation=1)


def test_only_settable_combobox_allows_fill():
    tree = TREE.replace(
        "5 push button Continue",
        "5 combo box (settable) City\n    10 combo box (collapsed) Country",
    )
    state = document_observation(tree, url="https://ats.test", generation=1)
    assert next(r for r in state["refs"] if r["name"] == "City")["actions"] == ["click", "fill"]
    assert next(r for r in state["refs"] if r["name"] == "Country")["actions"] == ["click"]


def test_toggle_buttons_expose_click_but_never_submit():
    tree = TREE.replace("6 check box (unchecked) Confirm", "6 toggle button (unpressed) No")
    tree = tree.replace("7 push button", "7 toggle button")
    state = document_observation(tree, url="https://ats.test", generation=1)
    assert next(r for r in state["refs"] if r["name"] == "No")["actions"] == ["click"]
    assert next(r for r in state["refs"] if r["name"] == "Submit application")["actions"] == []


@pytest.mark.parametrize(
    "action",
    [
        {"action": "click", "ref": "p1:7"},
        {"action": "click", "ref": "p0:5"},
        {"action": "fill", "ref": "p1:5", "value": "bad"},
        {"action": "click", "ref": "p1:1"},
    ],
)
async def test_unsafe_actions_never_reach_bridge(action):
    page = SimpleNamespace(url="https://ats.test", context=SimpleNamespace(pages=[1]))
    browser = LcuBrowserSession(None, page, 5)
    observation = document_observation(TREE, url=page.url, generation=1)
    browser.latest = observation
    with pytest.raises(UnsafeComputerAction):
        await browser.execute(action, observation)


async def test_origin_change_stops_actions():
    page = SimpleNamespace(url="https://ats.test", context=SimpleNamespace(pages=[1]))
    browser = LcuBrowserSession(None, page, 5)
    page.url = "https://other.test"
    with pytest.raises(UnsafeComputerAction, match="origin"):
        await browser.observe()


async def test_replaced_ax_node_rebinds_unique_name_before_typing():
    page = SimpleNamespace(url="https://ats.test", context=SimpleNamespace(pages=[1]))
    browser = LcuBrowserSession(None, page, 5)
    original = document_observation(TREE, url=page.url, generation=1)
    browser.latest = original
    calls = []

    async def request(payload):
        calls.append(payload)
        return {"text": TREE.replace("4 entry", "40 entry")}

    browser._request = request
    await browser.execute({"action": "fill", "ref": "p1:4", "value": "test"}, original)
    assert calls == [
        {"op": "observe"},
        {"op": "action", "action": "fill", "index": 40, "value": "test"},
    ]


async def test_scroll_uses_fresh_document_and_bounded_distance():
    page = SimpleNamespace(url="https://ats.test", context=SimpleNamespace(pages=[1]))
    browser = LcuBrowserSession(None, page, 5)
    original = document_observation(TREE, url=page.url, generation=1)
    browser.latest = original
    calls = []

    async def request(payload):
        calls.append(payload)
        return {"text": TREE.replace("2 document web", "20 document web")}

    browser._request = request
    await browser.execute({"action": "scroll", "ref": None, "value": 1000000}, original)
    assert calls == [
        {"op": "observe"},
        {"op": "action", "action": "scroll", "index": 20, "value": 1200},
    ]


@pytest.mark.parametrize(
    "changed_tree, ref",
    [
        (TREE.replace("Email", "Password"), "p1:4"),
        (TREE.replace("5 push button Continue", "5 entry (settable) Email"), "p1:4"),
        (TREE.replace("(unchecked)", "(checked)"), "p1:6"),
    ],
)
async def test_changed_or_ambiguous_target_never_receives_action(changed_tree, ref):
    page = SimpleNamespace(url="https://ats.test", context=SimpleNamespace(pages=[1]))
    browser = LcuBrowserSession(None, page, 5)
    original = document_observation(TREE, url=page.url, generation=1)
    browser.latest = original
    calls = []

    async def request(payload):
        calls.append(payload)
        return {"text": changed_tree}

    browser._request = request
    with pytest.raises(UnsafeComputerAction, match="changed"):
        await browser.execute({"action": "click", "ref": ref}, original)
    assert calls == [{"op": "observe"}]
