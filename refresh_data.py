#!/usr/bin/env python3
"""Pipeline 100% automatico de 'Avance de Cobranza' (segmentacion Segmento x DPD) -- sin Claude
de por medio, corre en GitHub Actions cada 2 horas. Descubre las pestanas "Base NN-mmm" del Sheet
de Cobranza automaticamente (solo las del esquema NUEVO de segmentacion, identificadas por sus
columnas -- las pestanas viejas de antes del 29-sep-2026 tienen un esquema completamente distinto
y se ignoran a proposito). Mismo patron de arquitectura que ocn-dashboard-growth /
ocn-avance-marcacion-v2: columnas siempre por NOMBRE de encabezado, nunca por posicion fija,
porque Ricardo reordena columnas a mano seguido."""
import os, re, json, urllib.request, urllib.parse, unicodedata
from collections import Counter, defaultdict
from datetime import date
from zoneinfo import ZoneInfo

MX_TZ = ZoneInfo('America/Mexico_City')

def datetime_now_mx_date():
    # NUNCA usar date.today() en GitHub Actions (corre en UTC) -- Mexico va 6h atras, asi que
    # desde las 6pm CDMX date.today() ya muestra el dia siguiente. Ver mismo bug documentado en
    # ocn-dashboard-growth (31-ago-2026).
    import datetime as _dt
    return _dt.datetime.now(MX_TZ).date().isoformat()

SHEET_ID = '1DoontdrZJsk5VGJ44HPSghYow1kViiTBvaSmo4KWJHU'
TAB_RE = re.compile(r'^Base (\d{2})-([a-z]{3})$')
# Pestanas del esquema nuevo que se excluyen a proposito del reporte (pedido explicito de
# Ricardo, 30-sep-2026 -- "Base 29-sep" ya no se muestra, aunque tecnicamente calificaria).
EXCLUDE_TABS = {'Base 29-sep'}
MESES = {'ene':1,'feb':2,'mar':3,'abr':4,'may':5,'jun':6,'jul':7,'ago':8,'sep':9,'oct':10,'nov':11,'dic':12}
MESES_INV = {v: k for k, v in MESES.items()}
REQUIRED_COLS = ['VIN', 'Segmento', 'Ventana DPD', 'Asesor Asignado', 'Status Calificado por Asesor', 'Equipo']

PRIORIDAD_VENTANA = [
    '8+ (Recovery)', 'lunes (7)', 'sáb-dom (5-6)', 'jue-vie (3-4)',
    'mar-mié (1-2)', 'lunes (0)', 'jue-dom (-4 a -1)', '(sin ventana / sin cruce)',
]

# ---------- Auth ----------
def get_access_token():
    data = urllib.parse.urlencode({
        'client_id': os.environ['GOOGLE_CLIENT_ID'],
        'client_secret': os.environ['GOOGLE_CLIENT_SECRET'],
        'refresh_token': os.environ['GOOGLE_REFRESH_TOKEN'],
        'grant_type': 'refresh_token',
    }).encode()
    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data, method='POST')
    return json.loads(urllib.request.urlopen(req).read())['access_token']

def sheets_get(token, path, params=None):
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    return json.loads(urllib.request.urlopen(req).read())

# ---------- Clasificacion de las etiquetas de "Status Calificado por Asesor" ----------
def norm_key(s):
    if not s:
        return ''
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', s.strip().lower())

# Clasificacion propia (no viene del Sheet) -- pendiente que Ricardo la confirme o ajuste, mismo
# criterio que ya se aplico en "Avance de Marcacion" (ver ese repo). good=resuelto positivo,
# warn=en proceso/seguimiento, bad=critico/riesgo, neutral=no aplica a cobranza en si.
GOOD = {norm_key(x) for x in [
    'Driver al corriente', 'Pago realizado en gestion', 'Promesa de pago',
    'Recuperado por Strategic Recovery', 'Ya hizo devolución voluntaria', 'Ya tiene Ademdum',
]}
WARN = {norm_key(x) for x in [
    'Se Gestiona Adendum', 'En Seguimiento / Llamar después', 'Aclaración de pago',
    'Ligas pendientes de envío', 'En proceso de Recuperación Forzosa',
    'En proceso de Recuperación Voluntaria', 'Gestorías (Verificación, Tarjeta de circulación, Multas)',
    'Contrato nuevo',
]}
BAD = {norm_key(x) for x in [
    'Sin actividad/ingresos', 'Problemas personales / emergencia', 'Corralón',
    'Corralón (Tarjeta Circulación y Multas)', 'Taller', 'Seguros/Siniestro', 'Pérdida Total',
    'Legal - Denuncias', 'Legal - NUC', 'Legal - Unidad Vendida por Driver',
    'Legal - Robo ilocalizable', 'Legal - Robo localizado', 'Ilocalizable', 'Incongruencia en Data',
]}

