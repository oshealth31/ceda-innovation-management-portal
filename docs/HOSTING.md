# Hosting plan: CEDA Innovation Management Portal

*Prepared 19 November 2026. Prices change monthly; confirm them in the provider's pricing calculator before quoting.*

## 1. In one paragraph

We recommend running the portal on **Amazon Web Services (AWS) in the Africa (Cape Town) region, af-south-1**. It would run as two copies of the application in separate data centres, behind a load balancer, with a managed PostgreSQL database that has an automatic standby. A nightly encrypted copy of the database and uploaded documents would be kept in a **Botswana data centre**, as the Data Protection Act requires. The estimated running cost is **about P3,500 to P4,000 a month** before VAT, plus the Botswana backup copy and SMS charges. If CEDA prefers to keep everything in Botswana, section 6 gives an alternative.

## 2. Where the portal runs today

The prototype submitted to the hackathon runs on Render's free plan in Oregon, USA: https://ceda-innovation-portal.onrender.com. That setup is **only for the demonstration**: it holds sample data, the free server sleeps when idle, and the free database expires after 30 days. It is **not** the proposed production setup.

## 3. Recommended production setup

| Part | What we use | Why |
|---|---|---|
| Region | AWS Africa (Cape Town), af-south-1 | The closest AWS region to Botswana, with several separate data centres ("availability zones") |
| Application | 2 × EC2 t4g.medium (2 vCPU, 4 GB), one in each of two zones, running the same Docker image as the prototype | If one server or one data centre fails, the other keeps serving |
| Load balancer | AWS Application Load Balancer, HTTPS only | Spreads traffic, checks `/api/health/ready` every few seconds and stops sending traffic to an unhealthy copy |
| Database | Amazon RDS for PostgreSQL, db.t4g.medium, **Multi-AZ** | A standby copy in a second zone takes over automatically if the main one fails |
| Uploaded documents | Encrypted storage volume (or Amazon S3), backed up daily | Keeps concept notes and business plans safe |
| Secrets | AWS Secrets Manager | Passwords and keys are never stored in code |
| Email | Brevo (or CEDA's own SMTP server) | Sign-up codes, password resets and alerts |
| SMS | A Botswana SMS aggregator with an HTTP interface | Local delivery for alerts and reset codes |
| Botswana copy | Nightly encrypted backup to a Botswana data centre (for example BoFiNet's Digital Delta Data Centre or Orange Botswana's data centre in Gaborone) | Meets the Data Protection Act requirement to keep a copy in Botswana |

The portal is already built for this layout. It is one Docker image, it stores data in PostgreSQL, it has a health check, and it upgrades its own database tables on start-up.

## 4. Monthly cost estimate

On-demand prices for af-south-1, converted at **P14.0 to US$1** (the rate was about P14.02 on 17 November 2026).

| Item | US$/month | Pula/month | Source / note |
|---|---|---|---|
| 2 × EC2 t4g.medium (US$0.0434 per hour each) | 63 | 890 | Published on-demand rate for af-south-1 |
| RDS PostgreSQL db.t4g.medium, Multi-AZ (US$0.164 per hour) | 120 | 1,680 | Published Multi-AZ rate for af-south-1 |
| Load balancer | about 30 | about 420 | Estimate: depends on traffic |
| Database storage, backups, document storage, data transfer, monitoring | about 40 | about 560 | Estimate for 50 GB and light traffic |
| **Total running cost** | **about 253** | **about P3,550** | Before VAT |
| Botswana backup copy | by quote | by quote | Ask BoFiNet or Orange for a small storage or colocation quote |
| SMS | per message | per message | Ask local aggregators for a per-message rate; email costs little or nothing at CEDA's volumes |

**Ways to pay less:**
- Reserving the database for a year cuts its cost by about a third. The single-zone db.t4g.medium falls from about US$60 to about US$38 a month on a 1-year reservation.
- The application servers can be reserved in the same way once traffic is known.

**What this does not include:** people. Monitoring, support, updates and the help desk are priced separately in our proposal (hosting and support together: P14,300 a month).

## 5. How the portal stays up 24/7

| Risk | What protects against it |
|---|---|
| A server crashes | The load balancer's health check notices within seconds and sends everyone to the other copy. The crashed copy is restarted automatically. |
| A whole data centre fails | The application runs in two zones. The database fails over to its standby in another zone (Multi-AZ), usually within a minute or two. |
| Data is deleted or damaged | Automatic daily database backups with point-in-time restore (kept 14 days), plus the nightly copy in Botswana (kept 90 days). Every action is also in the tamper-evident audit trail. |
| A bad update | Updates are released only after the full automated test suite passes on both SQLite and PostgreSQL. The previous version can be redeployed in minutes. |
| Nobody notices an outage | An outside uptime monitor checks the site every minute and alerts the support team by SMS and email. |

**Targets we commit to:**
- Availability: 99.9% a month, which is at most about 43 minutes of unplanned downtime.
- Recovery point: no more than 5 minutes of data lost.
- Recovery time: back online within 1 hour, even after a data-centre failure.
- Planned maintenance: announced in advance on the portal, on Sundays between 02:00 and 04:00 Botswana time.

**Two settings needed when running more than one copy:**
- Turn off the background message sender on every copy except one (`ALERT_DISPATCH=off`), so no alert goes out twice.
- The limits on repeated sign-in and code attempts are kept by each copy. That is fine for two copies. For a larger setup they can move to a shared store such as Redis.

## 6. Keeping data in Botswana (Data Protection Act, 2024)

Botswana's Data Protection Act, 2024 has applied since 14 January 2025. It covers the personal data in this portal: names, contact details, optional age, gender and district, and the ideas themselves.

When personal data is transferred outside Botswana, a copy must stay in Botswana. Transfers out are allowed only under the conditions the Act sets, such as an adequacy decision. This plan meets that in two ways:
- the nightly encrypted copy kept in a Botswana data centre; and
- a data-processing agreement with the hosting provider.

**If CEDA prefers all data in Botswana**, the same Docker image and database can run on servers in a Botswana data centre instead. Options include BoFiNet's Tier III Digital Delta Data Centre at the Botswana Innovation Hub, Orange Botswana's data centre, or the Government data centre. The setup would be two application servers, a primary database with a standby, and backups kept off-site. Costs depend on the provider's quote. The trade-off is that CEDA, or its support partner, then manages more of the servers itself.

Either way, before go-live CEDA should:
- confirm the approach with its Data Protection Officer;
- register with the Information and Data Protection Commission as required; and
- keep the privacy notice shown at sign-up up to date.

## 7. Security in hosting

- All traffic is encrypted (HTTPS only, HSTS). The database and backups are encrypted at rest.
- The database and servers are not reachable from the internet. Only the load balancer is.
- Administrator access to the cloud account uses multi-factor authentication. Changes are logged.
- Operating system and dependency updates are applied monthly, and urgent security fixes within 72 hours.
- The application adds its own protections: strong password hashing, rate limits, one-time codes, role-based access, a security-header policy and a hash-chained audit log.

## 8. Moving from the prototype to production

1. Create the AWS account in CEDA's name, with CEDA as owner.
2. Create the database, servers and load balancer (about one day, scripted).
3. Point a CEDA web address (for example `innovation.ceda.co.bw`) at the load balancer and switch on HTTPS.
4. Set the settings: email, SMS, `PORTAL_URL`, `DEMO_SHOW_CODES=false` and the CEDA brand colours.
5. Set up the nightly Botswana backup and the uptime monitor.
6. Run the test suite against the new setup, then do a test restore from backup.
7. Train administrators, then open the portal.

## Sources

- AWS EC2 t4g.medium hourly price in af-south-1: devzero.io and sparecores.com price listings (November 2026)
- Amazon RDS db.t4g.medium prices for af-south-1, single-AZ and Multi-AZ: bytebase.com (updated July 2026) and sparecores.com
- Exchange rate: exchange-rates.org, 816 USD = 11,439 BWP on 17 November 2026
- Data Protection Act, 2024, start date and scope: Michalsons; data-localisation requirement: Mondaq/Bowmans guide citing the Act and the Transfer of Personal Data Order, 2022
- Botswana data centres: BoFiNet Digital Delta Data Centre (Tier III, Uptime Institute); Orange Botswana data centre (DatacenterDynamics)
