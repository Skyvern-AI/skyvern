import random
import structlog

from skyvern import app

LOG = structlog.get_logger()


async def click(workflow_run_id: str, x: int | float, y: int | float):
    browser_state = app.BROWSER_MANAGER.pages.get(workflow_run_id)

    if not browser_state:
        LOG.error(f"No browser state for {workflow_run_id}")
        return

    page = await browser_state.get_working_page()

    if not page:
        LOG.error(f"No page for {workflow_run_id}")
        return

    viewport = page.viewport_size

    if not viewport:
        viewport = await page.evaluate(
            "() => ({ width: window.innerWidth, height: window.innerHeight })"
        )

    px = x * viewport['width']
    py = y * viewport['height']

    await page.mouse.click(px, py)

    LOG.info("We have clicked", px=px, py=py, viewport=viewport)

    r = random.randint(0, 255)
    g = random.randint(0, 255)
    b = random.randint(0, 255)
    size = 48

    expr = f"""() => {{
        const el = document.createElement('div');
        el.style.position = 'absolute';
        el.style.left = '{px - size / 2}px';
        el.style.top = '{py - size / 2}px';
        el.style.width = '{size}px';
        el.style.height = '{size}px';
        el.style.backgroundColor = 'rgb({r}, {g}, {b})';
        el.style.borderRadius = '50%';
        el.style.zIndex = '10000';
        document.body.appendChild(el);
        setTimeout(() => el.remove(), 10000);
    }}"""

    await page.evaluate(expr)

    LOG.info("We have inserted an element.", expr=expr)
