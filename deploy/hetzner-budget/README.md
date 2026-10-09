# USD 10 persistent acceptance host

This creates **one persistent acceptance host**, not a production HA cluster. It does not deploy OmniDesk or manufacture production/device/signing evidence. The independent Real GA gate remains mandatory. No paid resources have been created by committing these files or running the mock CI check.

## Current quote and account boundary

The owner has no cloud account and authorized a USD 10 monthly server budget. Complete owner registration, identity/payment verification and a dedicated OmniDesk project at <https://console.hetzner.com/>. Do not put passwords, identity documents or API tokens in chat or this repository.

As checked on 2026-10-02, the official European CX23 price is USD 6.49/month excluding VAT and IPv4; IPv4 is USD 0.60/month; seven provider backup slots cost 20% of the server price. The indicative total is **USD 8.388/month before tax**. This is a calculation, not an invoice or a availability guarantee. Check the actual gross project quote, billing currency, conversion/card fees and stock before creating anything. At 19% tax the indicative total is USD 9.98172, leaving virtually no margin. A higher total must stop; do not silently remove backups or select a more expensive plan.

Sources:

- <https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/>
- <https://docs.hetzner.com/cloud/servers/primary-ips/overview/>
- <https://docs.hetzner.com/cloud/billing/faq/>
- <https://www.hetzner.com/cloud/cost-optimized/>

Only CX23 x86, Nuremberg/nbg1 or Helsinki/hel1, Ubuntu 24.04, one IPv4, IPv6 and backups are allowed by the template. Stock is not verified without an account. No load balancer, extra volume, paid snapshot, managed DB, second node or billable fallback is provisioned. Included transfer does not constitute a hard spending cap; excess transfer can still be billed. Avoid such traffic, monitor billing/transfer and suspend the environment before exceeding the limit. Model/API usage, Apple membership, store charges and device subscriptions are **not included** in this server quote and require their own authorization.

## Provisioning

Prefer the owner console for the first machine; reproduce the template settings, restricted SSH source IPs and cloud-init. SSH requires a dedicated Ed25519 public key and the operator's current /32 IPv4 or /128 IPv6. Never upload a private key to cloud-init. Do not register a persistent self-hosted Actions runner on this host.