def classify(status):
    k = norm_key(status)
    if not k:
        return 'neutral'
    if k in GOOD: return 'good'
    if k in BAD: return 'bad'
    if k in WARN: return 'warn'
    return 'neutral'

def load_tab_rows(values):
    if not values or len(values) < 2:
        return None
    header = values[0]
    idx = {}
    for name in header:
        if name not in idx:
            idx[name] = header.index(name)
    missing = [c for c in REQUIRED_COLS if c not in idx]
    if missing:
        return None  # esquema viejo / distinto -- se ignora esta pestana

    def g(row, name, default=''):
        i = idx.get(name)
        if i is None or i >= len(row):
            return default
        return str(row[i]).strip()

    out = []
    for r in values[1:]:
        if not g(r, 'VIN'):
            continue
        out.append({
            'vin': g(r, 'VIN'), 'segmento': g(r, 'Segmento') or 'E SIN CRUCE',
            'ventana': g(r, 'Ventana DPD') or '(sin ventana / sin cruce)',
            'equipo': g(r, 'Equipo'), 'asesor': g(r, 'Asesor Asignado'),
            'status_calif': g(r, 'Status Calificado por Asesor'),
            'contacto': g(r, 'Contacto'), 'comentarios': g(r, 'Comentarios'),
            'voluntad': g(r, '¿Tiene Voluntad de Pago?'),
        })
    return out

def is_avanzado(row):
    return bool(row['status_calif'] or row['contacto'] or row['comentarios'])

def compute_base(rows):
    total = len(rows)
    avanzado = sum(1 for r in rows if is_avanzado(r))

    equipo_stats = {}
    for eq in sorted({r['equipo'] for r in rows if r['equipo']}, key=lambda x: (len(x), x)):
        erows = [r for r in rows if r['equipo'] == eq]
        etotal = len(erows)
        eav = sum(1 for r in erows if is_avanzado(r))
        equipo_stats[eq] = {'avanzado': eav, 'total': etotal, 'pct': (eav / etotal * 100 if etotal else 0)}

    ventana_stats = {}
    for v in PRIORIDAD_VENTANA:
        vrows = [r for r in rows if r['ventana'] == v]
        vtotal = len(vrows)
        vav = sum(1 for r in vrows if is_avanzado(r))
        if vtotal:
            ventana_stats[v] = {'avanzado': vav, 'total': vtotal, 'pct': (vav / vtotal * 100 if vtotal else 0)}

    segmento_order = ['NUEVO', 'A', 'B', 'C', 'D', 'E SIN CRUCE']
    segmento_stats = {}
    for s in segmento_order:
        srows = [r for r in rows if r['segmento'] == s]
        stotal = len(srows)
        sav = sum(1 for r in srows if is_avanzado(r))
        if stotal:
            segmento_stats[s] = {'avanzado': sav, 'total': stotal, 'pct': (sav / stotal * 100 if stotal else 0)}

    # Heatmap Segmento x Ventana DPD -- mismo cruce de la "Treatment by segment x DPD" de la
    # estrategia (Customer Care Structure.pdf, slide 7), pero mostrando volumen + % ya trabajado
    # en vez del tratamiento sugerido (el tratamiento ya vive en la columna "Tratamiento sugerido"
    # de cada caso, no se repite aqui). Pedido explicito de Ricardo 5-oct-2026.
    heatmap = {}
    for s in segmento_order:
        fila = {}
        for v in PRIORIDAD_VENTANA:
            celda_rows = [r for r in rows if r['segmento'] == s and r['ventana'] == v]
            ctotal = len(celda_rows)
            if not ctotal:
                continue
            cav = sum(1 for r in celda_rows if is_avanzado(r))
            fila[v] = {'total': ctotal, 'avanzado': cav, 'pct': (cav / ctotal * 100 if ctotal else 0)}
        if fila:
            heatmap[s] = fila

    con_voluntad = sum(1 for r in rows if norm_key(r['voluntad']) == norm_key('Sí'))
    con_promesa = sum(1 for r in rows if norm_key(r['status_calif']) == norm_key('Promesa de pago'))

    status_counter = Counter(r['status_calif'] for r in rows if r['status_calif'])
    status_total = sum(status_counter.values())
    mix_class_totals = Counter()
    status_list = []
    for status, count in status_counter.most_common():
        cls = classify(status)
        mix_class_totals[cls] += count
        status_list.append({'status': status, 'count': count, 'pct': (count / status_total * 100 if status_total else 0), 'class': cls})
    mix_legend = {cls: (mix_class_totals.get(cls, 0) / status_total * 100 if status_total else 0) for cls in ['good', 'warn', 'bad', 'neutral']}

    asesor_stats = defaultdict(lambda: {'total': 0, 'avanzado': 0, 'equipo': '', 'mix_class': Counter()})
    for r in rows:
        a = r['asesor']
        if not a:
            continue
        st = asesor_stats[a]
        st['total'] += 1
        if is_avanzado(r):
            st['avanzado'] += 1
        st['equipo'] = r['equipo']
        if r['status_calif']:
            st['mix_class'][classify(r['status_calif'])] += 1

    asesores_list = []
    for name, st in asesor_stats.items():
        pct = st['avanzado'] / st['total'] * 100 if st['total'] else 0
        mix_total = sum(st['mix_class'].values())
        mix = {cls: (st['mix_class'].get(cls, 0) / mix_total * 100 if mix_total else 0) for cls in ['good', 'warn', 'bad', 'neutral']}
        asesores_list.append({
            'name': name, 'equipo': st['equipo'], 'total': st['total'], 'avanzado': st['avanzado'],
            'pct': pct, 'mix': mix,
        })

    return {
        'total': total, 'avanzado': avanzado, 'pct': (avanzado / total * 100 if total else 0),
        'equipo_stats': equipo_stats, 'ventana_stats': ventana_stats, 'segmento_stats': segmento_stats,
        'heatmap': heatmap,
        'con_voluntad': con_voluntad, 'con_promesa': con_promesa,
        'status_list': status_list[:12], 'mix_legend': mix_legend, 'status_total': status_total,
        'asesores_list': asesores_list, 'n_total_asesores': len(asesores_list),
    }

