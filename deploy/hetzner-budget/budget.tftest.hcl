mock_provider "hcloud" {}

variables {
  ssh_public_key   = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK7txPvTBVoEV4RJwdMHngFx2JJsXeUgDoqhcaWMjGFN test-only"
  ssh_cidrs        = ["192.0.2.17/32"]
  outbound_cidrs   = ["198.51.100.0/24", "2001:db8:1::/64"]
  quoted_total_usd = 9.50
}

run "restricted_persistent_single_host" {
  command = plan
  assert {
    condition     = hcloud_server.acceptance.server_type == "cx23" && hcloud_server.acceptance.backups && hcloud_server.acceptance.delete_protection && hcloud_server.acceptance.rebuild_protection
    error_message = "The budget host must retain backups and deletion protection without a paid type substitution."
  }
  assert {
    condition     = length([for rule in hcloud_firewall.acceptance.rule : rule if rule.direction == "in"]) == 3 && alltrue([for rule in hcloud_firewall.acceptance.rule : rule.direction != "in" || contains(["22", "80", "443"], rule.port)])
    error_message = "Database, gateway, sandbox and runtime sockets must not be exposed by the cloud firewall."
  }
  assert {
    condition     = alltrue([for rule in hcloud_firewall.acceptance.rule : rule.port != "22" || rule.source_ips == toset(var.ssh_cidrs)])
    error_message = "SSH must remain restricted to approved client IPs."
  }
  assert {
    condition     = length([for rule in hcloud_firewall.acceptance.rule : rule if rule.direction == "out"]) == 4 && alltrue([for rule in hcloud_firewall.acceptance.rule : rule.direction != "out" || (rule.destination_ips == toset(var.outbound_cidrs) && (rule.protocol == "tcp" ? contains(["53", "80", "443"], rule.port) : rule.protocol == "udp" && rule.port == "53"))])
    error_message = "Outbound access must be limited to approved destinations and TCP DNS/HTTP/HTTPS or UDP DNS."
  }
}

run "reject_unknown_outbound_destinations" {
  command = plan
  variables {
    outbound_cidrs = []
  }
  expect_failures = [var.outbound_cidrs]
}

run "reject_unrestricted_ipv4_outbound" {
  command = plan
  variables {
    outbound_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.outbound_cidrs]
}

run "reject_unrestricted_ipv6_outbound" {
  command = plan
  variables {
    outbound_cidrs = ["::/0"]
  }
  expect_failures = [var.outbound_cidrs]
}

run "reject_unrestricted_ipv4_outbound_zero_padded_prefix" {
  command = plan
  variables {
    outbound_cidrs = ["0.0.0.0/00"]
  }
  expect_failures = [var.outbound_cidrs]
}

run "reject_unrestricted_ipv6_outbound_zero_padded_prefix" {
  command = plan
  variables {
    outbound_cidrs = ["::/000"]
  }
  expect_failures = [var.outbound_cidrs]
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
