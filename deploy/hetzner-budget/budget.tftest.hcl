mock_provider "hcloud" {}

variables {
  ssh_public_key   = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK7txPvTBVoEV4RJwdMHngFx2JJsXeUgDoqhcaWMjGFN test-only"
  ssh_cidrs        = ["192.0.2.17/32"]
  quoted_total_usd = 9.50
}

run "restricted_persistent_single_host" {
  command = plan
  assert {
    condition     = hcloud_server.acceptance.server_type == "cx23" && hcloud_server.acceptance.backups && hcloud_server.acceptance.delete_protection && hcloud_server.acceptance.rebuild_protection
    error_message = "The budget host must retain backups and deletion protection without a paid type substitution."
  }
  assert {
    condition     = length(hcloud_firewall.acceptance.rule) == 3 && alltrue([for rule in hcloud_firewall.acceptance.rule : contains(["22", "80", "443"], rule.port)])
    error_message = "Database, gateway, sandbox and runtime sockets must not be exposed by the cloud firewall."
  }
  assert {
    condition     = alltrue([for rule in hcloud_firewall.acceptance.rule : rule.port != "22" || rule.source_ips == toset(var.ssh_cidrs)])
    error_message = "SSH must remain restricted to approved client IPs."
  }
}

run "reject_over_budget" {
  command = plan
  variables {
    quoted_total_usd = 10.01
  }
  expect_failures = [var.quoted_total_usd]
}

run "reject_unknown_quote" {
  command = plan
  variables {
    quoted_total_usd = 0
  }
  expect_failures = [var.quoted_total_usd]
}

run "reject_public_ssh" {
  command = plan
  variables {
    ssh_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.ssh_cidrs]
}

run "reject_unquoted_region" {
  command = plan
  variables {
    location = "sin"
  }
  expect_failures = [var.location]
}
