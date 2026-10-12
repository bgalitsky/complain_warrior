// Local matching only: no site text or complaint data is sent to an AI service.
export const SCHEMA = 'cw.form-assistant.v1';
export const KEYS = [
  'consumer.full_name','consumer.first_name','consumer.last_name','consumer.email','consumer.phone',
  'consumer.street','consumer.city','consumer.state','consumer.postal_code','consumer.country',
  'business.name','business.email','business.phone','business.street','business.city','business.state',
  'business.postal_code','business.country','complaint.subject','complaint.narrative',
  'complaint.requested_resolution','complaint.amount','complaint.transaction_date',
  'complaint.reference','complaint.date','jurisdiction.country','jurisdiction.state'
];
export function normalize(s) {
  return String(s || '').replace(/([a-z])([A-Z])/g,'$1 $2')
    .normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu,' ').trim();
}
export function parseComplaint(data) {
  if (!data || data.schema !== SCHEMA || typeof data.complaint_id !== 'string' ||
      !data.complaint_id.trim() || data.complaint_id.length > 200 ||
      !data.values || typeof data.values !== 'object' || Array.isArray(data.values)) {
    throw new Error('Choose a CW Form Assistant JSON export.');
  }
  const values = {};
  for (const [key, value] of Object.entries(data.values)) {
    if (!KEYS.includes(key)) throw new Error(`Unknown CW data field: ${key}`);
    if (typeof value !== 'string' || value.length > 30000) throw new Error('Invalid or oversized complaint value.');
    values[key] = value;
  }
  if (!Object.values(values).some(v => v.trim())) throw new Error('This complaint contains no data.');
  return {schema: SCHEMA, complaint_id: data.complaint_id, values};
}
const TYPES = [
  ['first_name', /\b(first name|given name|forename|nombre de pila|prenom)\b/],
  ['last_name', /\b(last name|surname|family name|apellido|nom de famille)\b/],
  ['email', /\b(e mail|email|correo electronico|courriel)\b/],
  ['phone', /\b(phone|telephone|mobile|telefono|tel)\b/],
  ['postal_code', /\b(zip|zip code|postal code|postcode|codigo postal|code postal)\b/],
  ['country', /\b(country|pais|pays)\b/],
  ['state', /\b(state|province|region|estado)\b/],
  ['city', /\b(city|town|ciudad|ville|locality)\b/],
  ['street', /\b(street|street address|address line 1|address1|direccion|adresse|address)\b/],
  ['full_name', /\b(full name|your name|name|nombre completo|nombre|nom)\b/]
];
const AUTOCOMPLETE = {'name':'full_name','given-name':'first_name','family-name':'last_name',
  'email':'email','tel':'phone','tel-national':'phone','street-address':'street','address-line1':'street',
  'address-level2':'city','address-level1':'state','postal-code':'postal_code',
  'country':'country','country-name':'country','organization':'name'};
