# უსაფრთხო production runbook

ეს დოკუმენტი აღწერს deployment gate-ს. ის არ ახორციელებს Supabase, AWS, DNS, firewall ან GitHub-ის გარე ცვლილებებს — მათთვის საჭიროა შესაბამისი ანგარიშის/ინფრასტრუქტურის ავტორიზაცია.

## Release gate — ყველა პუნქტი სავალდებულოა

1. ერთი Supabase Pro/Micro პროექტი Frankfurt რეგიონშია შექმნილი; `warehouse` **არ** არის Data API-ის exposed schema-ში.
2. Supabase network restrictions უშვებს მხოლოდ Lightsail worker-ის ფიქსირებულ public IP-ს; writer URL-ს აქვს `sslmode=verify-full`.
3. Lightsail Linux worker-ს აქვს მხოლოდ key-based SSH ადმინისტრატორის allowlisted IP-დან, dedicated non-login `warehouse-sync` და `warehouse-backup` მომხმარებლები და დაყენებული systemd hardening.
4. წყაროს legacy port `6068` საჯაროდ დახურულია. Worker ↔ legacy host მუშაობს WireGuard-ით; worker წყაროს ხედავს მხოლოდ HTTPS reverse proxy-ით (`warehouse.example.com` placeholder).
5. source-ში შეიქმნა read-only integration account. Browser cookie production-ში არ გამოიყენება.
6. 30 ყოველდღიური და 24 თვიური encrypted off-host logical backup retention lifecycle დადასტურებულია; გაკეთებულია იზოლირებული restore drill.
7. GitHub private repository-ში ჩართულია MFA, branch protection, required CI checks, required `production` environment approval, Dependabot, secret scanning და CodeQL.
8. `$28` billing alert, sync/credential/90-minute miss/backup/dependency-scan alertები გადამოწმებულია test notification-ით.
9. დასრულებულია authenticated external penetration test და [ASVS evidence matrix](asvs-l2-evidence.md).
10. თუ საჯარო საიტი ([garage-site.md](garage-site.md)) განთავსდება: penetration test მოიცავს `/login`, `/admin`, `/api/site`, `/api/auth` და `/api/admin` ზედაპირს; აპლიკაცია მუშაობს HTTPS reverse proxy-ის უკან, sync worker-ისგან განცალკევებულ host-ზე, ცალკე `garage_site` და `automotive_runtime` login-ებით.

თუ რომელიმე პუნქტი დაუდასტურებელია, production deployment უნდა შეჩერდეს.

## 1. Database roles და migrations

საწყისად Supabase administrator-მა უსაფრთხო ადმინისტრაციის არხით ერთხელ გაუშვას [`ops/sql/provision_production_roles.sql`](../ops/sql/provision_production_roles.sql). Script არ შეიცავს password-ს. თითო LOGIN role-ს შეუქმენი განსხვავებული, გრძელი password და ჩააბარე მხოლოდ encrypted credential storage-ს:

- `warehouse_migration_owner` — migration/deploy automation; არა ადამიანის ყოველდღიური წვდომისთვის.
- `warehouse_sync_writer` — worker-სთვის; არ აქვს table read, delete ან DDL.
- `warehouse_backup_restore_operator` — encrypted logical backup/restore და მხოლოდ `warehouse.prune_product_versions()`.

ახალი database:

```bash
WAREHOUSE_APP_ENV=production \
WAREHOUSE_MIGRATION_DATABASE_URL_FILE=/run/credentials/.../warehouse-migration-database-url \
uv run python -m warehouse_sync --migrate
```

არსებული 0.1 database, რომელშიც შესაძლოა unsigned ვერსიებია:

```bash
python -m warehouse_sync --migrate --migrate-through 0002_secure_warehouse
python -m warehouse_sync --sign-legacy-versions
python -m warehouse_sync --migrate
```

