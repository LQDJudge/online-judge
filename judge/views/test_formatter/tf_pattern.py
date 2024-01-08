import os
import random
from judge.views.test_formatter import tf_utils as utils

SAMPLE_SIZE = 16
NUMBERED_MM = ["0", "1", "00", "01", "000", "001", "0000", "0001"]
VALID_MM = ["*"] + NUMBERED_MM

MSG_TOO_MANY_OCCURRENCES = (
    "400: Invalid pattern: Pattern cannot have more than one '{}'"
)
MSG_MM_NOT_FOUND = "400: Invalid pattern: Wildcard not found. Wildcard list: {}"


class Pattern:
    def __init__(self, ll, mm, rr):
        assert mm in VALID_MM, "Invalid wildcard"
        self.ll = ll
        self.mm = mm
        self.rr = rr

    def __repr__(self):
        return "Pattern('{}', '{}', '{}')".format(self.ll, self.mm, self.rr)

    def __eq__(self, other):
        return self.__repr__() == other.__repr__()

    def __hash__(self):
        return self.__repr__().__hash__()

    @classmethod
    def from_string(cls, text):
        for mm in ["*"] + sorted(NUMBERED_MM, key=len, reverse=True):
            if mm in text:
                if text.count(mm) > 1:
                    raise Exception(MSG_TOO_MANY_OCCURRENCES.format(mm))
                i = text.index(mm)
                return cls(text[:i], mm, text[i + len(mm) :])
        raise Exception(MSG_MM_NOT_FOUND.format(",".join(VALID_MM)))

    def to_string(self):
        return self.ll + self.mm + self.rr

    def is_valid_test_id(self, test_id):
        if self.mm == "*":
            return True
        if self.mm in NUMBERED_MM:
            return test_id.isdigit() and len(test_id) >= len(self.mm)
        raise NotImplementedError

    def matched(self, name):
        return (
            name.startswith(self.ll)
            and name.endswith(self.rr)
            and len(name) >= len(self.ll) + len(self.rr)
            and self.is_valid_test_id(self.get_test_id(name))
        )

    def get_test_id(self, name):
        return name[len(self.ll) : len(name) - len(self.rr)]

    def get_test_id_from_index(self, index):
        assert self.mm in NUMBERED_MM, "Wildcard is not a number"
        return str(int(self.mm) + index).zfill(len(self.mm))

    def get_name(self, test_id, index=None, use_index=False):
        if use_index and self.mm in NUMBERED_MM:
            return self.ll + self.get_test_id_from_index(index) + self.rr
        return self.ll + test_id + self.rr

    def matches(self, names, returns):
        if returns == "test_id":
            result = [n for n in names]
            result = [n for n in result if self.matched(n)]
            result = [self.get_test_id(n) for n in result]
            return result
        else:
            raise NotImplementedError


class PatternPair:
    def __init__(self, x: Pattern, y: Pattern):
        assert x.mm == y.mm, "Input wildcard and output wildcard must be equal"
        self.x = x
        self.y = y

    def __repr__(self):
        return "PatternPair({}, {})".format(self.x, self.y)

    def __eq__(self, other):
        return self.__repr__() == other.__repr__()

    def __hash__(self):
        return self.__repr__().__hash__()

    @classmethod
    def from_string_pair(cls, inp_format, out_format):
        return cls(Pattern.from_string(inp_format), Pattern.from_string(out_format))

    def matches(self, names, returns):
        x_test_ids = self.x.matches(names, returns="test_id")
        y_test_ids = self.y.matches(names, returns="test_id")

        test_ids = set(x_test_ids) & set(y_test_ids)
        test_ids = list(sorted(test_ids, key=utils.natural_sorting_key))

        if returns == "fast_count":
            if self.x.mm == "*":
                return len(test_ids)
            elif self.x.mm in NUMBERED_MM:
                count_valid = 0
                for t in test_ids:
                    if t == self.x.get_test_id_from_index(count_valid):
                        count_valid += 1

                return count_valid

        extra_files = list(names)
        valid_test_ids = []
        for t in test_ids:
            if self.x.mm in NUMBERED_MM:
                if t != self.x.get_test_id_from_index(len(valid_test_ids)):
                    continue

            inp_name = self.x.get_name(t)
            out_name = self.y.get_name(t)

            if inp_name == out_name:
                continue
            if inp_name not in extra_files:
                continue
            if out_name not in extra_files:
                continue

            valid_test_ids.append(t)
            extra_files.remove(inp_name)
            extra_files.remove(out_name)

        if returns == "count":
            return len(valid_test_ids)
        elif returns == "test_id":
            return valid_test_ids
        elif returns == "test_id_with_extra_files":
            return valid_test_ids, extra_files
        else:
            raise NotImplementedError

    def score(self, names):
        def ls(s):
            return len(s) - s.count("0")

        def zs(s):
            return -s.count("0")

        def vs(s):
            return sum(
                s.lower().count(c) * w
                for c, w in [("a", -1), ("e", -1), ("i", +1), ("o", -1), ("u", -1)]
            )

        count_score = self.matches(names, returns="fast_count")

        len_score = ls(self.x.ll + self.x.rr + self.y.ll + self.y.rr)
        zero_score = zs(self.x.ll + self.x.rr + self.y.ll + self.y.rr)

        assert self.x.mm in ["*"] + NUMBERED_MM
        specific_score = 0 if self.x.mm == "*" else len(self.x.mm)

        vowel_score = vs(self.x.ll + self.x.rr) - vs(self.y.ll + self.y.rr)

        return count_score, specific_score, len_score, zero_score, vowel_score

    def is_string_safe(self):
        try:
            x = Pattern.from_string(self.x.to_string())
            y = Pattern.from_string(self.y.to_string())
            return self == PatternPair(x, y)
        except:
            return False


