import os

from judge.views.test_formatter import test_formatter as tf
from judge.views.test_formatter import tf_pattern as pattern


class TestSuite:
    def __init__(
        self,
        file_id: str,
        pattern_pair: pattern.PatternPair,
        test_id_list: list,
        extra_files: list,
    ):
        self.file_id = file_id
        self.pattern_pair = pattern_pair
        self.test_id_list = test_id_list
        self.extra_files = extra_files

    @classmethod
    def get_test_suite(cls, file_name: str, inp_format: str, out_format: str):
        pattern_pair = pattern.PatternPair.from_string_pair(inp_format, out_format)
        names = tf.get_names_in_archive(file_name)
        test_id_list, extra_files = pattern_pair.matches(
            names, returns="test_id_with_extra_files"
        )
        return cls(file_name, pattern_pair, test_id_list, extra_files)

    def get_name_list(self, add_extra_info=False):
        important_files = []

        for index, t in enumerate(self.test_id_list):
            inp_name = self.pattern_pair.x.get_name(t, index=index, use_index=True)
            out_name = self.pattern_pair.y.get_name(t, index=index, use_index=True)
            important_files.extend([inp_name, out_name])

        result = []

        for name in important_files:
            if add_extra_info:
                result.append({"value": name, "is_extra_file": False})
            else:
                result.append(name)

        for name in self.extra_files:
            if add_extra_info:
                result.append({"value": name, "is_extra_file": True})
            else:
                result.append(name)

        return result


def is_valid_file_type(file_name):
    _, ext = os.path.splitext(file_name)
    return ext in [".zip", ".ZIP"]


def preview(params):
    bif = params["bef_inp_format"]
    bof = params["bef_out_format"]
    aif = params["aft_inp_format"]
    aof = params["aft_out_format"]
    file_str = params["file_str"]

    try:
        test_suite = TestSuite.get_test_suite(file_str, bif, bof)
        bef_preview = test_suite.get_name_list(add_extra_info=True)
        try:
            test_suite.pattern_pair = pattern.PatternPair.from_string_pair(aif, aof)
            aft_preview = test_suite.get_name_list(add_extra_info=True)
            return {"bef_preview": bef_preview, "aft_preview": aft_preview}
        except:
            return {"bef_preview": bef_preview, "aft_preview": []}
    except:
        test_suite = TestSuite.get_test_suite(file_id, "*", "*")
        preview = test_suite.get_name_list(add_extra_info=True)
        return {"bef_preview": preview, "aft_preview": []}


def convert(params):
    bif = params["bef_inp_format"]
    bof = params["bef_out_format"]
    aif = params["aft_inp_format"]
    aof = params["aft_out_format"]
    file_str = params["file_str"]
    file_name = params["file_name"]
    file_path = params["file_path"]

    test_suite = TestSuite.get_test_suite(file_str, bif, bof)
    bef_preview = test_suite.get_name_list()
    test_suite.pattern_pair = pattern.PatternPair.from_string_pair(aif, aof)
    aft_preview = test_suite.get_name_list()

    result = tf.get_renamed_archive(
        file_str, file_name, file_path, bef_preview, aft_preview
    )
    return result


def prefill(params):
    file_str = params["file_str"]
    file_name = params["file_name"]

    names = tf.get_names_in_archive(file_str)
    pattern_pair = pattern.find_best_pattern_pair(names)

    return {
        "file_name": file_name,
        "inp_format": pattern_pair.x.to_string(),
        "out_format": pattern_pair.y.to_string(),
    }


def preview_file(file_str):
    """
    Get the names of the files in the archive. Only read files that end with .in or .out.
    """
    names = tf.get_names_in_archive(file_str)
    return names
