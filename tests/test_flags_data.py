"""Tests for utils/flags_data.py — the /giveaway guessflag answer matcher."""
from utils.flags_data import FLAGS, answers_for, find_by_code, flag_url, normalize, random_flag


class TestNormalize:
    def test_case_and_punctuation(self):
        assert normalize("NETHERLANDS!") == "netherlands"
        assert normalize("The Netherlands") == "netherlands"
        assert normalize("sOUTH  kOREA") == "south korea"

    def test_accents_are_stripped(self):
        assert normalize("Côte d'Ivoire") == "cote divoire"

    def test_ampersand_becomes_and(self):
        assert normalize("antigua & barbuda") == "antigua and barbuda"

    def test_guess_prefixes_are_removed(self):
        # people type the answer like "it's france" or "the flag is japan"
        assert normalize("it's france") == "france"
        assert normalize("the flag is Japan") == "japan"

    def test_empty_is_safe(self):
        assert normalize(None) == ""
        assert normalize("") == ""


class TestDataset:
    def test_flags_are_well_formed(self):
        codes = [f[0] for f in FLAGS]
        assert len(codes) == len(set(codes)), "country codes must be unique"
        assert len(FLAGS) > 100, "the game needs a decent pool of flags"
        for code, name, aliases in FLAGS:
            assert len(code) == 2 and code.islower()
            assert isinstance(name, str) and name
            assert isinstance(aliases, list)

    def test_answers_for_includes_name_and_aliases(self):
        entry = find_by_code("ag")  # Antigua and Barbuda
        answers = answers_for(entry)
        assert "antigua and barbuda" in answers
        assert "antigua" in answers

    def test_find_by_code(self):
        assert find_by_code("nl") is not None
        assert find_by_code("xx") is None

    def test_random_flag_is_from_the_pool(self):
        assert random_flag() in FLAGS

    def test_flag_url_shape(self):
        assert flag_url("nl") == "https://flagcdn.com/w640/nl.png"