New routed outbound connections covered by the cloud firewall are limited to TCP 53/80/443 and UDP 53 to the required owner-approved `outbound_cidrs`. Review the resolver, Ubuntu/package registry and application-provider destination networks before apply; the template rejects empty lists and IPv4/IPv6 default routes. DNS/CDN addresses can change, so bootstrap may fail until the owner reviews an updated allowlist. Do not restore unrestricted egress to conceal that failure. Hetzner exempts local Layer 2/metadata traffic and its DNS/rescue services; established/related traffic and pre-existing connections also pass, so this is not complete host egress isolation. [Hetzner Firewall FAQ](https://docs.hetzner.com/cloud/firewalls/faq/). This network policy does not provide application-level URL or credential isolation.

If Terraform is used, run it on an authorized cloud administration shell, not on the project user's Mac. Use Terraform 1.16.4 and the pinned provider 1.69.0. Supply a project-scoped `HCLOUD_TOKEN` only in that shell's secret environment, and keep the state and generated dependency lock in the owner's encrypted durable storage. This module intentionally has no remote state backend because no account/storage is available yet. Do not put state, plans, credentials or tfvars in public Actions artifacts or Git. Example inputs, with real owner values supplied separately:

```hcl
ssh_public_key   = "ssh-ed25519 REPLACE_WITH_DEPLOYMENT_PUBLIC_KEY"
ssh_cidrs        = ["REPLACE_WITH_OPERATOR_IP/32"]
outbound_cidrs   = ["REPLACE_WITH_APPROVED_DESTINATION_CIDR"]
quoted_total_usd = REPLACE_WITH_VERIFIED_GROSS_USD_QUOTE
```

The example is intentionally invalid until completed. `quoted_total_usd` is a fail-closed input gate; it is not a live pricing API check or a provider billing limit. Recheck the invoice quote before every apply. Use `terraform plan -out=owner.plan` and review that exactly one CX23, one SSH key and one firewall are proposed, then apply that reviewed plan. Do not automate `apply -auto-approve`.

## Application bootstrap after the host exists

1. Wait for `cloud-init status --wait`; verify OS packages and host fingerprint through the provider console. Root login is public-key only and cloud SSH is restricted to operator IPs. Do not open SSH globally for GitHub-hosted runners; use a separately approved controlled deployment path.
2. Create an owner-controlled free DNS subdomain, e.g. DuckDNS (<https://www.duckdns.org/about.jsp>), point it at the actual IPv4, and verify control. No example subdomain is an allocated service address. Terminate valid TLS on ports 80/443. Do not send authentication tokens to cleartext HTTP or use disabled certificate verification.
3. Obtain the independent review and signed release artifacts for the selected commit. Verify immutable app, PostgreSQL and sandbox runner image digests, release signatures, provenance and expected version before deployment.
4. Initialize protected PostgreSQL/data directories, fresh secrets and production config using `scripts/init_production_config.py` and the existing production validator. The template has no sample secrets. Preserve admin/RBAC, request signing, audit checkpointing, encrypted memory and production guards.
5. Establish a dedicated rootless Podman sandbox user, derive the actual user UID/socket path, enable its user service/linger, and verify isolation. Do not mount the orchestrator's rootful Docker socket into the sandbox. Do not guess UID 1000 or authorize local host argv to bypass a missing runner.
6. Use `deploy/docker/docker-compose.full.yml` for the one-host topology, keep the gateway loopback-bound and PostgreSQL/runner ports private. Its network is `internal: true`: explicitly review and configure controlled outbound access for real providers before functional validation, while preserving DB and sandbox isolation. No existing internal bridge should be silently made public.
7. Verify config/bootstrap placement under the persistent data volume before launching the signed image. The host provisioner does not claim to initialize this volume. Run startup and all application checks exclusively on the authorized remote host/cloud workflows.
8. Set staging URL and smoke secret references only after successful live TLS/readiness checks. The staging, production and release environments retain independent review. Existing `deploy-staging.yml` and `promote-production.yml` need the actual approved SSH/Kubernetes connection configured; a hostname alone is insufficient.

## Acceptance and evidence

- First run the remote production config validator and `/ready`/deep health, verify exact release/source/image identity, unauthorized access rejection, authenticated metrics, strict sandbox positive/negative tests and DB persistence across a reboot.
- Exercise Web/Desktop/Mobile enrollment and signed device requests against the live endpoint with a synthetic tenant. Cloud physical-device launch smoke is separate from live API approval/push/secure-storage acceptance.
- Keep provider machine backups, plus PostgreSQL-consistent dumps and config/audit checkpoint copies encrypted in an independent owner-controlled location. Machine backups alone are not a verified DB restore or disaster-recovery process. Test restoration into an isolated temporary remote target, budget it separately, record actual measured RPO/RTO and never overwrite the active database.
- Run the existing soak, rollback, production closure and external-evidence workflows on the real endpoint. Save raw timestamps, source/release digests, receipts and workflow IDs; import only actual evidence. A single machine cannot prove three replicas, two independent workers, AZ failover, or industrial HA. Staging and production on this host also do not have infrastructure isolation.
- Upgrade to independently provisioned HA infrastructure before claiming the existing multi-instance/HA Real GA categories. Keep those categories blocked under this budget instead of reducing their requirements.

## Risk and rollback

The host, disk and provider region are single failure domains. Shared CPU can vary; production capacity, latency from China/Malaysia and rootless sandbox concurrency are unmeasured. Free DDNS has no project-controlled availability guarantee. The resource quote has little tax/FX margin and no excess-traffic billing cap.

Before application promotion, retain the previously verified immutable image/config and a consistent backup, then use the existing remote rollback workflow and verify actual recovery. This template enables both provider deletion protection and Terraform `prevent_destroy`; rollback is not `terraform destroy`. For planned retirement, export and verify independent backups, get owner authorization to remove protections, delete the server **and any remaining billable IP/snapshot/volume**, and inspect the provider billing page. Terraform state loss must be recovered/imported rather than creating a duplicate host.

The Budget Infrastructure Check workflow runs format/schema validation and mock plan tests without cloud credentials. It does not prove account availability, cloud-init success, a persistent deployment or customer GA.
