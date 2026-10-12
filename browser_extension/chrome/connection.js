export const DEFAULT_CW_ORIGIN = 'https://app.knowledge-trail.com';

export function normalizeCwOrigin(input) {
  let value = String(input || '').trim();
  if (!value) throw new Error('Enter your CW address, for example https://app.knowledge-trail.com.');
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) value = 'https://' + value;
  let url;
  try { url = new URL(value); }
  catch { throw new Error('That address is invalid. Enter an HTTPS website address such as https://app.knowledge-trail.com.'); }
  if (url.protocol !== 'https:' || url.username || url.password || !url.hostname || /\s/.test(value)) {
    throw new Error('Use your CW HTTPS website address, without a username, password or spaces.');
  }
  // Accept a pasted CW app link; the API lives at the same site's /auth path.
  return url.origin;
}
