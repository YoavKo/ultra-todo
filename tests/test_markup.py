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
