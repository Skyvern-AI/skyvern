# Free of Skyvern imports so modules inside the db.utils import cycle can name the vendor. A registration with this
# browser_vendor attaches to a browser the caller already runs; Skyvern only ever detaches from it.
EXTERNAL_CDP_BROWSER_VENDOR = "external_cdp"
