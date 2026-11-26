"""Tests for utils/audit_diff.py — the dashboard audit log change renderer.

Pure functions (no Flask, Discord or DB imports), so they are verified here.
"""
from utils.audit_diff import cap, diff_collection, diff_lines, flatten, label_of, page_label


class TestFlatten:
    def test_nested_paths(self):
        assert flatten({"welcome": {"channel_id": 5}}) == {"welcome.channel_id": 5}

    def test_ignores_bookkeeping_keys(self):
        assert flatten({"_id": "x", "updated_at": 1, "at": 2, "real": 3}) == {"real": 3}

    def test_list_of_dicts_keyed_by_name(self):
        value = {"ladders": [{"name": "Mod", "days": 7}, {"name": "Admin", "days": 30}]}
        out = flatten(value)
        assert out == {"ladders[Mod].name": "Mod", "ladders[Mod].days": 7,
                       "ladders[Admin].name": "Admin", "ladders[Admin].days": 30}

    def test_plain_list_stays_a_leaf(self):
        assert flatten({"roles": [1, 2]}) == {"roles": [1, 2]}


class TestDiffLines:
    def test_changed(self):
        assert diff_lines({"x": 1}, {"x": 2}) == ["x: 1 -> 2"]

    def test_set(self):
        assert diff_lines({}, {"x": 1}) == ["x: set to 1"]

    def test_set_ignores_default_zero(self):
        # a default appearing for the first time isn't a change
        assert diff_lines({}, {"x": 0}) == []
        assert diff_lines({}, {"x": False}) == []

    def test_cleared(self):
        assert diff_lines({"x": 1}, {}) == ["x: cleared (was 1)"]

    def test_bools_read_as_on_off(self):
        assert diff_lines({"welcome": {"enabled": False}}, {"welcome": {"enabled": True}}) == \
            ["welcome.enabled: off -> on"]

    def test_secrets_are_hidden(self):
        lines = diff_lines({"password": "a"}, {"password": "b"})
        assert lines == ["password: changed (hidden)"]
        lines = diff_lines({}, {"webhook_url": "https://hooks.example/abc"})
        assert lines == ["webhook_url: changed (hidden)"]

    def test_snowflakes_are_resolved_to_names(self):
        names = {"1234567890123456789": "#general"}
        lines = diff_lines({"channel_id": 1}, {"channel_id": "1234567890123456789"}, names)
        assert lines == ["channel_id: 1 -> #general"]

    def test_long_values_are_trimmed(self):
        lines = diff_lines({"bio": "a"}, {"bio": "x" * 100})
        shown = lines[0].split(" -> ", 1)[1]
        assert len(shown) <= 48
        assert shown.endswith("…")


class TestCap:
    def test_short_lists_pass_through(self):
        assert cap(["a", "b"], 3) == ["a", "b"]

    def test_truncation_adds_a_count_line(self):
        out = cap([f"line{i}" for i in range(10)], 3)
        assert out[:3] == ["line0", "line1", "line2"]
        assert out[3] == "…and 7 more change(s)"


class TestLabels:
    def test_label_of_prefers_name(self):
        assert label_of({"_id": "abc", "name": "My Panel"}) == "My Panel"

    def test_label_of_falls_back_to_id(self):
        assert label_of({"_id": "abcdef1234567890"}) == "abcdef123456"

    def test_page_label(self):
        assert page_label("admin_page") == "Admin"
        assert page_label("live_ticket") == "Live Ticket"
        assert page_label("") == "Dashboard"


class TestDiffCollection:
    def test_added_removed_changed(self):
        before = {"1": {"name": "Keep", "days": 7}, "2": {"name": "Old", "days": 1}}
        after = {"1": {"name": "Keep", "days": 14}, "3": {"name": "New", "days": 3}}
        lines = diff_collection("strikes", before, after)
        assert "strikes: added 'New'" in lines
        assert "strikes: removed 'Old'" in lines
        assert "strikes[Keep].days: 7 -> 14" in lines