ეს სამი ნაბიჯი უნდა შესრულდეს maintenance window-ში, verified backup-ის შემდეგ, მხოლოდ migration-owner credential-ით. `0002` მიზანმიმართულად შლის raw HTML column-ს. Drop-ის შემდეგ საჭიროა storage team-ის მიერ დაგეგმილი `VACUUM (FULL, ANALYZE)` მხოლოდ იზოლირებულ maintenance window-ში, თუ physical page sanitation აუცილებელია.

არ შეცვალო უკვე გაშვებული migration file; rollback კეთდება ახალი forward migration-ით ან tested backup restore-ით.

Automotive ცოდნის ბაზა (`0008_automotive_knowledge`): migration-ის შემდეგ cluster administrator-მა ხელახლა გაუშვას [`ops/sql/provision_automotive_roles.sql`](../ops/sql/provision_automotive_roles.sql). Validator-ისთვის შექმენი ცალკე LOGIN, რომელსაც მხოლოდ `automotive_validator` აქვს, და მისი URL შეინახე მხოლოდ encrypted credential storage-ში. `AUTOMOTIVE_KNOWLEDGE_ENABLED` ჩართე მხოლოდ მას შემდეგ, რაც `automotive_knowledge.source_policies`-ში ჩაიწერება ის წყაროები, რომელთა პირობებიც ხელახალ გამოყენებას იძლევა. დეტალები: [Learned knowledge base](automotive.md#learned-knowledge-base).

## 2. systemd credentials და sandbox

დააინსტალირე `ops/systemd/*.service`, `*.timer` და `ops/bin/*` root-owned გზებზე. შექმენი `/etc/warehouse-sync/runtime.env` (`0640 root:warehouse-sync`) მხოლოდ non-secret მნიშვნელობებით:

```ini
WAREHOUSE_APP_ENV=production
WAREHOUSE_BASE_URL=https://warehouse.example.com
WAREHOUSE_PRODUCTS_PATH=/app/Warehouse/app/Ajax/GetProducts.php
WAREHOUSE_ALLOWED_SOURCE_HOSTS=warehouse.example.com
WAREHOUSE_ALLOWED_DATABASE_HOSTS=REAL_PROJECT.pooler.supabase.com
WAREHOUSE_SOURCE_USERNAME=warehouse-readonly
WAREHOUSE_TIMEOUT_SECONDS=30
WAREHOUSE_REQUEST_DELAY_SECONDS=0.30
WAREHOUSE_MAX_RETRIES=3
WAREHOUSE_MAX_RESPONSE_BYTES=4194304
```

შექმენი encrypted credentials `systemd-creds encrypt`-ით root terminal-ზე: database URL, source password და 32+ byte base64 HMAC key. მათი plain-text ვერსია არ უნდა იყოს `.env`-ში, GitHub output-ში, shell history-ში ან persistent file-ში. Unit-ის `IPAddressAllow` placeholders შეცვალე WireGuard proxy და Supabase-ის მიმდინარე მისამართებით; სანამ ეს არ გაკეთდება `IPAddressDeny=any` fail-closed მდგომარეობაში ტოვებს worker-ს.

ჯერ შექმენი service users (`warehouse-sync` და `warehouse-backup`) `--system --no-create-home --shell /usr/sbin/nologin` პარამეტრებით; მათ არ უნდა ჰქონდეთ SSH key, password ან sudo. დააკოპირე `warehouse-sync-*`/`warehouse-backup` scripts `/usr/local/libexec`-ში, ხოლო `warehouse-activate-release` `/usr/local/sbin`-ში, ყველა `root:root 0755`-ით. Worker repository და მისი read-only virtualenv იყოს `/opt/warehouse-sync`-ში.

```bash
systemctl daemon-reload
systemctl enable --now warehouse-sync.timer warehouse-sync-healthcheck.timer
systemd-analyze security warehouse-sync.service
```

Alerting-ისთვის `/etc/warehouse-sync/alert.env` შეიცავს მხოლოდ `WAREHOUSE_ALERT_EMAIL`; host MTA/SNS relay უნდა იყოს წინასწარ კონფიგურირებული. Alert შეტყობინება განზრახ არ შეიცავს exception ტექსტს, URL-ს ან credential-ს.

## 3. ქსელი და წყარო

Legacy host-ზე დააყენე [`ops/wireguard/wg0.conf.example`](../ops/wireguard/wg0.conf.example) რეალური გასაღებებით და [`ops/nginx/warehouse.conf.example`](../ops/nginx/warehouse.conf.example) მოქმედი certificate-ით. Nginx-ის `auth_basic_user_file` უნდა შეიცავდეს მხოლოდ ცალკე integration user-ს; მისი password არის worker-ის encrypted `warehouse-source-password` credential. Firewall policy:

- `6068/tcp`: მხოლოდ localhost ან private WireGuard segment; არა internet.
- `51820/udp`: მხოლოდ აუცილებელი WireGuard peer.
- `443/tcp`: მხოლოდ თუ TLS endpoint უნდა იყოს ინტერნეტიდან მისაწვდომი; უკეთესია მხოლოდ worker IP/WireGuard subnet.
- worker-ის egress: მხოლოდ WireGuard HTTPS proxy, Supabase DB და encrypted backup destination.

გააკეთე დამოუკიდებელი გარედან port scan და დააფიქსირე, რომ `6068` მიუწვდომელია. Supabase dashboard-ში შეამოწმე უარყოფილი DB connection სხვა IP-დან და წარმატებული კავშირი worker-ის IP-დან.

## 4. Backup და restore

`warehouse-backup.service` იყენებს `pg_dump` service/pgpass credential-ს, `age` public recipient-ს და `rclone` off-host remote-ს. `/etc/warehouse-sync/backup.env` შეიცავს მხოლოდ მაგალითად:

```ini
PGSERVICE=warehouse_backup
WAREHOUSE_BACKUP_DESTINATION=remote:private-warehouse-backups
```

Object-store lifecycle-მა უნდა შეინარჩუნოს `daily/` 30 დღე და `monthly/` 24 თვე. Private decryption key რჩება worker-ის გარეთ. ყოველ კვარტალში აღადგინე ერთი encrypted dump ახალ, იზოლირებულ database-ში, შეამოწმე row counts/HMAC tags და დააფიქსირე elapsed time. არასოდეს გაუშვა `pg_restore --clean` production database URL-ზე.

ამისთვის არსებობს [`ops/bin/warehouse-restore-drill`](../ops/bin/warehouse-restore-drill): ის განზრახ მოითხოვს `WAREHOUSE_RESTORE_DRILL_CONFIRM=RESTORE_ISOLATED_DATABASE`-ს და database name-ს `warehouse_restore_` prefix-ით.

## 5. ღირებულება და ოპერირება

Baseline: Supabase Pro/Micro დაახლოებით $25 და Lightsail Linux $3.50-დან — დაახლოებით $28.50/თვე გადასახადებისა და backup overage-ის გარეშე. ამიტომ billing alert დააყენე $28-ზე; `$30` არის მიზანი და არა მკაცრი ceiling. შეამოწმე timer, ბოლო successful run და dependency scan ყოველდღიურად; rotation/incident owner-ის გარეშე სისტემა production-ready არ არის.

GitHub-ში `@REPLACE_WITH_SECURITY_OWNER` ჩანაცვლდეს რეალური user/team-ით [`CODEOWNERS`](../.github/CODEOWNERS)-ში. შემდეგ ჩართე main-ის branch protection: required review from Code Owners, required `Security and test gate`, no force-push და no bypass. `production` Environment-ს მიუთითე required reviewer-ები და მხოლოდ იქ შეინახე deploy host/user/key secrets. CodeQL, Dependabot, GitHub secret scanning და workflow-failure notifications უნდა მიდიოდეს alert პასუხისმგებელ email-ზე.
