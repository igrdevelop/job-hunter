You are helping Ihar Petrasheuski process multiple job applications at once.

## Input
$ARGUMENTS

The input is a list of job URLs, one per line. Example:
```
https://www.linkedin.com/jobs/view/123/
https://justjoin.it/job-offer/company-role
https://nofluffjobs.com/pl/job/role-company
```

---

## Instructions

Process each URL **sequentially**, one by one.

For each URL:
1. Print: `Processing [N/total]: {url}`
2. Fetch the job posting (WebFetch). If it fails or returns empty content, print a warning and skip to next.
3. Run the full `/apply` logic for this job — analyze the posting, generate tailored content, write `content.json`, run the generator script.
4. Print: `Done [N/total]: {CompanyName} → Applications/{CompanyName}_{date}/`

After all jobs are processed, print a summary:

```
=== BATCH COMPLETE ===

Processed: N jobs
Folders created:
  - Applications/Devapo_2026-04-06/
  - Applications/4Soft_2026-04-06/
  - ...

Tracker updated: Applications/tracker.xlsx
Failed (skipped):
  - https://... (reason)
```

---

## Reuse the full apply logic from the `/apply` skill

Use Ihar's full background embedded below and follow all the same rules:
- ATS-optimized resume (keyword matching, reorder skills, adapt summary)
- Cover letter EN + PL (250-350 words, 3-4 paragraphs)
- About Me EN + PL (3-5 sentences)
- Stack detection: Angular / React / JavaScript
- Language detection: EN / PL (Polish posting → add PL resume)
- Write `content.json` with `apply_url` and `job_title` fields
- Run: `python D:/LearningProject/Claude/generate_docs.py D:/LearningProject/Claude/content.json`

---

## Ihar's full background

**Contact**: Ihar Petrasheuski (also known as Igor Pietraszewski)
+48 571 525 110 | igrflex@gmail.com | linkedin.com/in/ijerweb | Wrocław, Poland

**Core stack**: Angular (2–19), NgRx, RxJS, Signals, Nx Monorepo, AG Grid, TypeScript, JavaScript, HTML, Bootstrap, SCSS
**Tools**: Jest, Jasmine, Cypress, Git, Jenkins, Webpack, Node.js
**Methodologies**: Agile (Scrum, SAFe), Frontend Architecture, Code Reviews, Performance Optimization, CI/CD
**Languages**: English (Fluent), Russian (Native), Polish (B1 Intermediate)

**Work Experience**:

**Senior Frontend Developer (Angular) | Fairmarkit (via contractor)** — Jun 2025 – March 2026
AI-powered Enterprise Procurement Platform | USA (Global)
- Contributed to frontend development of an AI-powered procurement platform serving enterprise clients globally, built with Angular 19 in an Nx monorepo.
- Delivered two domain-specific features covering complex procurement workflow logic, including one feature with direct AI integration; built an automation dashboard improving procurement workflow visibility.
- Worked extensively with Angular Signals, NgRx state management, and AG Grid for complex heavy data tables in a production environment.
- Participated in frontend architecture decisions within a cross-functional team of ~10, part of a ~200-person engineering organization.
- Maintained code quality through regular code reviews in Agile (Scrum).
Stack: Angular 19, TypeScript, Signals, RxJS, NgRx, Nx Monorepo, AG Grid, SCSS.

**Senior Frontend Developer (Angular) | Venture Labs** — July 2023 – April 2025
Banking Sector | Carbon Footprint Calculations | Poland | Client: Atruvia AG — core banking IT provider for 300+ German cooperative banks
- Built two Angular applications from scratch, actively used by 300+ German banks.
- Provided ongoing support and feature development for a third critical application.
- Migrated projects across Angular versions (14 → 19) ensuring code quality and minimal downtime.
- Ensured high code quality through unit tests, E2E tests, and integration with SonarQube.
- Designed and maintained Jenkins pipelines for automated builds, tests, and deployments.
- Conducted regular code reviews and worked in a cross-functional Agile team (10+ members).
Stack: Angular 14-19, TypeScript, SCSS, RxJS, NgRx, Java (backend).

**Senior Frontend Developer (Angular) | SII** — November 2022 – July 2023
Finance Sector | Financial Instruments Management
- Developed new frontend features and modules, led Angular version upgrades.
- Participated in architecture discussions, worked closely with backend, analysts, and QA in Agile.
Stack: Angular 10-12, TypeScript, SCSS, RxJS, NgRx, AG Grid, Java (backend). Team: 10+ members.

**Senior Frontend Developer (Angular) | Altoros** — April 2018 – November 2022
E-commerce | Insurance | Healthcare
- E-commerce: Built and scaled an advanced platform with a powerful admin panel. Stack: Angular 11-14, TypeScript, SCSS, Node.js.
- Healthcare (British Hospital): Inherited unfinished app, completed, optimized and stabilized it. Stack: Angular 11-14, RxJS, AG Grid, .NET.
- Insurance: Built real-time incident management with live maps and SignalR. Stack: Angular 6-8, AG Grid.

**Frontend Developer (Angular) | SolbegSoft** — April 2016 – April 2018
Maintenance Services Management — Developed a task management platform for service engineers; collaborated with BE and QA in Agile. Stack: Angular 2-6, TypeScript, SCSS, Bootstrap. Backend: .NET.

**Frontend Developer | Staronka** — November 2015 – March 2016
Startup | Website Builder — Worked on the core website-building tool; focused on responsive layouts and UI fixes. Stack: AngularJS, JavaScript, SCSS.

**Education**: Belarusian State Technological University — Bachelor, PE and Systems of Information Processing

**Additional Courses**: Angular Updates Course, Angular Advanced Course, Angular Core Course, JS Architecture Workshop, RxJS Course, Java basic Course, Node.js Course, JavaScript Advanced Level
