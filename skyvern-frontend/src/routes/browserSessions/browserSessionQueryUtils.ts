function getBrowserSessionRefetchIntervalMs(
  status: string | undefined,
): number | false {
  if (!status) {
    return 2000;
  }
  if (status === "running") {
    return 5000;
  }
  if (status === "created" || status === "retry") {
    return 2000;
  }
  return false;
}

export { getBrowserSessionRefetchIntervalMs };
