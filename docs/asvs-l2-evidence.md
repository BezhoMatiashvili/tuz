# OWASP ASVS Level 2 release evidence matrix

ეს მატრიცა არის production release gate. „N/A“ შეიძლება მონიშნოს მხოლოდ security owner-მა მოკლე არგუმენტით და შესაბამისი მტკიცებულების ბმულით.

| ASVS area | ამ პროექტის კონტროლი | აუცილებელი evidence |
|---|---|---|
| V1 architecture | საჯარო API/UI არ არსებობს; worker იზოლირებულია dedicated non-login user-ით | architecture diagram, `systemd-analyze security`, deployment review |
| V2 authentication | source-ის ცალკე read-only integration user; cookie production-ში უარყოფილია | source-account screenshot/audit, credential rotation record |
| V3 session management | production browser session საერთოდ არ გამოიყენება | configuration test and production unit environment review |
| V4 access control | DB writer მხოლოდ scoped functions-ს იძახებს; table read/delete/DDL deny | CI `PostgresPrivilegeTests`, `ops/sql/verify_privileges.sql` output |
| V5 validation / injection | პარამეტრიანი SQL, JSONB object validation, response cap, redirect deny | unit/integration test output, authenticated review |
| V6 cryptography | TLS verify-full, HMAC-SHA-256 key DB-ს გარეთ, encrypted credentials/backups | connection setting, credential inventory, restore report |
| V7 error handling | stored safe failure code; URL/cookie/driver errors არ ილოგება | `test_client.py`, journal sample redaction review |
| V8 data protection | raw HTML არ ინახება; private `warehouse` schema; 24-month protected pruning | migration record, Supabase exposed-schema screenshot, retention job record |
| V10 malicious input | parser მხოლოდ cell text-ს ინახავს; bounded response | `test_parser.py`, mock-source tests |
| V11 business logic | one-run advisory lock; full scan only deactivates products | service/repository test, run logs |
| V12 files/resources | systemd hardening, read-only system paths, private temp, bounded HTTP | unit file review and `systemd-analyze` result |
| V14 configuration | fail-closed production configuration and SHA-pinned CI actions | `test_config.py`, CI run URL, environment review |

**Garage site (2026-11-10).** V1 და V3 სტრიქონები აღარ არის ზუსტი იმ შემთხვევაში, თუ `automotive` აპლიკაცია საჯაროდ განთავსდება: კოდში ახლა არსებობს საჯარო გვერდი, თანამშრომლის ავტორიზაცია და ბრაუზერის სესიები ([garage-site.md](garage-site.md)). ეს ცვლილება აპლიკაციას არ ათავსებს production-ში. განთავსებამდე security owner-მა ხელახლა უნდა შეაფასოს V1, V2, V3, V4 და V13 (session cookie, CSRF, role split, rate limits, upload), მტკიცებულებად: `tests/test_site_*.py`, reverse proxy-ის კონფიგურაცია და ამ ზედაპირის penetration test.

**Garage site, V11 anti-automation (2026-11-19).** IPv6 /64-ის შეცვლა ლიმიტს აღარ ზრდის: sign-in-ის, ჩატის და VIN-ის კატეგორიებში (`site_guard.WIDE_CATEGORIES`) ერთი /48-ის ყველა /64 საერთო bucket-ს იყენებს. მთელი საიტისთვის საერთო ლიმიტი განზრახ არ არსებობს, რადგან რამდენიმე მისამართი მას ამოწურავდა და ყველას დაბლოკავდა. ჩატისა და VIN-ის დღიური ბიუჯეტი საათებზეა განაწილებული. ეს ბიუჯეტის ამოწურვას ანელებს, მაგრამ არ აჩერებს. Evidence: `tests/test_site_guard.py` (`RotationTests`, `TokenBucketTests`, `DailyBudgetTests`) და `tests/test_site_chat.py`. 2026-11-19-ს, როცა ჰკითხეს, მფლობელმა უარი თქვა sign-in-ის დამატებით გამაგრებაზე (per-account lock, მეორე ფაქტორი, common-password სია) ([garage-site.md](garage-site.md), „Sign-in throttling“). ქართული ტექსტი native speaker-მა უნდა გადახედოს.

Required final attachments: authenticated external penetration-test report with remediation status, current dependency scan/SBOM, Supabase IP-restriction evidence, source WireGuard/TLS evidence, successful isolated restore-drill report, and security-owner sign-off. No deployment is approved merely because automated tests pass.
