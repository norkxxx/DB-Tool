<div align="center">
## ONLY FOR EDUCATIONAL PURPOSES
<img src="assets/swamp-banner.svg" alt="SwampMatrix — Swamp OS Security Toolkit — SQLi Orchestrator" width="960">

![Python](https://img.shields.io/badge/Python-3.9%2B-527a32?style=flat-square&logo=python&logoColor=d5e8bd)
![Docker](https://img.shields.io/badge/Docker-supported-527a32?style=flat-square&logo=docker&logoColor=d5e8bd)
![SwampMatrix](https://img.shields.io/badge/SwampMatrix-Security-527a32?style=flat-square)
![SwampOS](https://img.shields.io/badge/Swamp_OS-Ecosystem-354a32?style=flat-square)

**Security research · Controlled environments · Authorized assessments**

</div>

---

## 00 / Overview

A Python-based SQL injection security testing framework that coordinates reconnaissance, database fingerprinting, testing backends, batch execution, reporting, and optional vulnerability management integrations.

The project includes active exploitation, WAF bypass capabilities, and AI-assisted payload generation. It is intended for controlled security research, isolated training environments, and explicitly authorized penetration testing. It is not a passive vulnerability scanner.

## 01 / Features

| Component | Description |
| --- | --- |
| Reconnaissance | GET, POST, and cookie parameter discovery, response baselining, and Cloudflare detection. |
| AI integration | Optional WormGPT-based generation of testing strategies, payloads, and tamper scripts, including iterative feedback. |
| DBMS fingerprinting | MySQL, PostgreSQL, Microsoft SQL Server, Oracle, and SQLite identification. |
| Fuzzing | Built-in SQL injection test payloads and mutation mechanisms. |
| Testing backends | sqlmap, optional Ghauri, and optional sqlninja. These include exploitation and, in the case of sqlninja, post-exploitation capabilities. |
| Batch processing | Concurrent processing of target lists. |
| Session recovery | JSON-backed state persistence and interrupted batch recovery. |
| Logging | Size-based rotation, gzip compression, and configurable retention. |
| Progress monitoring | Live execution status, worker counts, findings, and elapsed time. |
| Reporting | Per-target JSON reports and consolidated HTML reports. |
| Integrations | Optional Burp Scanner, DefectDojo, and Nessus API integrations. |

## 02 / Requirements

- Python 3.9 or later.
- Python dependencies listed in `requirements.txt`.
- Optional HTTP client: `curl_cffi`.
- Optional external backends: sqlmap, Ghauri, and sqlninja (Perl-based).
- Docker for containerized deployment.

The supplied project description states that missing optional backends are recorded in report errors rather than terminating the entire run.

## 03 / Architecture

```text
Reconnaissance
    |
Database fingerprinting and testing
    |
Optional AI and external testing backends
    |
Findings and execution logs
    |
JSON / HTML reports
    |
Optional vulnerability management integrations
```

## 04 / Batch state and recovery

The default state file is `sqli_batch_state.json`. The documented target states are:

| State | Meaning |
| --- | --- |
| `pending` | Waiting for processing. |
| `running` | Processing in progress. |
| `done` | Processing completed. |
| `error` | Processing failed; eligible for retry during recovery. |
| `skipped` | Target skipped. |

Interrupted running tasks are documented as returning to the pending state on restart. Completed targets are skipped during recovery.

## 05 / Configuration and credentials

| Environment variable | Purpose |
| --- | --- |
| `WORMGPT_API_KEY` | Optional external AI service credential. |
| `DOJO_API_KEY` | DefectDojo API token. |
| `NESSUS_ACCESS_KEY` | Nessus access key. |
| `NESSUS_SECRET_KEY` | Nessus secret key. |

Keep credentials outside source control. Review external service data handling before enabling integrations. AI-generated Python code must be treated as untrusted code; do not execute it without review and appropriate isolation.

## 06 / Output files

| File | Description |
| --- | --- |
| `sqli_report_target_N.json` | Per-target findings, backend output, AI strategy data, and errors. |
| `sqli_report.html` | Consolidated HTML report. |
| `sqli_batch_state.json` | Persistent batch execution state. |
| `logs/scan_YYYYMMDD_HHMMSS.log` | Active execution log. |
| `logs/scan_*.log.N.gz` | Compressed rotated log segments. |
| `logs/scan_*.log.gz` | Compressed completed execution logs. |

The documented logging defaults are 10 MB per segment, up to five compressed segments per run, and 30 days of retention. Completed logs larger than 1 MB are documented as compressed. Reports and logs may contain sensitive application data; do not publish them without review and redaction.

## 07 / Project structure

```text
.
├── sqli_orchestrator.py
├── README.md
├── requirements.txt
├── Dockerfile
├── .dockerignore
└── .github/
    └── workflows/
        └── lint.yml
```

The documented CI workflow includes syntax checks, flake8, and smoke tests.

## 08 / Responsible use

- Test only systems covered by explicit authorization and an agreed scope.
- Use isolated environments for exploitation and post-exploitation research.
- Do not use the project to access, extract, or disclose third-party data without authorization.
- Respect agreed request limits, testing windows, and service availability requirements.
- Report vulnerabilities through the affected organization's authorized disclosure process.

## 09 / Security and publication

Before publishing, review the implementation and repository history for embedded credentials, sensitive target lists, captured data, and unsafe execution defaults. Consider adding enforceable scope restrictions, explicit opt-in controls for intrusive operations, and isolated execution of generated code. These are recommendations, not claims about existing safeguards.

Review the [GitHub Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies) before distributing the project.

## 10 / License

No license was specified in the supplied project documentation. Consult the repository's license file, if present.

---

<div align="center">

**SWAMP MATRIX // SWAMP OS**

`SECURITY RESEARCH & ENGINEERING`

</div>
