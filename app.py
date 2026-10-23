import os, json, threading, logging
from datetime import datetime
from flask import Flask, render_template, jsonify, abort
import requests
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

# ──────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────
CFG = {
    'API_KEY'          : os.environ.get('COC_API_KEY', ''),
    'CLAN_TAG'         : '%2320VG8RGL0',
    'CLAN_TAG_RAW'     : '#20VG8RGL0',
    'API_BASE'         : 'https://proxy.royaleapi.dev/v1',
    'TZ'               : 'Europe/Paris',
    'UPDATE_HOURS'     : 6,
    'ROLLING_WARS'     : 10,
    'SEUIL_EXCLU'      : 55,
    'SEUIL_WATCH'      : 75,
}

DATA_DIR  = os.environ.get('DATA_DIR', 'data')
DATA_FILE = os.path.join(DATA_DIR, 'tracker.json')
os.makedirs(DATA_DIR, exist_ok=True)

_data = {
    'last_update': None, 'clan': {}, 'members': {},
    'wars': [], 'sanctions': {'excluded': [], 'watched': []},
    'updating': False, 'error': None,
}
_lock = threading.Lock()

# ──────────────────────────────────────────────────────────
# CoC API
# ──────────────────────────────────────────────────────────
def _api(path, params=None):
    url = CFG['API_BASE'] + path
    try:
        r = requests.get(url, headers={
            'Authorization': f"Bearer {CFG['API_KEY']}",
            'Accept': 'application/json'
        }, params=params, timeout=12)
        if r.status_code == 200:
            return r.json()
        log.warning(f"API {r.status_code}: {path}")
    except Exception as e:
        log.error(f"API error {path}: {e}")
    return None

# ──────────────────────────────────────────────────────────
# GDC Rule Checking
# ──────────────────────────────────────────────────────────
def _best_stars_before(defender_tag, before_order, all_attacks):
    return max((a['stars'] for a in all_attacks
                if a['defenderTag'] == defender_tag and a['order'] < before_order), default=0)

def _first_non3_below(my_pos, before_order, all_attacks, enemy_by_pos):
    for pos in range(my_pos + 1, max(enemy_by_pos.keys(), default=0) + 1):
        e = enemy_by_pos.get(pos)
        if e and _best_stars_before(e['tag'], before_order, all_attacks) < 3:
            return {'tag': e['tag'], 'name': e['name'], 'pos': pos}
    return None

def _check_atk1(member, atk, mirror, all_attacks, enemy_by_pos):
    if not mirror:
        return {'ok': False, 'na': True, 'why': 'Vis-à-vis introuvable'}
    stars = _best_stars_before(mirror['tag'], atk['order'], all_attacks)
    if stars < 3:
        ok = atk['defenderTag'] == mirror['tag']
        return {'ok': ok, 'na': False,
                'why': '' if ok else f"Devait attaquer vis-à-vis ({mirror['name']} pos.{member['mapPosition']})"}
    target = _first_non3_below(member['mapPosition'], atk['order'], all_attacks, enemy_by_pos)
    if not target:
        return {'ok': True, 'na': True, 'why': 'Tout déjà 3★'}
    ok = atk['defenderTag'] == target['tag']
    return {'ok': ok, 'na': False,
            'why': '' if ok else f"Vis-à-vis déjà 3★ → devait attaquer {target['name']} (pos.{target['pos']})"}

def _check_atk2(member, atk, all_attacks, enemy_by_pos):
    target = _first_non3_below(member['mapPosition'], atk['order'], all_attacks, enemy_by_pos)
    if not target:
        return {'ok': True, 'na': True, 'why': 'Tout déjà 3★'}
    ok = atk['defenderTag'] == target['tag']
    return {'ok': ok, 'na': False,
            'why': '' if ok else f"Devait attaquer {target['name']} (pos.{target['pos']})"}

