export function isWindowsPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const value = `${navigator.platform || ""} ${navigator.userAgent || ""}`;
  return /Windows|Win32|Win64/i.test(value);
}
