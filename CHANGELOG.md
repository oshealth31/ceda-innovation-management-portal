# Changelog

Version number lives in one place: the `VERSION` file. `backend/app.py` reads it; `/api/health`, reports and the Docker image all follow.

## 1.6.0 - 2026-11-19
Reports module (`backend/reports.py`).
- **Reports page** for administrators, evaluators and adjudicators: ideas, approval rate, youth and women's share, funding requested; ideas per month; where ideas are now; and breakdowns by sector, stage, gender, age group, district and call, each showing approved and declined.
- **How long each step takes**: average days from submission to first review, in evaluation, with adjudicators and to decision, plus ideas waiting more than 30 days and the share sent back for more information.
- **Spreadsheet downloads**: every idea as one row (with answers to the extra form questions), and the summary tables. Text that looks like a formula is neutralised so it cannot run in Excel.
- Filters by date range and call, and a print layout.
- **Live chat** (`backend/chat.py`): a chat button for signed-in users and a Live chat console for consultants. The administrator marks staff as chat consultants on the Users page. Chat is open on working days during set hours (09:00 to 16:00 Botswana time by default, with public holidays closed) while at least one consultant is online; otherwise people see the away message and can leave a message, which becomes a support request. Several consultants share one queue, take chats, hand them over and end them. A chat nobody takes within 10 minutes becomes a support request automatically. People can rate a chat. Statistics: chats, answer rate, missed chats, average wait and length, rating, chats per consultant and busiest hours.
- **Look and feel**: line icons on every menu item and key buttons, the menu grouped into sections (Work, Insight, Help, Set-up, System), and a Look and feel card where the administrator sets the portal name and colours to the brand guide. Colours too light for white text (below WCAG 4.5:1 contrast) are refused, with a live preview while choosing.
- **One-step installers** (`scripts/install.sh`, `scripts/install.ps1`) and an install guide (`docs/INSTALL.md`); CI now runs the installer from a fresh checkout and signs in with the password it creates. Docker Compose passes the email, SMS and code settings.
- **Submission checklist** (`docs/SUBMISSION_CHECKLIST.md`).
- **Hosting plan** (`docs/HOSTING.md`): recommended production setup in AWS Africa (Cape Town) with two application copies and a Multi-AZ database, monthly cost estimate, 24/7 availability targets, and how a copy is kept in Botswana under the Data Protection Act, 2024, with an all-in-Botswana alternative.
- **Optional profile details** at sign-up and in Settings: gender, year of birth and district, used only as totals in reports.

## 1.5.0 - 2026-11-13
Help, support and content management. Existing databases are upgraded on start-up (new tables, and one new column on ideas).
- **Help and FAQs**: questions and answers grouped by topic, plus a directory of regulators and partners (CEDA, CIPA, BOBS, BOCRA, CCA, BURS, NBFIRA). Open to everyone from the sign-in page, without an account. Contact details were checked against the organisations' published notices on 13 November 2026.
- **Support requests with reply times**: each kind of question has its own reply time in working days. Submitters send questions and see replies; administrators and evaluators see every request, which ones are overdue, and the share answered on time. Replies arrive as a notification and an email/SMS alert.
- **Site content** for administrators: the dashboard announcement, welcome text, help introduction, support hours and contacts, support categories and reply times, FAQs and the contacts directory can all be changed in the portal. Every change is in the audit trail.
- **Application form builder**: administrators add their own questions to the idea form (short or long answer, number, choose one, yes/no, date, web link), mark them required, reorder or hide them. Answers are checked by the server and shown with the idea.

## 1.4.0 - 2026-11-13
Accounts and alerts, to meet the updated judging criteria. Existing databases are upgraded on start-up (new user columns are added automatically).
- **Create an account**: anyone can register as a submitter with name, username, email and optional mobile number. The account works once the 6-digit code emailed to them is confirmed. Can be switched off with `ALLOW_SELF_REGISTRATION=false`.
- **Forgotten password**: a reset code goes to the account's email (or mobile if there is no email). The answer is the same whether or not the account exists. Resetting signs out every other session.
- **One-time code at sign-in**: users can choose to be asked for an emailed code each time they sign in.
- **Sign in with username or email.**
- **Free alerts by email and SMS**: users choose in Settings. Every status change that already created an in-portal notification now also sends an alert to those who subscribed.
- **Message log** for administrators: every email and SMS, its status and any error. Codes are never stored; they are masked in the log.
- Codes are 6 digits, stored only as a keyed hash, single use, expire (10 minutes for sign-in, 30 otherwise) and stop working after 5 wrong tries. Requests are rate limited.
- Email through Brevo's API or any SMTP server; SMS through any HTTP gateway. With neither configured, messages are recorded in the log, and `DEMO_SHOW_CODES=true` shows codes on screen for demos.

