"""
Playwright-based benchmark for JavaScript DOM parsing (domUtils.js).

Tests run a real browser to measure actual buildTreeFromBody() performance
on a synthetic HTML page with many elements.

These tests require playwright to be installed with browser binaries.
They are automatically skipped if the browser binary is missing.
Run `playwright install chromium` to enable these tests.
"""

import pytest

from skyvern.webeye.scraper.scraper import JS_FUNCTION_DEFS

# Only run if playwright is available
playwright = pytest.importorskip("playwright")


def _launch_browser():
    """Try to launch a Chromium browser. Returns (browser, playwright_instance) or raises."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
        return browser, pw
    except Exception:
        pw.stop()
        raise


@pytest.fixture(scope="module")
def browser_context():
    """Launch a browser for JS benchmarks. Skips if browser binary is missing."""
    try:
        browser, pw = _launch_browser()
    except Exception as e:
        pytest.skip(f"Playwright browser not available: {e}")

    context = browser.new_context()
    yield context
    context.close()
    browser.close()
    pw.stop()


def _generate_test_html(num_elements: int) -> str:
    """Generate an HTML page with the specified number of elements."""
    elements = []
    for i in range(num_elements):
        tag = ["div", "span", "button", "input", "a"][i % 5]
        if tag == "input":
            elements.append(
                f'<{tag} type="text" name="field_{i}" placeholder="Field {i}" '
                f'class="form-control" aria-label="Field {i}">'
            )
        elif tag == "a":
            elements.append(
                f'<{tag} href="https://example.com/{i}" class="link-{i}" aria-label="Link {i}">Link text {i}</{tag}>'
            )
        elif tag == "button":
            elements.append(f'<{tag} type="button" class="btn btn-{i}" aria-label="Button {i}">Click {i}</{tag}>')
        else:
            elements.append(f'<{tag} class="element-{i}" style="cursor: pointer;">Text content {i}</{tag}>')

    body_content = "\n".join(elements)
    return f"""
    <!DOCTYPE html>
    <html>
    <head><title>Benchmark Page</title></head>
    <body>
    <div id="main-container">
        {body_content}
    </div>
    </body>
    </html>
    """


class TestDomUtilsJsBenchmark:
    """Benchmark buildTreeFromBody() in a real browser."""

    @pytest.mark.parametrize("num_elements", [500, 2000, 5000])
    def test_build_tree_performance(self, browser_context, num_elements: int):
        page = browser_context.new_page()
        html = _generate_test_html(num_elements)
        page.set_content(html)

        # Load domUtils.js
        page.evaluate(JS_FUNCTION_DEFS)

        # Benchmark buildTreeFromBody
        result = page.evaluate("""
            async () => {
                const start = performance.now();
                const [elements, tree] = await buildTreeFromBody('main.frame', 0);
                const elapsed = performance.now() - start;
                return {
                    elapsed_ms: elapsed,
                    element_count: elements.length,
                    tree_root_count: tree.length,
                };
            }
        """)

        elapsed_ms = result["elapsed_ms"]
        element_count = result["element_count"]
        tree_root_count = result["tree_root_count"]

        print(f"\nbuildTreeFromBody({num_elements} DOM nodes):")
        print(f"  Time: {elapsed_ms:.1f}ms")
        print(f"  Elements found: {element_count}")
        print(f"  Tree roots: {tree_root_count}")

        # Should complete within reasonable time
        assert elapsed_ms < 10000, f"buildTreeFromBody took {elapsed_ms:.1f}ms for {num_elements} elements"
        assert element_count > 0, "Should find at least some elements"

        page.close()

    def test_hover_styles_map_performance(self, browser_context):
        """Benchmark getHoverStylesMap() with many stylesheets."""
        page = browser_context.new_page()

        # Create a page with many CSS rules
        css_rules = "\n".join([f".element-{i}:hover {{ cursor: pointer; background: #{i:06x}; }}" for i in range(500)])
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>{css_rules}</style>
            <style>
                .non-hover-1 {{ color: red; }}
                .non-hover-2 {{ color: blue; }}
            </style>
        </head>
        <body><div>Test</div></body>
        </html>
        """
        page.set_content(html)
        page.evaluate(JS_FUNCTION_DEFS)

        result = page.evaluate("""
            async () => {
                const start = performance.now();
                const hoverMap = await getHoverStylesMap();
                const elapsed = performance.now() - start;
                return {
                    elapsed_ms: elapsed,
                    hover_selectors: hoverMap.size,
                };
            }
        """)

        elapsed_ms = result["elapsed_ms"]
        hover_selectors = result["hover_selectors"]

        print("\ngetHoverStylesMap (500 hover rules + 2 non-hover rules):")
        print(f"  Time: {elapsed_ms:.1f}ms")
        print(f"  Hover selectors found: {hover_selectors}")

        assert elapsed_ms < 5000, f"getHoverStylesMap took {elapsed_ms:.1f}ms"
        assert hover_selectors > 0, "Should find hover selectors"

        page.close()

    def test_draw_bounding_boxes_performance(self, browser_context):
        """Benchmark drawBoundingBoxes() with visual grouping."""
        page = browser_context.new_page()
        html = _generate_test_html(1000)
        page.set_content(html)
        page.evaluate(JS_FUNCTION_DEFS)

        result = page.evaluate("""
            async () => {
                // First build the tree (populates cache)
                await buildTreeFromBody('main.frame', 0);

                const start = performance.now();
                await buildElementsAndDrawBoundingBoxes('main.frame', 0);
                const elapsed = performance.now() - start;

                // Count bounding boxes added
                const container = document.querySelector('#boundingBoxContainer');
                const boxCount = container ? container.children.length : 0;

                return {
                    elapsed_ms: elapsed,
                    box_count: boxCount,
                };
            }
        """)

        elapsed_ms = result["elapsed_ms"]
        box_count = result["box_count"]

        print("\nbuildElementsAndDrawBoundingBoxes (1000 elements):")
        print(f"  Time: {elapsed_ms:.1f}ms")
        print(f"  Bounding boxes: {box_count}")

        assert elapsed_ms < 10000, f"drawBoundingBoxes took {elapsed_ms:.1f}ms"

        page.close()

    def test_tree_correctness(self, browser_context):
        """Verify the tree structure is correct after optimizations."""
        page = browser_context.new_page()
        html = """
        <!DOCTYPE html>
        <html>
        <body>
            <div id="parent">
                <button id="btn1">Click me</button>
                <input type="text" name="field1" placeholder="Enter text">
                <a href="https://example.com">Link</a>
                <select name="dropdown">
                    <option value="a">Option A</option>
                    <option value="b">Option B</option>
                </select>
            </div>
        </body>
        </html>
        """
        page.set_content(html)
        page.evaluate(JS_FUNCTION_DEFS)

        result = page.evaluate("""
            async () => {
                const [elements, tree] = await buildTreeFromBody('main.frame', 0);
                return {
                    element_count: elements.length,
                    tree_root_count: tree.length,
                    // Check that interactable elements were found
                    interactable_count: elements.filter(e => e.interactable).length,
                    // Check a button was found
                    has_button: elements.some(e => e.tagName === 'button'),
                    // Check an input was found
                    has_input: elements.some(e => e.tagName === 'input'),
                    // Check a link was found
                    has_link: elements.some(e => e.tagName === 'a'),
                    // Check select was found with options
                    has_select: elements.some(e => e.tagName === 'select' && e.options && e.options.length === 2),
                };
            }
        """)

        assert result["element_count"] > 0
        assert result["interactable_count"] >= 4  # button, input, a, select
        assert result["has_button"]
        assert result["has_input"]
        assert result["has_link"]
        assert result["has_select"]

        page.close()
