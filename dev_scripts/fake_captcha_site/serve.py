"""Simple HTTP server for fake captcha test pages.

Usage:
    python dev_scripts/fake_captcha_site/serve.py

Pages:
    http://localhost:8888/instruction_captcha.html   — Instruction-based captcha (no image)
    http://localhost:8888/image_captcha.html         — SVG image-based captcha
    http://localhost:8888/invisible_recaptcha.html   — Invisible reCAPTCHA v3, never escalates
    http://localhost:8888/invisible_hcaptcha.html    — Invisible hCaptcha, executed only on Submit
"""

import http.server
import os
import sys
from typing import Any

PORT = 8888
DIRECTORY = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def translate_path(self, path: str) -> str:
        # Google serves the invisible widget's iframe from an extensionless /api2/anchor path.
        if path.split("?", 1)[0] == "/recaptcha/api2/anchor":
            path = "/recaptcha/anchor.html"
        return super().translate_path(path)


if __name__ == "__main__":
    # Threading matters: a browser holds keep-alive connections open, and the single-threaded
    # HTTPServer stops answering entirely for the rest of a run once one is parked on it.
    with http.server.ThreadingHTTPServer(("", PORT), Handler) as httpd:
        print(f"Serving fake captcha site at http://localhost:{PORT}/")
        print(f"  Instruction captcha: http://localhost:{PORT}/instruction_captcha.html")
        print(f"  Image captcha:       http://localhost:{PORT}/image_captcha.html")
        print(f"  Invisible reCAPTCHA: http://localhost:{PORT}/invisible_recaptcha.html")
        print(f"  Invisible hCaptcha:  http://localhost:{PORT}/invisible_hcaptcha.html")
        print("Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
            sys.exit(0)
