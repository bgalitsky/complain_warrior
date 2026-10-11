// Executes in Chrome's isolated world. No page-accessible message listener or data attributes.
(() => {
  if (globalThis.__cwFormAgent) return;
  let snapshot = null;
  let refs = new Map();
  const compact = s => String(s || '').replace(/\s+/g,' ').trim().slice(0, 600);
  const labelText = node => {
    const copy = node.cloneNode(true);
    copy.querySelectorAll('input,select,textarea,script,style').forEach(n => n.remove());
    return compact(copy.textContent);
  };
  function info(el) {
    const root = el.getRootNode();
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => root.getElementById?.(id)?.textContent || '').join(' ');
    const label = compact(Array.from(el.labels || []).map(labelText).join(' ') || labelled ||
      el.getAttribute('aria-label') || el.placeholder || '');
    const section = compact(el.closest('fieldset')?.querySelector('legend')?.textContent ||
      el.closest('[role=group]')?.getAttribute('aria-label') ||
      el.closest('form')?.getAttribute('aria-label') || '');
    return {tag: el.tagName.toLowerCase(), type: el.type, label, section,
      name: el.name || '', id: el.id || '', autocomplete: el.autocomplete || '',
      options: el.tagName === 'SELECT' ? Array.from(el.options).map(o =>
        ({label: o.label, value: o.value, disabled: o.disabled || !!o.closest('optgroup')?.disabled})) : []};
  }
  function eligible(el) {
    if (!el.isConnected || el.disabled || el.matches(':disabled') || el.readOnly || el.multiple || el.closest('[inert]')) return false;
    const view = el.ownerDocument.defaultView;
    const style = view.getComputedStyle(el);
    if (!el.getClientRects().length || style.visibility !== 'visible' || style.display === 'none') return false;
    if (el.tagName === 'INPUT' && !['text','email','tel','number','date','url'].includes(el.type)) return false;
    const f = info(el);
    const sensitive = `${f.label} ${f.name} ${f.id} ${f.autocomplete}`;
    return !!f.label && !/(password|passcode|one.time|otp\b|captcha|signature|sign here|social.security|ssn\b|credit.card|card.number|cc-|passport|national.id|bank.account|routing.number|iban|certify|consent|agree to|i declare)/i.test(sensitive);
  }
  function checkPage() {
    const u = new URL(location.href);
    if (!(u.protocol === 'https:' || (u.protocol === 'http:' && ['localhost','127.0.0.1'].includes(u.hostname)))) {
      throw new Error('Open an HTTPS complaint form. Only localhost test pages may use HTTP.');
    }
  }
  function scan() {
    checkPage(); refs.clear();
    snapshot = {id: crypto.randomUUID(), url: location.href, origin: location.origin};
    const fields = [];
    let skippedFrames = 0;
    function visit(root) {
      for (const el of root.querySelectorAll('*')) {
        if (fields.length >= 150) break;
        if (el.matches('input,textarea,select') && eligible(el)) {
          const f = info(el), token = crypto.randomUUID();
          refs.set(token, {el, fingerprint: JSON.stringify(f), value: el.value, document: el.ownerDocument,
            url: el.ownerDocument.URL});
          fields.push({...f, token, currentValue: el.value.slice(0, 30000)});
        }
        if (el.shadowRoot) visit(el.shadowRoot);
        if (el.tagName === 'IFRAME') {
          try {
            if (el.contentWindow.location.origin !== location.origin || !el.contentDocument) { skippedFrames++; continue; }
            if (el.getClientRects().length && getComputedStyle(el).visibility === 'visible') visit(el.contentDocument);
          } catch { skippedFrames++; }
        }
      }
    }
    visit(document);
    return {...snapshot, title: document.title, fields, skippedFrames};
  }
  function validate(ref) {
    return ref && ref.el.ownerDocument === ref.document && ref.document.URL === ref.url &&
      eligible(ref.el) && JSON.stringify(info(ref.el)) === ref.fingerprint && ref.el.value === ref.value;
  }
  async function fill(request) {
    checkPage();
    if (!snapshot || request.id !== snapshot.id || location.href !== snapshot.url) throw new Error('Page changed. Discover fields again.');
    if (!Array.isArray(request.fields) || request.fields.length > 150) throw new Error('Invalid fill request.');
    const seen = new Set();
    for (const item of request.fields) {
      const ref = refs.get(item.token);
      if (seen.has(item.token) || !validate(ref)) throw new Error('A field changed since review. Discover again before filling.');
      seen.add(item.token);
      if (typeof item.value !== 'string' || item.value.length > 30000) throw new Error('Invalid value.');
      if (ref.value && !item.overwrite) throw new Error('Approve replacing an existing value first.');
      if (ref.el.maxLength >= 0 && item.value.length > ref.el.maxLength) throw new Error('A proposed value exceeds the field length limit.');
      if (ref.el.tagName === 'SELECT' && !info(ref.el).options.some(o => !o.disabled && o.value === item.value)) throw new Error('Selected option is no longer available.');
    }
    const results = [];
    for (const item of request.fields) {
      const ref = refs.get(item.token);
      if (location.href !== snapshot.url || !validate(ref)) {
        results.push({token:item.token, status:'stopped', message:'Form changed during filling. Review the partial result and rescan.'}); break;
      }
      const el = ref.el, w = el.ownerDocument.defaultView;
      try {
        const proto = el.tagName === 'SELECT' ? w.HTMLSelectElement.prototype :
          el.tagName === 'TEXTAREA' ? w.HTMLTextAreaElement.prototype : w.HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, item.value);
        el.dispatchEvent(new w.Event('input', {bubbles:true, composed:true}));
        el.dispatchEvent(new w.Event('change', {bubbles:true, composed:true}));
        await new Promise(resolve => setTimeout(resolve, 120));
        const valid = el.isConnected && el.value === item.value;
        results.push({token:item.token, status:valid ? 'filled' : 'unverified',
          message:valid ? (el.validationMessage || 'Value verified. Review it on the page.') : 'Page replaced or rejected this field. Inspect it manually.'});
        if (!valid) break;
      } catch {
        results.push({token:item.token, status:'unverified', message:'Could not verify this field. Inspect it manually.'}); break;
      }
    }
    // A completed/partial batch requires a new scan. Never silently replay a fill.
    refs.clear(); snapshot = null;
    return results;
  }
  globalThis.__cwFormAgent = {scan, fill};
})();