def main():
    token = get_access_token()

    meta = sheets_get(token, '', params={'fields': 'sheets.properties.title'})
    all_titles = [s['properties']['title'] for s in meta['sheets']]

    candidatos = []
    for title in all_titles:
        if title in EXCLUDE_TABS:
            continue
        m = TAB_RE.match(title)
        if not m:
            continue
        dia, mes_txt = m.groups()
        mes = MESES.get(mes_txt)
        if not mes:
            continue
        candidatos.append((date(2026, mes, int(dia)), title))
    candidatos.sort()
    print('Pestanas "Base NN-mmm" detectadas:', [t for _, t in candidatos])

    bases = {}
    base_order = []
    for d, tab in candidatos:
        rng = urllib.parse.quote(f"'{tab}'!A1:AB10000")
        data = sheets_get(token, f'/values/{rng}')
        rows = load_tab_rows(data.get('values', []))
        if rows is None:
            print(f'  {tab}: esquema viejo o incompleto, se ignora')
            continue
        key = f'{d.day:02d}{MESES_INV[d.month]}'
        res = compute_base(rows)
        res['label'] = f'Base {d.day:02d}-{MESES_INV[d.month]}'
        res['date_iso'] = d.isoformat()
        bases[key] = res
        base_order.append(key)
        print(f"  {tab}: total={res['total']} avanzado={res['avanzado']} ({res['pct']:.1f}%)")

    base_dates = {}
    base_short = {}
    for d, tab in candidatos:
        key = f'{d.day:02d}{MESES_INV[d.month]}'
        if key not in bases:
            continue
        base_dates[key] = tab.replace('Base ', '')
        base_short[key] = f'{d.day} {MESES_INV[d.month]}'

    payload = {
        'baseOrder': base_order,
        'baseDates': base_dates,
        'baseShort': base_short,
        'bases': bases,
        'corte': datetime_now_mx_date(),
    }

    with open('template.html', encoding='utf-8') as f:
        template = f.read()
    html = template.replace('__DATA_JSON__', json.dumps(payload, ensure_ascii=False))
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print('index.html generado,', len(html), 'bytes')

if __name__ == '__main__':
    main()