const BUSINESS = /\b(business|company|merchant|trader|seller|defendant|respondent|supplier|empresa|comerciante|demandado|entreprise|professionnel)\b/;
const CONSUMER = /\b(consumer|complainant|plaintiff|your|personal|applicant|consumidor|reclamante|demandeur|vos|votre)\b/;
export function propose(field, values) {
  const label = normalize(field.label);
  const context = normalize(field.section);
  const all = `${label} ${context}`;
  // Unrecognised labels remain manual. A number input is not necessarily an amount.
  const cases = [
    ['complaint.requested_resolution', /\b(desired outcome|requested resolution|resolution sought|resolution.*seeking|what.*want|remedy|remedies|resolucion|solucion|reparation)\b/],
    ['complaint.narrative', /\b(complaint details|complaint description|describe.*(complaint|problem|incident|issue)|what happened|narrative|details of.*complaint|descripcion.*(queja|reclamacion)|description.*(plainte|probleme))\b/],
    ['complaint.subject', /\b(subject|complaint title|nature of.*complaint|asunto|objet)\b/],
    ['complaint.amount', /\b(amount (claimed|paid|disputed)|disputed amount|claim amount|purchase price|importe|montant)\b/],
    ['complaint.date', /\b(date of complaint|complaint date)\b/],
    ['complaint.transaction_date', /\b(transaction date|purchase date|date of purchase|fecha de compra)\b/],
    ['complaint.reference', /\b(order invoice number|invoice number|order number|booking reference|transaction reference|numero de pedido)\b/]
  ];
  for (const [key, pattern] of cases) if (pattern.test(label)) return suggestion(key, .95, 'Field label', field, values);
  const roleBusiness = BUSINESS.test(label) || (!CONSUMER.test(label) && BUSINESS.test(context));
  const roleConsumer = CONSUMER.test(label) || (!BUSINESS.test(label) && CONSUMER.test(context));
  if (roleBusiness && roleConsumer) return null;
  const ac = (field.autocomplete || '').split(/\s+/).at(-1);
  let type = AUTOCOMPLETE[ac];
  if (!type) type = TYPES.find(([,re]) => re.test(label))?.[0];
  if (!type) return null;
  // Second address line, signing names and unrelated IDs must not inherit generic matches.
  if (/\b(address line 2|address2|apartment|suite|signature|sign here|certify|confirm|consent|agree)\b/.test(label)) return null;
  let key;
  if (/\b(jurisdiction|filing location)\b/.test(all) && ['country','state'].includes(type)) key = `jurisdiction.${type}`;
  else if (roleBusiness || ac === 'organization') key = `business.${type === 'full_name' ? 'name' : type}`;
  else key = `consumer.${type}`;
  return suggestion(key, roleBusiness || roleConsumer ? .94 : .65,
    roleBusiness || roleConsumer ? 'Label and party context' : 'Ambiguous party — check consumer vs business', field, values);
}
function suggestion(key, confidence, reason, field, values) {
  if (!values[key]?.trim()) return null;
  let value = values[key];
  if (field.tag === 'select') {
    const opts = field.options.filter(o => !o.disabled && o.value &&
      (normalize(o.label) === normalize(value) || normalize(o.value) === normalize(value)));
    if (opts.length !== 1) return null;
    value = opts[0].value;
  }
  return {key, value, confidence, reason};
}

// Automatic mode never replaces user edits or acts on declarations/buttons.
export function autoPlan(fields, supplied) {
  const values = {...supplied};
  if ((!values['consumer.first_name'] || !values['consumer.last_name']) && values['consumer.full_name']) {
    const parts = values['consumer.full_name'].trim().split(/\s+/);
    if (parts.length === 2) {
      values['consumer.first_name'] ||= parts[0];
      values['consumer.last_name'] ||= parts[1];
    }
  }
  const plan = [], skipped = [];
  for (const field of fields) {
    if (field.currentValue) { skipped.push({label:field.label, reason:'Existing value preserved'}); continue; }
    const suggestion = propose(field, values);
    if (!suggestion) { skipped.push({label:field.label, reason:'No unambiguous stored value'}); continue; }
    let value = suggestion.value;
    if (suggestion.key === 'complaint.date' || suggestion.key === 'complaint.transaction_date') {
      const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
      const fmt = (field.dateFormat || field.placeholder || '').toLowerCase();
      if (!iso) { skipped.push({label:field.label, reason:'Date format not recognised'}); continue; }
      if (field.type === 'date' || fmt === 'yyyy-mm-dd') { /* already ISO */ }
      else if (fmt === 'mmddyyyy' || fmt === 'mm-dd-yyyy' || fmt === 'mm/dd/yyyy') {
        const sep = field.dateSeparator || (fmt.includes('/') ? '/' : '-');
        value = [iso[2],iso[3],iso[1]].join(sep);
      } else if (fmt === 'dd-mm-yyyy' || fmt === 'dd/mm/yyyy') {
        const sep = fmt.includes('/') ? '/' : '-'; value = [iso[3],iso[2],iso[1]].join(sep);
      } else { skipped.push({label:field.label, reason:'Date format unspecified'}); continue; }
    }
    if (field.maxLength >= 0 && value.length > field.maxLength) {
      skipped.push({label:field.label, reason:'Stored text exceeds the field limit'}); continue;
    }
    plan.push({token:field.token, value, overwrite:false});
  }
  return {fields:plan, skipped};
}
