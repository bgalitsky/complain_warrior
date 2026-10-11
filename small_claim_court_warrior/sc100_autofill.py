"""Explicit mappings for California SC-100, revision January 1, 2026."""
import io
import math
import fitz
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject

OFFICIAL_URL = 'https://courts.ca.gov/documents/sc100.pdf'
P = 'SC-100[0].'
MAPPING = {
    'Page2[0].List1[0].Item1[0].PlaintiffName1[0]': 'plaintiff_name',
    'Page2[0].List1[0].Item1[0].PlaintiffPhone1[0]': 'plaintiff_phone',
    'Page2[0].List1[0].Item1[0].PlaintiffAddress1[0]': 'plaintiff_street',
    'Page2[0].List1[0].Item1[0].PlaintiffCity1[0]': 'plaintiff_city',
    'Page2[0].List1[0].Item1[0].PlaintiffState1[0]': 'plaintiff_state',
    'Page2[0].List1[0].Item1[0].PlaintiffZip1[0]': 'plaintiff_zip',
    'Page2[0].List1[0].Item1[0].EmailAdd1[0]': 'plaintiff_email',
    'Page2[0].List2[0].item2[0].DefendantName1[0]': 'defendant_legal_name',
    'Page2[0].List2[0].item2[0].DefendantAddress1[0]': 'defendant_street',
    'Page2[0].List2[0].item2[0].DefendantCity1[0]': 'defendant_city',
    'Page2[0].List2[0].item2[0].DefendantState1[0]': 'defendant_state',
    'Page2[0].List2[0].item2[0].DefendantZip1[0]': 'defendant_zip',
    'Page2[0].List2[0].item2[0].DefendantName2[0]': 'agent_for_service',
    'Page2[0].List3[0].PlaintiffClaimAmount1[0]': 'amount_text',
    'Page2[0].List3[0].Lia[0].FillField2[0]': 'claim_detail',
    'Page3[0].List3[0].Lib[0].Date1[0]': 'incident_date',
    'Page3[0].List3[0].Lic[0].FillField1[0]': 'damages_summary',
}
for page in (2, 3, 4):
    MAPPING[f'Page{page}[0].PxCaption[0].Plaintiff[0]'] = 'plaintiff_name'


def field_map(names, payload):
    missing = {P + name for name in MAPPING} - set(names)
    if missing:
        raise ValueError('Unsupported SC-100 field layout. Obtain the January 2026 official form; no guessed mapping was used.')
    return {P + name: str(payload.get(key) or '') for name, key in MAPPING.items()}


def normalize_blank(data):
    # The official PDF can carry reader-extension encryption that pypdf cannot decode.
    with fitz.open(stream=data, filetype='pdf') as doc:
        if doc.needs_pass:
            raise ValueError('Password-protected PDF is not a blank SC-100 template.')
        if doc.page_count != 6 or 'January 1, 2026' not in doc[0].get_text():
            raise ValueError('Expected the six-page SC-100 revision January 1, 2026.')
        for page in doc:
            for widget in page.widgets() or []:
                if widget.field_type == fitz.PDF_WIDGET_TYPE_TEXT and widget.field_value:
                    raise ValueError('Upload a blank SC-100, not a previously populated or signed form.')
        return doc.tobytes(encryption=fitz.PDF_ENCRYPT_NONE)


def validate_fit(data, values):
    """Conservatively refuse text that cannot fit at a readable fixed font size."""
    with fitz.open(stream=data, filetype='pdf') as doc:
        for page in doc:
            for widget in page.widgets() or []:
                value = values.get(widget.field_name, '')
                if not value:
                    continue
                try:
                    value.encode('cp1252')
                except UnicodeEncodeError as exc:
                    raise ValueError('Use characters supported by the official PDF font or complete this field manually: ' + widget.field_label) from exc
                width = widget.rect.width - 5
                lines = 0
                for paragraph in value.split('\n'):
                    line_width = 0
                    lines += 1
                    for word in paragraph.split():
                        word_width = fitz.get_text_length(word + ' ', fontname='tiro', fontsize=10)
                        if word_width > width:
                            raise ValueError('Text is too wide for ' + widget.field_label)
                        if line_width and line_width + word_width > width:
                            lines += 1
                            line_width = 0
                        line_width += word_width
                capacity = max(1, math.floor((widget.rect.height - 2) / 12))
                if lines > capacity:
                    raise ValueError('Text is too long for ' + widget.field_label + '. Shorten it or prepare an attachment and complete this field manually; no text was truncated.')


def fill_verified(data, values, flatten=False):
    reader = PdfReader(io.BytesIO(data))
    fields = reader.get_fields() or {}
    if set(values) - set(fields):
        raise ValueError('Requested field is missing from the PDF.')
    validate_fit(data, values)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    # Use the existing embedded Times font; fixed size makes overflow checks meaningful.
    typed = {name: (value, '/TimesNewRomanPSMT', 10) for name, value in values.items()}
    writer.update_page_form_field_values(None, typed, auto_regenerate=False, flatten=flatten)
    if flatten:
        writer.remove_annotations(subtypes='/Widget')
        writer.root_object.pop(NameObject('/AcroForm'), None)
    result = io.BytesIO()
    writer.write(result)
    output = result.getvalue()
    check = PdfReader(io.BytesIO(output))
    if flatten:
        if check.get_fields() or any(str(a.get_object().get('/Subtype')) == '/Widget'
                                    for page in check.pages for a in page.get('/Annots', [])):
            raise ValueError('Flattening did not remove interactive fields.')
    else:
        actual = check.get_fields() or {}
        for name, value in values.items():
            if actual.get(name, {}).get('/V') != value:
                raise ValueError('Stored PDF field differs from requested value: ' + name)
        for page in check.pages:
            for annotation in page.get('/Annots', []):
                widget = annotation.get_object()
                parts = []
                node = widget
                while node is not None:
                    if node.get('/T'):
                        parts.insert(0, str(node['/T']))
                    parent = node.get('/Parent')
                    node = parent.get_object() if parent else None
                name = '.'.join(parts)
                if name in values:
                    appearance = widget.get('/AP', {}).get('/N')
                    if appearance is None or not appearance.get_object().get_data():
                        raise ValueError('Missing field appearance: ' + name)
        with fitz.open(stream=output, filetype='pdf') as doc:
            found = {}
            for page in doc:
                for widget in page.widgets() or []:
                    if widget.field_name in values:
                        found[widget.field_name] = widget.field_value or ''
            if any(found.get(name) != value for name, value in values.items()):
                raise ValueError('Widget values do not match the form field tree.')
    return output


def populate(data, payload):
    normalized = normalize_blank(data)
    fields = PdfReader(io.BytesIO(normalized)).get_fields() or {}
    values = field_map(fields, payload)
    return (fill_verified(normalized, values), fill_verified(normalized, values, flatten=True), values)
