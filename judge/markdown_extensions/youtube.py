import markdown
from markdown.inlinepatterns import InlineProcessor
from markdown.extensions import Extension
import xml.etree.ElementTree as etree

YOUTUBE_REGEX = (
    r"(https?://)?(www\.)?" "(youtube\.com/watch\?v=|youtu\.be/)" "([\w-]+)(&[\w=]*)?"
)


class YouTubeEmbedProcessor(InlineProcessor):
    def handleMatch(self, m, data):
        youtube_id = m.group(4)
        if not youtube_id:
            return None, None, None

        # Create an iframe element with the YouTube embed URL
        iframe = etree.Element("iframe")
        iframe.set("width", "100%")
        iframe.set("height", "360")
        iframe.set("src", f"https://www.youtube.com/embed/{youtube_id}")
        iframe.set("frameborder", "0")
        iframe.set("allowfullscreen", "true")
        center = etree.Element("center")
        center.append(iframe)

        # Return the iframe as the element to replace the match, along with the start and end indices
        return center, m.start(0), m.end(0)


class YouTubeExtension(Extension):
    def extendMarkdown(self, md):
        # Create the YouTube link pattern
        YOUTUBE_PATTERN = YouTubeEmbedProcessor(YOUTUBE_REGEX, md)
        # Register the pattern to apply the YouTubeEmbedProcessor
        md.inlinePatterns.register(YOUTUBE_PATTERN, "youtube", 175)
