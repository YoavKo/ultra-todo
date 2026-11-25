import markup


def html(text, resolve=None):
    return str(markup.render(text, resolve)).strip()


def test_basics():
    assert html("") == "" and html(None) == ""
    assert html("hello") == '<p dir="auto">hello</p>'
    assert "<strong>b</strong>" in html("**b**") and "<em>i</em>" in html("*i*") and "<s>x</s>" in html("~~x~~")
    assert '<h2 dir="auto">T</h2>' in html("## T")
    assert '<li dir="auto">' in html("- a\n- b") and "<ol>" in html("1. a\n2. b")
    assert "<blockquote dir=\"auto\">" in html("> q")


def test_a_newline_is_a_line_break_like_plain_comments_were():
    assert html("a\nb") == '<p dir="auto">a<br />\nb</p>'


def test_bare_urls_become_links_that_open_safely():
    out = html("see https://example.com/a?b=1 now")
    assert '<a href="https://example.com/a?b=1" rel="noopener noreferrer" target="_blank">' in out
    assert 'href="mailto:a@b.co"' in html("[mail](mailto:a@b.co)") and "target" not in html("[mail](mailto:a@b.co)")
    assert "target" not in html("[here](#top)") and 'href="#top"' in html("[here](#top)")


def test_tables_task_lists_and_code():
    out = html("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<table>" in out and '<th dir="auto">a</th>' in out and '<td dir="auto">2</td>' in out
    out = html("- [ ] todo\n- [x] done")
    assert out.count('type="checkbox"') == 2 and 'checked="checked"' in out and "disabled" in out
    assert "<pre><code>a &lt; b\n</code></pre>" in html("```\na < b\n```")


def test_hebrew_and_mixed_text_get_dir_auto_per_block():
    out = html("שלום עולם\n\nHello **world**")
    assert out.count('<p dir="auto">') == 2 and "<strong>world</strong>" in out


def test_raw_html_is_escaped_not_passed_through():
    out = html('<script>alert(1)</script> <img src=x onerror=alert(2)> <a href="x" onclick="y">z</a>')
    assert "<script" not in out and "<img" not in out and "<a " not in out
    assert "&lt;script&gt;" in out
    assert "&lt;div&gt;" in html("<div>x</div>")
    assert "<iframe" not in html("<iframe src=//evil></iframe>")


def test_unsafe_link_schemes_are_not_linked():
    for url in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "vbscript:x", "data:text/html;base64,AAAA", "file:///etc/passwd"):
        assert "<a " not in html(f"[x]({url})"), url
        assert f'src="{url}"' not in html(f"![x]({url})"), url
    assert "<a " in html("[x](https://example.com)") and "<a " in html("[x](/relative/path)")


def test_file_names_resolve_to_attachments_only_when_they_match():
    urls = {"shot.png": "/att/shot.png", "attachments/description/shot.png": "/att/shot.png", "a b.txt": "/att/a b.txt"}
    lookup = lambda href: urls.get(href)
    assert '<img src="/att/shot.png" alt="s"' in html("![s](shot.png)", lookup)
    assert '<a href="/att/shot.png">p</a>' in html("[p](attachments/description/shot.png)", lookup)
    assert 'href="other.txt"' in html("[o](other.txt)", lookup)                 # unknown names stay as written
    assert 'src="https://x.test/shot.png"' in html("![s](https://x.test/shot.png)", lookup)  # never rewritten
    assert 'href="/shot.png"' in html("[p](/shot.png)", lookup)                  # absolute paths are not names
    assert "loading=\"lazy\"" in html("![s](shot.png)", lookup) and "no-referrer" in html("![s](shot.png)")


def test_output_is_markup_so_templates_do_not_escape_it():
    from markupsafe import Markup
    assert isinstance(markup.render("**x**"), Markup)


def test_mermaid_blocks_become_placeholders_with_escaped_source():
    out = html("```mermaid\ngraph TD\n  A[<b>x</b>] --> B\n```")
    assert out == '<pre class="mermaid" dir="ltr">graph TD\n  A[&lt;b&gt;x&lt;/b&gt;] --&gt; B\n</pre>'
    assert html("```mermaid title\nA-->B\n```").startswith('<pre class="mermaid"')   # extra words after the name
    assert "<script" not in html("```mermaid\n<script>alert(1)</script>\n```")
    assert html("```mermaid\n```") == '<pre class="mermaid" dir="ltr"></pre>'


def test_other_code_blocks_are_unchanged():
    assert html("```python\nprint(1)\n```") == '<pre><code class="language-python">print(1)\n</code></pre>'
    assert html("```\nplain\n```") == "<pre><code>plain\n</code></pre>"
    assert 'class="mermaid"' not in html("```mermaidish\nx\n```")                  # only the exact name
    assert 'class="mermaid"' not in html("    mermaid\n    indented code")        # indented code, no info string
    assert 'class="mermaid"' not in html("`mermaid`")


def test_every_cheatsheet_example_really_renders_what_it_shows():
    """The cheatsheet is what users learn from: each example must produce the markup it teaches."""
    expect = {
        "headings": ['<h1 dir="auto">Heading 1</h1>', '<h2 dir="auto">Heading 2</h2>', '<h3 dir="auto">Heading 3</h3>'],
        "emphasis": ["<strong>bold</strong>", "<em>italic</em>", "<s>strikethrough</s>", "<code>inline code</code>"],
        "lists": ["<ul>", "nested one", "<ol>", "<li dir=\"auto\">second</li>"],
        "tasks": ['type="checkbox"', 'checked="checked"', "still to do"],
        "quotes": ["<blockquote", "a quote inside it"],
        "code": ['<code class="language-python">', "print(&#39;hello&#39;)"],
        "links": ['<a href="https://example.com"', ">https://bare.urls.link.too</a>", 'href="mailto:someone@example.com"'],
        "tables": ['<th style="text-align:left"', '<td style="text-align:right"', "apples"],
        "divider": ["<hr />"],
        "breaks": ["a new line<br />\nis a line break", "*a backslash escapes*"],
        "diagrams": ['<pre class="mermaid" dir="ltr">flowchart LR'],
    }
    drawn = {key: src for key, src, shown in markup.CHEATSHEET if shown}
    assert set(drawn) == set(expect), set(drawn) ^ set(expect)
    for key, needles in expect.items():
        out = html(drawn[key])
        for needle in needles:
            assert needle in out or needle.replace("&#39;", "'") in out, (key, needle, out)
    keys = [k for k, _, _ in markup.CHEATSHEET]
    assert len(keys) == len(set(keys)) and "files" in keys      # unique; the attachments example is shown, not drawn
    assert not [k for k, _, shown in markup.CHEATSHEET if not shown and k != "files"]


def test_what_the_cheatsheet_says_is_not_supported_really_is_not():
    assert "<b>" not in html("<b>x</b>") and "&lt;b&gt;" in html("<b>x</b>")      # raw HTML is text
    assert ":smile:" in html(":smile:") and "$a^2$" in html("$a^2$")             # no emoji codes, no math
    assert 'class="footnote' not in html("x[^1]\n\n[^1]: note") and "<sup" not in html("x[^1]\n\n[^1]: note")
