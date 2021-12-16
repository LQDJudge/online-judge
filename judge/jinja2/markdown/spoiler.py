import re
import mistune


class SpoilerInlineGrammar(mistune.InlineGrammar):
    spoiler = re.compile(r'^\|\|(.+?)\s+([\s\S]+?)\s*\|\|')


class SpoilerInlineLexer(mistune.InlineLexer):
    grammar_class = SpoilerInlineGrammar

    def __init__(self, *args, **kwargs):
        self.default_rules.insert(0, 'spoiler')
        super(SpoilerInlineLexer, self).__init__(*args, **kwargs)
        
    def output_spoiler(self, m):
        return self.renderer.spoiler(m.group(1), m.group(2))


class SpoilerRenderer(mistune.Renderer):
    def spoiler(self, summary, text):
        return '''<details>
            <summary style="color: brown">
                <span class="spoiler-summary">%s</span>
            </summary>
            <div class="spoiler-text">%s</div>
        </details>''' % (summary, text)