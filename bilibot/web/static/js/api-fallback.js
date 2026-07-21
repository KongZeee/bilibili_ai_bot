/** Pure helpers for flat→nested API route fallback (no browser globals). */

/**
 * True only when the flat route is missing / not applicable so nested may be tried.
 * Must NOT match business errors (VALIDATION_ERROR, INVALID_STATE, 5xx, network).
 */
export function isRouteMissError(err) {
    if (!err) return false;
    if (err.code === 'NOT_FOUND' || err.code === 'NO_ACCOUNT') return true;
    if (err.status === 404) return true;
    const msg = String(err.message || '');
    if (/\bHTTP 404\b/.test(msg)) return true;
    return false;
}
