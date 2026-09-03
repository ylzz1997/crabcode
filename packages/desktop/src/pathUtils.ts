export function projectPathKey(value: string): string {
  const normalized = value.replace(/\\/g, "/").replace(/\/+$/, "") || value;
  const isWindowsPath = /^[A-Za-z]:\//.test(normalized) || normalized.startsWith("//");
  return isWindowsPath ? normalized.toLowerCase() : normalized;
}

export function sameProjectPath(left: string | null, right: string | null): boolean {
  return left !== null && right !== null && projectPathKey(left) === projectPathKey(right);
}
