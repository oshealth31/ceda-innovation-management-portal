#!/usr/bin/env python3
"""Drive a running portal through the full CEDA idea lifecycle and capture screenshots.

    python scripts/screenshots.py http://localhost:8000 ADMIN_PASSWORD DEMO_PASSWORD out_dir

Sets up data through the API (standard library), then logs in through the real UI with Playwright.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, ADMIN_PW, DEMO_PW, OUT = sys.argv[1].rstrip('/'), sys.argv[2], sys.argv[3], Path(sys.argv[4])
OUT.mkdir(parents=True, exist_ok=True)


def call(method, path, token=None, body=None, form=None, file=None):
    headers, data = {}, None
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    elif form is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        data = urllib.parse.urlencode(form).encode()
    elif file is not None:
        name, content = file
        b = uuid.uuid4().hex
        headers['Content-Type'] = f'multipart/form-data; boundary={b}'
        data = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                f'Content-Type: text/plain\r\n\r\n').encode() + content + f'\r\n--{b}--\r\n'.encode()
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read() or b'null')
    except urllib.error.HTTPError as e:
        raise SystemExit(f'{method} {path} -> {e.code}: {e.read().decode()[:300]}')


for _ in range(30):
    try:
        call('GET', '/api/health')
        break
    except Exception:
        time.sleep(2)


def login(user, pw):
    return call('POST', '/api/token', form={'username': user, 'password': pw})['access_token']


admin = login('admin', ADMIN_PW)
call('POST', '/api/users', admin, {'username': 'evaluator_thato', 'password': DEMO_PW, 'role': 'officer'})
ids = {u['username']: u['id'] for u in call('GET', '/api/users', admin)}
sub, ev1, ev2, adj = login('applicant1', DEMO_PW), login('officer1', DEMO_PW), login('evaluator_thato', DEMO_PW), login('committee1', DEMO_PW)
prog = call('GET', '/api/programmes', sub)[0]['id']
crit = call('GET', '/api/score-criteria', admin)

DOC = (b'Idea summary\nArtisans in rural Botswana struggle to reach buyers. Our platform lists their crafts online, '
       b'handles mobile-money payments and arranges courier delivery through Botswana Post.\nMarket: tourists, '
       b'export buyers and local gift shops. Team: two developers and a logistics coordinator with experience.\n')


def new_idea(title, problem, solution, users, sector, stage, team):
    a = call('POST', '/api/applications', sub, {'applicant_name': team, 'funding_request': 150000, 'owner_contribution': 20000,
                                                'programme_id': prog, 'idea_title': title, 'problem': problem, 'solution': solution,
                                                'target_users': users, 'sector': sector, 'stage': stage})
    call('POST', f"/api/applications/{a['id']}/analyse-file", sub, file=('idea.txt', DOC))
    return a['id']


def score(token, app_id, values):
    call('PUT', f'/api/applications/{app_id}/scores', token,
         {'scores': [{'criterion_id': c['id'], 'score': values[c['key']], 'comment': values.get('note', '') if c['key'] == 'innovation' else ''}
                     for c in crit]})


# Idea A: awarded journey (two evaluators disagree, referred, awarded)
A = new_idea('Kgotla Crafts: online marketplace for rural artisans', 'Rural artisans cannot reach buyers beyond their village.',
             'A mobile-first marketplace with mobile-money payments and courier delivery.', 'Artisans, tourists, gift shops',
             'Digital and ICT', 'Prototype', 'Kgotla Digital (Pty) Ltd')
call('PUT', f'/api/applications/{A}/assignments', admin, {'reviewer_ids': [ids['officer1'], ids['evaluator_thato']]})
score(ev1, A, {'innovation': 5, 'feasibility': 4, 'impact': 5, 'team': 4, 'alignment': 5, 'note': 'Strong local angle.'})
score(ev2, A, {'innovation': 3, 'feasibility': 2, 'impact': 3, 'team': 3, 'alignment': 4, 'note': 'Courier costs unclear.'})
call('POST', f'/api/applications/{A}/decision', ev1, {'decision': 'refer_committee', 'rationale': 'Scored well overall; panel split on feasibility.'})

# Idea B: sent back for revision with feedback
B = new_idea('AgriSense: soil moisture alerts by SMS', 'Farmers over- or under-water crops and lose yield.',
             'Low-cost soil sensors that send SMS alerts and weekly advice.', 'Smallholder farmers', 'Agriculture', 'Idea',
             'AgriSense Botswana')
call('PUT', f'/api/applications/{B}/assignments', admin, {'reviewer_ids': [ids['officer1']]})
call('POST', f'/api/applications/{B}/decision', ev1, {'decision': 'request_information',
     'rationale': 'Promising idea. Please add the cost of one sensor unit and explain how farmers will pay for SMS alerts.'})

# Idea C: under panel review, waiting for evaluator Thato to score (used for the scoring-form screenshot)
C = new_idea('SafeRide: verified taxi booking for Gaborone', 'Commuters have no way to verify combi and taxi drivers.',
             'A booking app with verified drivers, fare estimates and trip sharing.', 'Commuters and drivers in Gaborone',
             'Transport and logistics', 'Pilot', 'SafeRide Tech')
call('PUT', f'/api/applications/{C}/assignments', admin, {'reviewer_ids': [ids['evaluator_thato']]})

# ---- 1.4 to 1.6 features: content, extra questions, chat consultant, a support request with a reply
call('PUT', '/api/admin/content', admin, {'announcement': 'Submissions for the 2026 Innovation Challenge close on 23 November at 17:00.',
                                          'chat_open': '00:00', 'chat_close': '23:59', 'support_email': 'innovation@example.com'})
call('POST', '/api/form-fields', admin, {'key': 'team_size', 'label': 'How many people are in your team?', 'kind': 'number',
                                        'required': True, 'sort': 1})
call('POST', '/api/form-fields', admin, {'key': 'district', 'label': 'District where the idea will first be used', 'kind': 'select',
                                        'options': ['Gaborone', 'Francistown', 'Kweneng', 'Central', 'North-West', 'Other'], 'sort': 2})
call('POST', '/api/form-fields', admin, {'key': 'youth_led', 'label': 'Is the team youth-led (18 to 35)?', 'kind': 'yes_no', 'sort': 3})
call('PATCH', f"/api/users/{ids['officer1']}", admin, {'chat_agent': True})
call('PUT', '/api/me/preferences', sub, {'gender': 'female', 'birth_year': 1998, 'district': 'Kweneng'})
cats = call('GET', '/api/public/help')['support_categories']
t = call('POST', '/api/support/tickets', sub, {'category': cats[2]['key'], 'subject': 'Upload keeps failing',
                                               'message': 'My concept note will not upload. It is a scanned PDF.'})
call('POST', f"/api/support/tickets/{t['id']}/reply", admin,
     {'response': 'Scanned PDFs have no text we can read. Please upload the Word version or a PDF made from your document.'})

shots = []


def snap(page, name, caption, card=None):
    """Viewport shot, or a shot of one card when `card` names its heading."""
    page.wait_for_timeout(700)
    path = str(OUT / f'{len(shots) + 1:02d}_{name}.png')
    if card:
        page.locator(f'.card:has(> h3:text-is("{card}"))').first.screenshot(path=path)
    else:
        page.screenshot(path=path)
    shots.append({'file': f'{len(shots) + 1:02d}_{name}.png', 'caption': caption})


def ui_login(page, user, pw):
    page.goto(BASE + '/')
    page.evaluate('sessionStorage.clear()')
    page.goto(BASE + '/')
    page.fill('#username', user)
    page.fill('#password', pw)
    page.click('#loginForm button[type=submit]')
    page.wait_for_selector('#portal:not(.hidden)')
    page.wait_for_timeout(600)


def nav(page, name):
    page.click(f'#nav [data-page="{name}"]')
    page.wait_for_timeout(700)


def open_idea(page, title_part):
    nav(page, 'applications')
    page.click(f'tr:has-text("{title_part}") [data-action="open-app"]')
    page.wait_for_selector('#page h2')
    page.wait_for_timeout(700)


with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={'width': 1280, 'height': 900}, device_scale_factor=1.5)
    page.goto(BASE + '/')
    snap(page, 'login', 'Sign-in page')

    ui_login(page, 'applicant1', DEMO_PW)
    snap(page, 'submitter_dashboard', 'Submitter dashboard: my ideas and their status')
    nav(page, 'new')
    page.fill('input[name=applicant_name]', 'Thuso Solar (Pty) Ltd')
    page.fill('input[name=idea_title]', 'Pay-as-you-go solar kits for rural homes')
    page.fill('textarea[name=problem]', 'Many rural households still rely on paraffin and candles for light.')
    snap(page, 'submit_form', 'Submit an idea: required fields are marked * and checked by the server')
    open_idea(page, 'AgriSense')
    snap(page, 'submitter_sent_back', 'Submitter view of an idea sent back for revision: progress bar, notice and edit form')
    snap(page, 'submitter_feedback', 'The timeline shows the evaluator feedback to the submitter', card='Timeline')

    ui_login(page, 'admin', ADMIN_PW)
    snap(page, 'admin_dashboard', 'Administrator dashboard: ideas at every stage')
    open_idea(page, 'SafeRide')
    snap(page, 'admin_idea', 'Evaluator, adjudicator and administrator idea page: progress bar and idea details')
    snap(page, 'admin_assign', 'Administrator assigns evaluators to an idea', card='Evaluators')

    ui_login(page, 'evaluator_thato', DEMO_PW)
    nav(page, 'applications')
    snap(page, 'evaluator_list', 'Evaluator sees only the ideas assigned to them')
    open_idea(page, 'SafeRide')
    for i, sel in enumerate(page.query_selector_all('form[data-form="scores"] select')):
        sel.select_option(str([4, 4, 5, 3, 4][i % 5]))
    snap(page, 'evaluator_scoring', 'Evaluator scores each criterion from 1 to 5, independently', card='Scoring')

    ui_login(page, 'committee1', DEMO_PW)
    nav(page, 'committee')
    snap(page, 'adjudication_queue', 'Adjudication queue')
    open_idea(page, 'Kgotla Crafts')
    snap(page, 'adjudicator_panel', 'Adjudicator sees every evaluator\'s scores, the average and a disagreement warning', card='Scoring')
    snap(page, 'adjudicator_decision', 'Adjudicator records the decision with feedback for the submitter', card='Decision')

    call('POST', f'/api/applications/{A}/decision', adj, {'decision': 'approve', 'rationale': 'Awarded. Strong local impact; agree a courier cost plan in the first month.'})
    ui_login(page, 'applicant1', DEMO_PW)
    open_idea(page, 'Kgotla Crafts')
    snap(page, 'submitter_awarded', 'Submitter sees the award on the progress bar')
    snap(page, 'submitter_awarded_feedback', 'Award feedback in the submitter timeline', card='Timeline')

    ui_login(page, 'admin', ADMIN_PW)
    nav(page, 'scoring')
    snap(page, 'scoring_criteria', 'Administrator configures scoring criteria and weights without code changes')
    nav(page, 'management')
    snap(page, 'management', 'Management dashboard: status counts and programme KPIs')
    nav(page, 'audit')
    page.click('[data-action="audit-verify"]')
    snap(page, 'audit', 'Tamper-evident audit trail with an integrity check')
    nav(page, 'users')
    snap(page, 'users', 'User management with the four CEDA roles')

    # ---- 1.4 to 1.6 screens
    def signed_out(pg):
        pg.goto(BASE + '/')
        pg.evaluate('sessionStorage.clear()')
        pg.goto(BASE + '/')
        pg.wait_for_timeout(600)

    signed_out(page)
    page.click('[data-auth="help"]')
    page.wait_for_timeout(900)
    snap(page, 'public_help', 'Help and FAQs, open to everyone from the sign-in page, with regulator contacts')
    signed_out(page)
    page.click('[data-auth="register"]')
    page.fill('#authAlt [name=full_name]', 'Neo Molefe')
    page.fill('#authAlt [name=username]', 'neo.molefe')
    page.fill('#authAlt [name=email]', 'neo.molefe@example.com')
    page.fill('#authAlt [name=phone]', '71 234 567')
    page.fill('#authAlt [name=password]', DEMO_PW)
    page.check('#authAlt [name=accept_privacy]')
    snap(page, 'register', 'Anyone can create an account')
    page.click('#authAlt button[type=submit]')
    page.wait_for_selector('form[data-auth-form="verify"]')
    snap(page, 'verify', 'The account works once the emailed 6-digit code is confirmed (shown on screen in demo mode)')
    code = page.inner_text('#loginOk .demo-code')
    page.fill('[name=code]', code)
    page.click('#authAlt button[type=submit]')
    page.wait_for_selector('#portal:not(.hidden)')
    page.wait_for_timeout(800)
    snap(page, 'new_user_dashboard', 'A new submitter sees the announcement and welcome message')
    nav(page, 'new')
    page.locator('[name=x_team_size]').scroll_into_view_if_needed()
    page.fill('[name=x_team_size]', '3')
    page.select_option('[name=x_district]', 'Kweneng')
    snap(page, 'extra_questions', 'Extra questions the administrator added to the idea form')
    nav(page, 'settings')
    snap(page, 'alerts', 'Contact details, free email and SMS alerts, and an optional sign-in code', card='Contact details and alerts')

    ui_login(page, 'applicant1', DEMO_PW)
    nav(page, 'support')
    snap(page, 'support', 'Support: reply times for each kind of question, and the answer to an earlier request')
    nav(page, 'help')
    page.click('details.faq summary')
    snap(page, 'help', 'Help and FAQs inside the portal')

    agent = b.new_page(viewport={'width': 1280, 'height': 900}, device_scale_factor=1.5)
    ui_login(agent, 'officer1', DEMO_PW)
    nav(agent, 'chat')  # opening the console puts the consultant online
    agent.wait_for_timeout(1500)
    nav(page, 'dashboard')
    page.click('.chat-fab')
    page.wait_for_selector('form[data-form="chat-start"]')
    page.fill('form[data-form="chat-start"] [name=topic]', 'Deadline')
    page.fill('form[data-form="chat-start"] [name=message]', 'Hello, can I still change my idea after I submit it?')
    page.click('form[data-form="chat-start"] button[type=submit]')
    page.wait_for_selector('#chatWho')
    agent.wait_for_selector('[data-action="con-open"]')
    agent.click('[data-action="con-open"]')
    agent.wait_for_selector('[data-action="con-claim"]')
    agent.click('[data-action="con-claim"]')
    agent.wait_for_selector('form[data-form="con-send"] textarea')
    agent.fill('form[data-form="con-send"] textarea', 'Hi! Not while it is with the panel, but if we send it back you can edit and resubmit.')
    agent.press('form[data-form="con-send"] textarea', 'Enter')
    agent.wait_for_timeout(1500)
    page.wait_for_timeout(4000)
    page.locator('#chatPanel').screenshot(path=str(OUT / f'{len(shots) + 1:02d}_chat_user.png'))
    shots.append({'file': f'{len(shots) + 1:02d}_chat_user.png', 'caption': 'Live chat from the submitter side'})
    agent.wait_for_timeout(4500)
    snap(agent, 'chat_console', 'Chat consultant console: queue, conversation, hand-over and statistics')
    agent.close()
    page.click('.chat-x')

    ui_login(page, 'admin', ADMIN_PW)
    nav(page, 'reports')
    snap(page, 'reports_top', 'Reports: totals, youth and women share, time per step')
    page.locator('.report-grid').first.scroll_into_view_if_needed()
    snap(page, 'reports_breakdowns', 'Reports: breakdowns by sector, stage, gender, age, district and call')
    nav(page, 'formbuilder')
    snap(page, 'form_builder', 'Administrator adds questions to the idea form without code')
    nav(page, 'content')
    snap(page, 'site_content', 'Site content: announcement, support details, reply times and chat hours')
    page.locator('form[data-form="theme"]').scroll_into_view_if_needed()
    snap(page, 'look_and_feel', 'Look and feel: portal name and colours, with a readability check')
    nav(page, 'messages')
    snap(page, 'message_log', 'Message log: every email and SMS, with codes hidden')

    m = b.new_page(viewport={'width': 390, 'height': 844}, device_scale_factor=2, is_mobile=True)
    ui_login(m, 'applicant1', DEMO_PW)
    m.eval_on_selector('#nav [data-page="applications"]', 'e => e.click()')  # menu is a slide-out drawer on phones
    m.wait_for_timeout(900)
    m.eval_on_selector('tr:has-text("AgriSense") [data-action="open-app"]', 'e => e.click()')
    m.wait_for_selector('#page h2')
    m.wait_for_timeout(900)
    snap(m, 'mobile', 'The same submitter page on a phone')

    # ---- responsive pass: the same pages at five screen sizes
    R = OUT / 'responsive'
    R.mkdir(exist_ok=True)
    sizes = [('phone', 360, 780, True), ('tablet', 768, 1024, True), ('laptop', 1024, 768, False),
             ('desktop', 1440, 900, False), ('wide', 1920, 1080, False)]

    def go(pg, page_name):
        pg.eval_on_selector(f'#nav [data-page="{page_name}"]', 'e => e.click()')
        pg.wait_for_timeout(900)

    def open_js(pg, title_part):
        go(pg, 'applications')
        pg.eval_on_selector(f'tr:has-text("{title_part}") [data-action="open-app"]', 'e => e.click()')
        pg.wait_for_selector('#page h2')
        pg.wait_for_timeout(900)

    for label, w, h, mobile in sizes:
        pg = b.new_page(viewport={'width': w, 'height': h}, device_scale_factor=2 if mobile else 1, is_mobile=mobile, has_touch=mobile)
        full = w <= 768
        ui_login(pg, 'admin', ADMIN_PW)
        pg.screenshot(path=str(R / f'{label}_1_dashboard.png'), full_page=full)
        go(pg, 'applications')
        pg.screenshot(path=str(R / f'{label}_2_ideas.png'), full_page=full)
        open_js(pg, 'Kgotla Crafts')
        pg.screenshot(path=str(R / f'{label}_3_review.png'), full_page=False)
        if mobile:
            pg.click('[data-action="menu"].mobile-menu')
            pg.wait_for_timeout(500)
            pg.screenshot(path=str(R / f'{label}_4_menu.png'))
        pg.close()
    b.close()

(OUT / 'captions.json').write_text(json.dumps(shots, indent=1))
print(f'{len(shots)} screenshots written to {OUT}')