def maximal(a, key):
    max_score = max(map(key, a))
    result = [x for x in a if key(x) == max_score]
    if len(result) == 1:
        return result[0]
    else:
        print(result)
        raise Exception("More than one maximum values")


def get_all_star_pattern_pairs(names):
    sample = random.sample(names, min(len(names), SAMPLE_SIZE))

    star_pattern_pairs = []

    all_prefixes = [n[:i] for n in sample for i in range(len(n) + 1)]
    all_prefixes = list(sorted(set(all_prefixes)))
    all_suffixes = [n[i:] for n in sample for i in range(len(n) + 1)]
    all_suffixes = list(sorted(set(all_suffixes)))

    for prefix in all_prefixes:
        matched_names = [n for n in names if n.startswith(prefix)]
        if len(matched_names) == 2:
            mn0, mn1 = matched_names
            for i in range(len(prefix) + 1):
                x = Pattern(prefix[:i], "*", mn0[len(prefix) :])
                y = Pattern(prefix[:i], "*", mn1[len(prefix) :])
                star_pattern_pairs.append(PatternPair(x, y))

    for suffix in all_suffixes:
        matched_names = [n for n in names if n.endswith(suffix)]
        if len(matched_names) == 2:
            mn0, mn1 = matched_names
            for i in range(len(suffix) + 1):
                x = Pattern(mn0[: len(mn0) - len(suffix)], "*", suffix[i:])
                y = Pattern(mn1[: len(mn1) - len(suffix)], "*", suffix[i:])
                star_pattern_pairs.append(PatternPair(x, y))

    star_pattern_pairs = list(set(star_pattern_pairs))
    return star_pattern_pairs


def get_variant_pattern_pairs(pp):
    return [
        PatternPair(Pattern(pp.x.ll, mm, pp.x.rr), Pattern(pp.y.ll, mm, pp.y.rr))
        for mm in VALID_MM
    ] + [
        PatternPair(Pattern(pp.y.ll, mm, pp.y.rr), Pattern(pp.x.ll, mm, pp.x.rr))
        for mm in VALID_MM
    ]


def find_best_pattern_pair(names):
    star_pattern_pairs = get_all_star_pattern_pairs(names)
    star_pattern_pairs = [
        pp for pp in star_pattern_pairs if pp.matches(names, returns="fast_count") >= 2
    ]
    # for pp in star_pattern_pairs:
    #     print(pp, pp.is_string_safe(), pp.score(names))

    if len(star_pattern_pairs) == 0:
        return PatternPair(Pattern("", "*", ""), Pattern("", "*", ""))
    best_star_pattern_pair = maximal(star_pattern_pairs, key=lambda pp: pp.score(names))

    pattern_pairs = get_variant_pattern_pairs(best_star_pattern_pair)
    # for pp in pattern_pairs:
    #     print(pp, pp.is_string_safe(), pp.score(names))
    pattern_pairs = [pp for pp in pattern_pairs if pp.is_string_safe()]
    best_pattern_pair = maximal(pattern_pairs, key=lambda pp: pp.score(names))

    return best_pattern_pair


def list_dir_recursively(folder):
    old_cwd = os.getcwd()
    os.chdir(folder)
    result = []
    for root, _, filenames in os.walk("."):
        for filename in filenames:
            result.append(os.path.join(root, filename))
    os.chdir(old_cwd)
    return result


def test_with_dir(folder):
    names = list_dir_recursively(folder)
    print(folder, find_best_pattern_pair(names))
