# Long-lead items: contacts and first messages (S1.13)

These depend on other people and can take weeks, so they start in sprint 1 and are checked at every
sprint review (see the table in [Sprints.md](../Sprints.md)). For each item: who to contact, what to ask,
and a first message ready to adapt and send. **Fill in the contact and the date sent**; S1.13 is done
when every row has both.

| # | Item | Needed by | Contact (name, role) | First message sent | Status |
|---|---|---|---|---|---|
| 1 | Michael's approval of the restructure and D-01 to D-12 | S1 | Michael Todd | 2026-11-19 | **Done** |
| 2 | Test fleet | S4 | Colin (8 laptops available) | — | Started: 8 laptops; OS and GPU mix to confirm |
| 3 | Campus IT: workstation opt-in policy, firewall rule, antivirus/EDR product | S4, S10 | _IT service desk or endpoint team_ | | Open |
| 4 | Privacy office: student data, retention, operational telemetry | S4, S9 | _Privacy or compliance officer_ | | Open |
| 5 | Campus single sign-on app registration (OIDC or SAML) | S9 | _Identity and access team_ | | Open |
| 6 | Code-signing certificates (Authenticode, Apple Developer ID) | S10 | _FAIRlab lead or IT procurement_ | | Open |
| 7 | University IP and tech transfer; written agreement on Michael's code | Before any commercial step | _Tech-transfer office_; Michael Todd | | Open |

---

## 2. Test fleet: what to record for each laptop

Colin has 8 laptops for test nodes. Before sprint 4, record for each one (no personal data needed):

| Laptop | OS and version | CPU | RAM | GPU (model, VRAM) or none | Wi-Fi or wired | Owner consent recorded |
|---|---|---|---|---|---|---|
| L1 | | | | | | |
| L2 | | | | | | |
| … | | | | | | |

Gaps to watch: the plan wants at least one Mac, one NVIDIA machine and one CPU-only machine, and at
least two OSes for the Phase 1 exit (S6.6). If all 8 are Windows laptops, ask FAIRlab for a Mac and a
Linux machine (or install Ubuntu on one laptop) early. Laptops also exercise battery and Wi-Fi
behaviour, which DIFA's Round 7 showed the placement model under-predicts.

## 3. Campus IT

> Subject: Research pilot: background agent on opted-in lab workstations
>
> Hello,
>
> I'm Colin Woods in FAIRlab. We're building IdleMesh, a research service that runs a campus LLM chat
> service on spare capacity from opted-in workstations while they're idle. We'd like to start with a
> few lab-owned laptops and, later, a small pilot on lab workstations. Before we do, could you help
> with three things?
>
> 1. **Policy:** is there an existing process for installing a research agent on university
>    workstations, and what approvals does it need?
> 2. **Network:** the agent talks to one coordinator over HTTPS (TCP 8765) and serves inference over a
>    mutually authenticated TLS tunnel (one TCP port per node). Can we get a firewall rule within a
>    lab subnet, and later between buildings?
> 3. **Endpoint security:** which antivirus/EDR product is on managed machines? We'll sign every
>    binary and would like to test against it and provide an allowlist document.
>
> Nodes install a single signed installer and nothing else; a manual install is always available, and
> push through Intune/SCCM/Jamf is optional. Happy to meet and share the design.

## 4. Privacy office

> Subject: Privacy review request: campus LLM chat service and workstation telemetry
>
> Hello,
>
> FAIRlab is building a campus LLM chat service that runs on idle, opted-in workstations. We'd like
> your guidance before a pilot on two kinds of data:
>
> 1. **Student chats:** prompts and replies. Our default is private-by-default history, retention
>    limits and a no-history mode. Prompt and reply text never appears in logs or telemetry. What does
>    FERPA and campus policy require (retention, access, notice)?
> 2. **Operational telemetry from workstations:** idle time (seconds since the last input, never what
>    was typed), CPU/GPU load, hardware model, network latency, error and crash reports, and anonymised
>    performance records. Node identifiers are pseudonymised in exported data. What may we collect,
>    for how long, and what consent do workstation owners need to give?
>
> We can share our data model and redaction rules, and we'd like to agree a telemetry tier that fits
> policy before we start collecting.

## 5. Single sign-on

> Subject: SSO app registration for a FAIRlab research service
>
> Hello,
>
> We'd like to register a FAIRlab research web app (a campus LLM chat service) with university
> sign-in. We can use OIDC (preferred) or SAML. We need the user's stable identifier and, if
> available, a role attribute (student or staff); we don't need names or email unless policy
> requires them. What's the process and lead time, and is there a test tenant we can use first?

## 6. Code signing

> Subject: Code-signing certificates for a FAIRlab research tool
>
> Hello,
>
> Our research agent must be signed to install on Windows (Smart App Control and antivirus) and macOS
> (Gatekeeper and notarization). Does the university or FAIRlab already have an Authenticode
> certificate (ideally EV) and an Apple Developer ID we can use, or should we purchase them? We need
> them before our installer sprint.

## 7. IP and tech transfer

> Subject: IP question: FAIRlab research software that may be commercialised
>
> Hello,
>
> FAIRlab is developing IdleMesh, distributed LLM inference on idle campus workstations, as research
> that might later become a product. It builds on a prototype written by Michael Todd and on MIT-licensed
> open-source software (llama.cpp). Before we go further, could we discuss university IP policy for
> student and staff software, how Michael's prior code should be assigned or licensed, and whether a
> patent search is advisable? A first-pass dependency licence inventory is in our repository
> (docs/licenses.md).
