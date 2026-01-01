import markdown
from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor
import xml.etree.ElementTree as etree
import re

EMOTICON_EMOJI_MAP = {
    ":D": "\U0001f603",  # Smiling Face with Open Mouth
    ":)": "\U0001f642",  # Slightly Smiling Face
    ":-)": "\U0001f642",  # Slightly Smiling Face with Nose
    ":(": "\U0001f641",  # Slightly Frowning Face
    ":-(": "\U0001f641",  # Slightly Frowning Face with Nose
    ";)": "\U0001f609",  # Winking Face
    ";-)": "\U0001f609",  # Winking Face with Nose
    ":P": "\U0001f61b",  # Face with Tongue
    ":-P": "\U0001f61b",  # Face with Tongue and Nose
    ":p": "\U0001f61b",  # Face with Tongue
    ":-p": "\U0001f61b",  # Face with Tongue and Nose
    ";P": "\U0001f61c",  # Winking Face with Tongue
    ";-P": "\U0001f61c",  # Winking Face with Tongue and Nose
    ";p": "\U0001f61c",  # Winking Face with Tongue
    ";-p": "\U0001f61c",  # Winking Face with Tongue and Nose
    ":'(": "\U0001f622",  # Crying Face
    ":o": "\U0001f62e",  # Face with Open Mouth
    ":-o": "\U0001f62e",  # Face with Open Mouth and Nose
    ":O": "\U0001f62e",  # Face with Open Mouth
    ":-O": "\U0001f62e",  # Face with Open Mouth and Nose
    ":-0": "\U0001f62e",  # Face with Open Mouth and Nose
    ">:(": "\U0001f620",  # Angry Face
    ">:-(": "\U0001f620",  # Angry Face with Nose
    ">:)": "\U0001f608",  # Smiling Face with Horns
    ">:-)": "\U0001f608",  # Smiling Face with Horns and Nose
    "XD": "\U0001f606",  # Grinning Squinting Face
    "xD": "\U0001f606",  # Grinning Squinting Face
    "B)": "\U0001f60e",  # Smiling Face with Sunglasses
    "B-)": "\U0001f60e",  # Smiling Face with Sunglasses and Nose
    "O:)": "\U0001f607",  # Smiling Face with Halo
    "O:-)": "\U0001f607",  # Smiling Face with Halo and Nose
    "0:)": "\U0001f607",  # Smiling Face with Halo
    "0:-)": "\U0001f607",  # Smiling Face with Halo and Nose
    ">:P": "\U0001f92a",  # Zany Face (sticking out tongue and winking)
    ">:-P": "\U0001f92a",  # Zany Face with Nose
    ">:p": "\U0001f92a",  # Zany Face (sticking out tongue and winking)
    ">:-p": "\U0001f92a",  # Zany Face with Nose
    ":/": "\U0001f615",  # Confused Face
    ":-/": "\U0001f615",  # Confused Face with Nose
    ":\\": "\U0001f615",  # Confused Face
    ":-\\": "\U0001f615",  # Confused Face with Nose
    "3:)": "\U0001f608",  # Smiling Face with Horns
    "3:-)": "\U0001f608",  # Smiling Face with Horns and Nose
    "<3": "\u2764\ufe0f",  # Red Heart
    "</3": "\U0001f494",  # Broken Heart
    ":*": "\U0001f618",  # Face Blowing a Kiss
    ":-*": "\U0001f618",  # Face Blowing a Kiss with Nose
    ";P": "\U0001f61c",  # Winking Face with Tongue
    ";-P": "\U0001f61c",
    ">:P": "\U0001f61d",  # Face with Stuck-Out Tongue and Tightly-Closed Eyes
    ":-/": "\U0001f615",  # Confused Face
    ":/": "\U0001f615",
    ":\\": "\U0001f615",
    ":-\\": "\U0001f615",
    ":|": "\U0001f610",  # Neutral Face
    ":-|": "\U0001f610",
    "8)": "\U0001f60e",  # Smiling Face with Sunglasses
    "8-)": "\U0001f60e",
    "O:)": "\U0001f607",  # Smiling Face with Halo
    "O:-)": "\U0001f607",
    ":3": "\U0001f60a",  # Smiling Face with Smiling Eyes
    "^.^": "\U0001f60a",
    "-_-": "\U0001f611",  # Expressionless Face
    "T_T": "\U0001f62d",  # Loudly Crying Face
    "T.T": "\U0001f62d",
    ">.<": "\U0001f623",  # Persevering Face
    "x_x": "\U0001f635",  # Dizzy Face
    "X_X": "\U0001f635",
    ":]": "\U0001f600",  # Grinning Face
    ":[": "\U0001f641",  # Slightly Frowning Face
    "=]": "\U0001f600",
    "=[": "\U0001f641",
    "D:<": "\U0001f621",  # Pouting Face
    "D:": "\U0001f629",  # Weary Face
    "D=": "\U0001f6ab",  # No Entry Sign (sometimes used to denote dismay or frustration)
    ":'D": "\U0001f602",  # Face with Tears of Joy
    "D':": "\U0001f625",  # Disappointed but Relieved Face
    "D8": "\U0001f631",  # Face Screaming in Fear
    "-.-": "\U0001f644",  # Face with Rolling Eyes
    "-_-;": "\U0001f612",  # Unamused
}


class EmoticonEmojiInlineProcessor(InlineProcessor):
    def handleMatch(self, m, data):
        emoticon = m.group(1)
        emoji = EMOTICON_EMOJI_MAP.get(emoticon, "")
        if emoji:
            el = etree.Element("span")
            el.text = markdown.util.AtomicString(emoji)
            el.set("class", "big-emoji")
            return el, m.start(0), m.end(0)
        else:
            return None, m.start(0), m.end(0)


class EmoticonExtension(Extension):
    def extendMarkdown(self, md):
        emoticon_pattern = (
            r"(?:(?<=\s)|^)"  # Lookbehind for a whitespace character or the start of the string
            r"(" + "|".join(map(re.escape, EMOTICON_EMOJI_MAP.keys())) + r")"
            r"(?=\s|$)"  # Lookahead for a whitespace character or the end of the string
        )
        emoticon_processor = EmoticonEmojiInlineProcessor(emoticon_pattern, md)
        md.inlinePatterns.register(emoticon_processor, "emoticon_to_emoji", 1)