def _analyze_war(war):
    if not war or war.get('state') != 'warEnded':
        return None
    clan  = war.get('clan', {})
    enemy = war.get('opponent', {})

    all_attacks = [a for m in clan.get('members', []) for a in m.get('attacks', [])] + \
                  [a for m in enemy.get('members', []) for a in m.get('attacks', [])]

    enemy_by_pos = {m['mapPosition']: m for m in enemy.get('members', [])}

    cs, es = clan.get('stars', 0), enemy.get('stars', 0)
    cp, ep = clan.get('destructionPercentage', 0), enemy.get('destructionPercentage', 0)
    result = 'win' if (cs > es or (cs == es and cp > ep)) else ('tie' if cs == es and cp == ep else 'lose')

    members_out = {}
    for m in clan.get('members', []):
        tag     = m['tag']
        my_pos  = m['mapPosition']
        mirror  = enemy_by_pos.get(my_pos)
        attacks = sorted(m.get('attacks', []), key=lambda a: a['order'])

        atk_results = []
        compliant = infraction = 0
        for i, atk in enumerate(attacks):
            res = _check_atk1(m, atk, mirror, all_attacks, enemy_by_pos) if i == 0 \
                  else _check_atk2(m, atk, all_attacks, enemy_by_pos)
            defender = enemy_by_pos.get(
                next((mm['mapPosition'] for mm in enemy.get('members', [])
                      if mm['tag'] == atk['defenderTag']), 0), {})
            atk_results.append({
                'stars': atk['stars'],
                'pct': atk.get('destructionPercentage', 0),
                'defenderTag': atk['defenderTag'],
                'defenderName': next((mm['name'] for mm in enemy.get('members', [])
                                      if mm['tag'] == atk['defenderTag']), '?'),
                'defenderPos': next((mm['mapPosition'] for mm in enemy.get('members', [])
                                     if mm['tag'] == atk['defenderTag']), 0),
                'ok': res['ok'], 'na': res.get('na', False), 'why': res['why'],
            })
            if not res.get('na'):
                if res['ok']: compliant += 1
                else:         infraction += 1

        members_out[tag] = {
            'tag': tag, 'name': m['name'], 'mapPosition': my_pos,
            'attacks': atk_results, 'participated': len(attacks),
            'expected': 2, 'compliant': compliant, 'infraction': infraction,
        }

    return {
        'id': war.get('preparationStartTime', ''),
        'date': (war.get('endTime', '') or '')[:10],
        'teamSize': war.get('teamSize', 0),
        'result': result,
        'clanStars': cs, 'enemyStars': es,
        'clanPct': round(cp, 1), 'enemyPct': round(ep, 1),
        'clanName': clan.get('name', ''), 'enemyName': enemy.get('name', ''),
        'members': members_out,
    }

# ──────────────────────────────────────────────────────────
# Score Calculation
# ──────────────────────────────────────────────────────────
def _calc_scores(wars, tags):
    recent = wars[-CFG['ROLLING_WARS']:]
    scores = {}
    for tag in tags:
        participated = expected = compliant = infraction = 0
        for w in recent:
            mw = w['members'].get(tag)
            if mw:
                expected    += mw['expected']
                participated += mw['participated']
                compliant   += mw['compliant']
                infraction  += mw['infraction']
        checkable = compliant + infraction
        comp_pct  = round(compliant / checkable * 100) if checkable else 100
        part_pct  = round(participated / expected * 100) if expected else 100
        scores[tag] = {
            'score': round(comp_pct * 0.5 + part_pct * 0.5),
            'compliance': comp_pct,
            'participation': part_pct,
            'wars_played': sum(1 for w in recent if w['members'].get(tag, {}).get('participated', 0) > 0),
            'wars_window': len(recent),
        }
    return scores

# ──────────────────────────────────────────────────────────
# Main Update
# ──────────────────────────────────────────────────────────
def update_all():
    with _lock:
        if _data.get('updating'):
            return
        _data['updating'] = True
    log.info("=== update_all start ===")
    try:
        clan = _api(f"/clans/{CFG['CLAN_TAG']}")
        if not clan:
            raise Exception("Clan introuvable — vérifiez la clé API")

        war   = _api(f"/clans/{CFG['CLAN_TAG']}/currentwar")
        wlog  = _api(f"/clans/{CFG['CLAN_TAG']}/warlog", {'limit': 20})

        # Players
        tags    = [m['tag'] for m in clan.get('memberList', [])]
        players = {}
        for tag in tags:
            p = _api(f"/players/{tag.replace('#','%23')}")
            if not p:
                continue
            heroes = {h['name']: h.get('level', 0) for h in p.get('heroes', [])
                      if h.get('village') == 'home'}
            m_info = next((m for m in clan.get('memberList', []) if m['tag'] == tag), {})
            players[tag] = {
                'tag': tag, 'name': p['name'],
                'expLevel': p.get('expLevel', 0),
                'trophies': p.get('trophies', 0),
                'townHallLevel': p.get('townHallLevel', 0),
                'role': m_info.get('role', 'member'),
                'donations': m_info.get('donations', 0),
                'heroes': heroes,
            }

        # Analyze current war if ended
        existing = {w['id']: w for w in _data.get('wars', [])}
        wars_list = list(existing.values())

        analyzed = _analyze_war(war)
        if analyzed and analyzed['id'] and analyzed['id'] not in existing:
            wars_list.append(analyzed)
            log.info(f"Nouvelle GDC analysée: {analyzed['date']}")

        wars_list.sort(key=lambda w: w.get('date', ''))

        # Scores
        scores = _calc_scores(wars_list, list(players.keys())) if wars_list else {}
        for tag, p in players.items():
            p.update(scores.get(tag, {
                'score': 100, 'compliance': 100, 'participation': 100,
                'wars_played': 0, 'wars_window': 0,
            }))

        # Warlog summary (win/lose only, no attack detail)
        wlog_items = []
        if wlog:
            for item in wlog.get('items', []):
                if item.get('result') in ('win', 'lose', 'tie'):
                    wlog_items.append({
                        'date': (item.get('endTime', '') or '')[:10],
                        'result': item.get('result'),
                        'teamSize': item.get('teamSize', 0),
                        'clanStars': item.get('clan', {}).get('stars', 0),
                        'enemyStars': item.get('opponent', {}).get('stars', 0),
                        'enemyName': item.get('opponent', {}).get('name', '?'),
                    })

        # Sanctions
        exclu, watch = [], []
        for tag, p in players.items():
            sc = p.get('score', 100)
            entry = {'tag': tag, 'name': p['name'], 'score': sc,
                     'compliance': p.get('compliance', 100),
                     'participation': p.get('participation', 100)}
            if sc < CFG['SEUIL_EXCLU']:   exclu.append(entry)
            elif sc < CFG['SEUIL_WATCH']: watch.append(entry)
        exclu.sort(key=lambda x: x['score'])
        watch.sort(key=lambda x: x['score'])

        now = datetime.now(pytz.timezone(CFG['TZ'])).strftime('%d/%m/%Y %H:%M')
        new = {
            'last_update': now,
            'clan': {
                'name': clan.get('name', ''), 'tag': clan.get('tag', ''),
                'badgeUrls': clan.get('badgeUrls', {}),
                'clanLevel': clan.get('clanLevel', 0),
                'members_count': clan.get('members', 0),
                'warWins': clan.get('warWins', 0),
                'description': clan.get('description', ''),
            },
            'members': players,
            'wars': wars_list,
            'warlog': wlog_items,
            'sanctions': {'excluded': exclu, 'watched': watch},
            'updating': False, 'error': None,
        }
        with _lock:
            _data.update(new)
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
        log.info(f"=== update_all done — {len(players)} membres, {len(wars_list)} GDC ===")
    except Exception as e:
        log.error(f"update_all error: {e}")
        with _lock:
            _data['updating'] = False
            _data['error'] = str(e)

