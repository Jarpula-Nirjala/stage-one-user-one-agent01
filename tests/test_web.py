"""The live demo runs the real agent on a synthetic world; the web helpers must never raise on odd input."""
from stage_agent.demo_world import run_scenario
from stage_agent.web import md_to_html


def test_demo_lapsed_viewer_gets_g06_page_and_message():
    r = run_scenario({"status": "lapsed", "plays": 0, "completes": 0, "opens": 8, "push": True})
    assert r["decision"]["goal"]["id"] == "G06" and {a["type"] for a in r["actions"]} >= {"home_page", "push"}


def test_demo_holdout_gets_nothing_and_garbage_input_is_clamped():
    r = run_scenario({"arm": "holdout", "signup_days": "abc", "plays": 10 ** 9, "status": "<script>"})
    assert r["actions"] == [] and r["decision"]["status"] == "HOLDOUT_NO_ACTION"


def test_demo_unsupported_claim_is_rejected():
    r = run_scenario({"model_mode": "unsupported_claim"})
    assert r["decision"]["model_claims"][0]["accepted"] is False


def test_markdown_renderer_escapes_html():
    out = md_to_html("# T <b>\n\n| a | b |\n|---|---|\n| <i>x</i> | **y** |\n")
    assert "<b>" not in out.replace("<b>y</b>", "") and "&lt;i&gt;" in out and "<th>a</th>" in out
