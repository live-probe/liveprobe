/** Drops success and failure from superseded requests; only the latest call may update its consumer. */
export function latestOnly() {
  let seq = 0;
  return { async run<T>(load: () => Promise<T>, apply: (v: T) => void, fail?: (error: unknown) => void) {
    const mine = ++seq; let value: T;
    try { value = await load(); }
    catch (error) { if (mine !== seq) return; if (fail) { fail(error); return; } throw error; }
    if (mine === seq) apply(value);
  } };
}
