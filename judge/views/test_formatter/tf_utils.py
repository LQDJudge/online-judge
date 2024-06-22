def get_char_kind(char):
    return 1 if char.isdigit() else 2 if char.isalpha() else 3


def natural_sorting_key(name):
    result = []
    last_kind = -1
    for char in name:
        curr_kind = get_char_kind(char)
        if curr_kind != last_kind:
            result.append("")
        result[-1] += char
        last_kind = curr_kind

    return [x.zfill(16) if x.isdigit() else x for x in result]