# ──────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', d=_data)

@app.route('/membres')
def membres():
    members = sorted(_data.get('members', {}).values(), key=lambda m: m.get('score', 100))
    return render_template('membres.html', members=members, d=_data)

@app.route('/sanctions')
def sanctions():
    return render_template('sanctions.html', d=_data)

@app.route('/guerres')
def guerres():
    wars = list(reversed(_data.get('wars', [])))
    warlog = _data.get('warlog', [])
    return render_template('guerres.html', wars=wars, warlog=warlog, d=_data)

@app.route('/guerre/<path:war_id>')
def guerre_detail(war_id):
    war = next((w for w in _data.get('wars', []) if w['id'] == war_id), None)
    if not war:
        abort(404)
    members = sorted(war.get('members', {}).values(), key=lambda m: m['mapPosition'])
    return render_template('guerre_detail.html', war=war, members=members, d=_data)

@app.route('/api/update', methods=['POST'])
def api_update():
    t = threading.Thread(target=update_all, daemon=True)
    t.start()
    return jsonify({'status': 'started'})

@app.route('/api/status')
def api_status():
    return jsonify({
        'last_update': _data.get('last_update'),
        'updating': _data.get('updating', False),
        'error': _data.get('error'),
        'members': len(_data.get('members', {})),
        'wars': len(_data.get('wars', [])),
    })

# ──────────────────────────────────────────────────────────
# Jinja filters
# ──────────────────────────────────────────────────────────
@app.template_filter('score_color')
def score_color(score):
    if score < CFG['SEUIL_EXCLU']:   return '#e74c3c'
    if score < CFG['SEUIL_WATCH']:   return '#f39c12'
    if score >= 90:                   return '#c8aa6e'
    return '#2ecc71'

@app.template_filter('role_fr')
def role_fr(role):
    return {'leader': 'Chef', 'coLeader': 'Co-chef',
            'admin': 'Ancien', 'member': 'Membre'}.get(role, role)

@app.template_filter('th_emoji')
def th_emoji(level):
    return '🏰' if level >= 16 else ('🏯' if level >= 12 else '🛖')

# ──────────────────────────────────────────────────────────
# Startup
# ──────────────────────────────────────────────────────────
def _load():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding='utf-8') as f:
                saved = json.load(f)
            with _lock:
                _data.update(saved)
                _data['updating'] = False
            log.info(f"Données chargées: {len(_data.get('members',{}))} membres")
        except Exception as e:
            log.error(f"Load error: {e}")

_load()

scheduler = BackgroundScheduler(timezone=pytz.timezone(CFG['TZ']))
scheduler.add_job(update_all, 'interval', hours=CFG['UPDATE_HOURS'], id='update_all')
scheduler.start()

if not _data.get('clan'):
    threading.Thread(target=update_all, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
