const tabNames = ["recordings", "downloads", "timeline", "runs"] as const;

type TabName = "stream" | (typeof tabNames)[number];

function getBrowserSessionTabFromPathname(pathname: string): TabName {
  return tabNames.find((tab) => pathname.endsWith(`/${tab}`)) ?? "stream";
}

export { getBrowserSessionTabFromPathname, type TabName };