## 1.3.1 - 2026-11-11
- Phones: no empty band above pages; the menu closes by tapping outside it, with its x button, or with the phone's Back button.
- Releases now reach phones straight away: the page, styles and script are served with `Cache-Control: no-cache`, and the page asks for `styles.css` and `app.js` by version, so a browser can never pair a new page with an old stylesheet.

## 1.3.0 - 2026-11-08
CEDA Innovation Management Portal edition (hackathon prototype). Renamed to match the updated hackathon guidelines (10 Nov 2026). **Requires a fresh database** (new columns and tables).

### Product
- **Idea submission**: title, problem, solution, target users, sector and stage. Required for submitters; checked by the server, not just the form.
- **Reviewer assignment**: administrators assign evaluators to each idea. Evaluators only see ideas they are assigned to (or created).
- **Human scoring**: weighted 1-5 criteria configured by administrators. Evaluators score independently and never see each other's scores; adjudicators and administrators see the panel view with averages, spread and a disagreement flag.
- **Revise and resubmit**: submitters can edit their idea and upload a new version after it is sent back. Feedback written by evaluators and adjudicators is shown to the submitter.
- **Timeline** of every status change for each idea, plus a progress stepper.
- CEDA labels throughout: Submitter, Evaluator, Adjudicator, Administrator; Panel review, Revise and resubmit, Adjudication, Awarded, Declined.
- Submitters cannot upload while their idea is with the panel.

### Engineering
- New tables `score_criteria`, `review_assignments`, `review_scores`; new endpoints under `/api/applications/{id}/assignments|scores|idea|timeline` and `/api/score-criteria`.
- New API tests for assignment visibility, independent scoring, panel maths, mandatory idea fields, editing locks, resubmission and the timeline.

## 1.2.0 - 2026-10-03
Hardening and standards release following the v1.1 code review. **Requires a fresh database** (v1.1 and earlier held demo data only; the app refuses to start on an old schema rather than corrupt it).

### Product
- **Institution standards drive the analysis.** New per-institution / per-programme *rubric* (numeric, evidence and qualitative criteria, mandatory flags, weights). Risks, evidence gaps, due-diligence questions and the criteria-coverage indicator all come from it. Policy knowledge items are supplied to the AI as context.
- **User management** (admin creates officers, committee members, applicants), roles `admin / officer / committee / applicant`, applicants see only their own cases and never the assessment.
- **Workflow state machine** with separation of duties: officers analyse and refer; committee members approve or decline referred cases; nobody can decide a case they referred.
- **One decision endpoint** that writes the DecisionRecord (with the assessment version relied on), updates status, notifies and audits in one transaction.
- **Assessment versioning**: every upload or officer correction creates a new assessment version; due-diligence answers survive re-analysis; stale questions are marked superseded.
- Officers can **confirm or correct** revenue/expenses read from a document; the source line is shown next to each figure.
- AI runs record model, prompt version and input hash. Quotations returned by the AI are checked against the document.

### Fixes
- Financial extraction: years, percentages, `1.2 million`, `900k`, `1 200 000` and sentence-crossing matches handled; unknown is `None`, not `0`.
- Disabled rules really are disabled. AI can no longer replace rule-based risks. "Mentioned" is no longer reported as "supported"; negations ("no quotations") are detected.
- Removed the mislabelled `break_even` metric (it was just expenses).
- Postgres startup crash (`institution_id=""`) removed together with the hand-rolled SQLite migration.
- Committee report buttons now work (fetched with the auth header, not `window.open`). Reports are built in memory, not written to a shared folder.
- Timestamps are UTC with `Z` and display in local time.
- Upload: bounded read, magic-byte check, DOCX zip-bomb guard, empty-text detection (scanned PDFs), analysis runs in a worker thread.
- Audit log is hash-chained, written in the same transaction as the action, and verifiable at `/api/audit/verify`.

### Security
- No default passwords or secrets; production refuses to start without `JWT_SECRET` (32+) and `ADMIN_PASSWORD` (12+). Login form no longer pre-filled.
- Login rate limit keyed by IP + username (cannot lock out admin from elsewhere); stale keys purged.
- Tokens revocable (`token_version`), sessionStorage instead of localStorage, strict CSP (no inline script), HSTS in production, container runs as non-root.
- AI is **opt-in per institution**, strips e-mails/phone numbers, fences the plan text as untrusted input, validates enums.

### Engineering
- Backend rewritten as one coherent module (no per-version layers or route patching); pure logic in `backend/analysis.py`.
- 46 unit tests (analysis), ~20 end-to-end API tests, repo-consistency tests; CI runs them on SQLite **and** PostgreSQL, then builds and smoke-tests the container.

## 1.1.0 and earlier
Pilot prototypes (see the Master package). Superseded by 1.2.0.
